"""Tests for engine.py and watchdog.py."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from agent_power.engine import MAX_EVENTS, PowerEngine, load_state_file
from agent_power.policy import ActionType, FakeClock, Mode
from agent_power.power import BatteryStatus, PowerResult
from agent_power.watchdog import run_watchdog_check


def _write_presence(room_dir: Path, agent: str, status: str, task: str, updated_epoch: float) -> None:
    pdir = room_dir / "presence"
    pdir.mkdir(parents=True, exist_ok=True)
    dt = datetime.fromtimestamp(updated_epoch, tz=timezone.utc).isoformat()
    (pdir / f"{agent}.json").write_text(
        json.dumps(
            {
                "agent": agent,
                "status": status,
                "task": task,
                "pid": 4321,
                "updated_at": dt,
            }
        ),
        encoding="utf-8",
    )


def test_engine_lifecycle_hold_grace_release_and_shutdown(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    room_dir = tmp_path / "room"
    clock = FakeClock(initial=1_700_000_000.0)

    calls: list[tuple[str, bool]] = []

    def fake_disable_sleep(disable: bool, *, dry_run: bool | None = None) -> PowerResult:
        calls.append(("disablesleep", disable))
        return PowerResult(ok=True, command=f"disablesleep {int(disable)}", dry_run=True)

    def fake_sleep_now(*, release_first: bool = True, dry_run: bool | None = None) -> PowerResult:
        calls.append(("sleepnow", release_first))
        return PowerResult(ok=True, command="sleepnow", dry_run=True)

    def fake_battery() -> BatteryStatus:
        return BatteryStatus(on_ac_power=True, percent=92, charging_state="charged")

    engine = PowerEngine(
        state_dir=state_dir,
        room_home=room_dir,
        clock=clock,
        battery_reader=fake_battery,
        disable_sleep_fn=fake_disable_sleep,
        sleep_now_fn=fake_sleep_now,
        dry_run=True,
    )

    # 1. Agent starts working -> HOLD
    _write_presence(room_dir, "claude", "working", "compare databases", clock.now)
    tick1 = engine.step()
    assert tick1.action.kind is ActionType.HOLD
    assert tick1.hold_active is True
    assert tick1.icon_state == "awake"
    assert calls == [("disablesleep", True)]

    saved = load_state_file(state_dir)
    assert saved["hold_active"] is True
    assert saved["heartbeat_epoch"] == clock.now
    assert "Kept awake: claude is working on 'compare databases'" in saved["events"]

    # 2. Agent finishes working -> 5s later still in 20s grace period -> NOOP
    clock.advance(5.0)
    _write_presence(room_dir, "claude", "idle", "compare databases", clock.now)
    tick2 = engine.step()
    assert tick2.action.kind is ActionType.NOOP
    assert tick2.hold_active is True

    # 3. 20s after last working -> RELEASE
    clock.advance(20.0)
    tick3 = engine.step()
    assert tick3.action.kind is ActionType.RELEASE
    assert tick3.hold_active is False
    assert tick3.icon_state == "normal"
    assert calls[-1] == ("disablesleep", False)

    # 4. Acquire hold again, then test shutdown() releases on normal exit / SIGTERM
    clock.advance(5.0)
    _write_presence(room_dir, "claude", "working", "final task", clock.now)
    tick4 = engine.step()
    assert tick4.hold_active is True

    engine.shutdown("Released sleep hold: received SIGTERM")
    assert engine.hold_active is False
    assert calls[-1] == ("disablesleep", False)
    final_state = load_state_file(state_dir)
    assert final_state["hold_active"] is False
    assert "Released sleep hold: received SIGTERM" in final_state["events"][-1]


def test_engine_event_log_capped_at_50_and_problem_icon(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    room_dir = tmp_path / "room"
    clock = FakeClock(initial=1_700_000_000.0)

    def failing_disable_sleep(disable: bool, *, dry_run: bool | None = None) -> PowerResult:
        return PowerResult(
            ok=False,
            command="sudo -n /usr/bin/pmset -a disablesleep 1",
            dry_run=False,
            problem="sudo failed",
        )

    engine = PowerEngine(
        state_dir=state_dir,
        room_home=room_dir,
        clock=clock,
        battery_reader=lambda: BatteryStatus(on_ac_power=True, percent=100, charging_state="charged"),
        disable_sleep_fn=failing_disable_sleep,
    )

    for i in range(65):
        engine.record_event(f"Event number {i}")
    assert len(engine.events) == MAX_EVENTS
    assert engine.events[0] == "Event number 15"
    assert engine.events[-1] == "Event number 64"

    _write_presence(room_dir, "claude", "working", "build", clock.now)
    tick = engine.step()
    assert tick.icon_state == "problem"
    assert tick.problem == "sudo failed"

    engine.set_mode(Mode.OFF)
    engine.problem = None
    assert engine.icon_state == "off"


def test_watchdog_releases_only_when_heartbeat_older_than_90s(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    now_epoch = 2_000_000.0

    released_calls: list[bool] = []

    def fake_disable_sleep(disable: bool, *, dry_run: bool | None = None) -> PowerResult:
        released_calls.append(disable)
        return PowerResult(ok=True, command="sudo -n /usr/bin/pmset -a disablesleep 0", dry_run=True)

    # Case 1: Hold is active, heartbeat is exactly 90.0s old -> fresh (not > 90s)
    (state_dir / "state.json").write_text(
        json.dumps({"heartbeat_epoch": now_epoch - 90.0, "hold_active": True, "events": []}),
        encoding="utf-8",
    )
    res_fresh = run_watchdog_check(
        state_dir=state_dir,
        now=now_epoch,
        is_sleep_disabled_fn=lambda: True,
        disable_sleep_fn=fake_disable_sleep,
    )
    assert res_fresh.released is False
    assert released_calls == []

    # Case 2: Hold is active, heartbeat is 90.1s old -> stale (> 90s) -> release and log!
    (state_dir / "state.json").write_text(
        json.dumps({"heartbeat_epoch": now_epoch - 90.1, "hold_active": True, "events": []}),
        encoding="utf-8",
    )
    res_stale = run_watchdog_check(
        state_dir=state_dir,
        now=now_epoch,
        is_sleep_disabled_fn=lambda: True,
        disable_sleep_fn=fake_disable_sleep,
    )
    assert res_stale.released is True
    assert released_calls == [False]

    updated_state = load_state_file(state_dir)
    assert updated_state["hold_active"] is False
    assert "Watchdog released orphaned sleep hold" in updated_state["events"][-1]
    assert (state_dir / "watchdog.log").is_file()
    assert "Watchdog released orphaned sleep hold" in (state_dir / "watchdog.log").read_text(encoding="utf-8")

    # Case 3: Missing state.json while pmset reports SleepDisabled=1 -> release!
    (state_dir / "state.json").unlink()
    res_missing = run_watchdog_check(
        state_dir=state_dir,
        now=now_epoch,
        is_sleep_disabled_fn=lambda: True,
        disable_sleep_fn=fake_disable_sleep,
    )
    assert res_missing.released is True
    assert len(released_calls) == 2
