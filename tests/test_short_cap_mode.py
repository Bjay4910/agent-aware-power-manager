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
