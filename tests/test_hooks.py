"""Tests for Claude Code and Antigravity lifecycle hooks, detached heartbeats, and config merge/unmerge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from agent_power.cli import main
from agent_power.engine import PowerEngine
from agent_power.hooks import (
    apply_install_hooks,
    apply_uninstall_hooks,
    format_install_hooks_print,
    handle_hook_event,
    is_pid_alive,
    run_heartbeat_loop,
)
from agent_power.policy import FakeClock
from agent_power.power import BatteryStatus
from agent_power.presence_reader import read_presence, working_agents


def _make_engine(tmp_path: Path, clock: FakeClock) -> PowerEngine:
    return PowerEngine(
        state_dir=tmp_path / "state",
        room_home=tmp_path / "room",
        clock=clock,
        battery_reader=lambda: BatteryStatus(on_ac_power=True, percent=95, charging_state="charged"),
        dry_run=True,
    )


def test_claude_and_agy_lifecycle_and_two_sessions_concurrency(tmp_path: Path) -> None:
    """Two concurrent sessions (Claude + Antigravity): one stops, the other keeps holding."""
    clock = FakeClock(initial=1_700_000_000.0)
    eng = _make_engine(tmp_path, clock)
    room_home = tmp_path / "room"

    # Session 1: Claude starts on UserPromptSubmit (secret prompt text must never be stored)
    claude_start = json.dumps(
        {
            "session_id": "sess-alpha-123",
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/Users/kingjames/Developer/project-one",
            "prompt": "TOP_SECRET_USER_PROMPT_DO_NOT_LOG",
        }
    )
    assert handle_hook_event(claude_start, room_home=room_home, spawn_heartbeat=False) == {}

    # Session 2: Antigravity starts on PreInvocation
    agy_start = json.dumps(
        {
            "conversationId": "conv-beta-456",
            "hookEventName": "PreInvocation",
            "cwd": "/Users/kingjames/Developer/project-two",
            "lastUserInput": "ANOTHER_SECRET_PROMPT_DO_NOT_LOG",
        }
    )
    assert handle_hook_event(agy_start, room_home=room_home, spawn_heartbeat=False) == {}

    # Verify two distinct presence files exist and contain NO prompt text
    p_dir = room_home / "presence"
    claude_file = p_dir / "claude-sess-alpha-123.json"
    agy_file = p_dir / "agy-conv-beta-456.json"
    assert claude_file.is_file()
    assert agy_file.is_file()
    assert "TOP_SECRET_USER_PROMPT_DO_NOT_LOG" not in claude_file.read_text(encoding="utf-8")
    assert "ANOTHER_SECRET_PROMPT_DO_NOT_LOG" not in agy_file.read_text(encoding="utf-8")

    # Engine step: acquires hold with 2 active agents
    tick1 = eng.step()
    assert tick1.hold_active is True
    assert len(tick1.active_agents) == 2

    # Claude session stops (Stop event) -> claude file removed, agy file remains
    claude_stop = json.dumps(
        {
            "session_id": "sess-alpha-123",
            "hook_event_name": "Stop",
            "cwd": "/Users/kingjames/Developer/project-one",
        }
    )
    assert handle_hook_event(claude_stop, room_home=room_home, spawn_heartbeat=False) == {}
    assert not claude_file.is_file()
    assert agy_file.is_file()

    # Advance clock past the 20s idle grace period -> still held because Antigravity is still working!
    clock.advance(25.0)
    # Refresh agy via PostToolUse
    agy_post = json.dumps(
        {
            "conversationId": "conv-beta-456",
            "hookEventName": "PostToolUse",
            "cwd": "/Users/kingjames/Developer/project-two",
        }
    )
    assert handle_hook_event(agy_post, room_home=room_home, spawn_heartbeat=False) == {}
    tick2 = eng.step()
    assert tick2.hold_active is True
    assert len(tick2.active_agents) == 1
    assert tick2.active_agents[0].agent == "antigravity"

    # Now Antigravity finishes (Stop event) -> file removed, hold releases after 20s grace
    agy_stop = json.dumps(
        {
            "conversationId": "conv-beta-456",
            "hookEventName": "Stop",
            "cwd": "/Users/kingjames/Developer/project-two",
        }
    )
    assert handle_hook_event(agy_stop, room_home=room_home, spawn_heartbeat=False) == {}
    assert not agy_file.is_file()

    clock.advance(5.0)
    assert eng.step().hold_active is True  # still in 20s grace
    clock.advance(20.0)
    assert eng.step().hold_active is False  # released!


def test_stale_expiry_when_agent_crashes_without_heartbeat(tmp_path: Path) -> None:
    """If a presence file is never refreshed, it expires after 120s and releases the hold."""
    room_home = tmp_path / "room"
    payload = json.dumps(
        {
            "session_id": "crashed-sess",
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/tmp/work",
        }
    )
    handle_hook_event(payload, room_home=room_home, spawn_heartbeat=False)
    p_file = room_home / "presence" / "claude-crashed-sess.json"
    data = json.loads(p_file.read_text(encoding="utf-8"))
    t0 = datetime.fromisoformat(data["updated_at"])

    # At +119s: still working
    entries_119 = read_presence(room_home, now=t0 + timedelta(seconds=119))
    assert len(working_agents(entries_119)) == 1

    # At +121s: stale -> marked idle
    entries_121 = read_presence(room_home, now=t0 + timedelta(seconds=121))
    assert len(entries_121) == 1
    assert entries_121[0].is_stale is True
    assert len(working_agents(entries_121)) == 0


def test_heartbeat_survives_long_silent_step_and_stops_on_stop_event(tmp_path: Path) -> None:
    """Heartbeat daemon refreshes updated_at across multiple intervals without duplicate daemons, and exits on Stop."""
    room_home = tmp_path / "room"
    start_payload = json.dumps(
        {
            "session_id": "long-step-sess",
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/tmp/long-step",
        }
    )
    handle_hook_event(
        start_payload,
        room_home=room_home,
        agent_pid=os.getpid(),
        spawn_heartbeat=True,
        heartbeat_interval_seconds=0.15,
        heartbeat_max_seconds=10.0,
    )
    p_file = room_home / "presence" / "claude-long-step-sess.json"
    assert p_file.is_file()
    initial_data = json.loads(p_file.read_text(encoding="utf-8"))
    hb_pid = initial_data.get("hb_pid")
    assert isinstance(hb_pid, int) and hb_pid > 1
    assert is_pid_alive(hb_pid)
    first_ts = initial_data["updated_at"]

    # Wait across >3 heartbeat intervals with NO hook events (simulating a silent >3-minute tool/model step)
    time.sleep(0.55)
    refreshed_data = json.loads(p_file.read_text(encoding="utf-8"))
    assert refreshed_data["updated_at"] > first_ts
    assert refreshed_data["hb_pid"] == hb_pid

    # Firing PostToolUse while heartbeat is alive must NOT spawn a second heartbeat daemon
    post_payload = json.dumps(
        {
            "session_id": "long-step-sess",
            "hook_event_name": "PostToolUse",
            "cwd": "/tmp/long-step",
        }
    )
    handle_hook_event(
        post_payload,
        room_home=room_home,
        agent_pid=os.getpid(),
        spawn_heartbeat=True,
        heartbeat_interval_seconds=0.15,
        heartbeat_max_seconds=10.0,
    )
    after_post_data = json.loads(p_file.read_text(encoding="utf-8"))
    assert after_post_data["hb_pid"] == hb_pid

    # Firing Stop removes the file and terminates the heartbeat daemon
    stop_payload = json.dumps(
        {
            "session_id": "long-step-sess",
            "hook_event_name": "Stop",
            "cwd": "/tmp/long-step",
        }
    )
    handle_hook_event(stop_payload, room_home=room_home)
    assert not p_file.is_file()

    # Confirm heartbeat process exits cleanly (no orphan left behind)
    for _ in range(30):
        if not is_pid_alive(hb_pid):
            break
        time.sleep(0.05)
    assert not is_pid_alive(hb_pid)


def test_heartbeat_orphan_prevention_when_agent_process_dies(tmp_path: Path) -> None:
    """If the watched agent process dies without firing Stop/SessionEnd, the heartbeat removes the file and exits."""
    room_home = tmp_path / "room"
    # Spawn a dummy short-lived "agent" process
    dummy_agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
    try:
        start_payload = json.dumps(
            {
                "conversationId": "orphan-test-conv",
                "hookEventName": "PreInvocation",
                "cwd": "/tmp/orphan-test",
            }
        )
        handle_hook_event(
            start_payload,
            room_home=room_home,
            agent_pid=dummy_agent.pid,
            spawn_heartbeat=True,
            heartbeat_interval_seconds=0.1,
            heartbeat_max_seconds=10.0,
        )
        p_file = room_home / "presence" / "agy-orphan-test-conv.json"
        assert p_file.is_file()
        hb_pid = json.loads(p_file.read_text(encoding="utf-8"))["hb_pid"]
        assert is_pid_alive(hb_pid)

        # Wait for dummy agent to exit
        dummy_agent.wait(timeout=2.0)

        # Heartbeat should detect dead agent_pid, remove the presence file, and exit
        for _ in range(40):
            if not p_file.is_file() and not is_pid_alive(hb_pid):
                break
            time.sleep(0.05)

        assert not p_file.is_file()
        assert not is_pid_alive(hb_pid)
    finally:
        if dummy_agent.poll() is None:
            dummy_agent.kill()


def test_heartbeat_max_seconds_cap_exits_and_cleans_up(tmp_path: Path) -> None:
    """Heartbeat loop exits and removes the presence file once max_seconds is reached."""
    p_file = tmp_path / "presence" / "claude-cap.json"
    p_file.parent.mkdir(parents=True, exist_ok=True)
    p_file.write_text(
        json.dumps(
            {
                "agent": "claude",
                "status": "working",
                "task": "long job",
                "pid": os.getpid(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    rc = run_heartbeat_loop(
        p_file,
        agent_pid=os.getpid(),
        interval_seconds=0.05,
        max_seconds=0.15,
    )
    assert rc == 0
    assert not p_file.is_file()


@pytest.mark.parametrize(
    "bad_stdin",
    [
        "",
        "   ",
        "{not valid json",
        "[]",
        "null",
        json.dumps({"foo": "bar"}),
        json.dumps({"session_id": "", "hook_event_name": "UserPromptSubmit"}),
        json.dumps({"conversationId": 12345, "hookEventName": "PreInvocation"}),
    ],
)
def test_malformed_stdin_always_returns_empty_json_and_exits_zero(
    tmp_path: Path,
    bad_stdin: str,
) -> None:
    room_home = tmp_path / "room"
    assert handle_hook_event(bad_stdin, room_home=room_home, spawn_heartbeat=False) == {}
    p_dir = room_home / "presence"
    assert not p_dir.exists() or list(p_dir.glob("*.json")) == []


def test_install_hooks_apply_is_idempotent_and_uninstall_preserves_other_hooks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Applying hooks twice does not duplicate entries, creates backups, and uninstall leaves user hooks intact."""
    claude_path = tmp_path / "claude_settings.json"
    agy_path = tmp_path / "agy_hooks.json"

    # Seed both config files with existing user settings and unrelated hooks
    initial_claude = {
        "theme": "dark",
        "permissions": {"allow": ["Read"]},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "echo custom-bash-guard"}],
                }
            ],
            "CustomEvent": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "echo keep-me"}],
                }
            ],
        },
    }
    initial_agy = {
        "lint-checker": {
            "enabled": True,
            "PostToolUse": [
                {
                    "matcher": "run_command",
                    "hooks": [{"type": "command", "command": "./scripts/lint.sh"}],
                }
            ],
        }
    }
    claude_path.write_text(json.dumps(initial_claude, indent=2), encoding="utf-8")
    agy_path.write_text(json.dumps(initial_agy, indent=2), encoding="utf-8")

    # 1. Test --print via CLI does not modify files
    rc = main(
        [
            "install-hooks",
            "--print",
            "--claude-settings",
            str(claude_path),
            "--agy-hooks",
            str(agy_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "UserPromptSubmit" in out
    assert "PreInvocation" in out
    assert json.loads(claude_path.read_text(encoding="utf-8")) == initial_claude
    assert json.loads(agy_path.read_text(encoding="utf-8")) == initial_agy

    # 2. First --apply
    c_bak1, a_bak1 = apply_install_hooks(
        claude_settings_path=claude_path,
        agy_hooks_path=agy_path,
        hook_command="/custom/bin/agent-power hook-event",
        timestamp="20261118-190000",
    )
    assert c_bak1 is not None and c_bak1.is_file()
    assert a_bak1 is not None and a_bak1.is_file()

    after_first_claude = json.loads(claude_path.read_text(encoding="utf-8"))
    after_first_agy = json.loads(agy_path.read_text(encoding="utf-8"))

    # Preserved existing settings and hooks
    assert after_first_claude["theme"] == "dark"
    assert len(after_first_claude["hooks"]["PreToolUse"]) == 2
    assert "lint-checker" in after_first_agy
    assert "agent-power" in after_first_agy

    # 3. Second --apply (must be idempotent!)
    apply_install_hooks(
        claude_settings_path=claude_path,
        agy_hooks_path=agy_path,
        hook_command="/custom/bin/agent-power hook-event",
        timestamp="20261118-190001",
    )
    after_second_claude = json.loads(claude_path.read_text(encoding="utf-8"))
    after_second_agy = json.loads(agy_path.read_text(encoding="utf-8"))
    assert after_second_claude == after_first_claude
    assert after_second_agy == after_first_agy

    # 4. Uninstall leaves initial user settings and unrelated hooks completely intact
    apply_uninstall_hooks(
        claude_settings_path=claude_path,
        agy_hooks_path=agy_path,
        timestamp="20261118-190002",
    )
    after_uninstall_claude = json.loads(claude_path.read_text(encoding="utf-8"))
    after_uninstall_agy = json.loads(agy_path.read_text(encoding="utf-8"))
    assert after_uninstall_claude == initial_claude
    assert after_uninstall_agy == initial_agy


_DUMMY_AGENT_SCRIPT = """
import subprocess, sys
children = []
for line in sys.stdin:
    parts = line.strip().split()
    if not parts:
        continue
    cmd = parts[0]
    if cmd == "SPAWN":
        secs = parts[1]
        p = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({secs})"])
        children.append(p)
        print(f"CHILD {p.pid}", flush=True)
    elif cmd == "SPAWN_NESTED":
        secs = parts[1]
        p = subprocess.Popen(
            ["/bin/sh", "-c", f"{sys.executable} -c 'import time; time.sleep({secs})'"]
        )
        children.append(p)
        print(f"CHILD {p.pid}", flush=True)
    elif cmd == "HOOK":
        hook_cmd = line.strip().split(" ", 1)[1]
        payload = sys.stdin.readline()
        res = subprocess.run(["/bin/sh", "-c", hook_cmd], input=payload, text=True, capture_output=True)
        print(f"HOOK_RC {res.returncode} {res.stdout.strip()}", flush=True)
    elif cmd == "EXIT":
        break
for c in children:
    if c.poll() is None:
        c.terminate()
for c in children:
    try:
        c.wait(timeout=2)
    except Exception:
        c.kill()
"""


def _start_dummy_agent() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-u", "-c", _DUMMY_AGENT_SCRIPT],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _spawn_agent_child(agent: subprocess.Popen[str], seconds: float) -> int:
    assert agent.stdin is not None and agent.stdout is not None
    agent.stdin.write(f"SPAWN {seconds}\n")
    agent.stdin.flush()
    line = agent.stdout.readline().strip()
    assert line.startswith("CHILD "), f"Unexpected agent response: {line!r}"
    return int(line.split()[1])


def _stop_dummy_agent(agent: subprocess.Popen[str]) -> None:
    try:
        if agent.stdin and not agent.stdin.closed:
            agent.stdin.write("EXIT\n")
            agent.stdin.flush()
            agent.stdin.close()
    except OSError:
        pass
    try:
        agent.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        agent.kill()
        agent.wait(timeout=2.0)


def test_stop_keeps_presence_while_turn_background_child_runs_and_releases_after_exit(
    tmp_path: Path,
) -> None:
    """A background child started during the turn keeps presence/hold past Stop and releases after it exits."""
    clock = FakeClock(initial=1_700_000_000.0)
    eng = _make_engine(tmp_path, clock)
    room_home = tmp_path / "room"
    agent = _start_dummy_agent()
    try:
        start_payload = json.dumps(
            {
                "conversationId": "bg-child-conv",
                "hookEventName": "PreInvocation",
                "cwd": "/tmp/bg-work",
            }
        )
        assert (
            handle_hook_event(
                start_payload,
                room_home=room_home,
                agent_pid=agent.pid,
                spawn_heartbeat=True,
                heartbeat_interval_seconds=0.15,
                heartbeat_max_seconds=15.0,
            )
            == {}
        )
        p_file = room_home / "presence" / "agy-bg-child-conv.json"
        assert p_file.is_file()
        hb_pid = json.loads(p_file.read_text(encoding="utf-8"))["hb_pid"]

        # Spawn a background child DURING this turn that will outlive Stop
        bg_pid = _spawn_agent_child(agent, 1.5)
        assert is_pid_alive(bg_pid)

        # Agent ends its turn (Stop) while bg_pid is still running
        stop_payload = json.dumps(
            {
                "conversationId": "bg-child-conv",
                "hookEventName": "Stop",
                "cwd": "/tmp/bg-work",
            }
        )
        assert (
            handle_hook_event(
                stop_payload,
                room_home=room_home,
                agent_pid=agent.pid,
                spawn_heartbeat=True,
                heartbeat_interval_seconds=0.15,
                heartbeat_max_seconds=15.0,
            )
            == {}
        )

        # Presence file MUST remain working with waiting_for_children=True
        assert p_file.is_file()
        after_stop = json.loads(p_file.read_text(encoding="utf-8"))
        assert after_stop["status"] == "working"
        assert after_stop.get("waiting_for_children") is True
        assert bg_pid in after_stop.get("waiting_for_pids", [])
        assert is_pid_alive(hb_pid)
        assert eng.step().hold_active is True

        # Heartbeat keeps refreshing while bg_pid runs
        ts_at_stop = after_stop["updated_at"]
        time.sleep(0.45)
        assert p_file.is_file()
        refreshed = json.loads(p_file.read_text(encoding="utf-8"))
        assert refreshed["updated_at"] > ts_at_stop

        # Wait for background child to exit; heartbeat must clean up promptly
        for _ in range(60):
            if not p_file.is_file() and not is_pid_alive(hb_pid):
                break
            time.sleep(0.1)

        assert not is_pid_alive(bg_pid)
        assert not p_file.is_file()
        assert not is_pid_alive(hb_pid)

        # Engine releases hold after 20s idle grace
        assert eng.step().hold_active is True
        clock.advance(25.0)
        assert eng.step().hold_active is False
    finally:
        _stop_dummy_agent(agent)


def test_stop_ignores_pre_turn_mcp_like_old_child(tmp_path: Path) -> None:
    """Long-lived children started BEFORE the turn (e.g. MCP/LSP servers) must NOT keep the hold at Stop."""
    room_home = tmp_path / "room"
    agent = _start_dummy_agent()
    try:
        # Spawn a long-lived MCP-like child BEFORE the turn starts
        old_mcp_pid = _spawn_agent_child(agent, 20.0)
        assert is_pid_alive(old_mcp_pid)

        # Start turn
        start_payload = json.dumps(
            {
                "session_id": "mcp-old-child-sess",
                "hook_event_name": "UserPromptSubmit",
                "cwd": "/tmp/mcp-test",
            }
        )
        handle_hook_event(
            start_payload,
            room_home=room_home,
            agent_pid=agent.pid,
            spawn_heartbeat=True,
            heartbeat_interval_seconds=0.15,
            heartbeat_max_seconds=10.0,
        )
        p_file = room_home / "presence" / "claude-mcp-old-child-sess.json"
        assert p_file.is_file()
        hb_pid = json.loads(p_file.read_text(encoding="utf-8"))["hb_pid"]

        # End turn with no turn-started children (only old_mcp_pid is running)
        stop_payload = json.dumps(
            {
                "session_id": "mcp-old-child-sess",
                "hook_event_name": "Stop",
                "cwd": "/tmp/mcp-test",
            }
        )
        handle_hook_event(stop_payload, room_home=room_home, agent_pid=agent.pid)

        assert not p_file.is_file()
        assert is_pid_alive(old_mcp_pid)
        for _ in range(30):
            if not is_pid_alive(hb_pid):
                break
            time.sleep(0.05)
        assert not is_pid_alive(hb_pid)
    finally:
        _stop_dummy_agent(agent)


def test_stop_excludes_old_child_by_start_time_even_without_baseline(tmp_path: Path) -> None:
    """Process start time comparison alone must exclude pre-turn children even if baseline list is empty."""
    room_home = tmp_path / "room"
    agent = _start_dummy_agent()
    try:
        old_pid = _spawn_agent_child(agent, 20.0)
        assert is_pid_alive(old_pid)
        # Ensure wall clock advances past the 1s ps lstart boundary
        time.sleep(1.2)

        start_payload = json.dumps(
            {
                "conversationId": "lstart-only-conv",
                "hookEventName": "PreInvocation",
                "cwd": "/tmp/lstart-test",
            }
        )
        handle_hook_event(
            start_payload,
            room_home=room_home,
            agent_pid=agent.pid,
            spawn_heartbeat=False,
        )
        p_file = room_home / "presence" / "agy-lstart-only-conv.json"
        data = json.loads(p_file.read_text(encoding="utf-8"))
        # Clear baseline snapshot so ONLY start-time comparison can exclude old_pid
        data["turn_baseline_pids"] = []
        p_file.write_text(json.dumps(data), encoding="utf-8")

        stop_payload = json.dumps(
            {
                "conversationId": "lstart-only-conv",
                "hookEventName": "Stop",
                "cwd": "/tmp/lstart-test",
            }
        )
        handle_hook_event(stop_payload, room_home=room_home, agent_pid=agent.pid)
        assert not p_file.is_file()
        assert is_pid_alive(old_pid)
    finally:
        _stop_dummy_agent(agent)


def test_stop_does_not_count_hook_wrapper_or_heartbeat_processes(tmp_path: Path) -> None:
    """Running hooks via `sh -c` under the agent with a live heartbeat must NOT self-count at Stop."""
    room_home = tmp_path / "room"
    agent = _start_dummy_agent()
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    hook_cmd = (
        f"AGENT_ROOM_HOME={room_home} PYTHONPATH={src_dir} "
        f"{sys.executable} -m agent_power.cli hook-event"
    )
    try:
        assert agent.stdin is not None and agent.stdout is not None
        start_payload = json.dumps(
            {
                "conversationId": "self-exclude-conv",
                "hookEventName": "PreInvocation",
                "cwd": "/tmp/self-exclude",
            }
        )
        agent.stdin.write(f"HOOK {hook_cmd}\n{start_payload}\n")
        agent.stdin.flush()
        resp1 = agent.stdout.readline().strip()
        assert resp1 == "HOOK_RC 0 {}"

        p_file = room_home / "presence" / "agy-self-exclude-conv.json"
        assert p_file.is_file()
        data = json.loads(p_file.read_text(encoding="utf-8"))
        assert data["pid"] == agent.pid
        hb_pid = data["hb_pid"]
        assert is_pid_alive(hb_pid)

        # Fire Stop via `sh -c` under the same agent with NO background command
        stop_payload = json.dumps(
            {
                "conversationId": "self-exclude-conv",
                "hookEventName": "Stop",
                "cwd": "/tmp/self-exclude",
            }
        )
        agent.stdin.write(f"HOOK {hook_cmd}\n{stop_payload}\n")
        agent.stdin.flush()
        resp2 = agent.stdout.readline().strip()
        assert resp2 == "HOOK_RC 0 {}"

        assert not p_file.is_file()
        for _ in range(30):
            if not is_pid_alive(hb_pid):
                break
            time.sleep(0.05)
        assert not is_pid_alive(hb_pid)
    finally:
        _stop_dummy_agent(agent)


def test_forever_running_background_child_still_hits_max_seconds_cap(tmp_path: Path) -> None:
    """A turn-started child that runs forever is still bounded by the heartbeat max_seconds (2h) cap."""
    room_home = tmp_path / "room"
    agent = _start_dummy_agent()
    try:
        start_payload = json.dumps(
            {
                "session_id": "forever-child-sess",
                "hook_event_name": "UserPromptSubmit",
                "cwd": "/tmp/forever",
            }
        )
        handle_hook_event(
            start_payload,
            room_home=room_home,
            agent_pid=agent.pid,
            spawn_heartbeat=True,
            heartbeat_interval_seconds=0.1,
            heartbeat_max_seconds=0.6,
        )
        p_file = room_home / "presence" / "claude-forever-child-sess.json"
        hb_pid = json.loads(p_file.read_text(encoding="utf-8"))["hb_pid"]

        # Spawn a long-running child during the turn
        forever_pid = _spawn_agent_child(agent, 30.0)
        assert is_pid_alive(forever_pid)

        stop_payload = json.dumps(
            {
                "session_id": "forever-child-sess",
                "hook_event_name": "Stop",
                "cwd": "/tmp/forever",
            }
        )
        handle_hook_event(
            stop_payload,
            room_home=room_home,
            agent_pid=agent.pid,
            spawn_heartbeat=True,
            heartbeat_interval_seconds=0.1,
            heartbeat_max_seconds=0.6,
        )
        assert p_file.is_file()

        # Once max_seconds (0.6s) elapses, presence file MUST be removed and heartbeat MUST exit
        for _ in range(30):
            if not p_file.is_file() and not is_pid_alive(hb_pid):
                break
            time.sleep(0.1)

        assert not p_file.is_file()
        assert not is_pid_alive(hb_pid)
        assert is_pid_alive(forever_pid)
    finally:
        _stop_dummy_agent(agent)


def test_session_end_cleans_up_immediately_even_with_turn_child(tmp_path: Path) -> None:
    """SessionEnd (session closing) always cleans up immediately even if a child is running."""
    room_home = tmp_path / "room"
    agent = _start_dummy_agent()
    try:
        start_payload = json.dumps(
            {
                "session_id": "session-end-child",
                "hook_event_name": "UserPromptSubmit",
                "cwd": "/tmp/end",
            }
        )
        handle_hook_event(
            start_payload,
            room_home=room_home,
            agent_pid=agent.pid,
            spawn_heartbeat=True,
            heartbeat_interval_seconds=0.15,
            heartbeat_max_seconds=10.0,
        )
        p_file = room_home / "presence" / "claude-session-end-child.json"
        hb_pid = json.loads(p_file.read_text(encoding="utf-8"))["hb_pid"]
        _spawn_agent_child(agent, 15.0)

        end_payload = json.dumps(
            {
                "session_id": "session-end-child",
                "hook_event_name": "SessionEnd",
                "cwd": "/tmp/end",
            }
        )
        handle_hook_event(end_payload, room_home=room_home, agent_pid=agent.pid)
        assert not p_file.is_file()
        for _ in range(30):
            if not is_pid_alive(hb_pid):
                break
            time.sleep(0.05)
        assert not is_pid_alive(hb_pid)
    finally:
        _stop_dummy_agent(agent)


def test_ps_snapshot_does_not_truncate_long_args_past_80_columns() -> None:
    """macOS ps must use -ww so long hook/heartbeat paths past 80 cols are visible to exclusion logic."""
    from agent_power.hooks import _is_hook_or_heartbeat_args, _snapshot_processes

    long_marker = "x" * 90 + " agent-power hook-heartbeat --file /tmp/test.json"
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)", long_marker],
    )
    try:
        procs = _snapshot_processes()
        assert proc.pid in procs
        assert "hook-heartbeat" in procs[proc.pid].args
        assert _is_hook_or_heartbeat_args(procs[proc.pid].args) is True
    finally:
        proc.terminate()
        proc.wait(timeout=3.0)


def test_agent_death_while_draining_still_cleans_up_no_orphans(tmp_path: Path) -> None:
    """If the agent dies while draining a background child, heartbeat must still remove presence and exit."""
    room_home = tmp_path / "room"
    agent = _start_dummy_agent()
    try:
        start_payload = json.dumps(
            {
                "conversationId": "drain-orphan-conv",
                "hookEventName": "PreInvocation",
                "cwd": "/tmp/drain-orphan",
            }
        )
        handle_hook_event(
            start_payload,
            room_home=room_home,
            agent_pid=agent.pid,
            spawn_heartbeat=True,
            heartbeat_interval_seconds=0.1,
            heartbeat_max_seconds=10.0,
        )
        p_file = room_home / "presence" / "agy-drain-orphan-conv.json"
        hb_pid = json.loads(p_file.read_text(encoding="utf-8"))["hb_pid"]
        _spawn_agent_child(agent, 20.0)

        stop_payload = json.dumps(
            {
                "conversationId": "drain-orphan-conv",
                "hookEventName": "Stop",
                "cwd": "/tmp/drain-orphan",
            }
        )
        handle_hook_event(stop_payload, room_home=room_home, agent_pid=agent.pid)
        assert p_file.is_file()
        assert json.loads(p_file.read_text(encoding="utf-8")).get("waiting_for_children") is True

        # Kill the agent abruptly without SessionEnd
        agent.kill()
        agent.wait(timeout=3.0)

        for _ in range(40):
            if not p_file.is_file() and not is_pid_alive(hb_pid):
                break
            time.sleep(0.05)
        assert not p_file.is_file()
        assert not is_pid_alive(hb_pid)
    finally:
        if agent.poll() is None:
            _stop_dummy_agent(agent)


def test_fresh_turn_after_previous_turn_gets_fresh_cap_and_carryover_preserves_cap(
    tmp_path: Path,
) -> None:
    """A fresh turn gets a fresh max_seconds deadline, while carrying over a draining child preserves Turn 1's cap."""
    room_home = tmp_path / "room"
    agent = _start_dummy_agent()
    try:
        # Turn 1 starts with a short 0.7s cap and spawns a long child
        t1_start = json.dumps(
            {
                "session_id": "cap-carry-sess",
                "hook_event_name": "UserPromptSubmit",
                "cwd": "/tmp/cap-carry",
            }
        )
        handle_hook_event(
            t1_start,
            room_home=room_home,
            agent_pid=agent.pid,
            spawn_heartbeat=True,
            heartbeat_interval_seconds=0.1,
            heartbeat_max_seconds=0.8,
        )
        p_file = room_home / "presence" / "claude-cap-carry-sess.json"
        hb1 = json.loads(p_file.read_text(encoding="utf-8"))["hb_pid"]
        bg_pid = _spawn_agent_child(agent, 20.0)

        # Turn 1 stops -> enters drain mode
        t1_stop = json.dumps(
            {
                "session_id": "cap-carry-sess",
                "hook_event_name": "Stop",
                "cwd": "/tmp/cap-carry",
            }
        )
        handle_hook_event(
            t1_stop,
            room_home=room_home,
            agent_pid=agent.pid,
            spawn_heartbeat=True,
            heartbeat_interval_seconds=0.1,
            heartbeat_max_seconds=0.8,
        )
        assert p_file.is_file()

        # Kill hb1 to simulate heartbeat crash before Turn 2 starts while bg_pid is STILL running
        os.kill(hb1, 15)
        for _ in range(20):
            if not is_pid_alive(hb1):
                break
            time.sleep(0.05)

        time.sleep(0.35)
        # Turn 2 starts while bg_pid is STILL alive -> must carry over Turn 1's cap (remaining ~0.45s), NOT extend +0.8s!
        handle_hook_event(
            t1_start,
            room_home=room_home,
            agent_pid=agent.pid,
            spawn_heartbeat=True,
            heartbeat_interval_seconds=0.1,
            heartbeat_max_seconds=0.8,
        )
        t2_data = json.loads(p_file.read_text(encoding="utf-8"))
        hb2 = t2_data["hb_pid"]
        assert bg_pid not in t2_data.get("turn_baseline_pids", [])

        # Turn 2 stops quickly; bg_pid is STILL tracked and must expire around Turn 1's 0.8s deadline (< 1.1s total)
        handle_hook_event(
            t1_stop,
            room_home=room_home,
            agent_pid=agent.pid,
            spawn_heartbeat=True,
            heartbeat_interval_seconds=0.1,
            heartbeat_max_seconds=0.8,
        )
        assert p_file.is_file()
        assert bg_pid in json.loads(p_file.read_text(encoding="utf-8")).get("waiting_for_pids", [])

        # Should expire promptly (within 0.7s, well before a renewed 0.8s cap from t=0.35 would)
        for _ in range(14):
            if not p_file.is_file() and not is_pid_alive(hb2):
                break
            time.sleep(0.05)
        assert not p_file.is_file()
        assert not is_pid_alive(hb2)
    finally:
        _stop_dummy_agent(agent)


def test_turn_started_grandchild_keeps_presence_until_grandchild_exits(tmp_path: Path) -> None:
    """Nested grandchild (`agent -> sh -c -> python sleep`) started during turn keeps presence until exit."""
    room_home = tmp_path / "room"
    agent = _start_dummy_agent()
    try:
        start_payload = json.dumps(
            {
                "conversationId": "grandchild-conv",
                "hookEventName": "PreInvocation",
                "cwd": "/tmp/grandchild",
            }
        )
        handle_hook_event(
            start_payload,
            room_home=room_home,
            agent_pid=agent.pid,
            spawn_heartbeat=True,
            heartbeat_interval_seconds=0.15,
            heartbeat_max_seconds=10.0,
        )
        p_file = room_home / "presence" / "agy-grandchild-conv.json"
        hb_pid = json.loads(p_file.read_text(encoding="utf-8"))["hb_pid"]

        # Spawn a nested shell child (`agent -> sh -c -> python sleep`)
        assert agent.stdin is not None and agent.stdout is not None
        agent.stdin.write("SPAWN_NESTED 1.2\n")
        agent.stdin.flush()
        child_line = agent.stdout.readline().strip()
        assert child_line.startswith("CHILD ")
        sh_pid = int(child_line.split()[1])
        time.sleep(0.15)  # allow sh to spawn the grandchild python process

        stop_payload = json.dumps(
            {
                "conversationId": "grandchild-conv",
                "hookEventName": "Stop",
                "cwd": "/tmp/grandchild",
            }
        )
        handle_hook_event(stop_payload, room_home=room_home, agent_pid=agent.pid)
        assert p_file.is_file()
        waiting = json.loads(p_file.read_text(encoding="utf-8")).get("waiting_for_pids", [])
        assert sh_pid in waiting
        assert len(waiting) >= 1

        for _ in range(50):
            if not p_file.is_file() and not is_pid_alive(hb_pid):
                break
            time.sleep(0.1)
        assert not p_file.is_file()
        assert not is_pid_alive(hb_pid)
    finally:
        _stop_dummy_agent(agent)


def test_antigravity_mid_turn_pre_invocation_does_not_reset_turn_baseline(tmp_path: Path) -> None:
    """Antigravity fires PreInvocation before every LLM step; mid-turn PreInvocation must not hide a spawned background child."""
    room_home = tmp_path / "room"
    agent = _start_dummy_agent()
    try:
        pre_payload = json.dumps(
            {
                "conversationId": "agy-multistep-conv",
                "hookEventName": "PreInvocation",
                "cwd": "/tmp/agy-multistep",
            }
        )
        post_payload = json.dumps(
            {
                "conversationId": "agy-multistep-conv",
                "hookEventName": "PostToolUse",
                "cwd": "/tmp/agy-multistep",
            }
        )
        stop_payload = json.dumps(
            {
                "conversationId": "agy-multistep-conv",
                "hookEventName": "Stop",
                "cwd": "/tmp/agy-multistep",
            }
        )

        # Step 1: Turn starts with PreInvocation #1
        handle_hook_event(
            pre_payload,
            room_home=room_home,
            agent_pid=agent.pid,
            spawn_heartbeat=True,
            heartbeat_interval_seconds=0.15,
            heartbeat_max_seconds=10.0,
        )
        p_file = room_home / "presence" / "agy-agy-multistep-conv.json"
        initial = json.loads(p_file.read_text(encoding="utf-8"))
        t0 = initial["turn_started_at"]
        hb_pid = initial["hb_pid"]

        # Step 2: run_command spawns background child and PostToolUse fires
        bg_pid = _spawn_agent_child(agent, 2.0)
        handle_hook_event(post_payload, room_home=room_home, agent_pid=agent.pid)

        # Step 3: Antigravity fires PreInvocation #2 for the follow-up LLM reply while bg_pid is running
        time.sleep(0.2)
        handle_hook_event(pre_payload, room_home=room_home, agent_pid=agent.pid)
        mid = json.loads(p_file.read_text(encoding="utf-8"))
        assert mid["turn_started_at"] == t0
        assert bg_pid not in mid.get("turn_baseline_pids", [])

        # Step 4: Turn ends (Stop) -> bg_pid MUST still keep presence alive
        handle_hook_event(stop_payload, room_home=room_home, agent_pid=agent.pid)
        assert p_file.is_file()
        after_stop = json.loads(p_file.read_text(encoding="utf-8"))
        assert after_stop.get("waiting_for_children") is True
        assert bg_pid in after_stop.get("waiting_for_pids", [])

        # Wait for bg_pid to exit and confirm clean release
        for _ in range(50):
            if not p_file.is_file() and not is_pid_alive(hb_pid):
                break
            time.sleep(0.1)
        assert not p_file.is_file()
        assert not is_pid_alive(hb_pid)
    finally:
        _stop_dummy_agent(agent)


def test_stale_or_expired_agy_presence_starts_fresh_turn_on_pre_invocation(tmp_path: Path) -> None:
    """A stale (>120s) or expired (>2h) Antigravity presence file must not block a fresh PreInvocation."""
    room_home = tmp_path / "room"
    p_dir = room_home / "presence"
    p_dir.mkdir(parents=True, exist_ok=True)
    p_file = p_dir / "agy-stale-agy-conv.json"
    agent = _start_dummy_agent()
    try:
        # 1. Expired (>2h ago) stale presence file left behind by a crash
        old_time = time.time() - 9000.0
        p_file.write_text(
            json.dumps(
                {
                    "agent": "antigravity",
                    "status": "working",
                    "task": "old session",
                    "pid": agent.pid,
                    "updated_at": datetime.fromtimestamp(old_time, tz=timezone.utc).isoformat(),
                    "turn_started_at": old_time,
                    "max_deadline_epoch": old_time + 7200.0,
                    "turn_baseline_pids": [],
                }
            ),
            encoding="utf-8",
        )
        # Pre-existing MCP-like child started before the new turn
        mcp_pid = _spawn_agent_child(agent, 20.0)

        pre_payload = json.dumps(
            {
                "conversationId": "stale-agy-conv",
                "hookEventName": "PreInvocation",
                "cwd": "/tmp/stale-agy",
            }
        )
        handle_hook_event(
            pre_payload,
            room_home=room_home,
            agent_pid=agent.pid,
            spawn_heartbeat=True,
            heartbeat_interval_seconds=0.15,
            heartbeat_max_seconds=10.0,
        )
        assert p_file.is_file()
        data = json.loads(p_file.read_text(encoding="utf-8"))
        assert data["turn_started_at"] > old_time + 8000.0
        assert data["max_deadline_epoch"] > time.time()
        assert mcp_pid in data.get("turn_baseline_pids", [])
        hb_pid = data["hb_pid"]
        assert is_pid_alive(hb_pid)

        # Clean up via Stop (old mcp_pid must NOT keep presence alive)
        stop_payload = json.dumps(
            {
                "conversationId": "stale-agy-conv",
                "hookEventName": "Stop",
                "cwd": "/tmp/stale-agy",
            }
        )
        handle_hook_event(stop_payload, room_home=room_home, agent_pid=agent.pid)
        assert not p_file.is_file()
        for _ in range(30):
            if not is_pid_alive(hb_pid):
                break
            time.sleep(0.05)
        assert not is_pid_alive(hb_pid)
    finally:
        _stop_dummy_agent(agent)


def test_resumed_session_with_new_agent_pid_replaces_heartbeat_and_old_hb_does_not_delete(
    tmp_path: Path,
) -> None:
    """Resuming the same conversationId under a new agent PID spawns a fresh heartbeat and old agent death must not delete it."""
    room_home = tmp_path / "room"
    agent1 = _start_dummy_agent()
    agent2 = _start_dummy_agent()
    try:
        pre_payload = json.dumps(
            {
                "conversationId": "resumed-conv",
                "hookEventName": "PreInvocation",
                "cwd": "/tmp/resumed",
            }
        )
        handle_hook_event(
            pre_payload,
            room_home=room_home,
            agent_pid=agent1.pid,
            spawn_heartbeat=True,
            heartbeat_interval_seconds=0.15,
            heartbeat_max_seconds=10.0,
        )
        p_file = room_home / "presence" / "agy-resumed-conv.json"
        hb1 = json.loads(p_file.read_text(encoding="utf-8"))["hb_pid"]
        assert is_pid_alive(hb1)

        # Agent 2 has its own pre-existing MCP child and resumes the same conversationId
        agent2_mcp = _spawn_agent_child(agent2, 20.0)
        handle_hook_event(
            pre_payload,
            room_home=room_home,
            agent_pid=agent2.pid,
            spawn_heartbeat=True,
            heartbeat_interval_seconds=0.15,
            heartbeat_max_seconds=10.0,
        )
        resumed = json.loads(p_file.read_text(encoding="utf-8"))
        hb2 = resumed["hb_pid"]
        assert resumed["pid"] == agent2.pid
        assert hb2 != hb1
        assert agent2_mcp in resumed.get("turn_baseline_pids", [])
        assert is_pid_alive(hb2)

        # Agent 1 exits -> must NOT delete agent 2's presence file or kill hb2
        _stop_dummy_agent(agent1)
        time.sleep(0.4)
        assert p_file.is_file()
        assert is_pid_alive(hb2)

        # Clean up agent 2
        stop_payload = json.dumps(
            {
                "conversationId": "resumed-conv",
                "hookEventName": "Stop",
                "cwd": "/tmp/resumed",
            }
        )
        handle_hook_event(stop_payload, room_home=room_home, agent_pid=agent2.pid)
        assert not p_file.is_file()
        for _ in range(30):
            if not is_pid_alive(hb2):
                break
            time.sleep(0.05)
        assert not is_pid_alive(hb2)
    finally:
        if agent1.poll() is None:
            _stop_dummy_agent(agent1)
        if agent2.poll() is None:
            _stop_dummy_agent(agent2)


def test_expired_carryover_on_turn2_starts_fresh_turn_and_baselines_expired_child(
    tmp_path: Path,
) -> None:
    """If Turn 1's draining child has already exceeded max_deadline_epoch, Turn 2 must still start cleanly with a fresh cap."""
    room_home = tmp_path / "room"
    p_dir = room_home / "presence"
    p_dir.mkdir(parents=True, exist_ok=True)
    p_file = p_dir / "claude-expired-carry.json"
    agent = _start_dummy_agent()
    try:
        old_bg = _spawn_agent_child(agent, 20.0)
        past_start = time.time() - 7300.0
        # Presence file is freshly updated (<120s) in drain mode, but its 2h max_deadline_epoch has already elapsed
        p_file.write_text(
            json.dumps(
                {
                    "agent": "claude",
                    "status": "working",
                    "task": "old turn",
                    "pid": agent.pid,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "turn_started_at": past_start,
                    "max_deadline_epoch": time.time() - 5.0,
                    "turn_baseline_pids": [],
                    "waiting_for_children": True,
                    "waiting_for_pids": [old_bg],
                }
            ),
            encoding="utf-8",
        )

        # Turn 2 starts now -> must NOT drop Turn 2; must baseline old_bg and start with fresh cap
        t2_start = json.dumps(
            {
                "session_id": "expired-carry",
                "hook_event_name": "UserPromptSubmit",
                "cwd": "/tmp/expired-carry",
            }
        )
        handle_hook_event(
            t2_start,
            room_home=room_home,
            agent_pid=agent.pid,
            spawn_heartbeat=False,
            heartbeat_max_seconds=10.0,
        )
        assert p_file.is_file()
        t2_data = json.loads(p_file.read_text(encoding="utf-8"))
        assert t2_data["max_deadline_epoch"] > time.time() + 5.0
        assert old_bg in t2_data.get("turn_baseline_pids", [])
        assert "carry_waiting_pids" not in t2_data

        # When Turn 2 stops with no new child, expired old_bg must NOT keep the hold alive
        t2_stop = json.dumps(
            {
                "session_id": "expired-carry",
                "hook_event_name": "Stop",
                "cwd": "/tmp/expired-carry",
            }
        )
        handle_hook_event(t2_stop, room_home=room_home, agent_pid=agent.pid)
        assert not p_file.is_file()
    finally:
        _stop_dummy_agent(agent)


def test_claude_settings_merge_and_unmerge_with_python_module_fallback() -> None:
    """merge_claude_settings / unmerge_claude_settings must be idempotent for the `-m agent_power.cli hook-event` fallback."""
    from agent_power.hooks import merge_claude_settings, unmerge_claude_settings

    fallback_cmd = "/usr/bin/python3 -m agent_power.cli hook-event"
    base = {"theme": "dark"}
    once = merge_claude_settings(base, hook_command=fallback_cmd)
    twice = merge_claude_settings(once, hook_command=fallback_cmd)
    assert twice == once
    cleaned = unmerge_claude_settings(twice)
    assert cleaned == base


def test_stop_racing_with_new_turn_does_not_delete_or_clobber_new_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a new turn starts while Stop is running `/bin/ps`, Stop must not delete or clobber the new turn."""
    import agent_power.hooks as hooks_mod

    room_home = tmp_path / "room"
    agent = _start_dummy_agent()
    try:
        t1_start = json.dumps(
            {
                "session_id": "race-stop-sess",
                "hook_event_name": "UserPromptSubmit",
                "cwd": "/tmp/race-stop",
            }
        )
        t1_stop = json.dumps(
            {
                "session_id": "race-stop-sess",
                "hook_event_name": "Stop",
                "cwd": "/tmp/race-stop",
            }
        )
        handle_hook_event(t1_start, room_home=room_home, agent_pid=agent.pid, spawn_heartbeat=False)
        p_file = room_home / "presence" / "claude-race-stop-sess.json"
        t1_started = json.loads(p_file.read_text(encoding="utf-8"))["turn_started_at"]

        real_snapshot = hooks_mod._snapshot_processes
        injected = {"done": False}

        def racy_snapshot():
            if not injected["done"]:
                injected["done"] = True
                # Simulate Turn 2 starting concurrently while Turn 1's Stop is inside /bin/ps
                time.sleep(0.02)
                handle_hook_event(
                    t1_start,
                    room_home=room_home,
                    agent_pid=agent.pid,
                    spawn_heartbeat=False,
                )
            return real_snapshot()

        monkeypatch.setattr(hooks_mod, "_snapshot_processes", racy_snapshot)
        handle_hook_event(t1_stop, room_home=room_home, agent_pid=agent.pid, spawn_heartbeat=False)

        assert p_file.is_file()
        after = json.loads(p_file.read_text(encoding="utf-8"))
        assert after["turn_started_at"] > t1_started
        assert after.get("waiting_for_children") is not True
    finally:
        _stop_dummy_agent(agent)
