"""Core 5-second power management loop, state persistence, and event log.

Reads room presence and battery status, evaluates the pure policy, applies
power actions, writes ~/.agent-power/state.json with a heartbeat timestamp,
and maintains a rolling 50-item plain-word event log.
Always releases any active hold on normal exit or SIGTERM.
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import time
from typing import Any, Callable

from agent_power.policy import (
    Action,
    ActionType,
    Mode,
    PolicySettings,
    PolicyState,
    decide,
)
from agent_power.power import (
    BatteryStatus,
    PowerResult,
    get_battery_status,
    set_disable_sleep,
    sleep_now,
)
from agent_power.presence_reader import AgentPresence, read_presence, working_agents

LOOP_INTERVAL_SECONDS: float = 5.0
MAX_EVENTS: int = 50


def get_state_dir(state_dir: Path | str | None = None) -> Path:
    """Resolve the state directory (~/.agent-power or $AGENT_POWER_HOME)."""
    if state_dir is not None:
        return Path(state_dir).expanduser()
    env_dir = os.environ.get("AGENT_POWER_HOME", "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    return Path("~/.agent-power").expanduser()


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically using a sibling temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f".tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def load_state_file(state_dir: Path | str | None = None) -> dict[str, Any]:
    """Load ~/.agent-power/state.json if present and valid, else return empty dict."""
    path = get_state_dir(state_dir) / "state.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


@dataclass
class EngineTickResult:
    """Snapshot produced by a single step() of PowerEngine."""

    action: Action
    power_result: PowerResult | None
    hold_active: bool
    icon_state: str
    problem: str | None
    battery: BatteryStatus
    active_agents: list[AgentPresence]
    mode: Mode
    only_while_plugged_in: bool
    events: list[str] = field(default_factory=list)


class PowerEngine:
    """Stateful coordinator that runs the 5-second power management loop."""

    def __init__(
        self,
        *,
        state_dir: Path | str | None = None,
        room_home: Path | str | None = None,
        settings: PolicySettings | None = None,
        mode: Mode | str = Mode.AUTOMATIC,
        dry_run: bool | None = None,
        clock: Callable[[], float] = time.time,
        now_utc_fn: Callable[[], datetime] | None = None,
        battery_reader: Callable[[], BatteryStatus] = get_battery_status,
        presence_reader_fn: Callable[..., list[AgentPresence]] = read_presence,
        disable_sleep_fn: Callable[..., PowerResult] = set_disable_sleep,
        sleep_now_fn: Callable[..., PowerResult] = sleep_now,
    ) -> None:
        self.state_dir = get_state_dir(state_dir)
        self.room_home = room_home
        self.settings = settings or PolicySettings()
        self.mode = Mode.from_str(mode)
        self.dry_run = dry_run
        self._clock = clock
        self._now_utc_fn = now_utc_fn
        self._battery_reader = battery_reader
        self._presence_reader = presence_reader_fn
        self._disable_sleep = disable_sleep_fn
        self._sleep_now = sleep_now_fn

        self.hold_active: bool = False
        self.hold_started_at: float | None = None
        self.last_working_at: float | None = None
        self.menubar_icon_visible: bool | None = None
        self.menubar_icon_checked_at: float | None = None
        self.problem: str | None = None
        self.events: list[str] = []
        self._sleep_now_requested: bool = False
        self._running: bool = False
        self._cleaned_up: bool = False

        self._restore_existing_state()

    def _now_utc(self) -> datetime:
        if self._now_utc_fn is not None:
            return self._now_utc_fn()
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc)

    def _restore_existing_state(self) -> None:
        existing = load_state_file(self.state_dir)
        if not existing:
            return
        raw_events = existing.get("events")
        if isinstance(raw_events, list):
            self.events = [str(e) for e in raw_events if isinstance(e, str)][-MAX_EVENTS:]
        raw_mode = existing.get("mode")
        if isinstance(raw_mode, str):
            try:
                self.mode = Mode.from_str(raw_mode)
            except ValueError:
                pass
        plugged_only = existing.get("only_while_plugged_in")
        if isinstance(plugged_only, bool):
            self.settings = PolicySettings(
                only_while_plugged_in=plugged_only,
                min_battery_for_new_hold=self.settings.min_battery_for_new_hold,
                critical_battery_sleep=self.settings.critical_battery_sleep,
                max_hold_seconds=self.settings.max_hold_seconds,
                idle_grace_seconds=self.settings.idle_grace_seconds,
            )
        if isinstance(existing.get("hold_active"), bool):
            self.hold_active = existing["hold_active"]
        if isinstance(existing.get("hold_started_at"), (int, float)):
            self.hold_started_at = float(existing["hold_started_at"])
        if isinstance(existing.get("last_working_at"), (int, float)):
            self.last_working_at = float(existing["last_working_at"])
        if isinstance(existing.get("menubar_icon_visible"), bool):
            self.menubar_icon_visible = existing["menubar_icon_visible"]
        raw_icon_checked = existing.get("menubar_icon_checked_at")
        if isinstance(raw_icon_checked, (int, float)) and not isinstance(raw_icon_checked, bool):
            self.menubar_icon_checked_at = float(raw_icon_checked)

    def set_icon_visibility(
        self,
        visible: bool | None,
        *,
        checked_at: float | None = None,
    ) -> None:
        """Record the menu bar icon visibility state reported by the menu bar app."""
        self.menubar_icon_visible = visible
        self.menubar_icon_checked_at = (
            float(checked_at) if checked_at is not None else self._clock()
        )

    def record_event(self, text: str) -> None:
        """Append a plain-word event to the rolling 50-item log without consecutive duplicates."""
        clean = text.strip().rstrip(".")
        if not clean:
            return
        if self.events and self.events[-1] == clean:
            return
        self.events.append(clean)
        if len(self.events) > MAX_EVENTS:
            self.events = self.events[-MAX_EVENTS:]

    @property
    def icon_state(self) -> str:
        """Compute menu bar icon state: awake, normal, off, or problem."""
        if self.problem:
            return "problem"
        if self.mode is Mode.OFF:
            return "off"
        if self.hold_active:
            return "awake"
        return "normal"

    def set_mode(self, mode: Mode | str) -> EngineTickResult:
        """Switch mode (Automatic, Hold 2 hours, Off) and immediately evaluate policy."""
        new_mode = Mode.from_str(mode)
        if new_mode is not self.mode:
            self.mode = new_mode
            if new_mode is Mode.HOLD_2H and not self.hold_active:
                self.hold_started_at = None
            self.record_event(f"Mode changed to {new_mode.label}")
        return self.step()

    def set_only_while_plugged_in(self, enabled: bool) -> EngineTickResult:
        """Toggle 'Only while plugged in' setting and immediately evaluate policy."""
        self.settings = PolicySettings(
            only_while_plugged_in=bool(enabled),
            min_battery_for_new_hold=self.settings.min_battery_for_new_hold,
            critical_battery_sleep=self.settings.critical_battery_sleep,
            max_hold_seconds=self.settings.max_hold_seconds,
            idle_grace_seconds=self.settings.idle_grace_seconds,
        )
        state_word = "enabled" if enabled else "disabled"
        self.record_event(f"Only while plugged in {state_word}")
        return self.step()

    def request_sleep_now(self) -> EngineTickResult:
        """Trigger user's 'Sleep now' action (releases hold first, then sleeps)."""
        self._sleep_now_requested = True
        return self.step()

    def step(self) -> EngineTickResult:
        """Execute one iteration of reading presence/battery, deciding, applying, and saving state."""
        now_epoch = self._clock()
        now_dt = self._now_utc()

        all_presence = self._presence_reader(self.room_home, now=now_dt)
        active = working_agents(all_presence)
        if active:
            self.last_working_at = now_epoch

        battery = self._battery_reader()
        agent_tuples = tuple((a.agent, a.task) for a in active)

        sleep_req = self._sleep_now_requested
        self._sleep_now_requested = False

        policy_state = PolicyState(
            any_agent_working=len(active) > 0,
            active_agents=agent_tuples,
            on_ac_power=battery.on_ac_power,
            battery_percent=battery.percent,
            hold_active=self.hold_active,
            mode=self.mode,
            sleep_now_requested=sleep_req,
            settings=self.settings,
            now=now_epoch,
            hold_started_at=self.hold_started_at if self.hold_active else None,
            last_working_at=self.last_working_at,
        )

        decision = decide(policy_state)
        power_res: PowerResult | None = None

        if decision.kind is ActionType.HOLD:
            power_res = self._disable_sleep(True, dry_run=self.dry_run)
            if power_res.ok:
                self.hold_active = True
                self.hold_started_at = now_epoch
                self.problem = None
                self.record_event(decision.reason)
            else:
                self.problem = power_res.problem
                self.record_event(f"Problem holding awake: {power_res.problem}")

        elif decision.kind is ActionType.RELEASE:
            power_res = self._disable_sleep(False, dry_run=self.dry_run)
            if power_res.ok:
                self.hold_active = False
                self.hold_started_at = None
                self.problem = None
                self.record_event(decision.reason)
            else:
                self.problem = power_res.problem
                self.record_event(f"Problem releasing hold: {power_res.problem}")

        elif decision.kind is ActionType.SLEEP_NOW:
            power_res = self._sleep_now(release_first=True, dry_run=self.dry_run)
            if power_res.ok:
                self.hold_active = False
                self.hold_started_at = None
                self.problem = None
                self.record_event(decision.reason)
            else:
                self.problem = power_res.problem
                self.record_event(f"Problem sleeping now: {power_res.problem}")

        else:
            # NOOP: if holding and the working agent/task changed, log the new context
            if self.hold_active and active:
                first = active[0]
                task_part = f" on '{first.task}'" if first.task else ""
                self.record_event(f"Kept awake: {first.agent} is working{task_part}")

        self._write_state(
            now_dt=now_dt,
            now_epoch=now_epoch,
            decision=decision,
            battery=battery,
            active=active,
        )

        return EngineTickResult(
            action=decision,
            power_result=power_res,
            hold_active=self.hold_active,
            icon_state=self.icon_state,
            problem=self.problem,
            battery=battery,
            active_agents=active,
            mode=self.mode,
            only_while_plugged_in=self.settings.only_while_plugged_in,
            events=list(self.events),
        )

    def _write_state(
        self,
        *,
        now_dt: datetime,
        now_epoch: float,
        decision: Action,
        battery: BatteryStatus,
        active: list[AgentPresence],
    ) -> None:
        payload: dict[str, Any] = {
            "heartbeat_at": now_dt.isoformat(),
            "heartbeat_epoch": now_epoch,
            "pid": os.getpid(),
            "hold_active": self.hold_active,
            "hold_started_at": self.hold_started_at,
            "last_working_at": self.last_working_at,
            "mode": self.mode.value,
            "mode_label": self.mode.label,
            "only_while_plugged_in": self.settings.only_while_plugged_in,
            "icon_state": self.icon_state,
            "menubar_icon_visible": self.menubar_icon_visible,
            "menubar_icon_checked_at": self.menubar_icon_checked_at,
            "problem": self.problem,
            "last_action": decision.kind.value,
            "last_reason": decision.reason,
            "battery": {
                "on_ac_power": battery.on_ac_power,
                "percent": battery.percent,
                "charging_state": battery.charging_state,
            },
            "working_agents": [
                {
                    "agent": a.agent,
                    "task": a.task,
                    "pid": a.pid,
                    "age_seconds": round(a.age_seconds, 1),
                }
                for a in active
            ],
            "events": list(self.events),
        }
        _atomic_write_json(self.state_dir / "state.json", payload)

    def shutdown(self, reason: str = "Released sleep hold: application exiting") -> None:
        """Release any active hold on normal exit or SIGTERM and persist state."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        self._running = False
        if self.hold_active:
            res = self._disable_sleep(False, dry_run=self.dry_run)
            if res.ok:
                self.hold_active = False
                self.hold_started_at = None
                self.record_event(reason)
            else:
                self.problem = res.problem
                self.record_event(f"Failed to release hold on exit: {res.problem}")
            now_epoch = self._clock()
            now_dt = self._now_utc()
            battery = self._battery_reader()
            self._write_state(
                now_dt=now_dt,
                now_epoch=now_epoch,
                decision=Action(ActionType.RELEASE, reason),
                battery=battery,
                active=[],
            )

    def install_signal_handlers(self) -> None:
        """Register SIGTERM/SIGINT and atexit handlers so holds are released on exit."""
        atexit.register(self.shutdown)

        def _handle_signal(signum: int, _frame: Any) -> None:
            sig_name = signal.Signals(signum).name
            self.shutdown(f"Released sleep hold: received {sig_name}")

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    def run_forever(self, interval_seconds: float = LOOP_INTERVAL_SECONDS) -> None:
        """Run the 5-second loop until stopped or interrupted."""
        self.install_signal_handlers()
        self._running = True
        try:
            while self._running:
                self.step()
                time.sleep(interval_seconds)
        finally:
            self.shutdown()
