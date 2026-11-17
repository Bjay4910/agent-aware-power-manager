"""Tests for battery_viewer.py."""

from __future__ import annotations

import signal
from types import SimpleNamespace
from typing import Any
import psutil

from agent_power.battery_viewer import (
    BatteryProcessInfo,
    quit_user_process,
    sample_battery_processes,
)


class FakeProcess:
    def __init__(
        self,
        *,
        pid: int,
        uid: int,
        name: str,
        cmdline: list[str],
        cwd: str,
        ppid: int,
        parent_name: str,
        cpu_pct: float,
        ports: list[int] | None = None,
        survive_sigterm: bool = False,
    ) -> None:
        self.pid = pid
        self._uid = uid
        self._name = name
        self._cmdline = cmdline
        self._cwd = cwd
        self._ppid = ppid
        self._parent_name = parent_name
        self._cpu_pct = cpu_pct
        self._ports = ports or []
        self._survive_sigterm = survive_sigterm
        self.signals_received: list[int] = []
        self._running = True
        self._cpu_calls = 0

    def uids(self) -> Any:
        return SimpleNamespace(real=self._uid)

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list[str]:
        return list(self._cmdline)

    def cwd(self) -> str:
        return self._cwd

    def ppid(self) -> int:
        return self._ppid

    def parent(self) -> Any:
        if self._ppid <= 1:
            return None
        return SimpleNamespace(name=lambda: self._parent_name)

    def cpu_percent(self, interval: float | None = None) -> float:
        self._cpu_calls += 1
        return 0.0 if self._cpu_calls == 1 else self._cpu_pct

    def net_connections(self, kind: str = "inet") -> list[Any]:
        return [
            SimpleNamespace(
                status=psutil.CONN_LISTEN,
                laddr=SimpleNamespace(port=p),
            )
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


def test_sample_battery_processes_filters_and_formats() -> None:
    my_uid = 501
    self_pid = 9000
    slept: list[float] = []

    procs = [
        # 1. Valid high-CPU own Python script
        FakeProcess(
            pid=101,
            uid=my_uid,
            name="python3",
            cmdline=["python3", "train_model.py"],
            cwd="/Users/alice/projects/ml",
            ppid=80,
            parent_name="zsh",
            cpu_pct=42.4,
            ports=[8080],
        ),
        # 2. Below 5% threshold (5.0% exactly is not > 5%)
        FakeProcess(
            pid=102,
            uid=my_uid,
            name="node",
            cmdline=["node", "idle.js"],
            cwd="/Users/alice/app",
            ppid=80,
            parent_name="zsh",
            cpu_pct=5.0,
        ),
        # 3. Other user's process (root uid 0)
        FakeProcess(
            pid=103,
            uid=0,
            name="python3",
            cmdline=["python3", "root_job.py"],
            cwd="/var/root",
            ppid=1,
            parent_name="launchd",
            cpu_pct=99.0,
        ),
        # 4. System process
        FakeProcess(
            pid=104,
            uid=my_uid,
            name="WindowServer",
            cmdline=["/System/Library/PrivateFrameworks/SkyLight.framework/WindowServer"],
            cwd="/",
            ppid=1,
            parent_name="launchd",
            cpu_pct=35.0,
        ),
        # 5. The multi-agent-memory-hub room itself (protected)
        FakeProcess(
            pid=105,
            uid=my_uid,
            name="node",
            cmdline=["node", "/Users/alice/multi-agent-memory-hub/server.js"],
            cwd="/Users/alice/multi-agent-memory-hub",
            ppid=80,
            parent_name="zsh",
            cpu_pct=50.0,
        ),
        # 6. Self process (protected)
        FakeProcess(
            pid=self_pid,
            uid=my_uid,
            name="python3",
            cmdline=["python3", "-m", "agent_power.menubar"],
            cwd="/Users/alice",
            ppid=1,
            parent_name="launchd",
            cpu_pct=20.0,
        ),
    ]

    listed = sample_battery_processes(
        process_iter_fn=lambda: procs,
        sleep_fn=lambda s: slept.append(s),
        current_uid=my_uid,
        self_pid=self_pid,
    )

    assert slept == [0.7]
    assert len(listed) == 1
    item = listed[0]
    assert item.pid == 101
    assert item.what_it_is == "Python script (train_model.py)"
    assert item.working_folder == "/Users/alice/projects/ml"
    assert item.listening_ports == (8080,)
    assert item.started_by == "zsh (PID 80)"
    assert "ports 8080" in item.summary


def test_quit_user_process_confirmation_sigterm_and_sigkill() -> None:
    my_uid = 501
    info = BatteryProcessInfo(
        pid=201,
        name="python3",
        cpu_percent=75.0,
        what_it_is="Python script (spin.py)",
        working_folder="/tmp/work",
        listening_ports=(),
        started_by="zsh (PID 50)",
        cmdline=("python3", "spin.py"),
        uid=my_uid,
    )

    # 1. Cancelled confirmation does not send any signal
    fake_proc = FakeProcess(
        pid=201,
        uid=my_uid,
        name="python3",
        cmdline=["python3", "spin.py"],
        cwd="/tmp/work",
        ppid=50,
        parent_name="zsh",
        cpu_pct=75.0,
        survive_sigterm=True,
    )
    res_cancel = quit_user_process(
        info,
        confirm_fn=lambda _: False,
        process_factory=lambda _: fake_proc,
        sleep_fn=lambda _: None,
        current_uid=my_uid,
        self_pid=9999,
    )
    assert res_cancel.cancelled is True
    assert res_cancel.terminated is False
    assert fake_proc.signals_received == []

    # 2. Confirmed, process survives SIGTERM -> waits 5s and sends SIGKILL
    slept: list[float] = []
    res_kill = quit_user_process(
        info,
        confirm_fn=lambda _: True,
        process_factory=lambda _: fake_proc,
        sleep_fn=lambda s: slept.append(s),
        current_uid=my_uid,
        self_pid=9999,
    )
    assert res_kill.terminated is True
    assert res_kill.killed_with_sigkill is True
    assert fake_proc.signals_received == [signal.SIGTERM, signal.SIGKILL]
    assert sum(slept) == 5.0

    # 3. Protected room process is refused even if passed to quit_user_process
    room_info = BatteryProcessInfo(
        pid=301,
        name="node",
        cpu_percent=80.0,
        what_it_is="Node.js app (hub.js)",
        working_folder="/Users/alice/multi-agent-memory-hub",
        listening_ports=(3000,),
        started_by="zsh (PID 50)",
        cmdline=("node", "multi-agent-memory-hub/hub.js"),
        uid=my_uid,
    )
    res_protected = quit_user_process(
        room_info,
        confirm_fn=lambda _: True,
        process_factory=lambda _: fake_proc,
        sleep_fn=lambda _: None,
        current_uid=my_uid,
        self_pid=9999,
    )
    assert res_protected.terminated is False
    assert "Refused" in res_protected.reason
