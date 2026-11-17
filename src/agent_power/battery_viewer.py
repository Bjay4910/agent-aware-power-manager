"""Process battery/CPU viewer and safe process terminator.

Samples CPU usage twice 0.7 seconds apart using psutil and lists the current
user's own processes above 5% CPU, described in plain words:
- what it is
- its working folder
- ports it listens on
- who started it (parent)

Provides a Quit action that asks for confirmation, sends SIGTERM, and sends
SIGKILL after 5 seconds if the process has not exited.
Never touches system processes, other users' processes, itself, or the room.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import time
from typing import Any, Callable, Iterable, Sequence
import psutil


SAMPLE_INTERVAL_SECONDS: float = 0.7
MIN_CPU_PERCENT: float = 5.0
KILL_WAIT_SECONDS: float = 5.0

SYSTEM_NAME_DENYLIST: frozenset[str] = frozenset(
    {
        "kernel_task",
        "launchd",
        "WindowServer",
        "loginwindow",
        "SystemUIServer",
        "Dock",
        "Finder",
        "cfprefsd",
        "distnoted",
        "trustd",
        "securityd",
        "coreaudiod",
        "mds",
        "mds_stores",
    }
)

ROOM_PROTECTED_MARKERS: tuple[str, ...] = (
    "multi-agent-memory-hub",
    "agent-room",
    ".agent-room",
)

SELF_PROTECTED_MARKERS: tuple[str, ...] = (
    "agent-power",
    "agent_power",
    "agent-aware-power-manager",
)


@dataclass(frozen=True)
class BatteryProcessInfo:
    """Plain-word description of a high-CPU user process."""

    pid: int
    name: str
    cpu_percent: float
    what_it_is: str
    working_folder: str
    listening_ports: tuple[int, ...]
    started_by: str
    cmdline: tuple[str, ...] = ()
    uid: int | None = None

    @property
    def summary(self) -> str:
        """Single plain-word summary line suitable for menu bar or CLI display."""
        if self.listening_ports:
            ports_str = "ports " + ", ".join(str(p) for p in self.listening_ports)
        else:
            ports_str = "no open ports"
        return (
            f"{self.what_it_is} (PID {self.pid}, {self.cpu_percent:.1f}% CPU) — "
            f"folder: {self.working_folder} | {ports_str} | started by {self.started_by}"
        )


@dataclass(frozen=True)
class QuitProcessResult:
    """Result of attempting to quit a high-CPU user process."""

    pid: int
    terminated: bool
    killed_with_sigkill: bool
    cancelled: bool
    reason: str


def _shorten_home(path_str: str) -> str:
    if not path_str:
        return "unknown folder"
    home = str(Path.home())
    if path_str == home:
        return "~"
    if path_str.startswith(home + "/"):
        return "~" + path_str[len(home) :]
    return path_str


def describe_what_it_is(name: str, cmdline: Sequence[str]) -> str:
    """Produce a plain-word description of a process from its name and command line."""
    base = (name or (Path(cmdline[0]).name if cmdline else "process")).strip()
    lower = base.lower()

    script_arg: str | None = None
    for token in cmdline[1:]:
        if not token or token.startswith("-"):
            continue
        script_arg = Path(token).name or token
        break

    if lower.startswith("python"):
        return f"Python script ({script_arg})" if script_arg else "Python process"
    if lower in {"node", "nodejs"}:
        return f"Node.js app ({script_arg})" if script_arg else "Node.js process"
    if lower in {"ruby", "perl", "php", "deno", "bun"}:
        label = base.capitalize()
        return f"{label} script ({script_arg})" if script_arg else f"{label} process"
    if lower in {"bash", "zsh", "sh"}:
        return f"{base} shell script ({script_arg})" if script_arg else f"{base} shell"
    if script_arg and script_arg != base:
        return f"{base} ({script_arg})"
    return base


def is_protected_process(
    *,
    pid: int,
    uid: int | None,
    name: str,
    cmdline: Sequence[str],
    cwd: str,
    current_uid: int | None = None,
    self_pid: int | None = None,
) -> bool:
    """Return True if a process must NEVER be listed or touched."""
    my_uid = os.getuid() if current_uid is None else current_uid
    my_pid = os.getpid() if self_pid is None else self_pid

    if pid <= 1 or pid == my_pid:
        return True
    if uid is None or uid != my_uid:
        return True
    if name in SYSTEM_NAME_DENYLIST:
        return True

    exe_first = cmdline[0] if cmdline else ""
    if exe_first.startswith(("/System/", "/usr/libexec/", "/sbin/", "/usr/sbin/")):
        return True

    joined = " ".join([name, cwd, *cmdline]).lower()
    for marker in ROOM_PROTECTED_MARKERS:
        if marker in joined:
            return True
    for marker in SELF_PROTECTED_MARKERS:
        if marker in joined:
            return True

    return False


def _extract_listening_ports(proc: Any) -> tuple[int, ...]:
    ports: set[int] = set()
    try:
        conns = proc.net_connections(kind="inet")
    except (psutil.Error, OSError, AttributeError):
        return ()
    for conn in conns:
        status = getattr(conn, "status", None)
        if status == psutil.CONN_LISTEN or status == "LISTEN":
            laddr = getattr(conn, "laddr", None)
            port = getattr(laddr, "port", None) if laddr is not None else None
            if isinstance(port, int) and port > 0:
                ports.add(port)
    return tuple(sorted(ports))


def _describe_parent(proc: Any, ppid: int) -> str:
    if ppid <= 1:
        return "launchd (background)"
    try:
        parent = proc.parent() if hasattr(proc, "parent") else psutil.Process(ppid)
        if parent is not None:
            pname = parent.name()
            return f"{pname} (PID {ppid})"
    except (psutil.Error, OSError):
        pass
    return f"PID {ppid}"


def sample_battery_processes(
    *,
    sample_interval: float = SAMPLE_INTERVAL_SECONDS,
    min_cpu_percent: float = MIN_CPU_PERCENT,
    process_iter_fn: Callable[[], Iterable[Any]] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    current_uid: int | None = None,
    self_pid: int | None = None,
) -> list[BatteryProcessInfo]:
    """Sample CPU usage twice 0.7s apart and return eligible user processes above 5% CPU."""
    iter_procs = process_iter_fn or (lambda: psutil.process_iter())
    my_uid = os.getuid() if current_uid is None else current_uid
    my_pid = os.getpid() if self_pid is None else self_pid

    candidates: list[Any] = []
    for proc in iter_procs():
        try:
            pid = proc.pid
            uids = proc.uids()
            uid = getattr(uids, "real", None) if uids is not None else None
            if uid != my_uid or pid <= 1 or pid == my_pid:
                continue
            proc.cpu_percent(None)
            candidates.append(proc)
        except (psutil.Error, OSError):
            continue

    sleep_fn(sample_interval)

    results: list[BatteryProcessInfo] = []
    for proc in candidates:
        try:
            cpu_pct = float(proc.cpu_percent(None))
            if cpu_pct <= min_cpu_percent:
                continue

            pid = int(proc.pid)
            uids = proc.uids()
            uid = int(getattr(uids, "real", -1)) if uids is not None else None
            name = str(proc.name() or "")
            try:
                cmdline = tuple(str(x) for x in (proc.cmdline() or ()))
            except (psutil.Error, OSError):
                cmdline = ()
            try:
                cwd_raw = str(proc.cwd() or "")
            except (psutil.Error, OSError):
                cwd_raw = ""

            if is_protected_process(
                pid=pid,
                uid=uid,
                name=name,
                cmdline=cmdline,
                cwd=cwd_raw,
                current_uid=my_uid,
                self_pid=my_pid,
            ):
                continue

            ppid = int(proc.ppid())
            what = describe_what_it_is(name, cmdline)
            folder = _shorten_home(cwd_raw)
            ports = _extract_listening_ports(proc)
            started_by = _describe_parent(proc, ppid)

            results.append(
                BatteryProcessInfo(
                    pid=pid,
                    name=name,
                    cpu_percent=round(cpu_pct, 1),
                    what_it_is=what,
                    working_folder=folder,
                    listening_ports=ports,
                    started_by=started_by,
                    cmdline=cmdline,
                    uid=uid,
                )
            )
        except (psutil.Error, OSError):
            continue

    results.sort(key=lambda item: item.cpu_percent, reverse=True)
    return results


def quit_user_process(
    proc_info: BatteryProcessInfo,
    *,
    confirm_fn: Callable[[BatteryProcessInfo], bool],
    wait_seconds: float = KILL_WAIT_SECONDS,
    process_factory: Callable[[int], Any] = psutil.Process,
    sleep_fn: Callable[[float], None] = time.sleep,
    current_uid: int | None = None,
    self_pid: int | None = None,
) -> QuitProcessResult:
    """Safely quit a user process after confirmation: SIGTERM first, then SIGKILL after 5s."""
    if is_protected_process(
        pid=proc_info.pid,
        uid=proc_info.uid,
        name=proc_info.name,
        cmdline=proc_info.cmdline,
        cwd=proc_info.working_folder,
        current_uid=current_uid,
        self_pid=self_pid,
    ):
        return QuitProcessResult(
            pid=proc_info.pid,
            terminated=False,
            killed_with_sigkill=False,
            cancelled=False,
            reason="Refused to touch protected, system, room, or other user's process.",
        )

    if not confirm_fn(proc_info):
        return QuitProcessResult(
            pid=proc_info.pid,
            terminated=False,
            killed_with_sigkill=False,
            cancelled=True,
            reason=f"Cancelled quitting {proc_info.what_it_is} (PID {proc_info.pid}).",
        )

    try:
        proc = process_factory(proc_info.pid)
        uids = proc.uids()
        live_uid = getattr(uids, "real", None) if uids is not None else None
        my_uid = os.getuid() if current_uid is None else current_uid
        if live_uid != my_uid:
            return QuitProcessResult(
                pid=proc_info.pid,
                terminated=False,
                killed_with_sigkill=False,
                cancelled=False,
                reason="Refused: process owner does not match current user.",
            )

        proc.send_signal(signal.SIGTERM)
    except psutil.NoSuchProcess:
        return QuitProcessResult(
            pid=proc_info.pid,
            terminated=True,
            killed_with_sigkill=False,
            cancelled=False,
            reason=f"{proc_info.what_it_is} (PID {proc_info.pid}) already exited.",
        )
    except (psutil.Error, OSError) as exc:
        return QuitProcessResult(
            pid=proc_info.pid,
            terminated=False,
            killed_with_sigkill=False,
            cancelled=False,
            reason=f"Could not terminate PID {proc_info.pid}: {exc}",
        )

    # Wait up to wait_seconds (5s) for graceful exit before sending SIGKILL
    step = 0.25
    elapsed = 0.0
    while elapsed < wait_seconds:
        try:
            if not proc.is_running():
                return QuitProcessResult(
                    pid=proc_info.pid,
                    terminated=True,
                    killed_with_sigkill=False,
                    cancelled=False,
                    reason=f"Stopped {proc_info.what_it_is} (PID {proc_info.pid}) cleanly with SIGTERM.",
                )
        except psutil.NoSuchProcess:
            return QuitProcessResult(
                pid=proc_info.pid,
                terminated=True,
                killed_with_sigkill=False,
                cancelled=False,
                reason=f"Stopped {proc_info.what_it_is} (PID {proc_info.pid}) cleanly with SIGTERM.",
            )
        sleep_step = min(step, wait_seconds - elapsed)
        sleep_fn(sleep_step)
        elapsed += sleep_step

    try:
        if proc.is_running():
            proc.send_signal(signal.SIGKILL)
            return QuitProcessResult(
                pid=proc_info.pid,
                terminated=True,
                killed_with_sigkill=True,
                cancelled=False,
                reason=(
                    f"Force-stopped {proc_info.what_it_is} (PID {proc_info.pid}) "
                    f"with SIGKILL after {wait_seconds:.0f}s."
                ),
            )
    except psutil.NoSuchProcess:
        pass
    except (psutil.Error, OSError) as exc:
        return QuitProcessResult(
            pid=proc_info.pid,
            terminated=False,
            killed_with_sigkill=False,
            cancelled=False,
            reason=f"SIGTERM sent, but SIGKILL failed for PID {proc_info.pid}: {exc}",
        )

    return QuitProcessResult(
        pid=proc_info.pid,
        terminated=True,
        killed_with_sigkill=False,
        cancelled=False,
        reason=f"Stopped {proc_info.what_it_is} (PID {proc_info.pid}) cleanly with SIGTERM.",
    )
