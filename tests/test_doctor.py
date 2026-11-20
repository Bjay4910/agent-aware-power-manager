"""Tests for the read-only `agent-power doctor` diagnostic command."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Sequence

import pytest

from agent_power.cli import build_parser, main
from agent_power.doctor import format_doctor_report, run_doctor
from agent_power.hooks import build_antigravity_hooks_snippet, build_claude_hooks_snippet


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_executable_stub(tmp_path: Path, name: str = "agent-power") -> Path:
    exe = tmp_path / "bin" / name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


def _write_valid_hooks(
    tmp_path: Path,
    *,
    hook_cmd: str,
    mode: int = 0o600,
    extra_secret: str | None = None,
) -> tuple[Path, Path]:
    claude_path = tmp_path / "claude" / "settings.json"
    claude_path.parent.mkdir(parents=True, exist_ok=True)
    claude_data = build_claude_hooks_snippet(hook_cmd)
    if extra_secret:
        claude_data["apiKey"] = extra_secret
    claude_path.write_text(json.dumps(claude_data, indent=2), encoding="utf-8")
    claude_path.chmod(mode)

    agy_path = tmp_path / "gemini" / "hooks.json"
    agy_path.parent.mkdir(parents=True, exist_ok=True)
    agy_data = build_antigravity_hooks_snippet(hook_cmd)
    if extra_secret:
        agy_data["secretNote"] = extra_secret
    agy_path.write_text(json.dumps(agy_data, indent=2), encoding="utf-8")
    agy_path.chmod(mode)
    return claude_path, agy_path


def _write_state(
    state_dir: Path,
    *,
    heartbeat_epoch: float,
    icon_visible: bool | None = True,
    icon_checked_at: float | None = None,
    mode: str = "auto",
    only_while_plugged_in: bool = True,
    on_ac_power: bool = True,
) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "state.json"
    data = {
        "heartbeat_epoch": heartbeat_epoch,
        "heartbeat_at": datetime.fromtimestamp(heartbeat_epoch, tz=timezone.utc).isoformat(),
        "pid": 12345,
        "mode": mode,
        "only_while_plugged_in": only_while_plugged_in,
        "battery": {"on_ac_power": on_ac_power, "percent": 95, "charging_state": "charged"},
        "menubar_icon_visible": icon_visible,
        "menubar_icon_checked_at": heartbeat_epoch if icon_checked_at is None else icon_checked_at,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _healthy_launchctl_output() -> str:
    return (
        "PID\tStatus\tLabel\n"
        "4321\t0\tcom.agentpower.menubar\n"
        "-\t0\tcom.agentpower.watchdog\n"
        "-\t0\tcom.agentpower.guard\n"
    )


def _make_runner(
    *,
    launchctl_stdout: str | None = None,
    launchctl_rc: int = 0,
    sudo_rc: int = 0,
    sudo_stdout: str = "/usr/bin/pmset -a disablesleep 0\n",
    sudo_stderr: str = "",
    pmset_sleep_disabled: int = 0,
    ps_stdout: str = "",
    sudo_timeout: bool = False,
):
    if launchctl_stdout is None:
        launchctl_stdout = _healthy_launchctl_output()

    def _runner(cmd: Sequence[str], timeout: float = 3.0) -> subprocess.CompletedProcess[str]:
        parts = list(cmd)
        if parts and Path(parts[0]).name == "launchctl":
            return subprocess.CompletedProcess(parts, launchctl_rc, launchctl_stdout, "")
        if parts and Path(parts[0]).name == "sudo":
            if sudo_timeout:
                raise subprocess.TimeoutExpired(parts, timeout)
            assert "-n" in parts and "-l" in parts, f"doctor must only run non-interactive sudo -n -l, got {parts}"
            return subprocess.CompletedProcess(parts, sudo_rc, sudo_stdout, sudo_stderr)
        if parts and Path(parts[0]).name == "pmset":
            return subprocess.CompletedProcess(
                parts,
                0,
                f"System-wide power settings:\n SleepDisabled\t\t{pmset_sleep_disabled}\n",
                "",
            )
        if parts and Path(parts[0]).name == "ps":
            return subprocess.CompletedProcess(parts, 0, ps_stdout, "")
        raise AssertionError(f"Unexpected command in doctor: {parts}")

    return _runner


def test_cli_parser_includes_doctor() -> None:
    parser = build_parser()
    ns = parser.parse_args(["doctor"])
    assert ns.command == "doctor"


def test_doctor_all_healthy_returns_zero_and_leaves_files_untouched(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    exe = _make_executable_stub(tmp_path)
    secret = "TOP_SECRET_NEVER_PRINT_12345"
    claude_path, agy_path = _write_valid_hooks(
        tmp_path,
        hook_cmd=f"{exe} hook-event",
        mode=0o600,
        extra_secret=secret,
    )
    room_home = tmp_path / "room"
    (room_home / "presence").mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_path = _write_state(state_dir, heartbeat_epoch=now - 5.0, icon_visible=True)
    sudoers_file = tmp_path / "sudoers_agent_power"
    sudoers_file.write_text("# dummy\n", encoding="utf-8")

    # Capture SHA-256 hashes before running doctor
    before_hashes = {
        claude_path: _sha256(claude_path),
        agy_path: _sha256(agy_path),
        state_path: _sha256(state_path),
    }

    report = run_doctor(
        state_dir=state_dir,
        room_home=room_home,
        claude_settings_path=claude_path,
        agy_hooks_path=agy_path,
        sudoers_path=sudoers_file,
        now=now,
        runner=_make_runner(sudo_stdout="SUDO_SECRET_OUTPUT_999\n"),
    )
    text = format_doctor_report(report)

    assert report.exit_code == 0
    assert all(c.status == "OK" for c in report.checks), text
    assert secret not in text
    assert "SUDO_SECRET_OUTPUT_999" not in text

    # Prove hook files and state file are completely untouched
    for p, digest in before_hashes.items():
        assert _sha256(p) == digest


def test_doctor_missing_sudoers_rule_and_cannot_check_never_prints_sudo_output(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    exe = _make_executable_stub(tmp_path)
    claude_path, agy_path = _write_valid_hooks(tmp_path, hook_cmd=f"{exe} hook-event")
    room_home = tmp_path / "room"
    (room_home / "presence").mkdir(parents=True)
    state_dir = tmp_path / "state"
    _write_state(state_dir, heartbeat_epoch=now - 5.0, icon_visible=True)

    # 1. Missing sudoers rule -> FAIL ("no")
    missing_sudoers = tmp_path / "no_such_sudoers"
    rep_missing = run_doctor(
        state_dir=state_dir,
        room_home=room_home,
        claude_settings_path=claude_path,
        agy_hooks_path=agy_path,
        sudoers_path=missing_sudoers,
        now=now,
        runner=_make_runner(
            sudo_rc=1,
            sudo_stdout="",
            sudo_stderr="Sorry, user is not allowed to execute SECRET_SUDO_ERR",
        ),
    )
    sudo_check = {c.name: c for c in rep_missing.checks}["sudoers"]
    text_missing = format_doctor_report(rep_missing)
    assert sudo_check.status == "FAIL"
    assert "no" in sudo_check.summary.lower()
    assert sudo_check.fix
    assert "SECRET_SUDO_ERR" not in text_missing
    assert rep_missing.exit_code == 1

    # 2. Password required / timeout -> WARN ("cannot check without sudo"), exit code 0
    existing_sudoers = tmp_path / "sudoers_exists"
    existing_sudoers.write_text("# present\n", encoding="utf-8")
    for runner in (
        _make_runner(sudo_rc=1, sudo_stdout="", sudo_stderr="sudo: a password is required LEAK_TEST"),
        _make_runner(sudo_timeout=True),
    ):
        rep_cant = run_doctor(
            state_dir=state_dir,
            room_home=room_home,
            claude_settings_path=claude_path,
            agy_hooks_path=agy_path,
            sudoers_path=existing_sudoers,
            now=now,
            runner=runner,
        )
        sc = {c.name: c for c in rep_cant.checks}["sudoers"]
        txt = format_doctor_report(rep_cant)
        assert sc.status == "WARN"
        assert "cannot check without sudo" in sc.summary.lower()
        assert "LEAK_TEST" not in txt
        assert rep_cant.exit_code == 0


def test_doctor_negative_hooks_missing_malformed_loose_perms_and_non_executable(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    room_home = tmp_path / "room"
    (room_home / "presence").mkdir(parents=True)
    state_dir = tmp_path / "state"
    _write_state(state_dir, heartbeat_epoch=now - 5.0, icon_visible=True)
    sudoers_file = tmp_path / "sudoers"
    sudoers_file.write_text("# ok\n", encoding="utf-8")

    # 1. Missing hooks files -> FAIL
    rep_missing = run_doctor(
        state_dir=state_dir,
        room_home=room_home,
        claude_settings_path=tmp_path / "missing_claude.json",
        agy_hooks_path=tmp_path / "missing_agy.json",
        sudoers_path=sudoers_file,
        now=now,
        runner=_make_runner(),
    )
    checks_m = {c.name: c for c in rep_missing.checks}
    assert checks_m["claude-hooks"].status == "FAIL"
    assert checks_m["antigravity-hooks"].status == "FAIL"
    assert rep_missing.exit_code == 1

    # 2. Malformed JSON with secret -> FAIL, secret never printed
    bad_claude = tmp_path / "bad_claude.json"
    bad_claude.write_text('{"secret": "NEVER_LEAK_BAD_JSON", broken', encoding="utf-8")
    bad_claude.chmod(0o600)
    bad_agy = tmp_path / "bad_agy.json"
    bad_agy.write_text("[not-an-object, SECRET_AGY]", encoding="utf-8")
    bad_agy.chmod(0o600)
    rep_bad = run_doctor(
        state_dir=state_dir,
        room_home=room_home,
        claude_settings_path=bad_claude,
        agy_hooks_path=bad_agy,
        sudoers_path=sudoers_file,
        now=now,
        runner=_make_runner(),
    )
    txt_bad = format_doctor_report(rep_bad)
    checks_b = {c.name: c for c in rep_bad.checks}
    assert checks_b["claude-hooks"].status == "FAIL"
    assert checks_b["antigravity-hooks"].status == "FAIL"
    assert "NEVER_LEAK_BAD_JSON" not in txt_bad
    assert "SECRET_AGY" not in txt_bad

    # 3. Permissions looser than 600 -> WARN (with chmod 600 fix), exit code 0
    exe = _make_executable_stub(tmp_path)
    loose_c, loose_a = _write_valid_hooks(tmp_path / "loose", hook_cmd=f"{exe} hook-event", mode=0o644)
    rep_loose = run_doctor(
        state_dir=state_dir,
        room_home=room_home,
        claude_settings_path=loose_c,
        agy_hooks_path=loose_a,
        sudoers_path=sudoers_file,
        now=now,
        runner=_make_runner(),
    )
    checks_l = {c.name: c for c in rep_loose.checks}
    assert checks_l["claude-hooks"].status == "WARN"
    assert "600" in (checks_l["claude-hooks"].fix or "")
    assert checks_l["antigravity-hooks"].status == "WARN"
    assert "600" in (checks_l["antigravity-hooks"].fix or "")
    assert rep_loose.exit_code == 0

    # 4. Hook command points to non-existent binary OR non-executable file -> FAIL
    non_exec = tmp_path / "not_executable"
    non_exec.write_text("#!/bin/sh\n", encoding="utf-8")
    non_exec.chmod(0o644)
    c_nonexec, a_missing_bin = _write_valid_hooks(tmp_path / "badcmd", hook_cmd=f"{non_exec} hook-event", mode=0o600)
    a_missing_bin.write_text(
        json.dumps(build_antigravity_hooks_snippet("/no/such/path/agent-power hook-event")),
        encoding="utf-8",
    )
    a_missing_bin.chmod(0o600)

    rep_cmd = run_doctor(
        state_dir=state_dir,
        room_home=room_home,
        claude_settings_path=c_nonexec,
        agy_hooks_path=a_missing_bin,
        sudoers_path=sudoers_file,
        now=now,
        runner=_make_runner(),
    )
    checks_cmd = {c.name: c for c in rep_cmd.checks}
    assert checks_cmd["claude-hooks"].status == "FAIL"
    assert "executable" in checks_cmd["claude-hooks"].summary.lower()
    assert checks_cmd["antigravity-hooks"].status == "FAIL"
    assert "exist" in checks_cmd["antigravity-hooks"].summary.lower() or "executable" in checks_cmd["antigravity-hooks"].summary.lower()
    assert rep_cmd.exit_code == 1


def test_doctor_stale_presence_orphaned_heartbeat_and_sleep_mismatch(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    exe = _make_executable_stub(tmp_path)
    claude_path, agy_path = _write_valid_hooks(tmp_path, hook_cmd=f"{exe} hook-event")
    room_home = tmp_path / "room"
    pdir = room_home / "presence"
    pdir.mkdir(parents=True)
    state_dir = tmp_path / "state"
    _write_state(state_dir, heartbeat_epoch=now - 5.0, icon_visible=True)
    sudoers_file = tmp_path / "sudoers"
    sudoers_file.write_text("# ok\n", encoding="utf-8")

    # Stale presence file (>120s old) with secret task text that must not be printed
    stale_dt = datetime.fromtimestamp(now - 300.0, tz=timezone.utc).isoformat()
    (pdir / "claude-old.json").write_text(
        json.dumps(
            {
                "agent": "claude",
                "status": "working",
                "task": "SECRET_STALE_TASK_CONTENT",
                "pid": 9999,
                "updated_at": stale_dt,
            }
        ),
        encoding="utf-8",
    )

    # One orphaned heartbeat (session file gone) and one live heartbeat (session file exists)
    live_session = pdir / "claude-old.json"
    gone_session = pdir / "claude-gone.json"
    ps_out = (
        f"55501 S /tmp/venv/bin/python -m agent_power.cli hook-heartbeat --file {gone_session} --agent-pid 123\n"
        f"55502 S /tmp/venv/bin/python -m agent_power.cli hook-heartbeat --file {live_session} --agent-pid 124\n"
    )

    # SleepDisabled=1 while no FRESH working presence exists -> mismatch FAIL
    rep = run_doctor(
        state_dir=state_dir,
        room_home=room_home,
        claude_settings_path=claude_path,
        agy_hooks_path=agy_path,
        sudoers_path=sudoers_file,
        now=now,
        runner=_make_runner(pmset_sleep_disabled=1, ps_stdout=ps_out),
    )
    txt = format_doctor_report(rep)
    checks = {c.name: c for c in rep.checks}
    assert checks["presence"].status == "WARN"
    assert "claude-old.json" in checks["presence"].summary
    assert "SECRET_STALE_TASK_CONTENT" not in txt
    assert checks["heartbeats"].status == "WARN"
    assert "55501" in checks["heartbeats"].summary
    assert "55502" not in checks["heartbeats"].summary
    assert checks["sleep-hold"].status == "FAIL"
    assert "mismatch" in checks["sleep-hold"].summary.lower()
    assert rep.exit_code == 1

    # Reverse mismatch: fresh working presence exists, SleepDisabled=0 -> FAIL
    fresh_dt = datetime.fromtimestamp(now - 10.0, tz=timezone.utc).isoformat()
    (pdir / "claude-old.json").unlink()
    (pdir / "claude-fresh.json").write_text(
        json.dumps(
            {
                "agent": "claude",
                "status": "working",
                "task": "active work",
                "pid": 8888,
                "updated_at": fresh_dt,
            }
        ),
        encoding="utf-8",
    )
    rep_rev = run_doctor(
        state_dir=state_dir,
        room_home=room_home,
        claude_settings_path=claude_path,
        agy_hooks_path=agy_path,
        sudoers_path=sudoers_file,
        now=now,
        runner=_make_runner(pmset_sleep_disabled=0),
    )
    checks_rev = {c.name: c for c in rep_rev.checks}
    assert checks_rev["sleep-hold"].status == "FAIL"
    assert "mismatch" in checks_rev["sleep-hold"].summary.lower()


def test_doctor_icon_status_hidden_stale_and_app_not_running(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    exe = _make_executable_stub(tmp_path)
    claude_path, agy_path = _write_valid_hooks(tmp_path, hook_cmd=f"{exe} hook-event")
    room_home = tmp_path / "room"
    (room_home / "presence").mkdir(parents=True)
    state_dir = tmp_path / "state"
    sudoers_file = tmp_path / "sudoers"
    sudoers_file.write_text("# ok\n", encoding="utf-8")

    # 1. Hidden icon reported freshly while menubar running -> WARN
    _write_state(state_dir, heartbeat_epoch=now - 5.0, icon_visible=False)
    rep_hidden = run_doctor(
        state_dir=state_dir,
        room_home=room_home,
        claude_settings_path=claude_path,
        agy_hooks_path=agy_path,
        sudoers_path=sudoers_file,
        now=now,
        runner=_make_runner(),
    )
    c_hidden = {c.name: c for c in rep_hidden.checks}["menubar-icon"]
    assert c_hidden.status == "WARN"
    assert "hidden" in c_hidden.summary.lower()

    # 2. state.json older than ~2 minutes (180s) -> "icon status unknown (app not reporting)"
    _write_state(state_dir, heartbeat_epoch=now - 180.0, icon_visible=True)
    rep_stale = run_doctor(
        state_dir=state_dir,
        room_home=room_home,
        claude_settings_path=claude_path,
        agy_hooks_path=agy_path,
        sudoers_path=sudoers_file,
        now=now,
        runner=_make_runner(),
    )
    c_stale = {c.name: c for c in rep_stale.checks}["menubar-icon"]
    assert c_stale.status == "WARN"
    assert "icon status unknown (app not reporting)" in c_stale.summary.lower()

    # 3. Menu bar app NOT running (PID is '-') even with fresh state.json -> "icon status unknown (app not reporting)"
    _write_state(state_dir, heartbeat_epoch=now - 5.0, icon_visible=True)
    stopped_launchctl = (
        "PID\tStatus\tLabel\n"
        "-\t0\tcom.agentpower.menubar\n"
        "-\t0\tcom.agentpower.watchdog\n"
        "-\t0\tcom.agentpower.guard\n"
    )
    rep_stopped = run_doctor(
        state_dir=state_dir,
        room_home=room_home,
        claude_settings_path=claude_path,
        agy_hooks_path=agy_path,
        sudoers_path=sudoers_file,
        now=now,
        runner=_make_runner(launchctl_stdout=stopped_launchctl),
    )
    checks_s = {c.name: c for c in rep_stopped.checks}
    assert checks_s["launchd"].status == "FAIL"
    assert checks_s["menubar-icon"].status == "WARN"
    assert "icon status unknown (app not reporting)" in checks_s["menubar-icon"].summary.lower()
    assert rep_stopped.exit_code == 1


def test_doctor_cli_subcommand_invocation(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify `agent-power doctor` CLI wiring prints report and returns doctor's exit code."""
    now = 1_700_000_000.0
    exe = _make_executable_stub(tmp_path)
    claude_path, agy_path = _write_valid_hooks(tmp_path, hook_cmd=f"{exe} hook-event")
    room_home = tmp_path / "room"
    (room_home / "presence").mkdir(parents=True)
    state_dir = tmp_path / "state"
    _write_state(state_dir, heartbeat_epoch=now - 5.0, icon_visible=True)
    sudoers_file = tmp_path / "sudoers"
    sudoers_file.write_text("# ok\n", encoding="utf-8")

    import agent_power.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "_default_runner", _make_runner())
    monkeypatch.setattr(doctor_mod, "DEFAULT_SUDOERS_PATH", sudoers_file)
    monkeypatch.setattr(doctor_mod.time, "time", lambda: now)

    rc = main(
        [
            "doctor",
            "--claude-settings",
            str(claude_path),
            "--agy-hooks",
            str(agy_path),
            "--room-home",
            str(room_home),
        ],
        state_dir=state_dir,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "[OK]" in out
    assert "Summary:" in out
