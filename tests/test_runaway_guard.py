"""Comprehensive tests for runaway_guard.py."""

from __future__ import annotations

import json
from pathlib import Path
import signal
from types import SimpleNamespace
from typing import Any
import psutil

from agent_power.runaway_guard import (
    format_guard_status,
    is_script_interpreter,
    run_guard_check,
)


class FakeGuardProcess:
    def __init__(
        self,
        *,
        pid: int = 555,
        uid: int = 501,
        ppid: int = 1,
        name: str = "python3",
        cmdline: list[str] | None = None,
        cwd: str = "/Users/alice/jobs",
        ports: list[int] | None = None,
        create_time: float = 100.0,
        survive_sigterm: bool = False,
    ) -> None:
        self.pid = pid
        self._uid = uid
        self._ppid = ppid
        self._name = name
        self._cmdline = cmdline if cmdline is not None else ["python3", "orphan_loop.py"]
        self._cwd = cwd
        self._ports = ports or []
        self._create_time = create_time
        self._survive_sigterm = survive_sigterm
        self.cpu_user: float = 0.0
        self.cpu_system: float = 0.0
        self.signals_received: list[int] = []
        self._running = True

    def uids(self) -> Any:
        return SimpleNamespace(real=self._uid)

    def ppid(self) -> int:
        return self._ppid

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list[str]:
        return list(self._cmdline)

    def cwd(self) -> str:
        return self._cwd

    def create_time(self) -> float:
        return self._create_time

    def cpu_times(self) -> Any:
        return SimpleNamespace(user=self.cpu_user, system=self.cpu_system)

    def net_connections(self, kind: str = "inet") -> list[Any]:
        return [
            SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=p))
            for p in self._ports
        ]

    def is_running(self) -> bool:
        return self._running

    def send_signal(self, sig: int) -> None:
        self.signals_received.append(sig)
        if sig == signal.SIGTERM and not self._survive_sigterm:
            self._running = False
        elif sig == signal.SIGKILL:
            self._running = False


def _simulate_10_minutes(
    proc: FakeGuardProcess,
    *,
    state_dir: Path,
    config_dir: Path,
    log_path: Path,
    cpu_pct_per_minute: list[float],
    notifications: list[tuple[str, str]],
) -> Any:
    """Run baseline check at t=1000, then 1 check per minute for len(cpu_pct_per_minute) minutes."""
    now = 1000.0
    # Baseline check at t=1000
    run_guard_check(
        state_dir=state_dir,
        config_dir=config_dir,
        log_path=log_path,
        now=now,
        process_iter_fn=lambda: [proc],
        notify_fn=lambda t, m: (notifications.append((t, m)) or True),
        sleep_fn=lambda _: None,
        current_uid=501,
        self_pid=9999,
    )

    last_result = None
    for pct in cpu_pct_per_minute:
        now += 60.0
        proc.cpu_user += 60.0 * (pct / 100.0)
        last_result = run_guard_check(
            state_dir=state_dir,
            config_dir=config_dir,
            log_path=log_path,
            now=now,
            process_iter_fn=lambda: [proc],
            notify_fn=lambda t, m: (notifications.append((t, m)) or True),
            sleep_fn=lambda _: None,
            current_uid=501,
            self_pid=9999,
        )
    return last_result


def test_is_script_interpreter_recognizes_all_required_interpreters() -> None:
    for interp in ("python", "python3.13", "node", "ruby", "perl", "bash", "zsh", "sh", "deno", "bun", "php"):
        assert is_script_interpreter(interp, [interp, "script"]) is True
    assert is_script_interpreter("ffmpeg", ["ffmpeg", "-i", "in.mp4"]) is False
    assert is_script_interpreter("clang", ["/usr/bin/clang"]) is False


