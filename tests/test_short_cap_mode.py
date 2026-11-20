"""Tests for AGENT_POWER_MAX_HOLD_SECONDS short-cap test mode and 7200s default."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from agent_power.engine import PowerEngine
from agent_power.hooks import handle_hook_event
from agent_power.policy import (
    DEFAULT_MAX_HOLD_SECONDS,
    MAX_HOLD_SECONDS_ENV,
    ActionType,
    FakeClock,
    Mode,
    PolicySettings,
    resolve_max_hold_seconds,
)
from agent_power.power import BatteryStatus


def _ac_battery() -> BatteryStatus:
    return BatteryStatus(on_ac_power=True, percent=100, charging_state="charged")


def _write_working_presence(room_dir: Path, epoch: float) -> None:
    pdir = room_dir / "presence"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "claude.json").write_text(
        json.dumps(
            {
                "agent": "claude",
                "status": "working",
                "task": "short cap check",
                "pid": 4321,
                "updated_at": datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )


def test_default_max_hold_is_still_7200_seconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With AGENT_POWER_MAX_HOLD_SECONDS unset, default cap remains exactly 7200 seconds (2 hours)."""
    monkeypatch.delenv(MAX_HOLD_SECONDS_ENV, raising=False)

    assert DEFAULT_MAX_HOLD_SECONDS == 7200.0
    assert resolve_max_hold_seconds() == 7200.0
    assert PolicySettings().max_hold_seconds == 7200.0
    assert PolicySettings.from_env().max_hold_seconds == 7200.0

    eng = PowerEngine(
        state_dir=tmp_path / "state",
        room_home=tmp_path / "room",
        battery_reader=_ac_battery,
        dry_run=True,
    )
    assert eng.settings.max_hold_seconds == 7200.0


@pytest.mark.parametrize("bad_value", ["", "   ", "0", "-10", "abc", "nan", "inf"])
def test_invalid_max_hold_env_falls_back_to_7200_seconds(
    monkeypatch: pytest.MonkeyPatch,
    bad_value: str,
) -> None:
    monkeypatch.setenv(MAX_HOLD_SECONDS_ENV, bad_value)
    assert resolve_max_hold_seconds() == 7200.0
    assert PolicySettings.from_env().max_hold_seconds == 7200.0


def test_short_cap_env_releases_engine_hold_in_auto_and_manual_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting AGENT_POWER_MAX_HOLD_SECONDS=5 releases an active hold after 5 seconds."""
    monkeypatch.setenv(MAX_HOLD_SECONDS_ENV, "5")

    clock = FakeClock(initial=1_760_000_000.0)
    room_dir = tmp_path / "room"
    eng = PowerEngine(
        state_dir=tmp_path / "state",
        room_home=room_dir,
        clock=clock,
        battery_reader=_ac_battery,
        dry_run=True,
    )
    assert eng.settings.max_hold_seconds == 5.0

    # Automatic mode: agent stays working, hold acquired at t=0, held at t=4.9, released at t=5.0
    _write_working_presence(room_dir, clock.now)
    t0 = eng.step()
    assert t0.action.kind is ActionType.HOLD
    assert t0.hold_active is True

    clock.advance(4.9)
    _write_working_presence(room_dir, clock.now)
    t_before = eng.step()
    assert t_before.action.kind is ActionType.NOOP
    assert t_before.hold_active is True

    clock.advance(0.1)
    _write_working_presence(room_dir, clock.now)
    t_cap = eng.step()
    assert t_cap.action.kind is ActionType.RELEASE
    assert t_cap.hold_active is False
    assert "5 seconds" in t_cap.action.reason

    # Manual Hold mode also honors the short cap
    clock.advance(1.0)
    t_manual_start = eng.set_mode(Mode.HOLD_2H)
    assert t_manual_start.action.kind is ActionType.HOLD
    assert t_manual_start.hold_active is True

    clock.advance(5.0)
    t_manual_cap = eng.step()
    assert t_manual_cap.action.kind is ActionType.RELEASE
    assert t_manual_cap.hold_active is False


def test_short_cap_env_applies_to_hook_presence_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hook events also pick up AGENT_POWER_MAX_HOLD_SECONDS when no explicit max is passed."""
    monkeypatch.setenv(MAX_HOLD_SECONDS_ENV, "8")
    room_dir = tmp_path / "room"
    payload = json.dumps(
        {
            "conversationId": "short-cap-conv",
            "hookEventName": "PreInvocation",
            "cwd": "/tmp/short-cap",
        }
    )
    assert handle_hook_event(payload, room_home=room_dir, spawn_heartbeat=False) == {}
    p_file = room_dir / "presence" / "agy-short-cap-conv.json"
    data = json.loads(p_file.read_text(encoding="utf-8"))
    delta = data["max_deadline_epoch"] - data["turn_started_at"]
    assert delta == pytest.approx(8.0, abs=0.01)


