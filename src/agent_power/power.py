"""Wrappers around macOS pmset for battery reading, sleep hold, and sleepnow.

Honors AGENT_POWER_DRY_RUN=1 by printing commands instead of invoking pmset/sudo.
Never prompts for a password (uses sudo -n with stdin=DEVNULL).
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import subprocess
from typing import Callable, Sequence


PMSET_BIN = "/usr/bin/pmset"
SUDO_BIN = "sudo"

_BATT_PERCENT_RE = re.compile(r"(\d{1,3})%")
_SLEEP_DISABLED_RE = re.compile(r"^\s*(?:SleepDisabled|disablesleep)\s+(\d+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class BatteryStatus:
    """Parsed battery and power-source information from `pmset -g batt`."""

    on_ac_power: bool
    percent: int | None
    charging_state: str
    raw_output: str = ""


@dataclass(frozen=True)
class PowerResult:
    """Outcome of a power control command (`disablesleep` or `sleepnow`)."""

    ok: bool
    command: str
    dry_run: bool
    problem: str | None = None
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def is_dry_run(explicit: bool | None = None) -> bool:
    """Return True if dry-run mode is enabled via argument or AGENT_POWER_DRY_RUN."""
    if explicit is not None:
        return explicit
    val = os.environ.get("AGENT_POWER_DRY_RUN", "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )


def parse_pmset_batt(output: str) -> BatteryStatus:
    """Parse the text output of `pmset -g batt`."""
    text = output or ""
    lower = text.lower()

    match = _BATT_PERCENT_RE.search(text)
    percent: int | None = None
    if match:
        parsed_pct = int(match.group(1))
        percent = max(0, min(100, parsed_pct))

    has_batt_source = "'battery power'" in lower or "drawing from 'battery" in lower
    has_ac_source = (
        "'ac power'" in lower or "drawing from 'ac" in lower or "'ups power'" in lower
    )

    if percent is None:
        # Only treat as AC-only desktop Mac when pmset explicitly reports AC/UPS Power.
        # Empty, unreadable, or Battery Power without percentage must fail safe.
        on_ac = has_ac_source and not has_batt_source
    elif has_batt_source:
        on_ac = False
    elif has_ac_source:
        on_ac = True
    else:
        on_ac = "discharging" not in lower

    charging_state = "unknown"
    for candidate in ("finishing charge", "discharging", "charging", "charged", "no estimate"):
        if candidate in lower:
            charging_state = candidate
            break
    if percent is None and on_ac:
        charging_state = "ac_only"

    return BatteryStatus(
        on_ac_power=on_ac,
        percent=percent,
        charging_state=charging_state,
        raw_output=text.strip(),
    )


def get_battery_status(runner: CommandRunner | None = None) -> BatteryStatus:
    """Read current battery status using `/usr/bin/pmset -g batt`."""
    run = runner or _default_runner
    try:
        proc = run([PMSET_BIN, "-g", "batt"])
    except (OSError, subprocess.SubprocessError) as exc:
        return BatteryStatus(
            on_ac_power=False,
            percent=None,
            charging_state="unknown",
            raw_output=f"pmset error: {exc}",
        )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"pmset exited {proc.returncode}").strip()
        return BatteryStatus(
            on_ac_power=False,
            percent=None,
            charging_state="unknown",
            raw_output=detail,
        )
    return parse_pmset_batt(proc.stdout)


def parse_disablesleep(output: str) -> bool:
    """Parse `pmset -g` output and return True if SleepDisabled/disablesleep is non-zero."""
    match = _SLEEP_DISABLED_RE.search(output or "")
    if not match:
        return False
    return int(match.group(1)) != 0


def is_sleep_disabled(
    *,
    runner: CommandRunner | None = None,
) -> bool:
    """Check whether macOS sleep is currently disabled via `pmset -g`."""
    run = runner or _default_runner
    try:
        proc = run([PMSET_BIN, "-g"])
    except (OSError, subprocess.SubprocessError):
        return False
    return parse_disablesleep(proc.stdout)


def set_disable_sleep(
    disable: bool,
    *,
    dry_run: bool | None = None,
    runner: CommandRunner | None = None,
) -> PowerResult:
    """Run `sudo -n /usr/bin/pmset -a disablesleep 1|0` or print it in dry-run mode."""
    flag = "1" if disable else "0"
    cmd = [SUDO_BIN, "-n", PMSET_BIN, "-a", "disablesleep", flag]
    cmd_str = " ".join(cmd)

    if is_dry_run(dry_run):
        print(f"[DRY-RUN] {cmd_str}", flush=True)
        return PowerResult(
            ok=True,
            command=cmd_str,
            dry_run=True,
            problem=None,
            returncode=0,
            stdout=f"[DRY-RUN] {cmd_str}\n",
        )

    run = runner or _default_runner
    try:
        proc = run(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        return PowerResult(
            ok=False,
            command=cmd_str,
            dry_run=False,
            problem=(
                f"Failed to execute '{cmd_str}': {exc}. "
                "Run scripts/install.sh to configure passwordless sudo for pmset."
            ),
            returncode=1,
            stderr=str(exc),
        )

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        problem_msg = (
            "Permission denied when running 'sudo -n /usr/bin/pmset -a disablesleep'. "
            "Passwordless sudo is not configured for your user. "
            "Run scripts/install.sh to install /etc/sudoers.d/agent-power."
        )
        if detail:
            problem_msg = f"{problem_msg} ({detail})"
        return PowerResult(
            ok=False,
            command=cmd_str,
            dry_run=False,
            problem=problem_msg,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    return PowerResult(
        ok=True,
        command=cmd_str,
        dry_run=False,
        problem=None,
        returncode=0,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def sleep_now(
    *,
    release_first: bool = True,
    dry_run: bool | None = None,
    runner: CommandRunner | None = None,
) -> PowerResult:
    """Release any sleep hold first, then trigger immediate sleep via `/usr/bin/pmset sleepnow`."""
    dry = is_dry_run(dry_run)
    release_result: PowerResult | None = None
    if release_first:
        release_result = set_disable_sleep(False, dry_run=dry, runner=runner)

    cmd = [PMSET_BIN, "sleepnow"]
    cmd_str = " ".join(cmd)

    if dry:
        print(f"[DRY-RUN] {cmd_str}", flush=True)
        combined_cmd = f"{release_result.command} && {cmd_str}" if release_result else cmd_str
        return PowerResult(
            ok=True,
            command=combined_cmd,
            dry_run=True,
            problem=None,
            returncode=0,
            stdout=f"[DRY-RUN] {cmd_str}\n",
        )

    run = runner or _default_runner
    try:
        proc = run(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        return PowerResult(
            ok=False,
            command=cmd_str,
            dry_run=False,
            problem=f"Failed to run '{cmd_str}': {exc}",
            returncode=1,
            stderr=str(exc),
        )

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return PowerResult(
            ok=False,
            command=cmd_str,
            dry_run=False,
            problem=f"pmset sleepnow failed: {detail or proc.returncode}",
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    if release_result is not None and not release_result.ok:
        return PowerResult(
            ok=False,
            command=f"{release_result.command} && {cmd_str}",
            dry_run=False,
            problem=release_result.problem,
            returncode=release_result.returncode,
            stdout=proc.stdout,
            stderr=release_result.stderr,
        )

    return PowerResult(
        ok=True,
        command=f"{release_result.command} && {cmd_str}" if release_result else cmd_str,
        dry_run=False,
        problem=None,
        returncode=0,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
