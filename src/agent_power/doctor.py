"""Read-only diagnostic command (`agent-power doctor`).

Guarantees:
- Strictly read-only: never modifies files, settings, or power state.
- Never runs sudo to change anything and never prompts for a password.
- Uses `sudo -n -l` with a short timeout only to check the two narrow pmset
  commands, never prints sudo's output, and reports only yes / no / cannot-check.
- Never prints hook/settings/presence file contents or secrets.
- Returns exit code 0 if no check is FAIL (1 if any check is FAIL).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import time
from typing import Any, Callable, Literal, Sequence

from agent_power.engine import get_state_dir, load_state_file
from agent_power.hooks import (
    AGY_HOOK_GROUP_NAME,
    CLAUDE_ALL_EVENTS,
    HOOK_MARKER,
    _is_our_hook_command,
)
from agent_power.power import PMSET_BIN, SUDO_BIN, parse_disablesleep
from agent_power.presence_reader import (
    STALE_AFTER_SECONDS,
    _parse_iso_utc,
    get_presence_dir,
    read_presence,
    working_agents,
)
from agent_power.watchdog import _extract_heartbeat_epoch

CheckStatus = Literal["OK", "WARN", "FAIL"]

LAUNCHD_LABELS: tuple[str, ...] = (
    "com.agentpower.menubar",
    "com.agentpower.watchdog",
    "com.agentpower.guard",
)
DEFAULT_SUDOERS_PATH: Path = Path("/etc/sudoers.d/agent-power")
DEFAULT_CLAUDE_SETTINGS_PATH: Path = Path("~/.claude/settings.json")
DEFAULT_AGY_HOOKS_PATH: Path = Path("~/.gemini/config/hooks.json")

SUDO_CHECK_TIMEOUT_SECONDS: float = 2.0
COMMAND_TIMEOUT_SECONDS: float = 3.0
STATE_FRESH_SECONDS: float = 120.0

DoctorRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class DoctorCheckResult:
    """Single diagnostic check outcome."""

    name: str
    status: CheckStatus
    summary: str
    fix: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    """Complete read-only diagnostic report."""

    checks: list[DoctorCheckResult]

    @property
    def ok_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "OK")

    @property
    def warn_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "WARN")

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "FAIL")

    @property
    def exit_code(self) -> int:
        return 1 if self.fail_count > 0 else 0


def _default_runner(
    cmd: Sequence[str],
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _display_path(path: Path) -> str:
    try:
        home = Path.home()
        rel = path.expanduser().resolve().relative_to(home.resolve())
        return f"~/{rel}"
    except Exception:
        return str(path)


def _check_launchd(runner: DoctorRunner) -> tuple[DoctorCheckResult, bool | None]:
    """Check whether menubar, watchdog, and guard LaunchAgents are loaded/running."""
    try:
        proc = runner(["/bin/launchctl", "list"], COMMAND_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return (
            DoctorCheckResult(
                name="launchd",
                status="WARN",
                summary="Could not query launchctl for LaunchAgents",
                fix="Run 'launchctl list | grep agentpower' in your terminal to verify LaunchAgents.",
            ),
            None,
        )

    if proc.returncode != 0:
        return (
            DoctorCheckResult(
                name="launchd",
                status="WARN",
                summary="launchctl list returned a non-zero exit status",
                fix="Run 'launchctl list | grep agentpower' in your terminal to verify LaunchAgents.",
            ),
            None,
        )

    jobs: dict[str, tuple[int | None, int | None]] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3 or parts[2] == "Label":
            continue
        pid_raw, status_raw, label = parts
        if label not in LAUNCHD_LABELS:
            continue
        pid_val = int(pid_raw) if pid_raw.isdigit() else None
        try:
            exit_val: int | None = int(status_raw)
        except ValueError:
            exit_val = None
        jobs[label] = (pid_val, exit_val)

    missing: list[str] = []
    menubar_running = False
    warnings: list[str] = []

    for label in LAUNCHD_LABELS:
        if label not in jobs:
            missing.append(label)
            continue
        pid_val, exit_val = jobs[label]
        if label == "com.agentpower.menubar":
            if pid_val is not None and pid_val > 0:
                menubar_running = True
            else:
                missing.append(f"{label} (loaded but not running)")
        else:
            if exit_val is not None and exit_val != 0:
                warnings.append(f"{label} last exit {exit_val}")

    if missing:
        return (
            DoctorCheckResult(
                name="launchd",
                status="FAIL",
                summary=f"LaunchAgents not running/loaded: {', '.join(missing)}",
                fix="Run ./scripts/install.sh or restart the menu bar app with 'launchctl kickstart -k gui/$(id -u)/com.agentpower.menubar'.",
            ),
            menubar_running,
        )

    if warnings:
        return (
            DoctorCheckResult(
                name="launchd",
                status="WARN",
                summary=f"All 3 LaunchAgents loaded, with warnings: {', '.join(warnings)}",
                fix="Check logs in ~/.local/state/agent-power/ to see why a periodic agent exited non-zero.",
            ),
            menubar_running,
        )

    return (
        DoctorCheckResult(
            name="launchd",
            status="OK",
            summary="All 3 LaunchAgents (menubar, watchdog, guard) are loaded and healthy",
        ),
        menubar_running,
    )


def _check_sudoers(
    runner: DoctorRunner,
    sudoers_path: Path,
) -> DoctorCheckResult:
    """Check the narrow pmset sudoers rule without prompting or printing sudo output."""
    cmds = (
        [SUDO_BIN, "-n", "-l", PMSET_BIN, "-a", "disablesleep", "0"],
        [SUDO_BIN, "-n", "-l", PMSET_BIN, "-a", "disablesleep", "1"],
    )

    all_ok = True
    explicitly_denied = False
    cannot_check = False

    for cmd in cmds:
        try:
            proc = runner(cmd, SUDO_CHECK_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError):
            all_ok = False
            cannot_check = True
            break

        if proc.returncode == 0:
            continue

        all_ok = False
        combined_lower = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
        if "not allowed" in combined_lower:
            explicitly_denied = True
        else:
            cannot_check = True

    if all_ok:
        return DoctorCheckResult(
            name="sudoers",
            status="OK",
            summary="Narrow pmset sudoers rule: yes",
        )

    file_missing = False
    try:
        file_missing = not sudoers_path.exists()
    except OSError:
        file_missing = False

    if explicitly_denied or file_missing:
        return DoctorCheckResult(
            name="sudoers",
            status="FAIL",
            summary="Narrow pmset sudoers rule: no",
            fix="Run ./scripts/install.sh to install /etc/sudoers.d/agent-power.",
        )

    if cannot_check:
        return DoctorCheckResult(
            name="sudoers",
            status="WARN",
            summary="Narrow pmset sudoers rule: cannot check without sudo",
            fix="Run 'sudo -l' in your terminal or ./scripts/install.sh to verify /etc/sudoers.d/agent-power.",
        )

    return DoctorCheckResult(
        name="sudoers",
        status="FAIL",
        summary="Narrow pmset sudoers rule: no",
        fix="Run ./scripts/install.sh to install /etc/sudoers.d/agent-power.",
    )


def _check_command_executable(cmd: str) -> tuple[bool, str]:
    """Verify the executable referenced by a hook command exists and is executable."""
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()
    if not parts:
        return False, "empty hook command"

    exe = parts[0]
    if "/" in exe or exe.startswith("~"):
        target = Path(exe).expanduser()
        if not target.is_file():
            return False, "hook command target does not exist"
        if not os.access(target, os.X_OK):
            return False, "hook command target is not executable"
        return True, "ok"

    resolved = shutil.which(exe)
    if not resolved:
        return False, "hook command target does not exist on PATH"
    if not os.access(resolved, os.X_OK):
        return False, "hook command target is not executable"
    return True, "ok"


def _is_hook_event_command(cmd: object) -> bool:
    """Return True if a hook command invokes agent-power's hook-event handler."""
    return _is_our_hook_command(cmd) or (isinstance(cmd, str) and HOOK_MARKER in cmd)