@pytest.mark.parametrize("over_limit", ["7200.1", "7201", "14400", "86400"])
def test_max_hold_env_above_7200_clamps_to_7200(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    over_limit: str,
) -> None:
    """AGENT_POWER_MAX_HOLD_SECONDS may only shorten the cap; values above 7200s clamp to 7200s."""
    monkeypatch.setenv(MAX_HOLD_SECONDS_ENV, over_limit)

    assert resolve_max_hold_seconds() == 7200.0
    assert PolicySettings.from_env().max_hold_seconds == 7200.0

    eng = PowerEngine(
        state_dir=tmp_path / "state",
        room_home=tmp_path / "room",
        battery_reader=_ac_battery,
        dry_run=True,
    )
    assert eng.settings.max_hold_seconds == 7200.0

    room_dir = tmp_path / "room"
    payload = json.dumps(
        {
            "conversationId": f"clamp-{over_limit}",
            "hookEventName": "PreInvocation",
            "cwd": "/tmp/clamp",
        }
    )
    assert handle_hook_event(payload, room_home=room_dir, spawn_heartbeat=False) == {}
    p_file = room_dir / "presence" / f"agy-clamp-{over_limit}.json"
    data = json.loads(p_file.read_text(encoding="utf-8"))
    delta = data["max_deadline_epoch"] - data["turn_started_at"]
    assert delta == pytest.approx(7200.0, abs=0.01)


def test_short_cap_does_not_reacquire_from_same_working_presence_until_new_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After cap release, subsequent steps stay released until working presence ends and a new turn starts."""
    monkeypatch.setenv(MAX_HOLD_SECONDS_ENV, "5")

    clock = FakeClock(initial=1_760_000_000.0)
    room_dir = tmp_path / "room"
    calls: list[bool] = []

    from agent_power.power import PowerResult

    def fake_disable_sleep(disable: bool, *, dry_run: bool | None = None) -> PowerResult:
        calls.append(disable)
        return PowerResult(ok=True, command=f"disablesleep {int(disable)}", dry_run=True)

    eng = PowerEngine(
        state_dir=tmp_path / "state",
        room_home=room_dir,
        clock=clock,
        battery_reader=_ac_battery,
        disable_sleep_fn=fake_disable_sleep,
        dry_run=True,
    )

    # 1. Acquire hold in Automatic mode and hit the 5s cap
    _write_working_presence(room_dir, clock.now)
    assert eng.step().action.kind is ActionType.HOLD
    assert calls == [True]

    clock.advance(5.0)
    _write_working_presence(room_dir, clock.now)
    t_cap = eng.step()
    assert t_cap.action.kind is ActionType.RELEASE
    assert t_cap.hold_active is False
    assert calls == [True, False]

    # 2. Several subsequent step() calls with the SAME still-working presence MUST stay released
    for _ in range(4):
        clock.advance(5.0)
        _write_working_presence(room_dir, clock.now)
        tick = eng.step()
        assert tick.action.kind is ActionType.NOOP
        assert tick.hold_active is False
    assert calls == [True, False]  # SleepDisabled stayed 0; never re-acquired!

    # 3. Working presence ends (idle / removed) -> latch clears; then a genuinely new turn CAN hold again
    (room_dir / "presence" / "claude.json").unlink()
    clock.advance(5.0)
    t_idle = eng.step()
    assert t_idle.hold_active is False

    clock.advance(5.0)
    _write_working_presence(room_dir, clock.now)
    t_new_turn = eng.step()
    assert t_new_turn.action.kind is ActionType.HOLD
    assert t_new_turn.hold_active is True
    assert calls == [True, False, True]


def test_short_cap_manual_hold_2h_returns_to_automatic_and_stays_released(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Mode.HOLD_2H expires at the cap, mode returns to Automatic and subsequent steps stay released."""
    monkeypatch.setenv(MAX_HOLD_SECONDS_ENV, "5")

    clock = FakeClock(initial=1_760_000_000.0)
    room_dir = tmp_path / "room"
    calls: list[bool] = []

    from agent_power.power import PowerResult

    def fake_disable_sleep(disable: bool, *, dry_run: bool | None = None) -> PowerResult:
        calls.append(disable)
        return PowerResult(ok=True, command=f"disablesleep {int(disable)}", dry_run=True)

    eng = PowerEngine(
        state_dir=tmp_path / "state",
        room_home=room_dir,
        clock=clock,
        battery_reader=_ac_battery,
        disable_sleep_fn=fake_disable_sleep,
        dry_run=True,
    )

    assert eng.set_mode(Mode.HOLD_2H).action.kind is ActionType.HOLD
    assert eng.mode is Mode.HOLD_2H

    clock.advance(5.0)
    t_cap = eng.step()
    assert t_cap.action.kind is ActionType.RELEASE
    assert t_cap.hold_active is False
    assert eng.mode is Mode.AUTOMATIC

    # Several subsequent ticks stay released and in Automatic mode
    for _ in range(3):
        clock.advance(5.0)
        tick = eng.step()
        assert tick.hold_active is False
        assert tick.mode is Mode.AUTOMATIC
    assert calls == [True, False]
