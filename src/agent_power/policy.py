"""Pure power-management policy decision function and state models.

All decisions are side-effect-free and deterministic given a PolicyState.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import math
import os
from typing import Sequence

DEFAULT_MAX_HOLD_SECONDS: float = 7200.0
MAX_HOLD_SECONDS_ENV: str = "AGENT_POWER_MAX_HOLD_SECONDS"


def resolve_max_hold_seconds(explicit: float | None = None) -> float:
    """Resolve maximum hold duration from an explicit value or AGENT_POWER_MAX_HOLD_SECONDS.

    Falls back to DEFAULT_MAX_HOLD_SECONDS (7200.0 = 2 hours) when unset or invalid.
    """
    if explicit is not None:
        return float(explicit)
    raw = os.environ.get(MAX_HOLD_SECONDS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_HOLD_SECONDS
    try:
        parsed = float(raw)
    except ValueError:
        return DEFAULT_MAX_HOLD_SECONDS
    if not math.isfinite(parsed) or parsed <= 0.0:
        return DEFAULT_MAX_HOLD_SECONDS
    return parsed


class ActionType(StrEnum):
    """Possible power control actions returned by decide()."""

    HOLD = "HOLD"
    RELEASE = "RELEASE"
    SLEEP_NOW = "SLEEP_NOW"
    NOOP = "NOOP"


class Mode(StrEnum):
    """Operating modes selectable from the menu bar or CLI."""

    AUTOMATIC = "auto"
    HOLD_2H = "hold"
    OFF = "off"

    @classmethod
    def from_str(cls, value: str | Mode) -> Mode:
        """Parse a mode string (CLI slug or menu label) into a Mode enum."""
        if isinstance(value, Mode):
            return value
        normalized = value.strip().lower()
        mapping = {
            "auto": cls.AUTOMATIC,
            "automatic": cls.AUTOMATIC,
            "hold": cls.HOLD_2H,
            "hold 2 hours": cls.HOLD_2H,
            "hold_2h": cls.HOLD_2H,
            "off": cls.OFF,
        }
        if normalized not in mapping:
            raise ValueError(f"Unknown mode: {value!r}")
        return mapping[normalized]

    @property
    def label(self) -> str:
        """Human-friendly menu bar label."""
        if self is Mode.AUTOMATIC:
            return "Automatic"
        if self is Mode.HOLD_2H:
            return "Hold 2 hours"
        return "Off"


@dataclass(frozen=True)
class PolicySettings:
    """Configurable thresholds for the power policy."""

    only_while_plugged_in: bool = True
    min_battery_for_new_hold: int = 25
    critical_battery_sleep: int = 10
    max_hold_seconds: float = DEFAULT_MAX_HOLD_SECONDS  # 2 hours
    idle_grace_seconds: float = 20.0

    @classmethod
    def from_env(cls) -> PolicySettings:
        """Build PolicySettings honoring AGENT_POWER_MAX_HOLD_SECONDS (default 7200s)."""
        return cls(max_hold_seconds=resolve_max_hold_seconds())


@dataclass(frozen=True)
class PolicyState:
    """Snapshot of system and room state passed to decide()."""

    any_agent_working: bool = False
    active_agents: Sequence[tuple[str, str]] = field(default_factory=tuple)
    on_ac_power: bool = True
    battery_percent: int | None = 100
    hold_active: bool = False
    hold_duration_seconds: float = 0.0
    idle_duration_seconds: float | None = None
    mode: Mode | str = Mode.AUTOMATIC
    sleep_now_requested: bool = False
    settings: PolicySettings = field(default_factory=PolicySettings)
    now: float | None = None
    hold_started_at: float | None = None
    last_working_at: float | None = None

    def effective_any_working(self) -> bool:
        """Return True if any_agent_working is True or active_agents is non-empty."""
        return bool(self.any_agent_working or len(self.active_agents) > 0)

    def effective_hold_seconds(self) -> float:
        """Compute how long the current hold has lasted in seconds."""
        if self.now is not None and self.hold_started_at is not None:
            return max(0.0, self.now - self.hold_started_at)
        return max(0.0, self.hold_duration_seconds)

    def effective_idle_seconds(self) -> float | None:
        """Compute how long all agents have been idle in seconds."""
        if self.now is not None and self.last_working_at is not None:
            return max(0.0, self.now - self.last_working_at)
        return self.idle_duration_seconds


@dataclass(frozen=True)
class Action:
    """Decision returned by decide(state)."""

    kind: ActionType
    reason: str

    @property
    def action(self) -> ActionType:
        """Alias for kind."""
        return self.kind


class FakeClock:
    """Deterministic monotonic clock for testing time-based policy rules."""

    def __init__(self, initial: float = 1000.0) -> None:
        self._now = float(initial)

    def __call__(self) -> float:
        return self._now

    @property
    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("Cannot advance FakeClock by negative seconds")
        self._now += float(seconds)
        return self._now


def _format_working_reason(
    active_agents: Sequence[tuple[str, str]],
    *,
    continuing: bool = False,
) -> str:
    prefix = "Keeping Mac awake" if continuing else "Kept awake"
    if not active_agents:
        return f"{prefix}: an AI agent is working."

    first_agent, first_task = active_agents[0]
    task_desc = f" on '{first_task}'" if first_task else ""
    if len(active_agents) == 1:
        return f"{prefix}: {first_agent} is working{task_desc}."

    others = len(active_agents) - 1
    noun = "other agent" if others == 1 else "other agents"
    return f"{prefix}: {first_agent} is working{task_desc} (+{others} {noun})."


def decide(state: PolicyState) -> Action:
    """Pure decision function mapping a PolicyState to an Action and plain-word reason."""
    settings = state.settings
    mode = Mode.from_str(state.mode)
    any_working = state.effective_any_working()
    hold_seconds = state.effective_hold_seconds()
    idle_seconds = state.effective_idle_seconds()

    # 1. User's explicit "Sleep now" always wins.
    if state.sleep_now_requested:
        return Action(
            ActionType.SLEEP_NOW,
            "Going to sleep now as requested by user.",
        )

    # 2. At 10% or less on battery, force sleep immediately.
    if (
        not state.on_ac_power
        and state.battery_percent is not None
        and state.battery_percent <= settings.critical_battery_sleep
    ):
        return Action(
            ActionType.SLEEP_NOW,
            f"Critical battery ({state.battery_percent}% on battery): forcing sleep now.",
        )

    # 3. Mode == Off releases any active hold and otherwise does nothing.
    if mode is Mode.OFF:
        if state.hold_active:
            return Action(
                ActionType.RELEASE,
                "Released sleep hold: mode is set to Off.",
            )
        return Action(
            ActionType.NOOP,
            "Mode is Off; normal macOS sleep is active.",
        )

    # 4. "Only while plugged in" (default ON): on battery, behave like a normal laptop.
    if not state.on_ac_power and settings.only_while_plugged_in:
        if state.hold_active:
            return Action(
                ActionType.RELEASE,
                "Released sleep hold: unplugged from AC power ('Only while plugged in' is enabled).",
            )
        return Action(
            ActionType.NOOP,
            "Running on battery with 'Only while plugged in' enabled; normal sleep is active.",
        )

    # 4b. Unreadable battery while not confirmed on AC power -> fail safe.
    if not state.on_ac_power and state.battery_percent is None:
        if state.hold_active:
            return Action(
                ActionType.RELEASE,
                "Released sleep hold: battery status is unreadable (failing safe).",
            )
        return Action(
            ActionType.NOOP,
            "Blocked new hold: battery status is unreadable (failing safe).",
        )

    # 5. Maximum hold duration (default 2 hours = 7200 seconds).
    if hold_seconds >= settings.max_hold_seconds:
        if settings.max_hold_seconds < 60.0:
            limit_label = f"{settings.max_hold_seconds:g} seconds"
        else:
            max_mins = int(round(settings.max_hold_seconds / 60.0))
            limit_label = f"{max_mins} minutes"
        if state.hold_active:
            return Action(
                ActionType.RELEASE,
                f"Released sleep hold: maximum hold time ({limit_label}) reached.",
            )
        return Action(
            ActionType.NOOP,
            f"Maximum hold time ({limit_label}) reached; not re-acquiring hold.",
        )

    # 6. Manual "Hold 2 hours" mode.
    if mode is Mode.HOLD_2H:
        if not state.hold_active:
            if (
                state.battery_percent is not None
                and state.battery_percent < settings.min_battery_for_new_hold
            ):
                return Action(
                    ActionType.NOOP,
                    f"Blocked new hold: battery at {state.battery_percent}% is below the {settings.min_battery_for_new_hold}% minimum.",
                )
            return Action(
                ActionType.HOLD,
                "Kept awake: manual 2-hour hold mode enabled.",
            )
        return Action(
            ActionType.NOOP,
            "Keeping Mac awake: manual 2-hour hold mode is active.",
        )

    # 7. Automatic mode.
    if any_working:
        if not state.hold_active:
            if (
                state.battery_percent is not None
                and state.battery_percent < settings.min_battery_for_new_hold
            ):
                return Action(
                    ActionType.NOOP,
                    f"Blocked new hold: battery at {state.battery_percent}% is below the {settings.min_battery_for_new_hold}% minimum.",
                )
            return Action(
                ActionType.HOLD,
                _format_working_reason(state.active_agents, continuing=False),
            )
        return Action(
            ActionType.NOOP,
            _format_working_reason(state.active_agents, continuing=True),
        )

    # No agents currently working in Automatic mode.
    if state.hold_active:
        if idle_seconds is not None and idle_seconds < settings.idle_grace_seconds:
            remaining = max(1, int(round(settings.idle_grace_seconds - idle_seconds)))
            return Action(
                ActionType.NOOP,
                f"All agents idle; waiting {remaining}s more before releasing sleep hold.",
            )
        return Action(
            ActionType.RELEASE,
            "Released sleep hold: all agents finished working (20s grace period elapsed).",
        )

    return Action(
        ActionType.NOOP,
        "No agents working; normal macOS sleep is active.",
    )
