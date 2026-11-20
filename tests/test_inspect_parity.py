"""Parity tests ensuring PowerEngine.inspect() matches step() decisions with zero side effects."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import random
import subprocess
import time
from unittest import mock

from agent_power.cli import main as cli_main
from agent_power.engine import PowerEngine
from agent_power.policy import ActionType, FakeClock, PolicySettings
from agent_power.power import BatteryStatus


class _MutableWallClock:
    def __init__(self, initial: float) -> None:
        self._now = float(initial)

    def __call__(self) -> float:
        return self._now

    @property
    def now(self) -> float:
        return self._now

    def advance(self, delta: float) -> float:
        self._now += float(delta)
        return self._now


def _write_presence(room_dir: Path, agent: str, epoch: float, status: str = "working") -> None:
    pdir = room_dir / "presence"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{agent}.json").write_text(
        json.dumps(
            {
                "agent": agent,
                "status": status,
                "task": "parity test",
                "pid": 1234,
                "updated_at": datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )


def test_inspect_step_parity_on_unreadable_battery_ticks(tmp_path: Path) -> None:
    """inspect() and step() must return identical Action (kind and reason) on unreadable battery ticks 1, 2, and 3."""
    wall = _MutableWallClock(1_760_000_000.0)
    mono = FakeClock(initial=1000.0)
    room_dir = tmp_path / "room"
    current_batt = [BatteryStatus(on_ac_power=True, percent=90, charging_state="charging")]

    eng = PowerEngine(
        state_dir=tmp_path / "state",
        room_home=room_dir,
        settings=PolicySettings(only_while_plugged_in=True, max_hold_seconds=600.0),
        clock=wall,
        monotonic_clock=mono,
        battery_reader=lambda: current_batt[0],
        dry_run=True,
    )

    # Tick 0: start working on AC 90% -> both HOLD
    _write_presence(room_dir, "claude", wall.now, "working")
    ti0 = eng.inspect()
    ts0 = eng.step()
    assert ti0.action == ts0.action
    assert ts0.action.kind is ActionType.HOLD
    assert ts0.hold_active is True

    # Ticks 1, 2, 3: unreadable battery -> inspect() must match step() at every tick, with tick 3 == RELEASE
    current_batt[0] = BatteryStatus(on_ac_power=False, percent=None, charging_state="unknown")
    expected_kinds = [ActionType.NOOP, ActionType.NOOP, ActionType.RELEASE]
    for expected in expected_kinds:
        wall.advance(5.0)
        mono.advance(5.0)
        _write_presence(room_dir, "claude", wall.now, "working")
        streak_before = eng._unreadable_battery_streak
        ti = eng.inspect()
        # inspect() must not mutate streak
        assert eng._unreadable_battery_streak == streak_before
        ts = eng.step()
        assert ti.action == ts.action
        assert ti.action.kind is expected


def test_inspect_step_randomized_parity_fuzz_500_steps(tmp_path: Path) -> None:
    """Across 500 randomized steps (presence, battery, unreadable, cap, latch, clock jumps), inspect() matches step()."""
    rng = random.Random(20261120)
    wall = _MutableWallClock(1_760_000_000.0)
    mono = FakeClock(initial=2000.0)
    room_dir = tmp_path / "room"
    current_batt = [BatteryStatus(on_ac_power=True, percent=90, charging_state="charging")]

    eng = PowerEngine(
        state_dir=tmp_path / "state",
        room_home=room_dir,
        settings=PolicySettings(only_while_plugged_in=False, max_hold_seconds=90.0),
        clock=wall,
        monotonic_clock=mono,
        battery_reader=lambda: current_batt[0],
        dry_run=True,
    )

    battery_choices = [
        BatteryStatus(on_ac_power=True, percent=90, charging_state="charging"),
        BatteryStatus(on_ac_power=False, percent=90, charging_state="discharging"),
        BatteryStatus(on_ac_power=False, percent=25, charging_state="discharging"),
        BatteryStatus(on_ac_power=False, percent=24, charging_state="discharging"),
        BatteryStatus(on_ac_power=False, percent=10, charging_state="discharging"),
        BatteryStatus(on_ac_power=False, percent=5, charging_state="discharging"),
        BatteryStatus(on_ac_power=False, percent=None, charging_state="unknown"),
    ]

    for step_idx in range(500):
        dm = rng.choice([1.0, 5.0, 10.0, 20.0, 45.0])
        roll = rng.random()
        if roll < 0.08:
            dw = -300.0  # backward wall-clock jump
        elif roll < 0.14:
            dw = 3600.0  # forward wall-clock jump
        else:
            dw = dm
        wall.advance(dw)
        mono.advance(dm)

        # Bias slightly toward bursts of unreadable battery so 1/2/3 streaks occur repeatedly
        if rng.random() < 0.25:
            current_batt[0] = BatteryStatus(on_ac_power=False, percent=None, charging_state="unknown")
        else:
            current_batt[0] = rng.choice(battery_choices)

        pres_action = rng.choice(["working", "working", "idle", "keep"])
        if pres_action != "keep":
            _write_presence(room_dir, "claude", wall.now, pres_action)

        before_snapshot = (
            eng.hold_active,
            eng.hold_started_at,
            eng._hold_started_mono,
            eng.last_working_at,
            eng._last_working_mono,
            eng._unreadable_battery_streak,
            eng._cap_expired_latch,
            eng._max_wall_epoch,
            eng.mode,
            list(eng.events),
        )
        ti = eng.inspect()
        after_inspect = (
            eng.hold_active,
            eng.hold_started_at,
            eng._hold_started_mono,
            eng.last_working_at,
            eng._last_working_mono,
            eng._unreadable_battery_streak,
            eng._cap_expired_latch,
            eng._max_wall_epoch,
            eng.mode,
            list(eng.events),
        )
        assert after_inspect == before_snapshot, f"inspect() mutated engine state at step {step_idx}"

        ts = eng.step()
        assert ti.action == ts.action, (
            f"Divergence at step {step_idx}: inspect={ti.action!r} != step={ts.action!r}"
        )


def test_cli_status_readonly_spy_no_subprocess_no_writes(tmp_path: Path) -> None:
    """`agent-power status` invokes zero subprocesses and performs zero file writes."""
    state_dir = tmp_path / "state"
    room_dir = tmp_path / "room"
    state_dir.mkdir(parents=True)
    state_file = state_dir / "state.json"
    state_file.write_text(
        json.dumps({"hold_active": False, "mode": "auto", "events": ["seed"]}),
        encoding="utf-8",
    )
    before_bytes = state_file.read_bytes()
    _write_presence(room_dir, "claude", time.time(), "working")

    sub_calls: list[object] = []
    write_calls: list[str] = []
    orig_write_text = Path.write_text

    def spy_write(self: Path, *args: object, **kwargs: object) -> int:
        write_calls.append(str(self))
        return orig_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    def spy_sub(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        sub_calls.append(args[0] if args else kwargs)
        raise AssertionError("status must never invoke subprocess.run")

    eng = PowerEngine(
        state_dir=state_dir,
        room_home=room_dir,
        battery_reader=lambda: BatteryStatus(on_ac_power=True, percent=100, charging_state="charged"),
        dry_run=False,
    )
    with mock.patch.object(subprocess, "run", side_effect=spy_sub), mock.patch.object(
        Path, "write_text", spy_write
    ):
        rc = cli_main(["status"], engine=eng, state_dir=state_dir)

    assert rc == 0
    assert sub_calls == []
    assert write_calls == []
    assert state_file.read_bytes() == before_bytes


def test_restore_existing_state_ignores_non_finite_bool_and_bad_timestamps_and_enforces_2h_cap(
    tmp_path: Path,
) -> None:
    """NaN, inf, 1e309, bools, strings, and None in state.json timestamps must be ignored and 2h cap must still release."""
    bad_raw_payloads = [
        ("nan", '{"hold_active": true, "max_wall_epoch": NaN, "heartbeat_epoch": NaN, "hold_started_at": NaN, "last_working_at": NaN}'),
        ("inf", '{"hold_active": true, "max_wall_epoch": Infinity, "heartbeat_epoch": -Infinity, "hold_started_at": Infinity, "last_working_at": Infinity}'),
        ("1e309", '{"hold_active": true, "max_wall_epoch": 1e309, "heartbeat_epoch": 1e309, "hold_started_at": 1e309, "last_working_at": 1e309}'),
        ("bool_true", json.dumps({"hold_active": True, "max_wall_epoch": True, "heartbeat_epoch": True, "hold_started_at": True, "last_working_at": True})),
        ("bool_false", json.dumps({"hold_active": True, "max_wall_epoch": False, "heartbeat_epoch": False, "hold_started_at": False, "last_working_at": False})),
        ("strings", json.dumps({"hold_active": True, "max_wall_epoch": "999", "heartbeat_epoch": "bad", "hold_started_at": "123", "last_working_at": "456"})),
        ("none", json.dumps({"hold_active": True, "max_wall_epoch": None, "heartbeat_epoch": None, "hold_started_at": None, "last_working_at": None})),
    ]

    for label, raw_json in bad_raw_payloads:
        sdir = tmp_path / f"state_{label}"
        rdir = tmp_path / f"room_{label}"
        sdir.mkdir(parents=True)
        (sdir / "state.json").write_text(raw_json, encoding="utf-8")

        clk = FakeClock(initial=1_760_000_000.0)
        _write_presence(rdir, "claude", clk.now, "working")

        eng = PowerEngine(
            state_dir=sdir,
            room_home=rdir,
            clock=clk,
            battery_reader=lambda: BatteryStatus(on_ac_power=True, percent=100, charging_state="charged"),
            dry_run=True,
        )
        # All corrupt timestamp fields must have been ignored (not restored as NaN/inf/bool/str)
        assert eng._max_wall_epoch is None, f"{label}: _max_wall_epoch was not ignored ({eng._max_wall_epoch!r})"
        assert eng.hold_started_at is None, f"{label}: hold_started_at was not ignored ({eng.hold_started_at!r})"
        assert eng.last_working_at is None, f"{label}: last_working_at was not ignored ({eng.last_working_at!r})"

        # Active hold must still be capped at 2 hours (7200s) and release cleanly!
        t0 = eng.step()
        assert t0.hold_active is True, f"{label}: expected active hold at t=0"
        clk.advance(7200.0)
        _write_presence(rdir, "claude", clk.now, "working")
        t_cap = eng.step()
        assert t_cap.action.kind is ActionType.RELEASE, f"{label}: 2h cap did not release ({t_cap.action!r})"
        assert t_cap.hold_active is False
