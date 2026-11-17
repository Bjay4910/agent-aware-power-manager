"""Watchdog process designed to run every 60 seconds via launchd.

If `disablesleep` is active and the engine heartbeat in ~/.agent-power/state.json
is older than 90 seconds (or missing), releases the sleep hold immediately and logs it.
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import time
from typing import Any, Callable

from agent_power.engine import (
    MAX_EVENTS,
    _atomic_write_json,
    get_state_dir,
    load_state_file,
)
from agent_power.power import (
    PowerResult,
    is_sleep_disabled,
    set_disable_sleep,
)

HEARTBEAT_STALE_SECONDS: float = 90.0


@dataclass(frozen=True)
class WatchdogResult:
    """Outcome of a single watchdog check."""

    released: bool
    sleep_was_disabled: bool
    heartbeat_age_seconds: float | None
    reason: str
    power_result: PowerResult | None = None


def _extract_heartbeat_epoch(state: dict[str, Any]) -> float | None:
    epoch = state.get("heartbeat_epoch")
    if isinstance(epoch, (int, float)) and not isinstance(epoch, bool):
        return float(epoch)
    iso_str = state.get("heartbeat_at")
    if isinstance(iso_str, str) and iso_str.strip():
        try:
            dt = datetime.fromisoformat(iso_str.strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _append_watchdog_log(state_dir: Path, message: str, now_epoch: float) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()
    log_path = state_dir / "watchdog.log"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{ts}] {message}\n")


def run_watchdog_check(
    *,
    state_dir: Path | str | None = None,
    now: float | None = None,
    stale_after_seconds: float = HEARTBEAT_STALE_SECONDS,
    dry_run: bool | None = None,
    is_sleep_disabled_fn: Callable[[], bool] = is_sleep_disabled,
    disable_sleep_fn: Callable[..., PowerResult] = set_disable_sleep,
) -> WatchdogResult:
    """Check engine heartbeat and release orphaned sleep holds older than 90 seconds."""
    resolved_dir = get_state_dir(state_dir)
    now_epoch = float(now) if now is not None else time.time()
    state = load_state_file(resolved_dir)

    pmset_disabled = is_sleep_disabled_fn()
    state_hold = bool(state.get("hold_active", False))
    sleep_disabled = pmset_disabled or state_hold

    hb_epoch = _extract_heartbeat_epoch(state)
    age_seconds = (now_epoch - hb_epoch) if hb_epoch is not None else None

    if not sleep_disabled:
        return WatchdogResult(
            released=False,
            sleep_was_disabled=False,
            heartbeat_age_seconds=age_seconds,
            reason="Sleep hold is not active; nothing to release.",
        )

    if age_seconds is not None and age_seconds <= stale_after_seconds:
        return WatchdogResult(
            released=False,
            sleep_was_disabled=True,
            heartbeat_age_seconds=age_seconds,
            reason=f"Engine heartbeat is fresh ({age_seconds:.1f}s old <= {stale_after_seconds:.0f}s).",
        )

    age_desc = f"{age_seconds:.0f}s old" if age_seconds is not None else "missing"
    msg = (
        f"Watchdog released orphaned sleep hold: engine heartbeat was {age_desc} "
        f"(limit {int(stale_after_seconds)}s)"
    )

    power_res = disable_sleep_fn(False, dry_run=dry_run)
    _append_watchdog_log(resolved_dir, msg, now_epoch)

    if state:
        events = [str(e) for e in state.get("events", []) if isinstance(e, str)]
        events.append(msg)
        state["events"] = events[-MAX_EVENTS:]
        state["hold_active"] = False
        state["hold_started_at"] = None
        state["last_action"] = "RELEASE"
        state["last_reason"] = msg
        state["icon_state"] = "problem" if not power_res.ok else "normal"
        if not power_res.ok:
            state["problem"] = power_res.problem
        _atomic_write_json(resolved_dir / "state.json", state)

    return WatchdogResult(
        released=power_res.ok,
        sleep_was_disabled=True,
        heartbeat_age_seconds=age_seconds,
        reason=msg,
        power_result=power_res,
    )


def main() -> int:
    """Entry point for launchd `python -m agent_power.watchdog`."""
    pending_release = {"needed": False}

    def _cleanup() -> None:
        if pending_release["needed"]:
            set_disable_sleep(False)
            pending_release["needed"] = False

    def _on_sigterm(_signum: int, _frame: Any) -> None:
        _cleanup()
        raise SystemExit(0)

    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, _on_sigterm)
    signal.signal(signal.SIGINT, _on_sigterm)

    result = run_watchdog_check()
    return 0 if (not result.sleep_was_disabled or result.released or result.power_result is None) else 1


if __name__ == "__main__":
    raise SystemExit(main())
