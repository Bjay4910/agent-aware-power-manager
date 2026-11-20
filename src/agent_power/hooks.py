"""Lifecycle hook handler, detached heartbeat loop, and safe hook config installer.

Supports:
- Claude Code hooks (~/.claude/settings.json):
  UserPromptSubmit, PreToolUse, PostToolUse, Stop, SessionEnd
- Antigravity hooks (~/.gemini/config/hooks.json):
  PreInvocation, PostToolUse, Stop

Design guarantees:
- Pure Python standard library only.
- One presence file per session (`claude-<session_id>.json`, `agy-<conversation_id>.json`).
- Starts a tiny detached heartbeat process on working events that refreshes
  `updated_at` every 30 seconds and exits immediately when:
  (a) the session presence file is removed (or no longer `"working"`),
  (b) the watched agent process (`agent_pid`) is gone,
  (c) another heartbeat process replaces its `hb_pid`, or
  (d) `max_seconds` (default 7200s = 2 hours) has elapsed.
- Never logs or persists user prompt text (`prompt` / `lastUserInput`).
- Hook execution never blocks or fails the calling agent (always exits 0 and prints `{}`).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Iterator

from agent_power.policy import resolve_max_hold_seconds
from agent_power.presence_reader import STALE_AFTER_SECONDS, get_presence_dir

DEFAULT_HEARTBEAT_INTERVAL_SECONDS: float = 30.0
DEFAULT_HEARTBEAT_MAX_SECONDS: float = 7200.0
HOOK_MARKER: str = "hook-event"

CLAUDE_WORKING_EVENTS: tuple[str, ...] = ("UserPromptSubmit", "PreToolUse", "PostToolUse")
CLAUDE_STOP_EVENTS: tuple[str, ...] = ("Stop", "SessionEnd")
CLAUDE_ALL_EVENTS: tuple[str, ...] = CLAUDE_WORKING_EVENTS + CLAUDE_STOP_EVENTS

AGY_HOOK_GROUP_NAME: str = "agent-power"
AGY_FLAT_EVENTS: tuple[str, ...] = ("PreInvocation", "Stop")
AGY_GROUPED_EVENTS: tuple[str, ...] = ("PostToolUse",)

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SHELL_WRAPPER_NAMES = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "env"})


def _sanitize_session_id(raw_id: str) -> str:
    cleaned = _SAFE_ID_RE.sub("_", raw_id.strip()).strip("._-")
    return cleaned[:96] or "default"


def is_pid_alive(pid: int) -> bool:
    """Return True if a non-zombie process with `pid` exists on the system."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    # Reap if it is our own child process that has already terminated
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        pass

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno != errno.EPERM:
            return False

    # Check if the process is in zombie ('Z') state on macOS/POSIX
    try:
        res = subprocess.run(
            ["/bin/ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=0.3,
            check=False,
        )
        state_str = res.stdout.strip().upper()
        if not state_str or state_str.startswith("Z"):
            return False
    except Exception:
        pass

    return True


def resolve_agent_pid(start_pid: int | None = None) -> int:
    """Resolve the long-lived agent PID above any transient `sh -c` hook wrapper.

    Both Claude Code and Antigravity execute command hooks via `sh -c`. If the
    immediate parent is a transient shell (`sh`, `zsh`, `bash`, etc.), walk one
    level up via `ps -o ppid=,comm=` to get the real agent process PID.
    Falls back to `os.getppid()` (or `os.getpid()`) if inspection fails.
    """
    candidate = start_pid if (isinstance(start_pid, int) and start_pid > 0) else os.getppid()
    if candidate <= 1:
        return os.getpid()

    try:
        for _ in range(3):
            res = subprocess.run(
                ["/bin/ps", "-o", "ppid=,comm=", "-p", str(candidate)],
                capture_output=True,
                text=True,
                timeout=0.3,
                check=False,
            )
            if res.returncode != 0 or not res.stdout.strip():
                break
            parts = res.stdout.strip().split(None, 1)
            if len(parts) < 2:
                break
            ppid = int(parts[0])
            comm_name = Path(parts[1].strip()).name.lower().lstrip("-")
            if comm_name in _SHELL_WRAPPER_NAMES and ppid > 1:
                candidate = ppid
                continue
            break
    except Exception:
        pass

    return candidate if candidate > 0 else os.getpid()


@dataclass(frozen=True)
class _ProcInfo:
    """Lightweight process metadata parsed from a single `/bin/ps` snapshot."""

    pid: int
    ppid: int
    state: str
    start_epoch: float
    args: str

    @property
    def is_zombie(self) -> bool:
        return self.state.upper().startswith("Z")


def _is_hook_or_heartbeat_args(args: str) -> bool:
    """Return True if a command line belongs to our own hook-event or hook-heartbeat."""
    if not args:
        return False
    return (
        ("agent-power" in args or "agent_power.cli" in args)
        and (HOOK_MARKER in args or "hook-heartbeat" in args)
    )


_MONTH_ABBR_TO_NUM: dict[str, int] = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


def _is_valid_pid(val: object) -> bool:
    return not isinstance(val, bool) and isinstance(val, int) and val > 1


def _parse_ps_lstart_utc(tokens: list[str]) -> float | None:
    """Parse `LC_ALL=C TZ=UTC` `ps -o lstart=` 5 tokens (`Dow Mon DD HH:MM:SS YYYY`) to UTC epoch."""
    if len(tokens) != 5:
        return None
    month = _MONTH_ABBR_TO_NUM.get(tokens[1])
    if month is None:
        return None
    try:
        day = int(tokens[2])
        hms = tokens[3].split(":")
        if len(hms) != 3:
            return None
        hour, minute, second = int(hms[0]), int(hms[1]), int(hms[2])
        year = int(tokens[4])
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _snapshot_processes() -> dict[int, _ProcInfo]:
    """Capture a single stdlib `/bin/ps` table of PID, PPID, state, UTC start epoch, and full args."""
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["TZ"] = "UTC"
    try:
        res = subprocess.run(
            ["/bin/ps", "-axwwo", "pid=,ppid=,state=,lstart=,args="],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=0.5,
            check=False,
            env=env,
        )
    except Exception:
        return {}
    if res.returncode != 0 or not res.stdout:
        return {}

    procs: dict[int, _ProcInfo] = {}
    for line in res.stdout.splitlines():
        parts = line.strip().split(None, 8)
        if len(parts) < 8:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            state = parts[2]
            args = parts[8] if len(parts) >= 9 else ""
        except ValueError:
            continue
        start_epoch = _parse_ps_lstart_utc(parts[3:8])
        if start_epoch is None:
            continue
        procs[pid] = _ProcInfo(
            pid=pid,
            ppid=ppid,
            state=state,
            start_epoch=start_epoch,
            args=args,
        )
    return procs


def _collect_self_exclusion_pids(
    procs: dict[int, _ProcInfo],
    agent_pid: int,
    self_pid: int,
) -> set[int]:
    """Return PIDs for the current hook/heartbeat process and its transient shell wrappers below `agent_pid`."""
    excluded: set[int] = set()
    if self_pid <= 0:
        return excluded

    if self_pid == agent_pid:
        # In-process caller (e.g. tests): exclude the `/bin/ps` child spawned by this check
        for info in procs.values():
            if info.ppid == self_pid and info.args.startswith("/bin/ps "):
                excluded.add(info.pid)
        return excluded

    excluded.add(self_pid)
    # Walk ancestors from self_pid up toward agent_pid; if we reach agent_pid,
    # exclude every intermediate wrapper (e.g. `sh -c`) so Stop never counts itself.
    chain: list[int] = []
    cur = self_pid
    for _ in range(10):
        info = procs.get(cur)
        if info is None or info.ppid <= 1 or info.ppid == cur:
            break
        if info.ppid == agent_pid:
            excluded.update(chain)
            break
        chain.append(info.ppid)
        cur = info.ppid
    return excluded


def _sanitize_pid_set(raw: object) -> set[int]:
    if not isinstance(raw, list):
        return set()
    out: set[int] = set()
    for item in raw:
        if not isinstance(item, bool) and isinstance(item, int) and item > 1:
            out.add(item)
    return out


def list_live_descendant_pids(
    agent_pid: int,
    *,
    exclude_pids: Iterable[int] | None = None,
    min_start_epoch: float | None = None,
    procs: dict[int, _ProcInfo] | None = None,
    self_pid: int | None = None,
) -> list[int]:
    """Return sorted live non-zombie descendant PIDs of `agent_pid`, excluding hooks/heartbeats."""
    if not _is_valid_pid(agent_pid):
        return []
    table = procs if procs is not None else _snapshot_processes()
    if not table or agent_pid not in table:
        return []

    own_pid = self_pid if self_pid is not None else os.getpid()
    blocked: set[int] = set()
    if exclude_pids is not None:
        for pid in exclude_pids:
            if _is_valid_pid(pid):
                blocked.add(pid)
    blocked.update(_collect_self_exclusion_pids(table, agent_pid, own_pid))
    for info in table.values():
        if _is_hook_or_heartbeat_args(info.args):
            blocked.add(info.pid)

    children_by_ppid: dict[int, list[int]] = {}
    for info in table.values():
        children_by_ppid.setdefault(info.ppid, []).append(info.pid)

    descendants: list[int] = []
    visited: set[int] = {agent_pid}
    queue: list[int] = [agent_pid]
    while queue:
        cur = queue.pop(0)
        for child_pid in children_by_ppid.get(cur, ()):
            if child_pid in visited or child_pid in blocked:
                continue
            visited.add(child_pid)
            info = table.get(child_pid)
            if info is None:
                continue
            if min_start_epoch is not None and info.start_epoch < min_start_epoch:
                # Skip pre-turn ancestors (e.g. MCP/LSP servers) and their subtrees
                continue
            queue.append(child_pid)
            if not info.is_zombie:
                descendants.append(child_pid)

    return sorted(descendants)


def find_turn_descendants(
    agent_pid: int,
    *,
    turn_started_at: float,
    baseline_pids: Iterable[int] | None = None,
    exclude_pids: Iterable[int] | None = None,
    procs: dict[int, _ProcInfo] | None = None,
    self_pid: int | None = None,
) -> list[int]:
    """Return live non-zombie descendants of `agent_pid` started during the current turn.

    Excludes:
    - Processes already present at turn start (`baseline_pids`),
    - Processes (and their subtrees) started before `floor(turn_started_at)`,
    - The hook process, its transient shell wrapper(s), `/bin/ps`, and heartbeat processes.
    """
    if (
        not _is_valid_pid(agent_pid)
        or not isinstance(turn_started_at, (int, float))
        or isinstance(turn_started_at, bool)
        or turn_started_at <= 0
    ):
        return []

    table = procs if procs is not None else _snapshot_processes()
    if not table or agent_pid not in table:
        return []

    combined_exclude: set[int] = set()
    if baseline_pids is not None:
        for pid in baseline_pids:
            if _is_valid_pid(pid):
                combined_exclude.add(pid)
    if exclude_pids is not None:
        for pid in exclude_pids:
            if _is_valid_pid(pid):
                combined_exclude.add(pid)

    cutoff_epoch = float(math.floor(turn_started_at))
    return list_live_descendant_pids(
        agent_pid,
        exclude_pids=combined_exclude,
        min_start_epoch=cutoff_epoch,
        procs=table,
        self_pid=self_pid,
    )


def _sanitize_pid_starts(raw: object) -> dict[int, float]:
    if not isinstance(raw, dict):
        return {}
    out: dict[int, float] = {}
    for k, v in raw.items():
        try:
            pid = int(k)
        except (TypeError, ValueError):
            continue
        if _is_valid_pid(pid) and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            out[pid] = float(v)
    return out


def _find_remaining_waited_pids(
    agent_pid: int,
    *,
    waiting_for_pids: Iterable[int],
    waiting_for_pid_starts: dict[int, float] | None = None,
    turn_started_at: float,
    baseline_pids: Iterable[int],
    own_pid: int,
    procs: dict[int, _ProcInfo] | None = None,
) -> tuple[list[int], dict[str, float]]:
    """Return (alive_pids, pid_starts_map) for watched PIDs and turn-started descendants."""
    watched = [pid for pid in waiting_for_pids if _is_valid_pid(pid) and pid != own_pid]
    recorded_starts = waiting_for_pid_starts or {}
    table = procs if procs is not None else _snapshot_processes()
    if not table:
        # Transient ps failure: fall back to direct liveness checks on watched PIDs
        still = sorted(pid for pid in watched if is_pid_alive(pid))
        starts_out = {str(p): recorded_starts[p] for p in still if p in recorded_starts}
        return still, starts_out

    cutoff_epoch = float(math.floor(turn_started_at)) if turn_started_at > 0 else 0.0
    alive: set[int] = set(
        find_turn_descendants(
            agent_pid,
            turn_started_at=turn_started_at,
            baseline_pids=baseline_pids,
            exclude_pids={own_pid},
            procs=table,
            self_pid=own_pid,
        )
    )
    for pid in watched:
        info = table.get(pid)
        if info is None or info.is_zombie or _is_hook_or_heartbeat_args(info.args):
            continue
        expected_start = recorded_starts.get(pid)
        if expected_start is not None:
            if abs(info.start_epoch - expected_start) > 1.5:
                # PID was reused by a different process
                continue
        elif cutoff_epoch > 0.0 and info.start_epoch < cutoff_epoch:
            continue
        alive.add(pid)

    sorted_alive = sorted(alive)
    starts_out = {str(p): table[p].start_epoch for p in sorted_alive if p in table}
    return sorted_alive, starts_out


@contextmanager
def _presence_lock(presence_file: Path) -> Iterator[None]:
    """Best-effort advisory flock around presence file read-modify-write / unlink."""
    fh = None
    try:
        presence_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = presence_file.parent / ".hooks.lock"
        fh = open(lock_path, "a+", encoding="utf-8")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
            fh = None
    try:
        yield
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                fh.close()
            except OSError:
                pass


def _atomic_write_presence(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f".tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


def _is_heartbeat_pid_alive(hb_pid: object) -> bool:
    """Return True if `hb_pid` is a live non-zombie `hook-heartbeat` process (guards against PID reuse)."""
    if not _is_valid_pid(hb_pid):
        return False
    assert isinstance(hb_pid, int)
    if not is_pid_alive(hb_pid):
        return False
    try:
        res = subprocess.run(
            ["/bin/ps", "-ww", "-o", "state=,args=", "-p", str(hb_pid)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=0.3,
            check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return False
        parts = res.stdout.strip().split(None, 1)
        if not parts or parts[0].upper().startswith("Z"):
            return False
        args = parts[1] if len(parts) > 1 else ""
        return _is_hook_or_heartbeat_args(args)
    except Exception:
        return False


def _terminate_heartbeat_pid(hb_pid: object) -> None:
    if not _is_valid_pid(hb_pid):
        return
    assert isinstance(hb_pid, int)
    if hb_pid == os.getpid():
        return
    if not _is_heartbeat_pid_alive(hb_pid):
        return
    try:
        os.kill(hb_pid, signal.SIGTERM)
    except OSError:
        pass


def stop_session_presence(presence_file: Path) -> None:
    """Stop any running heartbeat for `presence_file` and unlink the file."""
    existing = _read_json_dict(presence_file)
    if existing is not None:
        _terminate_heartbeat_pid(existing.get("hb_pid"))
    try:
        presence_file.unlink(missing_ok=True)
    except OSError:
        pass


def spawn_heartbeat_daemon(
    presence_file: Path,
    *,
    agent_pid: int,
    interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    max_seconds: float = DEFAULT_HEARTBEAT_MAX_SECONDS,
) -> int | None:
    """Spawn a detached stdlib background process that refreshes `updated_at`."""
    cmd = [
        sys.executable,
        "-m",
        "agent_power.cli",
        "hook-heartbeat",
        "--file",
        str(presence_file),
        "--agent-pid",
        str(agent_pid),
        "--interval",
        str(interval_seconds),
        "--max-seconds",
        str(max_seconds),
    ]
    env = os.environ.copy()
    src_dir = str(Path(__file__).resolve().parent.parent)
    existing_py_path = env.get("PYTHONPATH", "")
    if src_dir not in existing_py_path.split(os.pathsep):
        env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing_py_path}" if existing_py_path else src_dir

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
        return proc.pid
    except Exception:
        return None


def run_heartbeat_loop(
    presence_file: Path,
    *,
    agent_pid: int,
    interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    max_seconds: float = DEFAULT_HEARTBEAT_MAX_SECONDS,
    my_pid: int | None = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
    wall_clock_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Refresh `updated_at` in `presence_file` until stopped, expired, or orphaned.

    Exits cleanly when ANY of the following occurs:
    1. `presence_file` is deleted or unreadable.
    2. `status` in `presence_file` is not `"working"`.
    3. `hb_pid` in `presence_file` is set to a different PID (superseded).
    4. `agent_pid` (> 0) is no longer alive.
    5. `max_seconds` has elapsed (tracked monotonically so backwards wall-clock jumps cannot extend it).
    """
    own_pid = my_pid if my_pid is not None else os.getpid()
    started = monotonic_fn()
    poll_step = min(0.25, max(0.02, interval_seconds / 10.0))

    stop_requested = False

    def _on_term(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    try:
        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGINT, _on_term)
    except ValueError:
        # In case run_heartbeat_loop is called from a non-main thread in tests
        pass

    next_refresh = started + interval_seconds
    child_check_interval = min(1.0, max(0.05, interval_seconds / 2.0))
    next_child_check = started + child_check_interval
    turn_mono_deadline = started + max_seconds
    cached_deadline: float | None = None

    def _sync_deadline(raw_dl: object, now_mono_val: float, now_wall_val: float) -> bool:
        """Update cached wall/monotonic deadlines from `max_deadline_epoch`.

        Returns True only when a fresh turn strictly increased `max_deadline_epoch`.
        Backwards wall-clock jumps never extend `turn_mono_deadline`.
        """
        nonlocal cached_deadline, turn_mono_deadline
        if not (isinstance(raw_dl, (int, float)) and not isinstance(raw_dl, bool) and raw_dl > 0):
            return False
        dl = float(raw_dl)
        if cached_deadline is None:
            cached_deadline = dl
            remaining = max(0.0, min(max_seconds, dl - now_wall_val))
            turn_mono_deadline = min(turn_mono_deadline, now_mono_val + remaining)
            return False
        if dl > cached_deadline + 1e-6:
            cached_deadline = dl
            remaining = max(0.0, min(resolve_max_hold_seconds(), dl - now_wall_val))
            turn_mono_deadline = now_mono_val + remaining
            return True
        if dl < cached_deadline - 1e-6:
            cached_deadline = dl
            remaining = max(0.0, dl - now_wall_val)
            turn_mono_deadline = min(turn_mono_deadline, now_mono_val + remaining)
        return False

    while not stop_requested:
        now_mono = monotonic_fn()
        now_wall = wall_clock_fn()
        cap_exceeded = (now_mono >= turn_mono_deadline) or (
            cached_deadline is not None and now_wall >= cached_deadline
        )
        if cap_exceeded:
            with _presence_lock(presence_file):
                latest = _read_json_dict(presence_file)
                if latest is None or latest.get("status") != "working":
                    return 0
                latest_hb = latest.get("hb_pid")
                if _is_valid_pid(latest_hb) and latest_hb != own_pid:
                    return 0
                extended = _sync_deadline(latest.get("max_deadline_epoch"), now_mono, now_wall)
                if (
                    extended
                    and now_mono < turn_mono_deadline
                    and (cached_deadline is None or now_wall < cached_deadline)
                ):
                    # A fresh turn strictly increased max_deadline_epoch; continue running
                    pass
                else:
                    stop_session_presence(presence_file)
                    return 0

        if not presence_file.is_file():
            return 0

        if agent_pid > 0 and not is_pid_alive(agent_pid):
            with _presence_lock(presence_file):
                latest = _read_json_dict(presence_file)
                if latest is None:
                    return 0
                latest_hb = latest.get("hb_pid")
                if _is_valid_pid(latest_hb) and latest_hb != own_pid:
                    return 0
                latest_agent = latest.get("pid")
                if _is_valid_pid(latest_agent) and latest_agent != agent_pid:
                    return 0
                stop_session_presence(presence_file)
            return 0

        if now_mono >= next_child_check:
            data = _read_json_dict(presence_file)
            if data is None or data.get("status") != "working":
                return 0
            recorded_hb = data.get("hb_pid")
            if _is_valid_pid(recorded_hb) and recorded_hb != own_pid:
                return 0
            _sync_deadline(data.get("max_deadline_epoch"), now_mono, now_wall)

            if data.get("waiting_for_children") is True:
                raw_turn_start = data.get("turn_started_at")
                turn_started_at = (
                    float(raw_turn_start)
                    if (
                        isinstance(raw_turn_start, (int, float))
                        and not isinstance(raw_turn_start, bool)
                        and raw_turn_start > 0
                    )
                    else 0.0
                )
                waiting_pids = sorted(_sanitize_pid_set(data.get("waiting_for_pids")))
                pid_starts = _sanitize_pid_starts(data.get("waiting_for_pid_starts"))
                baseline_pids = _sanitize_pid_set(data.get("turn_baseline_pids"))
                # Snapshot process table OUTSIDE the lock so hooks never stall
                still_alive, still_starts = _find_remaining_waited_pids(
                    agent_pid,
                    waiting_for_pids=waiting_pids,
                    waiting_for_pid_starts=pid_starts,
                    turn_started_at=turn_started_at,
                    baseline_pids=baseline_pids,
                    own_pid=own_pid,
                )
                with _presence_lock(presence_file):
                    latest = _read_json_dict(presence_file)
                    if latest is None or latest.get("status") != "working":
                        return 0
                    latest_hb = latest.get("hb_pid")
                    if _is_valid_pid(latest_hb) and latest_hb != own_pid:
                        return 0
                    # Only act if still in the SAME drain state (no new turn started mid-ps)
                    if (
                        latest.get("waiting_for_children") is True
                        and latest.get("turn_started_at") == raw_turn_start
                    ):
                        if not still_alive:
                            stop_session_presence(presence_file)
                            return 0
                        if still_alive != waiting_pids or still_starts != latest.get("waiting_for_pid_starts"):
                            latest["waiting_for_pids"] = still_alive
                            latest["waiting_for_pid_starts"] = still_starts
                            latest["hb_pid"] = own_pid
                            try:
                                _atomic_write_presence(presence_file, latest)
                            except OSError:
                                return 0
            next_child_check = now_mono + child_check_interval

        if now_mono >= next_refresh:
            with _presence_lock(presence_file):
                data = _read_json_dict(presence_file)
                if data is None or data.get("status") != "working":
                    return 0
                recorded_hb = data.get("hb_pid")
                if _is_valid_pid(recorded_hb) and recorded_hb != own_pid:
                    return 0
                _sync_deadline(data.get("max_deadline_epoch"), now_mono, now_wall)

                data["updated_at"] = datetime.fromtimestamp(wall_clock_fn(), tz=timezone.utc).isoformat()
                data["hb_pid"] = own_pid
                try:
                    _atomic_write_presence(presence_file, data)
                except OSError:
                    return 0
            next_refresh = now_mono + interval_seconds

        next_wake = min(next_refresh, next_child_check)
        sleep_for = min(poll_step, max(0.01, next_wake - monotonic_fn()))
        sleep_fn(sleep_for)

    return 0


@dataclass(frozen=True)
class ParsedHookEvent:
    """Normalized lifecycle event extracted from Claude Code or Antigravity stdin JSON."""

    source: str  # "claude" or "antigravity"
    agent_label: str  # "claude" or "antigravity"
    session_id: str
    event_name: str
    action: str  # "working", "stop", "session_end", or "ignore"
    task: str
    is_turn_start: bool = False

    @property
    def filename(self) -> str:
        prefix = "claude" if self.source == "claude" else "agy"
        return f"{prefix}-{_sanitize_session_id(self.session_id)}.json"


def parse_hook_stdin(raw_stdin: str) -> ParsedHookEvent | None:
    """Parse raw stdin JSON from Claude Code or Antigravity without reading prompt text."""
    if not raw_stdin or not raw_stdin.strip():
        return None
    try:
        payload = json.loads(raw_stdin)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    # Never inspect prompt or lastUserInput fields.
    cwd_raw = payload.get("cwd")
    task_name = "interactive session"
    if isinstance(cwd_raw, str) and cwd_raw.strip():
        folder = Path(cwd_raw.strip()).name
        if folder:
            task_name = f"session in {folder}"

    # 1. Antigravity format (camelCase: conversationId, hookEventName)
    if "conversationId" in payload or "hookEventName" in payload:
        conv_id = payload.get("conversationId")
        event_name = payload.get("hookEventName")
        if not isinstance(conv_id, str) or not conv_id.strip():
            return None
        if not isinstance(event_name, str) or not event_name.strip():
            return None
        ev = event_name.strip()
        if ev in ("PreInvocation", "PostToolUse", "PostInvocation", "PreToolUse"):
            action = "working"
        elif ev == "Stop":
            action = "stop"
        elif ev == "SessionEnd":
            action = "session_end"
        else:
            action = "ignore"
        return ParsedHookEvent(
            source="antigravity",
            agent_label="antigravity",
            session_id=conv_id.strip(),
            event_name=ev,
            action=action,
            task=task_name,
            # Antigravity fires PreInvocation before every LLM step inside a turn;
            # turn boundaries are detected from presence state (absent or draining).
            is_turn_start=False,
        )

    # 2. Claude Code format (snake_case: session_id, hook_event_name)
    if "session_id" in payload or "hook_event_name" in payload:
        sess_id = payload.get("session_id")
        event_name = payload.get("hook_event_name")
        if not isinstance(sess_id, str) or not sess_id.strip():
            return None
        if not isinstance(event_name, str) or not event_name.strip():
            return None
        ev = event_name.strip()
        if ev in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
            action = "working"
        elif ev == "Stop":
            action = "stop"
        elif ev == "SessionEnd":
            action = "session_end"
        else:
            action = "ignore"
        return ParsedHookEvent(
            source="claude",
            agent_label="claude",
            session_id=sess_id.strip(),
            event_name=ev,
            action=action,
            task=task_name,
            is_turn_start=(ev == "UserPromptSubmit"),
        )

    return None


def _extract_alive_hb_pid(existing: dict[str, Any] | None) -> int | None:
    if existing is None:
        return None
    raw_hb = existing.get("hb_pid")
    if _is_heartbeat_pid_alive(raw_hb):
        assert isinstance(raw_hb, int)
        return raw_hb
    return None


def _is_existing_stale_or_expired(existing: dict[str, Any] | None, now_epoch: float) -> bool:
    """Return True if an existing presence record is stale (>120s) or past its max_deadline_epoch."""
    if existing is None:
        return True
    raw_updated = existing.get("updated_at")
    if not isinstance(raw_updated, str) or not raw_updated.strip():
        return True
    try:
        dt = datetime.fromisoformat(raw_updated.strip())
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if (now_epoch - dt.timestamp()) > STALE_AFTER_SECONDS:
        return True
    raw_dl = existing.get("max_deadline_epoch")
    if isinstance(raw_dl, (int, float)) and not isinstance(raw_dl, bool) and raw_dl > 0:
        if now_epoch >= float(raw_dl):
            return True
    return False


def _has_different_agent_pid(existing: dict[str, Any] | None, resolved_pid: int) -> bool:
    if existing is None:
        return False
    raw_pid = existing.get("pid")
    return _is_valid_pid(raw_pid) and raw_pid != resolved_pid


def handle_hook_event(
    raw_stdin: str,
    *,
    room_home: Path | str | None = None,
    agent_pid: int | None = None,
    spawn_heartbeat: bool = True,
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    heartbeat_max_seconds: float | None = None,
) -> dict[str, Any]:
    """Process a single hook event safely. Always returns `{}` for stdout."""
    try:
        effective_max_seconds = resolve_max_hold_seconds(heartbeat_max_seconds)
        parsed = parse_hook_stdin(raw_stdin)
        if parsed is None or parsed.action == "ignore":
            return {}

        presence_dir = get_presence_dir(room_home)
        presence_file = presence_dir / parsed.filename

        if parsed.action == "session_end":
            with _presence_lock(presence_file):
                stop_session_presence(presence_file)
            return {}

        if parsed.action == "stop":
            existing = _read_json_dict(presence_file)
            if existing is None:
                # Never delete a presence file that might be created by a concurrent new turn
                return {}

            existing_hb_pid = _extract_alive_hb_pid(existing)
            raw_existing_pid = existing.get("pid")
            if isinstance(agent_pid, int) and not isinstance(agent_pid, bool) and agent_pid > 0:
                resolved_pid = agent_pid
            elif (
                isinstance(raw_existing_pid, int)
                and not isinstance(raw_existing_pid, bool)
                and raw_existing_pid > 0
            ):
                resolved_pid = raw_existing_pid
            else:
                resolved_pid = resolve_agent_pid()

            raw_turn_start = existing.get("turn_started_at")
            turn_started_at = (
                float(raw_turn_start)
                if (
                    isinstance(raw_turn_start, (int, float))
                    and not isinstance(raw_turn_start, bool)
                    and raw_turn_start > 0
                )
                else 0.0
            )
            baseline_pids = _sanitize_pid_set(existing.get("turn_baseline_pids"))
            carry_pids = _sanitize_pid_set(existing.get("carry_waiting_pids"))
            carry_starts = _sanitize_pid_starts(existing.get("carry_waiting_pid_starts"))
            exclude = {existing_hb_pid} if existing_hb_pid is not None else None

            table = _snapshot_processes()
            live_descendants: list[int] = []
            pid_starts_map: dict[str, float] = {}
            if turn_started_at > 0:
                live_descendants, pid_starts_map = _find_remaining_waited_pids(
                    resolved_pid,
                    waiting_for_pids=carry_pids,
                    waiting_for_pid_starts=carry_starts,
                    turn_started_at=turn_started_at,
                    baseline_pids=baseline_pids | (exclude or set()),
                    own_pid=os.getpid(),
                    procs=table,
                )

            with _presence_lock(presence_file):
                latest = _read_json_dict(presence_file)
                if latest is None:
                    return {}
                # Guard against a new turn starting concurrently while Stop ran `/bin/ps`
                if latest.get("turn_started_at") != raw_turn_start:
                    return {}
                if _is_valid_pid(raw_existing_pid) and latest.get("pid") != raw_existing_pid:
                    return {}
                if not live_descendants:
                    stop_session_presence(presence_file)
                    return {}

                now_epoch = time.time()
                raw_deadline = latest.get("max_deadline_epoch")
                if (
                    isinstance(raw_deadline, (int, float))
                    and not isinstance(raw_deadline, bool)
                    and raw_deadline > 0
                ):
                    max_deadline_epoch = float(raw_deadline)
                else:
                    max_deadline_epoch = turn_started_at + effective_max_seconds

                remaining_max = max(0.0, max_deadline_epoch - now_epoch)
                if remaining_max <= 0.0:
                    stop_session_presence(presence_file)
                    return {}

                latest_hb_pid = _extract_alive_hb_pid(latest)
                record = dict(latest)
                record.pop("carry_waiting_pids", None)
                record.pop("carry_waiting_pid_starts", None)
                record["status"] = "working"
                record["pid"] = resolved_pid
                record["updated_at"] = datetime.now(timezone.utc).isoformat()
                record["max_deadline_epoch"] = max_deadline_epoch
                record["waiting_for_children"] = True
                record["waiting_for_pids"] = live_descendants
                record["waiting_for_pid_starts"] = pid_starts_map
                if latest_hb_pid is not None:
                    record["hb_pid"] = latest_hb_pid
                else:
                    record.pop("hb_pid", None)
                _atomic_write_presence(presence_file, record)

                if spawn_heartbeat and latest_hb_pid is None:
                    new_hb_pid = spawn_heartbeat_daemon(
                        presence_file,
                        agent_pid=resolved_pid,
                        interval_seconds=heartbeat_interval_seconds,
                        max_seconds=remaining_max,
                    )
                    if new_hb_pid is not None:
                        record["hb_pid"] = new_hb_pid
                        _atomic_write_presence(presence_file, record)
            return {}

        # action == "working"
        resolved_pid = (
            agent_pid
            if (isinstance(agent_pid, int) and not isinstance(agent_pid, bool) and agent_pid > 0)
            else resolve_agent_pid()
        )
        now_pre = time.time()
        existing_pre = _read_json_dict(presence_file)
        need_snapshot = (
            parsed.is_turn_start
            or existing_pre is None
            or existing_pre.get("status") != "working"
            or existing_pre.get("waiting_for_children") is True
            or _is_existing_stale_or_expired(existing_pre, now_pre)
            or _has_different_agent_pid(existing_pre, resolved_pid)
            or not (
                isinstance(existing_pre.get("turn_started_at"), (int, float))
                and not isinstance(existing_pre.get("turn_started_at"), bool)
                and existing_pre.get("turn_started_at", 0) > 0
            )
        )
        table_opt: dict[int, _ProcInfo] | None = _snapshot_processes() if need_snapshot else None

        with _presence_lock(presence_file):
            existing = _read_json_dict(presence_file)
            now_epoch = time.time()
            now_iso = datetime.now(timezone.utc).isoformat()

            different_pid = _has_different_agent_pid(existing, resolved_pid)
            stale_or_expired = _is_existing_stale_or_expired(existing, now_epoch)

            existing_hb_pid = _extract_alive_hb_pid(existing)
            if different_pid and existing_hb_pid is not None:
                # Session resumed under a new agent PID: replace heartbeat watching old PID
                _terminate_heartbeat_pid(existing_hb_pid)
                existing_hb_pid = None

            raw_prev_start = existing.get("turn_started_at") if existing is not None else None
            has_prev_start = (
                isinstance(raw_prev_start, (int, float))
                and not isinstance(raw_prev_start, bool)
                and raw_prev_start > 0
            )
            raw_prev_deadline = existing.get("max_deadline_epoch") if existing is not None else None
            has_prev_deadline = (
                isinstance(raw_prev_deadline, (int, float))
                and not isinstance(raw_prev_deadline, bool)
                and raw_prev_deadline > 0
            )

            carry_waiting_pids: list[int] = []
            carry_waiting_starts: dict[str, float] = {}

            is_new_turn = (
                parsed.is_turn_start
                or existing is None
                or not has_prev_start
                or existing.get("status") != "working"
                or existing.get("waiting_for_children") is True
                or stale_or_expired
                or different_pid
            )
            if is_new_turn:
                exclude = {existing_hb_pid} if existing_hb_pid is not None else None
                snapshot_table = table_opt if table_opt is not None else _snapshot_processes()
                baseline_set = set(
                    list_live_descendant_pids(
                        resolved_pid,
                        exclude_pids=exclude,
                        procs=snapshot_table,
                    )
                )
                turn_started_at = now_epoch
                max_deadline_epoch = now_epoch + effective_max_seconds

                # Carry over a previous turn's still-draining child ONLY when the prior record
                # belongs to the same agent PID, is fresh, and its 2-hour cap has not expired.
                if (
                    existing is not None
                    and not stale_or_expired
                    and not different_pid
                    and existing.get("waiting_for_children") is True
                    and has_prev_start
                ):
                    prev_dl = (
                        float(raw_prev_deadline)
                        if has_prev_deadline
                        else float(raw_prev_start) + effective_max_seconds
                    )
                    if prev_dl > now_epoch:
                        prev_waiting = _sanitize_pid_set(existing.get("waiting_for_pids"))
                        prev_starts = _sanitize_pid_starts(existing.get("waiting_for_pid_starts"))
                        prev_baseline = _sanitize_pid_set(existing.get("turn_baseline_pids"))
                        still_prev, still_prev_starts = _find_remaining_waited_pids(
                            resolved_pid,
                            waiting_for_pids=prev_waiting,
                            waiting_for_pid_starts=prev_starts,
                            turn_started_at=float(raw_prev_start),
                            baseline_pids=prev_baseline | (exclude or set()),
                            own_pid=os.getpid(),
                            procs=snapshot_table,
                        )
                        if still_prev:
                            baseline_set -= set(still_prev)
                            turn_started_at = min(float(raw_prev_start), now_epoch)
                            max_deadline_epoch = min(prev_dl, now_epoch + effective_max_seconds)
                            carry_waiting_pids = still_prev
                            carry_waiting_starts = still_prev_starts
                turn_baseline_pids = sorted(baseline_set)
            else:
                turn_started_at = float(raw_prev_start)
                turn_baseline_pids = sorted(_sanitize_pid_set(existing.get("turn_baseline_pids")))
                max_deadline_epoch = (
                    float(raw_prev_deadline)
                    if has_prev_deadline
                    else turn_started_at + effective_max_seconds
                )
                carry_waiting_pids = sorted(_sanitize_pid_set(existing.get("carry_waiting_pids")))
                carry_waiting_starts = {
                    str(k): v for k, v in _sanitize_pid_starts(existing.get("carry_waiting_pid_starts")).items()
                }

            remaining_max = max(0.0, max_deadline_epoch - now_epoch)
            if remaining_max <= 0.0:
                stop_session_presence(presence_file)
                return {}

            record: dict[str, Any] = {
                "agent": parsed.agent_label,
                "status": "working",
                "task": parsed.task,
                "pid": resolved_pid,
                "updated_at": now_iso,
                "turn_started_at": turn_started_at,
                "turn_baseline_pids": turn_baseline_pids,
                "max_deadline_epoch": max_deadline_epoch,
            }
            if carry_waiting_pids:
                record["carry_waiting_pids"] = carry_waiting_pids
                record["carry_waiting_pid_starts"] = carry_waiting_starts
            if existing_hb_pid is not None:
                record["hb_pid"] = existing_hb_pid

            _atomic_write_presence(presence_file, record)

            if spawn_heartbeat and existing_hb_pid is None:
                new_hb_pid = spawn_heartbeat_daemon(
                    presence_file,
                    agent_pid=resolved_pid,
                    interval_seconds=heartbeat_interval_seconds,
                    max_seconds=remaining_max,
                )
                if new_hb_pid is not None:
                    record["hb_pid"] = new_hb_pid
                    _atomic_write_presence(presence_file, record)
    except Exception:
        # Never fail or block the calling agent.
        pass

    return {}


def default_hook_command() -> str:
    """Resolve the absolute `agent-power hook-event` command string."""
    venv_bin = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "agent-power"
    if venv_bin.is_file():
        return f"{venv_bin} {HOOK_MARKER}"
    argv0 = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if argv0 and argv0.name == "agent-power" and argv0.is_file():
        return f"{argv0} {HOOK_MARKER}"
    return f"{sys.executable} -m agent_power.cli {HOOK_MARKER}"


def _is_our_hook_command(cmd: object) -> bool:
    return (
        isinstance(cmd, str)
        and ("agent-power" in cmd or "agent_power.cli" in cmd)
        and HOOK_MARKER in cmd
    )


def build_claude_hooks_snippet(hook_command: str | None = None) -> dict[str, Any]:
    """Build the canonical Claude Code `hooks` block for `~/.claude/settings.json`."""
    cmd = hook_command or default_hook_command()
    hooks_obj: dict[str, Any] = {}
    for event in CLAUDE_ALL_EVENTS:
        hooks_obj[event] = [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": cmd,
                    }
                ],
            }
        ]
    return {"hooks": hooks_obj}


def build_antigravity_hooks_snippet(hook_command: str | None = None) -> dict[str, Any]:
    """Build the canonical Antigravity block for `~/.gemini/config/hooks.json`."""
    cmd = hook_command or default_hook_command()
    handler = {
        "type": "command",
        "command": cmd,
        "timeout": 5,
    }
    return {
        AGY_HOOK_GROUP_NAME: {
            "enabled": True,
            "PreInvocation": [dict(handler)],
            "PostToolUse": [
                {
                    "matcher": "*",
                    "hooks": [dict(handler)],
                }
            ],
            "Stop": [dict(handler)],
        }
    }


def merge_claude_settings(existing: dict[str, Any], hook_command: str | None = None) -> dict[str, Any]:
    """Idempotently merge our hook handlers into a Claude `settings.json` dict."""
    cmd = hook_command or default_hook_command()
    result = json.loads(json.dumps(existing))
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        result["hooks"] = hooks

    our_entry = {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": cmd,
            }
        ],
    }

    for event in CLAUDE_ALL_EVENTS:
        event_list = hooks.get(event)
        if not isinstance(event_list, list):
            event_list = []
            hooks[event] = event_list

        # Check if our hook command is already present in any matcher group for this event
        found = False
        for group in event_list:
            if not isinstance(group, dict):
                continue
            inner_hooks = group.get("hooks")
            if not isinstance(inner_hooks, list):
                continue
            for h in inner_hooks:
                if isinstance(h, dict) and _is_our_hook_command(h.get("command")):
                    h["type"] = "command"
                    h["command"] = cmd
                    found = True
        if not found:
            event_list.append(json.loads(json.dumps(our_entry)))

    return result


def unmerge_claude_settings(existing: dict[str, Any]) -> dict[str, Any]:
    """Remove only `agent-power hook-event` entries from a Claude `settings.json` dict."""
    result = json.loads(json.dumps(existing))
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result

    for event in list(hooks.keys()):
        event_list = hooks.get(event)
        if not isinstance(event_list, list):
            continue
        kept_groups: list[Any] = []
        for group in event_list:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            inner_hooks = group.get("hooks")
            if not isinstance(inner_hooks, list):
                kept_groups.append(group)
                continue
            filtered_inner = [
                h
                for h in inner_hooks
                if not (isinstance(h, dict) and _is_our_hook_command(h.get("command")))
            ]
            if filtered_inner:
                new_group = dict(group)
                new_group["hooks"] = filtered_inner
                kept_groups.append(new_group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]

    if not hooks:
        del result["hooks"]
    return result


def merge_antigravity_hooks(existing: dict[str, Any], hook_command: str | None = None) -> dict[str, Any]:
    """Idempotently merge the `agent-power` group into an Antigravity `hooks.json` dict."""
    result = json.loads(json.dumps(existing))
    snippet = build_antigravity_hooks_snippet(hook_command)
    result[AGY_HOOK_GROUP_NAME] = snippet[AGY_HOOK_GROUP_NAME]
    return result


def unmerge_antigravity_hooks(existing: dict[str, Any]) -> dict[str, Any]:
    """Remove only the `agent-power` group (and any `agent-power hook-event` handlers) from Antigravity `hooks.json`."""
    result = json.loads(json.dumps(existing))
    result.pop(AGY_HOOK_GROUP_NAME, None)
    return result


def _backup_and_write_json(
    path: Path,
    new_data: dict[str, Any],
    *,
    timestamp: str | None = None,
) -> Path | None:
    """Create `<file>.bak-<YYYYMMDD-HHMMSS>` if `path` exists, then atomically write `new_data`."""
    ts = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path: Path | None = None
    if path.is_file():
        backup_path = Path(f"{path}.bak-{ts}")
        shutil.copy2(path, backup_path)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f".tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(new_data, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)
    return backup_path


def _load_existing_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def format_install_hooks_print(
    *,
    claude_settings_path: Path | None = None,
    agy_hooks_path: Path | None = None,
    hook_command: str | None = None,
) -> str:
    """Return the human-readable `--print` output showing the exact JSON snippets."""
    c_path = claude_settings_path or Path("~/.claude/settings.json").expanduser()
    a_path = agy_hooks_path or Path("~/.gemini/config/hooks.json").expanduser()
    cmd = hook_command or default_hook_command()

    claude_json = json.dumps(build_claude_hooks_snippet(cmd), indent=2)
    agy_json = json.dumps(build_antigravity_hooks_snippet(cmd), indent=2)

    return (
        f"=== 1. Claude Code ({c_path}) ===\n"
        f"{claude_json}\n\n"
        f"=== 2. Antigravity ({a_path}) ===\n"
        f"{agy_json}"
    )


def apply_install_hooks(
    *,
    claude_settings_path: Path | None = None,
    agy_hooks_path: Path | None = None,
    hook_command: str | None = None,
    timestamp: str | None = None,
) -> tuple[Path | None, Path | None]:
    """Backup and merge `agent-power` hooks into Claude and Antigravity config files."""
    c_path = claude_settings_path or Path("~/.claude/settings.json").expanduser()
    a_path = agy_hooks_path or Path("~/.gemini/config/hooks.json").expanduser()
    cmd = hook_command or default_hook_command()
    ts = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")

    c_existing = _load_existing_json_file(c_path)
    c_merged = merge_claude_settings(c_existing, cmd)
    c_bak = _backup_and_write_json(c_path, c_merged, timestamp=ts)

    a_existing = _load_existing_json_file(a_path)
    a_merged = merge_antigravity_hooks(a_existing, cmd)
    a_bak = _backup_and_write_json(a_path, a_merged, timestamp=ts)

    return c_bak, a_bak


def apply_uninstall_hooks(
    *,
    claude_settings_path: Path | None = None,
    agy_hooks_path: Path | None = None,
    timestamp: str | None = None,
) -> tuple[Path | None, Path | None]:
    """Backup and remove `agent-power` hooks from Claude and Antigravity config files."""
    c_path = claude_settings_path or Path("~/.claude/settings.json").expanduser()
    a_path = agy_hooks_path or Path("~/.gemini/config/hooks.json").expanduser()
    ts = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")

    c_bak: Path | None = None
    if c_path.is_file():
        c_existing = _load_existing_json_file(c_path)
        c_unmerged = unmerge_claude_settings(c_existing)
        c_bak = _backup_and_write_json(c_path, c_unmerged, timestamp=ts)

    a_bak: Path | None = None
    if a_path.is_file():
        a_existing = _load_existing_json_file(a_path)
        a_unmerged = unmerge_antigravity_hooks(a_existing)
        a_bak = _backup_and_write_json(a_path, a_unmerged, timestamp=ts)

    return c_bak, a_bak
