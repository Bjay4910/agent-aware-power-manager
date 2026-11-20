"""Tests for notch/hidden menu bar icon detection, anti-false-alarm logic, and state persistence."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_power.engine import PowerEngine, load_state_file
from agent_power.menubar import (
    HIDDEN_ICON_NOTIFICATION_BODY,
    HIDDEN_ICON_NOTIFICATION_TITLE,
    IconVisibilityMonitor,
    read_status_item_visible,
)
from agent_power.policy import FakeClock
from agent_power.power import BatteryStatus


def _fake_status_item(occlusion_state: int | None, *, has_button: bool = True, has_window: bool = True) -> object:
    if not has_button:
        return SimpleNamespace(button=lambda: None)
    if not has_window:
        return SimpleNamespace(button=lambda: SimpleNamespace(window=lambda: None))
    win = SimpleNamespace(occlusionState=lambda: occlusion_state)
    return SimpleNamespace(button=lambda: SimpleNamespace(window=lambda: win))


def test_read_status_item_visible_bitmask_and_guards() -> None:
    # NSWindowOcclusionStateVisible is 1 << 1 == 2
    assert read_status_item_visible(_fake_status_item(2)) is True
    assert read_status_item_visible(_fake_status_item(3)) is True
    assert read_status_item_visible(_fake_status_item(0)) is False
    assert read_status_item_visible(_fake_status_item(1)) is False
    assert read_status_item_visible(None) is None
    assert read_status_item_visible(_fake_status_item(2, has_button=False)) is None
    assert read_status_item_visible(_fake_status_item(2, has_window=False)) is None

    broken = SimpleNamespace(button=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert read_status_item_visible(broken) is None


def test_hidden_icon_requires_three_checks_spanning_at_least_30_seconds() -> None:
    clock = FakeClock(initial=1_700_000_000.0)
    notifications: list[tuple[str, str]] = []
    visible_flag = {"v": False}

    mon = IconVisibilityMonitor(
        read_visible_fn=lambda: visible_flag["v"],
        is_screen_locked_fn=lambda: False,
        is_system_asleep_fn=lambda: False,
        is_fullscreen_fn=lambda: False,
        notify_fn=lambda title, body: notifications.append((title, body)) or True,
        clock=clock,
    )

    # 3 checks spanning only 10s (t=0, 5, 10) -> NOT confirmed yet
    r1 = mon.check()
    assert r1.confirmed_hidden is False
    assert r1.notified is False
    assert r1.visible is None

    clock.advance(5.0)
    r2 = mon.check()
    assert r2.confirmed_hidden is False
    assert r2.notified is False

    clock.advance(5.0)
    r3 = mon.check()
    assert r3.confirmed_hidden is False
    assert r3.notified is False
    assert notifications == []

    # At t=25s (still < 30s) -> still not confirmed
    clock.advance(15.0)
    r4 = mon.check()
    assert r4.confirmed_hidden is False
    assert r4.notified is False
    assert notifications == []

    # At t=30s (>= 3 consecutive checks AND >= 30s span) -> confirmed + notified ONCE
    clock.advance(5.0)
    r5 = mon.check()
    assert r5.confirmed_hidden is True
    assert r5.visible is False
    assert r5.notified is True
    assert len(notifications) == 1
    assert notifications[0] == (HIDDEN_ICON_NOTIFICATION_TITLE, HIDDEN_ICON_NOTIFICATION_BODY)

    # Subsequent hidden checks must NOT spam notifications
    for _ in range(5):
        clock.advance(10.0)
        rx = mon.check()
        assert rx.confirmed_hidden is True
        assert rx.visible is False
        assert rx.notified is False
    assert len(notifications) == 1


def test_hidden_icon_two_checks_over_60s_still_requires_third_check() -> None:
    clock = FakeClock(initial=1_700_000_000.0)
    notifications: list[tuple[str, str]] = []

    mon = IconVisibilityMonitor(
        read_visible_fn=lambda: False,
        is_screen_locked_fn=lambda: False,
        is_system_asleep_fn=lambda: False,
        is_fullscreen_fn=lambda: False,
        notify_fn=lambda title, body: notifications.append((title, body)) or True,
        clock=clock,
    )

    assert mon.check().confirmed_hidden is False
    clock.advance(60.0)
    assert mon.check().confirmed_hidden is False
    assert notifications == []

    clock.advance(5.0)
    r3 = mon.check()
    assert r3.confirmed_hidden is True
    assert r3.notified is True
    assert len(notifications) == 1


def test_hidden_icon_latch_rearms_only_after_seen_visible_again() -> None:
    clock = FakeClock(initial=1_700_000_000.0)
    notifications: list[tuple[str, str]] = []
    state = {"visible": False}

    mon = IconVisibilityMonitor(
        read_visible_fn=lambda: state["visible"],
        is_screen_locked_fn=lambda: False,
        is_system_asleep_fn=lambda: False,
        is_fullscreen_fn=lambda: False,
        notify_fn=lambda title, body: notifications.append((title, body)) or True,
        clock=clock,
    )

    # First hidden episode: t=0, t=15, t=30 -> notifies once
    mon.check()
    clock.advance(15.0)
    mon.check()
    clock.advance(15.0)
    assert mon.check().notified is True
    assert len(notifications) == 1

    # Stays hidden -> no second notification
    clock.advance(30.0)
    assert mon.check().notified is False
    assert len(notifications) == 1

    # Seen visible again -> latch re-arms
    state["visible"] = True
    clock.advance(5.0)
    rv = mon.check()
    assert rv.visible is True
    assert rv.confirmed_hidden is False
    assert rv.notified is False

    # Second hidden episode -> notifies once more after 3 checks / >=30s
    state["visible"] = False
    clock.advance(5.0)
    assert mon.check().notified is False
    clock.advance(15.0)
    assert mon.check().notified is False
    clock.advance(15.0)
    r_again = mon.check()
    assert r_again.confirmed_hidden is True
    assert r_again.visible is False
    assert r_again.notified is True
    assert len(notifications) == 2


def test_hidden_icon_skips_when_locked_asleep_or_fullscreen() -> None:
    clock = FakeClock(initial=1_700_000_000.0)
    notifications: list[tuple[str, str]] = []
    env = {"locked": False, "asleep": False, "fullscreen": False, "visible": True}

    mon = IconVisibilityMonitor(
        read_visible_fn=lambda: env["visible"],
        is_screen_locked_fn=lambda: env["locked"],
        is_system_asleep_fn=lambda: env["asleep"],
        is_fullscreen_fn=lambda: env["fullscreen"],
        notify_fn=lambda title, body: notifications.append((title, body)) or True,
        clock=clock,
    )

    # Start with icon visible
    assert mon.check().visible is True

    # 1. Screen locked: even if occlusion reports False for 60s across 5 checks, skip!
    env["visible"] = False
    env["locked"] = True
    for _ in range(5):
        clock.advance(15.0)
        res = mon.check()
        assert res.skipped_reason == "screen_locked"
        assert res.notified is False
        assert res.visible is True  # keeps last confirmed visible state
    assert notifications == []

    # 2. System asleep: skip!
    env["locked"] = False
    env["asleep"] = True
    for _ in range(5):
        clock.advance(15.0)
        res = mon.check()
        assert res.skipped_reason == "system_asleep"
        assert res.notified is False
        assert res.visible is True
    assert notifications == []

    # 3. Full-screen / menu bar hidden: skip!
    env["asleep"] = False
    env["fullscreen"] = True
    for _ in range(5):
        clock.advance(15.0)
        res = mon.check()
        assert res.skipped_reason == "fullscreen"
        assert res.notified is False
        assert res.visible is True
    assert notifications == []

    # 4. Partial hidden streak interrupted by lock must reset so unlock doesn't false-alarm
    env["fullscreen"] = False
    clock.advance(5.0)
    mon.check()  # hidden #1
    clock.advance(10.0)
    mon.check()  # hidden #2
    env["locked"] = True
    clock.advance(40.0)
    assert mon.check().skipped_reason == "screen_locked"
    env["locked"] = False
    clock.advance(5.0)
    # First check after unlock must start a fresh streak (not count pre-lock time!)
    r_after_unlock = mon.check()
    assert r_after_unlock.confirmed_hidden is False
    assert r_after_unlock.notified is False
    assert notifications == []


def test_engine_persists_and_preserves_icon_visibility_in_state_json(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    room_dir = tmp_path / "room"
    clock = FakeClock(initial=1_700_000_000.0)

    eng1 = PowerEngine(
        state_dir=state_dir,
        room_home=room_dir,
        clock=clock,
        battery_reader=lambda: BatteryStatus(on_ac_power=True, percent=95, charging_state="charged"),
        dry_run=True,
    )
    eng1.set_icon_visibility(True, checked_at=clock.now)
    eng1.step()

    saved = load_state_file(state_dir)
    assert saved["menubar_icon_visible"] is True
    assert saved["menubar_icon_checked_at"] == 1_700_000_000.0

    # A separate CLI PowerEngine stepping later must preserve menubar_icon_checked_at without spoofing freshness
    clock.advance(300.0)
    eng2 = PowerEngine(
        state_dir=state_dir,
        room_home=room_dir,
        clock=clock,
        battery_reader=lambda: BatteryStatus(on_ac_power=True, percent=95, charging_state="charged"),
        dry_run=True,
    )
    eng2.step()
    saved2 = load_state_file(state_dir)
    assert saved2["menubar_icon_visible"] is True
    assert saved2["menubar_icon_checked_at"] == 1_700_000_000.0
