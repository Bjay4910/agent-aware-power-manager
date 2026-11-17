"""Comprehensive tests for policy.py covering every rule and boundary with FakeClock."""

from __future__ import annotations

import pytest

from agent_power.policy import (
    ActionType,
    FakeClock,
    Mode,
    PolicySettings,
    PolicyState,
    decide,
)


def test_default_only_while_plugged_in_is_true() -> None:
    settings = PolicySettings()
    assert settings.only_while_plugged_in is True
    assert settings.min_battery_for_new_hold == 25
    assert settings.critical_battery_sleep == 10
    assert settings.max_hold_seconds == 7200.0
    assert settings.idle_grace_seconds == 20.0


def test_hold_when_agent_working_on_ac() -> None:
    res = decide(
        PolicyState(
            any_agent_working=True,
            active_agents=(("claude", "compare databases"),),
            on_ac_power=True,
            battery_percent=80,
            hold_active=False,
        )
    )
    assert res.kind is ActionType.HOLD
    assert "Kept awake: claude is working on 'compare databases'" in res.reason


def test_Already_holding_while_agent_working_is_noop() -> None:
    res = decide(
        PolicyState(
            any_agent_working=True,
            active_agents=(("claude", "compare databases"), ("codex", "lint"),),
            on_ac_power=True,
            battery_percent=80,
            hold_active=True,
            hold_duration_seconds=300.0,
        )
    )
    assert res.kind is ActionType.NOOP
    assert "+1 other agent" in res.reason


def test_only_while_plugged_in_blocks_hold_on_battery_and_releases_existing() -> None:
    # No new hold on battery when only_while_plugged_in is True (default)
    res_new = decide(
        PolicyState(
            any_agent_working=True,
            on_ac_power=False,
            battery_percent=90,
            hold_active=False,
        )
    )
    assert res_new.kind is ActionType.NOOP

    # Releases active hold if unplugged while only_while_plugged_in is True
    res_existing = decide(
        PolicyState(
            any_agent_working=True,
            on_ac_power=False,
            battery_percent=90,
            hold_active=True,
        )
    )
    assert res_existing.kind is ActionType.RELEASE
    assert "Only while plugged in" in res_existing.reason


def test_battery_25_percent_boundary_for_new_hold() -> None:
    settings = PolicySettings(only_while_plugged_in=False)

    # 24% -> below 25%, no new hold
    below = decide(
        PolicyState(
            any_agent_working=True,
            on_ac_power=False,
            battery_percent=24,
            hold_active=False,
            settings=settings,
        )
    )
    assert below.kind is ActionType.NOOP
    assert "24%" in below.reason

    # 25% -> at boundary, new hold allowed
    at_boundary = decide(
        PolicyState(
            any_agent_working=True,
            on_ac_power=False,
            battery_percent=25,
            hold_active=False,
            settings=settings,
        )
    )
    assert at_boundary.kind is ActionType.HOLD

    # Existing hold at 20% (between 11% and 24%) stays active because rule is "no new hold below 25%"
    existing_hold = decide(
        PolicyState(
            any_agent_working=True,
            on_ac_power=False,
            battery_percent=20,
            hold_active=True,
            settings=settings,
        )
    )
    assert existing_hold.kind is ActionType.NOOP


def test_critical_battery_10_percent_boundary_forces_sleep() -> None:
    settings = PolicySettings(only_while_plugged_in=False)

    # 11% on battery -> does not force sleep
    at_11 = decide(
        PolicyState(
            any_agent_working=True,
            on_ac_power=False,
            battery_percent=11,
            hold_active=True,
            settings=settings,
        )
    )
    assert at_11.kind is ActionType.NOOP

    # 10% on battery -> forces SLEEP_NOW
    at_10 = decide(
        PolicyState(
            any_agent_working=True,
            on_ac_power=False,
            battery_percent=10,
            hold_active=True,
            settings=settings,
        )
    )
    assert at_10.kind is ActionType.SLEEP_NOW
    assert "10%" in at_10.reason

    # 5% on battery -> forces SLEEP_NOW even when mode is Off
    at_5_off = decide(
        PolicyState(
            any_agent_working=False,
            on_ac_power=False,
            battery_percent=5,
            hold_active=False,
            mode=Mode.OFF,
        )
    )
    assert at_5_off.kind is ActionType.SLEEP_NOW

    # 10% on AC power (charging) -> does NOT force sleep
    at_10_ac = decide(
        PolicyState(
            any_agent_working=True,
            on_ac_power=True,
            battery_percent=10,
            hold_active=True,
        )
    )
    assert at_10_ac.kind is ActionType.NOOP