def test_notify_only_default_after_10_minutes_at_80_percent(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    log_path = tmp_path / "logs" / "guard.log"
    notifications: list[tuple[str, str]] = []

    proc = FakeGuardProcess(pid=700, name="python3", cmdline=["python3", "runaway.py"], cwd="/Users/alice/work")

    # 9 minutes at 85% -> should NOT trigger yet
    res_9m = _simulate_10_minutes(
        proc,
        state_dir=state_dir,
        config_dir=config_dir,
        log_path=log_path,
        cpu_pct_per_minute=[85.0] * 9,
        notifications=notifications,
    )
    assert res_9m.actions == []
    assert notifications == []

    # 10th minute at 80.0% -> triggers NOTIFY-ONLY (does NOT stop process)
    proc.cpu_user += 60.0 * 0.80
    res_10m = run_guard_check(
        state_dir=state_dir,
        config_dir=config_dir,
        log_path=log_path,
        now=1000.0 + 600.0,
        process_iter_fn=lambda: [proc],
        notify_fn=lambda t, m: (notifications.append((t, m)) or True),
        sleep_fn=lambda _: None,
        current_uid=501,
        self_pid=9999,
    )
    assert len(res_10m.actions) == 1
    act = res_10m.actions[0]
    assert act.notified is True
    assert act.stopped is False
    assert proc.signals_received == []
    assert len(notifications) == 1
    assert "runaway.py" in notifications[0][1]
    assert "/Users/alice/work" in notifications[0][1]
    assert log_path.is_file()
    assert "NOTIFY-ONLY runaway script" in log_path.read_text(encoding="utf-8")


def test_each_condition_that_must_prevent_action(tmp_path: Path) -> None:
    notifications: list[tuple[str, str]] = []

    # 1. Not my process (uid=502 != 501)
    p_other_user = FakeGuardProcess(pid=10, uid=502)
    res = _simulate_10_minutes(
        p_other_user,
        state_dir=tmp_path / "s1",
        config_dir=tmp_path / "c1",
        log_path=tmp_path / "l1" / "guard.log",
        cpu_pct_per_minute=[95.0] * 10,
        notifications=notifications,
    )
    assert res.actions == []

    # 2. Parent is NOT launchd (ppid=450 != 1)
    p_non_launchd = FakeGuardProcess(pid=11, ppid=450)
    res = _simulate_10_minutes(
        p_non_launchd,
        state_dir=tmp_path / "s2",
        config_dir=tmp_path / "c2",
        log_path=tmp_path / "l2" / "guard.log",
        cpu_pct_per_minute=[95.0] * 10,
        notifications=notifications,
    )
    assert res.actions == []

    # 3. Not a script interpreter (e.g. rustc)
    p_compiled = FakeGuardProcess(pid=12, name="rustc", cmdline=["rustc", "main.rs"])
    res = _simulate_10_minutes(
        p_compiled,
        state_dir=tmp_path / "s3",
        config_dir=tmp_path / "c3",
        log_path=tmp_path / "l3" / "guard.log",
        cpu_pct_per_minute=[95.0] * 10,
        notifications=notifications,
    )
    assert res.actions == []

    # 4. Listens on a network port (port 8000)
    p_listening = FakeGuardProcess(pid=13, ports=[8000])
    res = _simulate_10_minutes(
        p_listening,
        state_dir=tmp_path / "s4",
        config_dir=tmp_path / "c4",
        log_path=tmp_path / "l4" / "guard.log",
        cpu_pct_per_minute=[95.0] * 10,
        notifications=notifications,
    )
    assert res.actions == []

    # 5. Matches ~/.config/agent-power/allow substring
    c5 = tmp_path / "c5"
    c5.mkdir(parents=True)
    (c5 / "allow").write_text("# Allowed background worker\nmy_trusted_worker.py\n", encoding="utf-8")
    p_allowed = FakeGuardProcess(pid=14, cmdline=["python3", "/opt/jobs/my_trusted_worker.py"])
    res = _simulate_10_minutes(
        p_allowed,
        state_dir=tmp_path / "s5",
        config_dir=c5,
        log_path=tmp_path / "l5" / "guard.log",
        cpu_pct_per_minute=[95.0] * 10,
        notifications=notifications,
    )
    assert res.actions == []

    # 6. Dips below 80% on check #5 (e.g. 79.0%) -> resets streak, does NOT fire at 10m
    p_dip = FakeGuardProcess(pid=15)
    Pattern = [90.0, 90.0, 90.0, 90.0, 79.0, 90.0, 90.0, 90.0, 90.0, 90.0]
    res = _simulate_10_minutes(
        p_dip,
        state_dir=tmp_path / "s6",
        config_dir=tmp_path / "c6",
        log_path=tmp_path / "l6" / "guard.log",
        cpu_pct_per_minute=Pattern,
        notifications=notifications,
    )
    assert res.actions == []

    # 7. Kill switch ~/.config/agent-power/guard_disabled exists -> does nothing
    c7 = tmp_path / "c7"
    c7.mkdir(parents=True)
    (c7 / "guard_disabled").write_text("", encoding="utf-8")
    p_killswitch = FakeGuardProcess(pid=16)
    res_ks = run_guard_check(
        state_dir=tmp_path / "s7",
        config_dir=c7,
        log_path=tmp_path / "l7" / "guard.log",
        now=1000.0,
        process_iter_fn=lambda: [p_killswitch],
        notify_fn=lambda t, m: (notifications.append((t, m)) or True),
    )
    assert res_ks.disabled_by_kill_switch is True
    assert notifications == []


def test_auto_stop_sends_sigterm_then_sigkill_when_enabled(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"guard": {"auto_stop": True}}),
        encoding="utf-8",
    )
    log_path = tmp_path / "logs" / "guard.log"
    notifications: list[tuple[str, str]] = []

    proc = FakeGuardProcess(
        pid=888,
        name="node",
        cmdline=["node", "stuck_worker.js"],
        cwd="/Users/alice/workers",
        survive_sigterm=True,
    )

    res = _simulate_10_minutes(
        proc,
        state_dir=state_dir,
        config_dir=config_dir,
        log_path=log_path,
        cpu_pct_per_minute=[92.0] * 10,
        notifications=notifications,
    )
    assert len(res.actions) == 1
    act = res.actions[0]
    assert act.notified is True
    assert act.stopped is True
    assert act.killed_with_sigkill is True
    assert proc.signals_received == [signal.SIGTERM, signal.SIGKILL]
    assert "AUTO-STOPPED runaway script" in log_path.read_text(encoding="utf-8")

    status_text = format_guard_status(state_dir=state_dir, config_dir=config_dir, log_path=log_path)
    assert "Auto-stop" in status_text
    assert "AUTO-STOPPED" in status_text
