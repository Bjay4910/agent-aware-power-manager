"""Runaway background script guard.

Runs once a minute (from launchd or `agent-power guard-run`).
Acts ONLY when ALL conditions hold:
1. The process belongs to the current user (and is not self or the room).
2. Its parent is launchd (ppid == 1).
3. It is a script interpreter (python, node, ruby, perl, bash, zsh, sh, deno, bun, php).
4. It listens on NO network port.
5. No line in ~/.config/agent-power/allow matches its command line as a substring.
6. It used at least 80% of one core in every check for 10 minutes straight,
   measured from CPU time deltas stored in ~/.agent-power/guard_state.json.

DEFAULT BEHAVIOR IS NOTIFY-ONLY: sends a macOS notification via osascript
naming the process and working folder.
Only when `guard.auto_stop` is true does it send SIGTERM, then SIGKILL after 5s.
Logs events to ~/.local/state/agent-power/guard.log.
Kill switch: if ~/.config/agent-power/guard_disabled exists, does nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any, Callable, Iterable, Sequence
import psutil

from agent_power.battery_viewer import (
    _extract_listening_ports,
    _shorten_home,
    describe_what_it_is,
    is_protected_process,
)
from agent_power.engine import _atomic_write_json, get_state_dir
from agent_power.power import is_dry_run

CPU_THRESHOLD_PERCENT: float = 80.0
REQUIRED_STREAK_SECONDS: float = 600.0  # 10 minutes
REQUIRED_CONSECUTIVE_CHECKS: int = 10
KILL_WAIT_SECONDS: float = 5.0

EXACT_INTERPRETERS: frozenset[str] = frozenset(
    {
        "python",
        "node",
        "nodejs",
        "ruby",
        "perl",
        "bash",
        "zsh",
        "sh",
        "deno",
        "bun",
        "php",
    }
)
_PYTHON_VERSIONED_RE = re.compile(r"^python(\d+(\.\d+)*)?$")


@dataclass(frozen=True)
class GuardActionRecord:
    """Action taken (notify or auto-stop) on a runaway process."""

    pid: int
    name: str
    what_it_is: str
    working_folder: str
    cmdline: str
    cpu_percent: float
    streak_seconds: float
    consecutive_checks: int
    notified: bool
    stopped: bool
    killed_with_sigkill: bool
    message: str


@dataclass(frozen=True)
class GuardRunResult:
    """Summary returned by a runaway guard check."""

    disabled_by_kill_switch: bool
    auto_stop_enabled: bool
    tracked_pids: list[int] = field(default_factory=list)
    actions: list[GuardActionRecord] = field(default_factory=list)


def get_config_dir(config_dir: Path | str | None = None) -> Path:
    """Resolve ~/.config/agent-power (or $AGENT_POWER_CONFIG_DIR)."""
    if config_dir is not None:
        return Path(config_dir).expanduser()
    env_dir = os.environ.get("AGENT_POWER_CONFIG_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    return Path("~/.config/agent-power").expanduser()


def get_log_path(log_path: Path | str | None = None) -> Path:
    """Resolve ~/.local/state/agent-power/guard.log (or $AGENT_POWER_LOG_DIR)."""
    if log_path is not None:
        return Path(log_path).expanduser()
    env_dir = os.environ.get("AGENT_POWER_LOG_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser() / "guard.log"
    return Path("~/.local/state/agent-power/guard.log").expanduser()


def is_script_interpreter(name: str, cmdline: Sequence[str]) -> bool:
    """Return True if the process is one of the target script interpreters."""
    candidates: list[str] = []
    if name:
        candidates.append(Path(name).name.lower())
    if cmdline and cmdline[0]:
        candidates.append(Path(cmdline[0]).name.lower())

    for cand in candidates:
        if cand in EXACT_INTERPRETERS or _PYTHON_VERSIONED_RE.match(cand):
            return True
    return False


def load_allowlist(config_dir: Path | str | None = None) -> list[str]:
    """Read substring allowlist from ~/.config/agent-power/allow."""
    allow_file = get_config_dir(config_dir) / "allow"
    if not allow_file.is_file():
        return []
    try:
        lines = allow_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    patterns: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return patterns


def is_allowlisted(cmdline_str: str, allow_patterns: Sequence[str]) -> bool:
    """Return True if any substring in allow_patterns matches cmdline_str."""
    if not cmdline_str:
        return False
    for pattern in allow_patterns:
        if pattern and pattern in cmdline_str:
            return True
    return False


def load_auto_stop_setting(config_dir: Path | str | None = None) -> bool:
    """Read `guard.auto_stop` from ~/.config/agent-power/config.json (default False)."""
    cfg_path = get_config_dir(config_dir) / "config.json"
    if not cfg_path.is_file():
        return False
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    if isinstance(data.get("guard.auto_stop"), bool):
        return bool(data["guard.auto_stop"])
    guard_sec = data.get("guard")
    if isinstance(guard_sec, dict) and isinstance(guard_sec.get("auto_stop"), bool):
        return bool(guard_sec["auto_stop"])
    return False


def load_guard_state(state_dir: Path | str | None = None) -> dict[str, Any]:
    """Load ~/.agent-power/guard_state.json."""
    path = get_state_dir(state_dir) / "guard_state.json"
    if not path.is_file():
        return {"processes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("processes"), dict):
            return data
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    return {"processes": {}}


def append_guard_log(log_path: Path, message: str, now_epoch: float | None = None) -> None:
    """Append a timestamped entry to ~/.local/state/agent-power/guard.log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.fromtimestamp(
        now_epoch if now_epoch is not None else time.time(),
        tz=timezone.utc,
    ).isoformat()
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{ts}] {message}\n")