def test_grace_period_20_seconds_with_fake_clock() -> None:
    clock = FakeClock(initial=5000.0)
    hold_started = clock.now
    last_working = clock.now

    # Agent goes idle at t=5000.0; check at +5s, +19.9s, +20.0s
    clock.advance(5.0)
    res_5s = decide(
        PolicyState(
            any_agent_working=False,
            on_ac_power=True,
            battery_percent=100,
            hold_active=True,
            now=clock.now,
            hold_started_at=hold_started,
            last_working_at=last_working,
        )
    )
    assert res_5s.kind is ActionType.NOOP
    assert "15s" in res_5s.reason

    clock.advance(14.9)  # total 19.9s idle
    res_19_9s = decide(
        PolicyState(
            any_agent_working=False,
            on_ac_power=True,
            battery_percent=100,
            hold_active=True,
            now=clock.now,
            hold_started_at=hold_started,
            last_working_at=last_working,
        )
    )
    assert res_19_9s.kind is ActionType.NOOP

    clock.advance(0.1)  # total 20.0s idle -> grace period reached
    res_20s = decide(
        PolicyState(
            any_agent_working=False,
            on_ac_power=True,
            battery_percent=100,
            hold_active=True,
            now=clock.now,
            hold_started_at=hold_started,
            last_working_at=last_working,
        )
    )
    assert res_20s.kind is ActionType.RELEASE
    assert "20s grace period" in res_20s.reason


def test_max_hold_2_hours_boundary_with_fake_clock() -> None:
    clock = FakeClock(initial=10_000.0)
    hold_started = clock.now

    # Advance to 7199.9s (just under 2 hours)
    clock.advance(7199.9)
    res_before = decide(
        PolicyState(
            any_agent_working=True,
            active_agents=(("claude", "long training"),),
            on_ac_power=True,
            battery_percent=100,
            hold_active=True,
            now=clock.now,
            hold_started_at=hold_started,
        )
    )
    assert res_before.kind is ActionType.NOOP

    # Advance to 7200.0s (exact 2-hour limit) -> RELEASE even though agent is still working
    clock.advance(0.1)
    res_at_limit = decide(
        PolicyState(
            any_agent_working=True,
            active_agents=(("claude", "long training"),),
            on_ac_power=True,
            battery_percent=100,
            hold_active=True,
            now=clock.now,
            hold_started_at=hold_started,
        )
    )
    assert res_at_limit.kind is ActionType.RELEASE
    assert "120 minutes" in res_at_limit.reason


def test_modes_hold_2h_and_off() -> None:
    # Mode "Hold 2 hours" holds even when no agents are working
    res_hold_mode = decide(
        PolicyState(
            any_agent_working=False,
            on_ac_power=True,
            battery_percent=85,
            hold_active=False,
            mode="Hold 2 hours",
        )
    )
    assert res_hold_mode.kind is ActionType.HOLD

    # Mode "Off" releases an active hold even if agents are working
    res_off_release = decide(
        PolicyState(
            any_agent_working=True,
            on_ac_power=True,
            battery_percent=85,
            hold_active=True,
            mode=Mode.OFF,
        )
    )
    assert res_off_release.kind is ActionType.RELEASE

    # Mode "Off" does NOOP when no hold is active
    res_off_noop = decide(
        PolicyState(
            any_agent_working=True,
            on_ac_power=True,
            battery_percent=85,
            hold_active=False,
            mode="off",
        )
    )
    assert res_off_noop.kind is ActionType.NOOP


@pytest.mark.parametrize("mode", [Mode.AUTOMATIC, Mode.HOLD_2H, Mode.OFF])
def test_sleep_now_always_wins(mode: Mode) -> None:
    res = decide(
        PolicyState(
            any_agent_working=True,
            active_agents=(("claude", "important job"),),
            on_ac_power=True,
            battery_percent=100,
            hold_active=True,
            mode=mode,
            sleep_now_requested=True,
        )
    )
    assert res.kind is ActionType.SLEEP_NOW
    assert "requested by user" in res.reason
