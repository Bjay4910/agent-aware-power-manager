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


def test_cap_expired_latch_persists_across_restart_and_clears_on_idle_or_set_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_cap_expired_latch persists in state.json across restarts and clears when working presence ends or on set_mode."""
    from agent_power.engine import load_state_file
    from agent_power.power import PowerResult

    monkeypatch.setenv(MAX_HOLD_SECONDS_ENV, "5")
    clock = FakeClock(initial=1_760_000_000.0)
    state_dir = tmp_path / "state"
    room_dir = tmp_path / "room"
    calls: list[bool] = []

    def fake_disable_sleep(disable: bool, *, dry_run: bool | None = None) -> PowerResult:
        calls.append(disable)
        return PowerResult(ok=True, command=f"disablesleep {int(disable)}", dry_run=True)

    # 1. Engine 1 acquires hold and hits the 5s cap
    eng1 = PowerEngine(
        state_dir=state_dir,
        room_home=room_dir,
        clock=clock,
        battery_reader=_ac_battery,
        disable_sleep_fn=fake_disable_sleep,
        dry_run=True,
    )
    _write_working_presence(room_dir, clock.now)
    assert eng1.step().action.kind is ActionType.HOLD
    clock.advance(5.0)
    _write_working_presence(room_dir, clock.now)
    t_cap = eng1.step()
    assert t_cap.action.kind is ActionType.RELEASE
    assert load_state_file(state_dir).get("cap_expired_latch") is True
    assert calls == [True, False]

    # 2. Restart: new PowerEngine from the same state.json while same presence continues MUST stay released
    eng2 = PowerEngine(
        state_dir=state_dir,
        room_home=room_dir,
        clock=clock,
        battery_reader=_ac_battery,
        disable_sleep_fn=fake_disable_sleep,
        dry_run=True,
    )
    clock.advance(5.0)
    _write_working_presence(room_dir, clock.now)
    t_after_restart = eng2.step()
    assert t_after_restart.action.kind is ActionType.NOOP
    assert t_after_restart.hold_active is False
    assert calls == [True, False]

    # 3. Working presence ends -> latch clears -> genuinely new turn can hold again
    (room_dir / "presence" / "claude.json").unlink()
    clock.advance(5.0)
    assert eng2.step().hold_active is False
    assert load_state_file(state_dir).get("cap_expired_latch") is False

    clock.advance(5.0)
    _write_working_presence(room_dir, clock.now)
    assert eng2.step().action.kind is ActionType.HOLD
    assert calls == [True, False, True]

    # 4. Hit cap again, restart, then explicit set_mode(HOLD_2H) clears latch and acquires hold
    clock.advance(5.0)
    _write_working_presence(room_dir, clock.now)
    assert eng2.step().action.kind is ActionType.RELEASE
    assert load_state_file(state_dir).get("cap_expired_latch") is True

    eng3 = PowerEngine(
        state_dir=state_dir,
        room_home=room_dir,
        clock=clock,
        battery_reader=_ac_battery,
        disable_sleep_fn=fake_disable_sleep,
        dry_run=True,
    )
    t_manual = eng3.set_mode(Mode.HOLD_2H)
    assert t_manual.action.kind is ActionType.HOLD
    assert t_manual.hold_active is True
    assert load_state_file(state_dir).get("cap_expired_latch") is False


def test_cli_status_is_readonly_and_never_invokes_pmset_or_writes_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`agent-power status` must be strictly read-only: no pmset calls, no state.json writes, no re-acquire after cap."""
    from agent_power.cli import main
    from agent_power.power import PowerResult

    monkeypatch.setenv(MAX_HOLD_SECONDS_ENV, "5")
    clock = FakeClock(initial=1_760_000_000.0)
    state_dir = tmp_path / "state"
    room_dir = tmp_path / "room"

    def forbid_disable_sleep(disable: bool, *, dry_run: bool | None = None) -> PowerResult:
        raise AssertionError("agent-power status must never invoke set_disable_sleep")

    def forbid_sleep_now(*, release_first: bool = True, dry_run: bool | None = None) -> PowerResult:
        raise AssertionError("agent-power status must never invoke sleep_now")

    # Case A: Brand-new state with a working agent (would trigger HOLD if status called step())
    _write_working_presence(room_dir, clock.now)
    eng_fresh = PowerEngine(
        state_dir=state_dir,
        room_home=room_dir,
        clock=clock,
        battery_reader=_ac_battery,
        disable_sleep_fn=forbid_disable_sleep,
        sleep_now_fn=forbid_sleep_now,
        dry_run=False,
    )
    assert main(["status"], engine=eng_fresh, state_dir=state_dir) == 0
    out_a = capsys.readouterr().out
    assert "Sleep hold active:      NO" in out_a
    assert not (state_dir / "state.json").exists()
    assert eng_fresh.hold_active is False

    # Case B: Critical battery (5%) would trigger SLEEP_NOW if status acted on decision
    eng_crit = PowerEngine(
        state_dir=state_dir,
        room_home=room_dir,
        clock=clock,
        battery_reader=lambda: BatteryStatus(on_ac_power=False, percent=5, charging_state="discharging"),
        disable_sleep_fn=forbid_disable_sleep,
        sleep_now_fn=forbid_sleep_now,
        dry_run=False,
    )
    assert main(["status"], engine=eng_crit, state_dir=state_dir) == 0
    out_b = capsys.readouterr().out
    assert "SLEEP_NOW" in out_b
    assert not (state_dir / "state.json").exists()

    # Case C: After a real cap release, `agent-power status` does not re-acquire or touch state.json
    eng_run = PowerEngine(
        state_dir=state_dir,
        room_home=room_dir,
        clock=clock,
        battery_reader=_ac_battery,
        dry_run=True,
    )
    assert eng_run.step().action.kind is ActionType.HOLD
    clock.advance(5.0)
    _write_working_presence(room_dir, clock.now)
    assert eng_run.step().action.kind is ActionType.RELEASE
    state_before = (state_dir / "state.json").read_text(encoding="utf-8")

    eng_status = PowerEngine(
        state_dir=state_dir,
        room_home=room_dir,
        clock=clock,
        battery_reader=_ac_battery,
        disable_sleep_fn=forbid_disable_sleep,
        sleep_now_fn=forbid_sleep_now,
        dry_run=False,
    )
    assert main(["status"], engine=eng_status, state_dir=state_dir) == 0
    out_c = capsys.readouterr().out
    assert "Sleep hold active:      NO" in out_c
    assert (state_dir / "state.json").read_text(encoding="utf-8") == state_before
    assert eng_status.hold_active is False