def send_macos_notification(
    title: str,
    message: str,
    *,
    dry_run: bool | None = None,
) -> bool:
    """Send a macOS notification via osascript."""
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_msg}" with title "{safe_title}"'
    cmd = ["/usr/bin/osascript", "-e", script]

    if is_dry_run(dry_run):
        print(f"[DRY-RUN] {' '.join(cmd)}", flush=True)
        return True

    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _stop_process_sigterm_sigkill(
    proc: Any,
    *,
    wait_seconds: float = KILL_WAIT_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[bool, bool]:
    """Send SIGTERM, wait 5s, and send SIGKILL if still running. Returns (stopped, killed_with_sigkill)."""
    try:
        proc.send_signal(signal.SIGTERM)
    except psutil.NoSuchProcess:
        return True, False
    except (psutil.Error, OSError):
        return False, False

    step = 0.25
    elapsed = 0.0
    while elapsed < wait_seconds:
        try:
            if not proc.is_running():
                return True, False
        except psutil.NoSuchProcess:
            return True, False
        sleep_step = min(step, wait_seconds - elapsed)
        sleep_fn(sleep_step)
        elapsed += sleep_step

    try:
        if proc.is_running():
            proc.send_signal(signal.SIGKILL)
            return True, True
    except psutil.NoSuchProcess:
        return True, False
    except (psutil.Error, OSError):
        return False, False
    return True, False


def run_guard_check(
    *,
    state_dir: Path | str | None = None,
    config_dir: Path | str | None = None,
    log_path: Path | str | None = None,
    auto_stop: bool | None = None,
    now: float | None = None,
    dry_run: bool | None = None,
    process_iter_fn: Callable[[], Iterable[Any]] | None = None,
    notify_fn: Callable[[str, str], bool] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    current_uid: int | None = None,
    self_pid: int | None = None,
) -> GuardRunResult:
    """Execute one runaway-guard check across the current user's processes."""
    resolved_config_dir = get_config_dir(config_dir)
    resolved_state_dir = get_state_dir(state_dir)
    resolved_log_path = get_log_path(log_path)
    now_epoch = float(now) if now is not None else time.time()

    # Kill switch: if ~/.config/agent-power/guard_disabled exists, do nothing.
    if (resolved_config_dir / "guard_disabled").exists():
        return GuardRunResult(
            disabled_by_kill_switch=True,
            auto_stop_enabled=False,
        )

    effective_auto_stop = (
        auto_stop if auto_stop is not None else load_auto_stop_setting(resolved_config_dir)
    )
    allow_patterns = load_allowlist(resolved_config_dir)
    prev_state = load_guard_state(resolved_state_dir).get("processes", {})
    new_processes_state: dict[str, Any] = {}
    actions: list[GuardActionRecord] = []

    iter_procs = process_iter_fn or (lambda: psutil.process_iter())
    my_uid = os.getuid() if current_uid is None else current_uid
    my_pid = os.getpid() if self_pid is None else self_pid
    notifier = notify_fn or (
        lambda title, msg: send_macos_notification(title, msg, dry_run=dry_run)
    )

    for proc in iter_procs():
        try:
            pid = int(proc.pid)
            uids = proc.uids()
            uid = int(getattr(uids, "real", -1)) if uids is not None else None

            # Condition 1: Must belong to current user (and not protected/self/room)
            if uid != my_uid or pid <= 1 or pid == my_pid:
                continue

            # Condition 2: Parent must be launchd (ppid == 1)
            ppid = int(proc.ppid())
            if ppid != 1:
                continue

            name = str(proc.name() or "")
            try:
                cmdline_tuple = tuple(str(x) for x in (proc.cmdline() or ()))
            except (psutil.Error, OSError):
                cmdline_tuple = ()
            try:
                cwd_raw = str(proc.cwd() or "")
            except (psutil.Error, OSError):
                cwd_raw = ""

            if is_protected_process(
                pid=pid,
                uid=uid,
                name=name,
                cmdline=cmdline_tuple,
                cwd=cwd_raw,
                current_uid=my_uid,
                self_pid=my_pid,
            ):
                continue

            # Condition 3: Must be a script interpreter
            if not is_script_interpreter(name, cmdline_tuple):
                continue

            # Condition 4: Must listen on NO network port
            ports = _extract_listening_ports(proc)
            if len(ports) > 0:
                continue

            # Condition 5: Must NOT match any substring in ~/.config/agent-power/allow
            cmdline_str = " ".join(cmdline_tuple) if cmdline_tuple else name
            if is_allowlisted(cmdline_str, allow_patterns):
                continue

            # Condition 6: CPU time delta check (>= 80% of one core for 10 minutes straight)
            cpu_times = proc.cpu_times()
            total_cpu = float(getattr(cpu_times, "user", 0.0)) + float(
                getattr(cpu_times, "system", 0.0)
            )
            try:
                create_time = round(float(proc.create_time()), 2)
            except (psutil.Error, OSError, AttributeError):
                create_time = 0.0

            proc_key = str(pid)
            prev_entry = prev_state.get(proc_key)
            folder = _shorten_home(cwd_raw)
            what = describe_what_it_is(name, cmdline_tuple)

            if (
                not isinstance(prev_entry, dict)
                or prev_entry.get("create_time") != create_time
                or not isinstance(prev_entry.get("last_check_time"), (int, float))
                or not isinstance(prev_entry.get("last_cpu_time"), (int, float))
            ):
                # Baseline observation for this process
                new_processes_state[proc_key] = {
                    "pid": pid,
                    "name": name,
                    "what_it_is": what,
                    "working_folder": folder,
                    "cmdline": cmdline_str,
                    "create_time": create_time,
                    "last_check_time": now_epoch,
                    "last_cpu_time": total_cpu,
                    "high_cpu_since": now_epoch,
                    "consecutive_high_checks": 0,
                    "last_cpu_percent": 0.0,
                }
                continue

            dt = now_epoch - float(prev_entry["last_check_time"])
            dcpu = total_cpu - float(prev_entry["last_cpu_time"])
            if dt <= 0 or dcpu < 0:
                continue

            cpu_pct = (dcpu / dt) * 100.0
            if cpu_pct >= CPU_THRESHOLD_PERCENT:
                consec = int(prev_entry.get("consecutive_high_checks", 0)) + 1
                prev_since = prev_entry.get("high_cpu_since")
                high_since = (
                    float(prev_since)
                    if isinstance(prev_since, (int, float))
                    else float(prev_entry["last_check_time"])
                )
                streak_seconds = now_epoch - high_since
            else:
                # Below 80% breaks the continuous streak
                consec = 0
                high_since = None
                streak_seconds = 0.0

            new_processes_state[proc_key] = {
                "pid": pid,
                "name": name,
                "what_it_is": what,
                "working_folder": folder,
                "cmdline": cmdline_str,
                "create_time": create_time,
                "last_check_time": now_epoch,
                "last_cpu_time": total_cpu,
                "high_cpu_since": high_since,
                "consecutive_high_checks": consec,
                "last_cpu_percent": round(cpu_pct, 1),
            }

            if (
                consec >= REQUIRED_CONSECUTIVE_CHECKS
                and streak_seconds >= (REQUIRED_STREAK_SECONDS - 0.5)
            ):
                mins = int(round(streak_seconds / 60.0))
                notif_title = "Runaway Background Script Detected"
                notif_body = (
                    f"{what} (PID {pid}) in {folder} has used {cpu_pct:.0f}% CPU "
                    f"for {mins} minutes."
                )
                notified = notifier(notif_title, notif_body)

                stopped = False
                killed_sigkill = False
                if effective_auto_stop:
                    stopped, killed_sigkill = _stop_process_sigterm_sigkill(
                        proc,
                        sleep_fn=sleep_fn,
                    )
                    stop_method = "SIGKILL (after 5s SIGTERM)" if killed_sigkill else "SIGTERM"
                    log_msg = (
                        f"AUTO-STOPPED runaway script: {what} (PID {pid}) in {folder} "
                        f"[{cpu_pct:.1f}% CPU for {mins}m] via {stop_method}"
                    )
                else:
                    log_msg = (
                        f"NOTIFY-ONLY runaway script: {what} (PID {pid}) in {folder} "
                        f"[{cpu_pct:.1f}% CPU for {mins}m]"
                    )

                append_guard_log(resolved_log_path, log_msg, now_epoch=now_epoch)
                actions.append(
                    GuardActionRecord(
                        pid=pid,
                        name=name,
                        what_it_is=what,
                        working_folder=folder,
                        cmdline=cmdline_str,
                        cpu_percent=round(cpu_pct, 1),
                        streak_seconds=round(streak_seconds, 1),
                        consecutive_checks=consec,
                        notified=notified,
                        stopped=stopped,
                        killed_with_sigkill=killed_sigkill,
                        message=log_msg,
                    )
                )

        except (psutil.Error, OSError):
            continue

    _atomic_write_json(
        resolved_state_dir / "guard_state.json",
        {
            "updated_at": datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat(),
            "updated_epoch": now_epoch,
            "processes": new_processes_state,
        },
    )

    return GuardRunResult(
        disabled_by_kill_switch=False,
        auto_stop_enabled=effective_auto_stop,
        tracked_pids=sorted(int(k) for k in new_processes_state),
        actions=actions,
    )


def format_guard_status(
    *,
    state_dir: Path | str | None = None,
    config_dir: Path | str | None = None,
    log_path: Path | str | None = None,
) -> str:
    """Format a human-readable summary of the runaway guard status for the CLI."""
    resolved_config_dir = get_config_dir(config_dir)
    resolved_state_dir = get_state_dir(state_dir)
    resolved_log_path = get_log_path(log_path)

    kill_switch = (resolved_config_dir / "guard_disabled").exists()
    auto_stop = load_auto_stop_setting(resolved_config_dir)
    allow_patterns = load_allowlist(resolved_config_dir)
    state = load_guard_state(resolved_state_dir)
    procs = state.get("processes", {})

    lines = [
        f"Runaway Guard: {'DISABLED (kill switch present)' if kill_switch else 'ENABLED'}",
        f"Behavior: {'Auto-stop (SIGTERM -> SIGKILL)' if auto_stop else 'Notify-only (default)'}",
        f"Allowlist patterns ({len(allow_patterns)}): {', '.join(allow_patterns) if allow_patterns else 'none'}",
        f"Currently tracked launchd scripts: {len(procs)}",
    ]
    for info in procs.values():
        if isinstance(info, dict):
            lines.append(
                f"  - PID {info.get('pid')}: {info.get('what_it_is')} in {info.get('working_folder')} "
                f"(last CPU: {info.get('last_cpu_percent', 0.0)}%, "
                f"streak checks: {info.get('consecutive_high_checks', 0)}/{REQUIRED_CONSECUTIVE_CHECKS})"
            )

    if resolved_log_path.is_file():
        try:
            recent_logs = resolved_log_path.read_text(encoding="utf-8").splitlines()[-5:]
            if recent_logs:
                lines.append("Recent guard log entries:")
                for entry in recent_logs:
                    lines.append(f"  {entry}")
        except OSError:
            pass
    return "\n".join(lines)


def main() -> int:
    """Entry point for launchd `python -m agent_power.runaway_guard`."""
    run_guard_check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