def _check_permissions_600(path: Path) -> tuple[bool, str]:
    """Return (is_looser_than_600, octal_mode_str)."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False, "unknown"
    return bool(mode & 0o177), f"{mode:04o}"


def _check_claude_hooks(path: Path) -> DoctorCheckResult:
    """Read-only check of Claude Code hooks in ~/.claude/settings.json."""
    disp = _display_path(path)
    if not path.is_file():
        return DoctorCheckResult(
            name="claude-hooks",
            status="FAIL",
            summary=f"Claude Code settings file is missing ({disp})",
            fix="Run 'agent-power install-hooks --apply' to configure Claude Code hooks.",
        )

    loose_perms, octal_mode = _check_permissions_600(path)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return DoctorCheckResult(
            name="claude-hooks",
            status="FAIL",
            summary=f"Malformed or unreadable JSON in {disp}",
            fix=f"Repair the JSON syntax in {disp}, then run 'agent-power install-hooks --apply'.",
        )

    if not isinstance(data, dict):
        return DoctorCheckResult(
            name="claude-hooks",
            status="FAIL",
            summary=f"Expected a JSON object in {disp}",
            fix=f"Repair {disp} so its top-level value is a JSON object, then run 'agent-power install-hooks --apply'.",
        )

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return DoctorCheckResult(
            name="claude-hooks",
            status="FAIL",
            summary=f"Claude Code hooks are missing in {disp}",
            fix="Run 'agent-power install-hooks --apply' to install Claude Code hooks.",
        )

    missing_events: list[str] = []
    matched_commands: list[str] = []
    for event in CLAUDE_ALL_EVENTS:
        event_list = hooks.get(event)
        found_event = False
        if isinstance(event_list, list):
            for group in event_list:
                if not isinstance(group, dict):
                    continue
                inner = group.get("hooks")
                if not isinstance(inner, list):
                    continue
                for handler in inner:
                    if isinstance(handler, dict) and _is_hook_event_command(handler.get("command")):
                        found_event = True
                        matched_commands.append(str(handler["command"]))
        if not found_event:
            missing_events.append(event)

    if missing_events:
        perm_note = f"; permissions are {octal_mode} (looser than 0600)" if loose_perms else ""
        return DoctorCheckResult(
            name="claude-hooks",
            status="FAIL",
            summary=f"Missing Claude Code hooks for: {', '.join(missing_events)}{perm_note}",
            fix="Run 'agent-power install-hooks --apply' to install all Claude Code hooks.",
        )

    bad_cmd_reasons: list[str] = []
    for cmd in dict.fromkeys(matched_commands):
        ok, reason = _check_command_executable(cmd)
        if not ok and reason not in bad_cmd_reasons:
            bad_cmd_reasons.append(reason)

    if bad_cmd_reasons:
        return DoctorCheckResult(
            name="claude-hooks",
            status="FAIL",
            summary=f"Claude Code hook command invalid ({'; '.join(bad_cmd_reasons)})",
            fix="Re-run 'agent-power install-hooks --apply' from your active .venv so hooks point to an executable binary.",
        )

    if loose_perms:
        return DoctorCheckResult(
            name="claude-hooks",
            status="WARN",
            summary=f"Claude Code hooks present and executable, but {disp} permissions are {octal_mode} (looser than 0600)",
            fix=f"Run 'chmod 600 {disp}' to tighten file permissions.",
        )

    return DoctorCheckResult(
        name="claude-hooks",
        status="OK",
        summary=f"Claude Code hooks are installed, executable, and {disp} permissions are {octal_mode}",
    )


def _check_antigravity_hooks(path: Path) -> DoctorCheckResult:
    """Read-only check of Antigravity hooks in ~/.gemini/config/hooks.json."""
    disp = _display_path(path)
    if not path.is_file():
        return DoctorCheckResult(
            name="antigravity-hooks",
            status="FAIL",
            summary=f"Antigravity hooks file is missing ({disp})",
            fix="Run 'agent-power install-hooks --apply' to configure Antigravity hooks.",
        )

    loose_perms, octal_mode = _check_permissions_600(path)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return DoctorCheckResult(
            name="antigravity-hooks",
            status="FAIL",
            summary=f"Malformed or unreadable JSON in {disp}",
            fix=f"Repair the JSON syntax in {disp}, then run 'agent-power install-hooks --apply'.",
        )

    if not isinstance(data, dict):
        return DoctorCheckResult(
            name="antigravity-hooks",
            status="FAIL",
            summary=f"Expected a JSON object in {disp}",
            fix=f"Repair {disp} so its top-level value is a JSON object, then run 'agent-power install-hooks --apply'.",
        )

    group = data.get(AGY_HOOK_GROUP_NAME)
    if not isinstance(group, dict) or group.get("enabled") is False:
        return DoctorCheckResult(
            name="antigravity-hooks",
            status="FAIL",
            summary=f"Antigravity '{AGY_HOOK_GROUP_NAME}' hook group is missing or disabled in {disp}",
            fix="Run 'agent-power install-hooks --apply' to install Antigravity hooks.",
        )

    missing_events: list[str] = []
    matched_commands: list[str] = []

    for flat_event in ("PreInvocation", "Stop"):
        handlers = group.get(flat_event)
        found = False
        if isinstance(handlers, list):
            for handler in handlers:
                if isinstance(handler, dict) and _is_hook_event_command(handler.get("command")):
                    found = True
                    matched_commands.append(str(handler["command"]))
        if not found:
            missing_events.append(flat_event)

    post_groups = group.get("PostToolUse")
    found_post = False
    if isinstance(post_groups, list):
        for entry in post_groups:
            if not isinstance(entry, dict):
                continue
            if _is_hook_event_command(entry.get("command")):
                found_post = True
                matched_commands.append(str(entry["command"]))
            inner = entry.get("hooks")
            if isinstance(inner, list):
                for handler in inner:
                    if isinstance(handler, dict) and _is_hook_event_command(handler.get("command")):
                        found_post = True
                        matched_commands.append(str(handler["command"]))
    if not found_post:
        missing_events.append("PostToolUse")

    if missing_events:
        perm_note = f"; permissions are {octal_mode} (looser than 0600)" if loose_perms else ""
        return DoctorCheckResult(
            name="antigravity-hooks",
            status="FAIL",
            summary=f"Missing Antigravity hooks for: {', '.join(missing_events)}{perm_note}",
            fix="Run 'agent-power install-hooks --apply' to install all Antigravity hooks.",
        )

    bad_cmd_reasons: list[str] = []
    for cmd in dict.fromkeys(matched_commands):
        ok, reason = _check_command_executable(cmd)
        if not ok and reason not in bad_cmd_reasons:
            bad_cmd_reasons.append(reason)

    if bad_cmd_reasons:
        return DoctorCheckResult(
            name="antigravity-hooks",
            status="FAIL",
            summary=f"Antigravity hook command invalid ({'; '.join(bad_cmd_reasons)})",
            fix="Re-run 'agent-power install-hooks --apply' from your active .venv so hooks point to an executable binary.",
        )

    if loose_perms:
        return DoctorCheckResult(
            name="antigravity-hooks",
            status="WARN",
            summary=f"Antigravity hooks present and executable, but {disp} permissions are {octal_mode} (looser than 0600)",
            fix=f"Run 'chmod 600 {disp}' to tighten file permissions.",
        )

    return DoctorCheckResult(
        name="antigravity-hooks",
        status="OK",
        summary=f"Antigravity hooks are installed, executable, and {disp} permissions are {octal_mode}",
    )


def _check_presence(
    room_home: Path | str | None,
    now_dt: datetime,
) -> tuple[DoctorCheckResult, int]:
    """Check presence directory existence and report stale or corrupt presence files."""
    presence_dir = get_presence_dir(room_home)
    disp = _display_path(presence_dir)

    if not presence_dir.is_dir():
        return (
            DoctorCheckResult(
                name="presence",
                status="WARN",
                summary=f"Presence directory does not exist ({disp})",
                fix=f"Create {disp} or start a Claude Code / Antigravity session.",
            ),
            0,
        )

    try:
        paths = sorted(presence_dir.glob("*.json"))
    except OSError:
        return (
            DoctorCheckResult(
                name="presence",
                status="WARN",
                summary=f"Could not list presence directory ({disp})",
                fix=f"Check permissions on {disp}.",
            ),
            0,
        )

    stale_names: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            stale_names.append(path.name)
            continue
        if not isinstance(raw, dict):
            stale_names.append(path.name)
            continue
        updated_at = _parse_iso_utc(raw.get("updated_at"))
        if updated_at is None or (now_dt - updated_at).total_seconds() > STALE_AFTER_SECONDS:
            stale_names.append(path.name)

    working_count = len(working_agents(read_presence(room_home, now=now_dt)))

    if stale_names:
        shown = ", ".join(stale_names[:5])
        extra = f" (+{len(stale_names) - 5} more)" if len(stale_names) > 5 else ""
        return (
            DoctorCheckResult(
                name="presence",
                status="WARN",
                summary=f"Found {len(stale_names)} stale presence file(s): {shown}{extra}",
                fix=f"Remove stale JSON file(s) from {disp} if those sessions have ended.",
            ),
            working_count,
        )

    return (
        DoctorCheckResult(
            name="presence",
            status="OK",
            summary=f"Presence directory exists ({disp}); {working_count} fresh working agent(s), 0 stale files",
        ),
        working_count,
    )


def _extract_heartbeat_file_arg(args: str) -> str | None:
    try:
        tokens = shlex.split(args)
    except ValueError:
        tokens = args.split()
    for idx, tok in enumerate(tokens):
        if tok == "--file" and idx + 1 < len(tokens):
            return tokens[idx + 1]
        if tok.startswith("--file="):
            return tok.split("=", 1)[1]
    return None


def _check_orphaned_heartbeats(runner: DoctorRunner) -> DoctorCheckResult:
    """Detect running `hook-heartbeat` processes whose session presence file is gone."""
    try:
        proc = runner(["/bin/ps", "-axwwo", "pid=,state=,args="], COMMAND_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return DoctorCheckResult(
            name="heartbeats",
            status="WARN",
            summary="Could not inspect running processes for orphaned heartbeats",
            fix="Run 'ps -axww | grep hook-heartbeat' in your terminal to check manually.",
        )

    if proc.returncode != 0:
        return DoctorCheckResult(
            name="heartbeats",
            status="WARN",
            summary="Process table query failed when checking for orphaned heartbeats",
            fix="Run 'ps -axww | grep hook-heartbeat' in your terminal to check manually.",
        )

    orphaned_pids: list[int] = []
    active_hb_count = 0
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid_str, state_str, args = parts
        if state_str.upper().startswith("Z"):
            continue
        if "hook-heartbeat" not in args or (
            "agent-power" not in args and "agent_power.cli" not in args
        ):
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        file_arg = _extract_heartbeat_file_arg(args)
        if not file_arg:
            continue
        if Path(file_arg).expanduser().is_file():
            active_hb_count += 1
        else:
            orphaned_pids.append(pid)

    if orphaned_pids:
        pid_list = ", ".join(str(p) for p in sorted(orphaned_pids))
        kill_args = " ".join(str(p) for p in sorted(orphaned_pids))
        return DoctorCheckResult(
            name="heartbeats",
            status="WARN",
            summary=f"Found {len(orphaned_pids)} orphaned heartbeat process(es) with missing session file: PID {pid_list}",
            fix=f"Stop orphaned heartbeat process(es) with 'kill {kill_args}'.",
        )

    return DoctorCheckResult(
        name="heartbeats",
        status="OK",
        summary=f"No orphaned heartbeat processes ({active_hb_count} active)",
    )


def _check_sleep_hold_consistency(
    runner: DoctorRunner,
    *,
    working_count: int,
    state: dict[str, Any],
    state_fresh: bool,
) -> DoctorCheckResult:
    """Compare pmset `SleepDisabled` state against fresh working presence files."""
    try:
        proc = runner([PMSET_BIN, "-g"], COMMAND_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return DoctorCheckResult(
            name="sleep-hold",
            status="WARN",
            summary="Could not read SleepDisabled state from pmset -g",
            fix="Run 'pmset -g | grep SleepDisabled' in your terminal to check manually.",
        )

    if proc.returncode != 0:
        return DoctorCheckResult(
            name="sleep-hold",
            status="WARN",
            summary="pmset -g returned a non-zero exit status",
            fix="Run 'pmset -g | grep SleepDisabled' in your terminal to check manually.",
        )

    sleep_disabled = parse_disablesleep(proc.stdout or "")
    mode = str(state.get("mode", "")) if state_fresh else ""
    plugged_only = bool(state.get("only_while_plugged_in", True)) if state_fresh else False
    batt = state.get("battery") if state_fresh and isinstance(state.get("battery"), dict) else {}
    on_ac = batt.get("on_ac_power") if isinstance(batt, dict) else None

    if sleep_disabled and working_count > 0:
        return DoctorCheckResult(
            name="sleep-hold",
            status="OK",
            summary=f"SleepDisabled is 1 and {working_count} working agent(s) are active",
        )

    if not sleep_disabled and working_count == 0:
        return DoctorCheckResult(
            name="sleep-hold",
            status="OK",
            summary="SleepDisabled is 0 and no agents are working",
        )

    if sleep_disabled and working_count == 0:
        if state_fresh and mode == "hold":
            return DoctorCheckResult(
                name="sleep-hold",
                status="OK",
                summary="SleepDisabled is 1 (manual 'Hold 2 hours' mode is active)",
            )
        return DoctorCheckResult(
            name="sleep-hold",
            status="FAIL",
            summary="Mismatch: SleepDisabled is 1 but no working presence file exists",
            fix="Run 'agent-power release' to re-enable normal macOS sleep.",
        )

    # sleep_disabled is False and working_count > 0
    if state_fresh and mode == "off":
        return DoctorCheckResult(
            name="sleep-hold",
            status="WARN",
            summary=f"SleepDisabled is 0 while {working_count} working agent(s) exist (mode is Off)",
            fix="Run 'agent-power mode auto' if you want lid-closed awake holds while agents work.",
        )
    if state_fresh and plugged_only and on_ac is False:
        return DoctorCheckResult(
            name="sleep-hold",
            status="WARN",
            summary=f"SleepDisabled is 0 while {working_count} working agent(s) exist (on battery with 'Only while plugged in' enabled)",
            fix="Plug in power or disable 'Only while plugged in' in the menu bar if you want holds on battery.",
        )

    return DoctorCheckResult(
        name="sleep-hold",
        status="FAIL",
        summary=f"Mismatch: {working_count} working presence file(s) exist but SleepDisabled is 0",
        fix="Verify the menu bar app is running in Automatic mode and that the sudoers rule works.",
    )


def _check_menubar_icon(
    *,
    state: dict[str, Any],
    state_fresh: bool,
    menubar_running: bool | None,
) -> DoctorCheckResult:
    """Report menu bar icon visibility from state.json, or unknown if app is not reporting."""
    if menubar_running is False or not state_fresh:
        return DoctorCheckResult(
            name="menubar-icon",
            status="WARN",
            summary="Menu bar icon status unknown (app not reporting)",
            fix="Start or restart the menu bar app: launchctl kickstart -k gui/$(id -u)/com.agentpower.menubar",
        )

    visible = state.get("menubar_icon_visible")
    if visible is True:
        return DoctorCheckResult(
            name="menubar-icon",
            status="OK",
            summary="Menu bar icon is visible",
        )
    if visible is False:
        return DoctorCheckResult(
            name="menubar-icon",
            status="WARN",
            summary="Menu bar icon is hidden by the menu bar or notch",
            fix="Free up menu bar space or choose More Space in System Settings > Displays.",
        )

    return DoctorCheckResult(
        name="menubar-icon",
        status="WARN",
        summary="Menu bar icon status unknown (app not reporting)",
        fix="Wait a few seconds for the menu bar app to check visibility, or restart it.",
    )


def run_doctor(
    *,
    state_dir: Path | str | None = None,
    room_home: Path | str | None = None,
    claude_settings_path: Path | str | None = None,
    agy_hooks_path: Path | str | None = None,
    sudoers_path: Path | str | None = None,
    now: float | None = None,
    runner: DoctorRunner | None = None,
) -> DoctorReport:
    """Run all read-only diagnostic checks and return a structured DoctorReport."""
    run_cmd = runner or _default_runner
    now_epoch = float(now) if now is not None else time.time()
    now_dt = datetime.fromtimestamp(now_epoch, tz=timezone.utc)

    c_path = (
        Path(claude_settings_path).expanduser()
        if claude_settings_path is not None
        else DEFAULT_CLAUDE_SETTINGS_PATH.expanduser()
    )
    a_path = (
        Path(agy_hooks_path).expanduser()
        if agy_hooks_path is not None
        else DEFAULT_AGY_HOOKS_PATH.expanduser()
    )
    s_path = (
        Path(sudoers_path).expanduser()
        if sudoers_path is not None
        else DEFAULT_SUDOERS_PATH
    )

    state = load_state_file(get_state_dir(state_dir))
    hb_epoch = _extract_heartbeat_epoch(state)
    state_fresh = hb_epoch is not None and (now_epoch - hb_epoch) <= STATE_FRESH_SECONDS

    launchd_check, menubar_running = _check_launchd(run_cmd)
    sudoers_check = _check_sudoers(run_cmd, s_path)
    claude_check = _check_claude_hooks(c_path)
    agy_check = _check_antigravity_hooks(a_path)
    presence_check, working_count = _check_presence(room_home, now_dt)
    heartbeat_check = _check_orphaned_heartbeats(run_cmd)
    sleep_check = _check_sleep_hold_consistency(
        run_cmd,
        working_count=working_count,
        state=state,
        state_fresh=state_fresh,
    )
    icon_check = _check_menubar_icon(
        state=state,
        state_fresh=state_fresh,
        menubar_running=menubar_running,
    )

    return DoctorReport(
        checks=[
            launchd_check,
            sudoers_check,
            claude_check,
            agy_check,
            presence_check,
            heartbeat_check,
            sleep_check,
            icon_check,
        ]
    )


def format_doctor_report(report: DoctorReport) -> str:
    """Format a DoctorReport as plain-word lines with OK / WARN / FAIL and fix hints."""
    lines: list[str] = []
    for check in report.checks:
        tag = f"[{check.status}]"
        lines.append(f"{tag:<6} {check.name}: {check.summary}")
        if check.fix:
            lines.append(f"       Fix: {check.fix}")
    lines.append(
        f"Summary: {report.ok_count} OK, {report.warn_count} WARN, {report.fail_count} FAIL"
    )
    return "\n".join(lines)
