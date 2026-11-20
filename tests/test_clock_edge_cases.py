"""Clock edge-case tests with fake clocks: DST, backwards wall clock, and midnight crossings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from agent_power.engine import PowerEngine
from agent_power.hooks import run_heartbeat_loop
from agent_power.policy import ActionType, FakeClock
from agent_power.power import BatteryStatus
from agent_power.presence_reader import parse_presence_record, read_presence, working_agents


NY_TZ = ZoneInfo("America/New_York")


class MutableWallClock:
    """Fake wall clock that can advance normally or jump backwards (e.g. NTP step)."""

    def __init__(self, initial_epoch: float) -> None:
        self._now = float(initial_epoch)

    def __call__(self) -> float:
        return self._now

    @property
    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        self._now += float(seconds)
        return self._now

    def jump(self, delta_seconds: float) -> float:
        self._now += float(delta_seconds)
        return self._now


def _write_presence_iso(room_dir: Path, agent: str, updated_iso: str, status: str = "working") -> None:
    pdir = room_dir / "presence"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{agent}.json").write_text(
        json.dumps(
            {
                "agent": agent,
                "status": status,
                "task": "clock edge test",
                "pid": 4321,
                "updated_at": updated_iso,
            }
        ),
        encoding="utf-8",
    )


def _ac_battery() -> BatteryStatus:
    return BatteryStatus(on_ac_power=True, percent=100, charging_state="charged")


def test_dst_spring_forward_2h_cap_and_stale_presence(tmp_path: Path) -> None:
    """During US DST spring-forward (2026-03-08 02:00 EST -> 03:00 EDT), 2h cap and 120s staleness use real elapsed time."""
    # 01:00 EST (UTC-5) = 06:00 UTC
    start_local = datetime(2026, 3, 8, 1, 0, 0, tzinfo=NY_TZ)
    wall = MutableWallClock(start_local.timestamp())
    mono = FakeClock(initial=1000.0)
    room_dir = tmp_path / "room"

    eng = PowerEngine(
        state_dir=tmp_path / "state",
        room_home=room_dir,
        clock=wall,
        monotonic_clock=mono,
        battery_reader=_ac_battery,
        dry_run=True,
    )

    # Acquire hold at 01:00 EST
    _write_presence_iso(room_dir, "claude", start_local.isoformat())
    t0 = eng.step()
    assert t0.action.kind is ActionType.HOLD
    assert t0.hold_active is True

    # Advance 1 real hour -> local time is 03:00 EDT (2 wall-clock hours ahead, but only 3600 real seconds!)
    wall.advance(3600.0)
    mono.advance(3600.0)
    local_after_1h = datetime.fromtimestamp(wall.now, tz=NY_TZ)
    assert local_after_1h.hour == 3 and local_after_1h.utcoffset() == timedelta(hours=-4)
    _write_presence_iso(room_dir, "claude", local_after_1h.isoformat())
    t1 = eng.step()
    assert t1.action.kind is ActionType.NOOP
    assert t1.hold_active is True

    # Advance to 7199s real elapsed (03:59:59 EDT) -> still held
    wall.advance(3599.0)
    mono.advance(3599.0)
    _write_presence_iso(room_dir, "claude", datetime.fromtimestamp(wall.now, tz=NY_TZ).isoformat())
    assert eng.step().hold_active is True

    # Advance 1s more -> 7200s real elapsed (04:00:00 EDT) -> 2-hour cap releases!
    wall.advance(1.0)
    mono.advance(1.0)
    _write_presence_iso(room_dir, "claude", datetime.fromtimestamp(wall.now, tz=NY_TZ).isoformat())
    t_cap = eng.step()
    assert t_cap.action.kind is ActionType.RELEASE
    assert t_cap.hold_active is False

    # Stale presence across the exact spring-forward boundary (01:59:00 EST -> 03:00:30 EDT is 90s; 03:01:01 EDT is 121s)
    before_jump = datetime(2026, 3, 8, 1, 59, 0, tzinfo=NY_TZ)
    after_90s = datetime(2026, 3, 8, 3, 0, 30, tzinfo=NY_TZ)
    after_121s = datetime(2026, 3, 8, 3, 1, 1, tzinfo=NY_TZ)
    payload = {
        "agent": "claude",
        "status": "working",
        "task": "dst spring",
        "pid": 111,
        "updated_at": before_jump.isoformat(),
    }
    fresh = parse_presence_record(payload, now=after_90s)
    assert fresh is not None and fresh.is_working is True and fresh.is_stale is False
    stale = parse_presence_record(payload, now=after_121s)
    assert stale is not None and stale.is_working is False and stale.is_stale is True


def test_dst_fall_back_2h_cap_and_stale_presence(tmp_path: Path) -> None:
    """During US DST fall-back (2026-11-01 02:00 EDT -> 01:00 EST), 2h cap still releases at 7200 real seconds."""
    # 01:00 EDT (fold=0, UTC-4) = 05:00 UTC
    start_local = datetime(2026, 11, 1, 1, 0, 0, fold=0, tzinfo=NY_TZ)
    wall = MutableWallClock(start_local.timestamp())
    mono = FakeClock(initial=2000.0)
    room_dir = tmp_path / "room"

    eng = PowerEngine(
        state_dir=tmp_path / "state",
        room_home=room_dir,
        clock=wall,
        monotonic_clock=mono,
        battery_reader=_ac_battery,
        dry_run=True,
    )

    _write_presence_iso(room_dir, "claude", start_local.isoformat())
    assert eng.step().action.kind is ActionType.HOLD

    # 1 real hour later: local clock falls back to 01:00 EST (fold=1, UTC-5) -> still held
    wall.advance(3600.0)
    mono.advance(3600.0)
    local_1h = datetime.fromtimestamp(wall.now, tz=NY_TZ)
    assert local_1h.hour == 1 and local_1h.utcoffset() == timedelta(hours=-5)
    _write_presence_iso(room_dir, "claude", local_1h.isoformat())
    assert eng.step().hold_active is True

    # 2 real hours later (02:00 EST, only +1h on local wall clock!) -> MUST release at 7200s!
    wall.advance(3600.0)
    mono.advance(3600.0)
    local_2h = datetime.fromtimestamp(wall.now, tz=NY_TZ)
    assert local_2h.hour == 2
    _write_presence_iso(room_dir, "claude", local_2h.isoformat())
    t_cap = eng.step()
    assert t_cap.action.kind is ActionType.RELEASE
    assert t_cap.hold_active is False

    # Stale presence across fall-back boundary (01:59:00 EDT -> 01:00:30 EST is 90s; 01:01:01 EST is 121s)
    before_fallback = datetime(2026, 11, 1, 1, 59, 0, fold=0, tzinfo=NY_TZ)
    after_90s = datetime(2026, 11, 1, 1, 0, 30, fold=1, tzinfo=NY_TZ)
    after_121s = datetime(2026, 11, 1, 1, 1, 1, fold=1, tzinfo=NY_TZ)
    payload = {
        "agent": "claude",
        "status": "working",
        "task": "dst fall",
        "pid": 222,
        "updated_at": before_fallback.isoformat(),
    }
    fresh = parse_presence_record(payload, now=after_90s)
    assert fresh is not None and fresh.is_working is True and fresh.is_stale is False
    stale = parse_presence_record(payload, now=after_121s)
    assert stale is not None and stale.is_working is False and stale.is_stale is True


def test_wall_clock_moving_backwards_still_enforces_2h_cap_and_idle_grace(tmp_path: Path) -> None:
    """If system wall clock jumps backwards mid-hold, monotonic time still enforces the 2-hour cap and 20s idle grace."""
    wall = MutableWallClock(1_760_000_000.0)
    mono = FakeClock(initial=5000.0)
    room_dir = tmp_path / "room"

    eng = PowerEngine(
        state_dir=tmp_path / "state",
        room_home=room_dir,
        clock=wall,
        monotonic_clock=mono,
        battery_reader=_ac_battery,
        dry_run=True,
    )

    # Start hold
    _write_presence_iso(room_dir, "claude", datetime.fromtimestamp(wall.now, tz=timezone.utc).isoformat())
    assert eng.step().action.kind is ActionType.HOLD

    # 3600s elapse, then NTP jumps wall clock BACKWARDS by 5400s!
    wall.advance(3600.0)
    mono.advance(3600.0)
    wall.jump(-5400.0)
    _write_presence_iso(room_dir, "claude", datetime.fromtimestamp(wall.now, tz=timezone.utc).isoformat())
    assert eng.step().hold_active is True

    # Another 3599s elapse (7199s monotonic total, wall clock still behind start+7200) -> still held
    wall.advance(3599.0)
    mono.advance(3599.0)
    _write_presence_iso(room_dir, "claude", datetime.fromtimestamp(wall.now, tz=timezone.utc).isoformat())
    assert eng.step().hold_active is True

    # +1s more -> 7200s monotonic elapsed -> MUST release even though wall < initial + 7200!
    wall.advance(1.0)
    mono.advance(1.0)
    _write_presence_iso(room_dir, "claude", datetime.fromtimestamp(wall.now, tz=timezone.utc).isoformat())
    t_cap = eng.step()
    assert t_cap.action.kind is ActionType.RELEASE
    assert t_cap.hold_active is False

    # End prior capped turn (idle), then start a genuinely new turn for the idle-grace check
    _write_presence_iso(room_dir, "claude", datetime.fromtimestamp(wall.now, tz=timezone.utc).isoformat(), status="idle")
    assert eng.step().hold_active is False

    wall.advance(10.0)
    mono.advance(10.0)
    _write_presence_iso(room_dir, "claude", datetime.fromtimestamp(wall.now, tz=timezone.utc).isoformat())
    assert eng.step().action.kind is ActionType.HOLD

    # Presence file is now 125s old (stale) -> ignored, starts idle grace
    stale_ts = datetime.fromtimestamp(wall.now - 125.0, tz=timezone.utc).isoformat()
    _write_presence_iso(room_dir, "claude", stale_ts)
    assert len(working_agents(read_presence(room_dir, now=datetime.fromtimestamp(wall.now, tz=timezone.utc)))) == 0
    wall.advance(5.0)
    mono.advance(5.0)
    assert eng.step().hold_active is True  # 5s idle < 20s grace

    # Wall clock jumps backwards by 600s, while monotonic advances 15s (total 20s idle) -> MUST release!
    wall.jump(-600.0)
    mono.advance(15.0)
    _write_presence_iso(room_dir, "claude", datetime.fromtimestamp(wall.now - 125.0, tz=timezone.utc).isoformat())
    t_idle = eng.step()
    assert t_idle.action.kind is ActionType.RELEASE
    assert t_idle.hold_active is False


def test_heartbeat_loop_enforces_2h_cap_when_wall_clock_jumps_backwards(tmp_path: Path) -> None:
    """run_heartbeat_loop must still exit and remove presence at 7200s even if wall clock steps backwards."""
    wall = MutableWallClock(1_760_000_000.0)
    mono = FakeClock(initial=100.0)

    p_file = tmp_path / "presence" / "claude-backwards-cap.json"
    p_file.parent.mkdir(parents=True, exist_ok=True)
    p_file.write_text(
        json.dumps(
            {
                "agent": "claude",
                "status": "working",
                "task": "long job",
                "pid": os.getpid(),
                "updated_at": datetime.fromtimestamp(wall.now, tz=timezone.utc).isoformat(),
                "max_deadline_epoch": wall.now + 7200.0,
            }
        ),
        encoding="utf-8",
    )

    jumped = {"done": False}

    def fake_sleep(seconds: float) -> None:
        # Advance both clocks in larger deterministic steps so 7200s completes immediately
        step = max(seconds, 600.0)
        mono.advance(step)
        wall.advance(step)
        if not jumped["done"] and mono.now >= 1900.0:
            jumped["done"] = True
            # Jump wall clock backwards by 3 hours!
            wall.jump(-10_800.0)

    rc = run_heartbeat_loop(
        p_file,
        agent_pid=os.getpid(),
        interval_seconds=30.0,
        max_seconds=7200.0,
        monotonic_fn=mono,
        wall_clock_fn=wall,
        sleep_fn=fake_sleep,
    )
    assert rc == 0
    assert not p_file.is_file()
    # Released right after 7200s monotonic elapsed (NOT delayed by the 3h backward wall-clock jump)
    assert (mono.now - 100.0) < 8000.0


def test_hold_crossing_midnight_releases_at_2h_cap_and_ignores_stale_presence(tmp_path: Path) -> None:
    """A hold starting at 23:00 UTC crosses midnight cleanly and releases at 01:00 UTC (7200s)."""
    start_dt = datetime(2026, 11, 20, 23, 0, 0, tzinfo=timezone.utc)
    wall = MutableWallClock(start_dt.timestamp())
    mono = FakeClock(initial=10_000.0)
    room_dir = tmp_path / "room"

    eng = PowerEngine(
        state_dir=tmp_path / "state",
        room_home=room_dir,
        clock=wall,
        monotonic_clock=mono,
        battery_reader=_ac_battery,
        dry_run=True,
    )

    # 23:00:00Z -> acquire hold
    _write_presence_iso(room_dir, "claude", start_dt.isoformat())
    assert eng.step().action.kind is ActionType.HOLD

    # 00:00:05Z (next day, +3605s) -> still held
    wall.advance(3605.0)
    mono.advance(3605.0)
    after_midnight = datetime.fromtimestamp(wall.now, tz=timezone.utc)
    assert after_midnight.day == 21 and after_midnight.hour == 0
    _write_presence_iso(room_dir, "claude", after_midnight.isoformat())
    assert eng.step().hold_active is True

    # 01:00:00Z (+7200s exact) -> 2-hour cap releases
    wall.advance(3595.0)
    mono.advance(3595.0)
    at_cap = datetime.fromtimestamp(wall.now, tz=timezone.utc)
    assert at_cap.day == 21 and at_cap.hour == 1 and at_cap.minute == 0
    _write_presence_iso(room_dir, "claude", at_cap.isoformat())
    t_cap = eng.step()
    assert t_cap.action.kind is ActionType.RELEASE
    assert t_cap.hold_active is False

    # Stale presence across midnight: written 23:59:00Z, checked 00:00:30Z (90s -> working), 00:01:01Z (121s -> stale)
    pre_midnight = datetime(2026, 11, 20, 23, 59, 0, tzinfo=timezone.utc)
    post_90s = datetime(2026, 11, 21, 0, 0, 30, tzinfo=timezone.utc)
    post_121s = datetime(2026, 11, 21, 0, 1, 1, tzinfo=timezone.utc)
    _write_presence_iso(room_dir, "claude", pre_midnight.isoformat())
    assert len(working_agents(read_presence(room_dir, now=post_90s))) == 1
    assert len(working_agents(read_presence(room_dir, now=post_121s))) == 0


def test_heartbeat_fresh_turn_after_small_leftover_gets_full_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heartbeat started with small leftover max_seconds (60s) must get a full 7200s cap when a fresh turn raises max_deadline_epoch."""
    monkeypatch.delenv("AGENT_POWER_MAX_HOLD_SECONDS", raising=False)
    wall = MutableWallClock(1_760_000_000.0)
    mono = FakeClock(initial=100.0)

    p_file = tmp_path / "presence" / "claude-fresh-full-cap.json"
    p_file.parent.mkdir(parents=True, exist_ok=True)
    p_file.write_text(
        json.dumps(
            {
                "agent": "claude",
                "status": "working",
                "task": "turn 1 leftover",
                "pid": os.getpid(),
                "updated_at": datetime.fromtimestamp(wall.now, tz=timezone.utc).isoformat(),
                "max_deadline_epoch": wall.now + 60.0,
            }
        ),
        encoding="utf-8",
    )

    state = {"turn2_written": False, "alive_at_1h": False}

    def fake_sleep(seconds: float) -> None:
        if not state["turn2_written"] and mono.now >= 120.0:
            state["turn2_written"] = True
            # Fresh Turn 2 resets max_deadline_epoch to a full 7200s from now
            data = json.loads(p_file.read_text(encoding="utf-8"))
            data["max_deadline_epoch"] = wall.now + 7200.0
            data["updated_at"] = datetime.fromtimestamp(wall.now, tz=timezone.utc).isoformat()
            p_file.write_text(json.dumps(data), encoding="utf-8")
        step = 30.0 if not state["turn2_written"] else 600.0
        mono.advance(step)
        wall.advance(step)
        if mono.now >= 3700.0 and p_file.is_file():
            state["alive_at_1h"] = True

    rc = run_heartbeat_loop(
        p_file,
        agent_pid=os.getpid(),
        interval_seconds=30.0,
        max_seconds=60.0,
        monotonic_fn=mono,
        wall_clock_fn=wall,
        sleep_fn=fake_sleep,
    )
    assert rc == 0
    assert not p_file.is_file()
    assert state["turn2_written"] is True
    assert state["alive_at_1h"] is True
    assert (mono.now - 100.0) >= 7200.0
