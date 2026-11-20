"""Fixture-based tests for battery policy, pmset parsing, and unreadable-battery fail-safe."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Sequence

import pytest

from agent_power.engine import PowerEngine
from agent_power.policy import ActionType, FakeClock, Mode, PolicySettings, PolicyState, decide
from agent_power.power import get_battery_status, parse_pmset_batt


def _pmset_batt_fixture(*, on_ac: bool, percent: int, state: str | None = None) -> str:
    source = "AC Power" if on_ac else "Battery Power"
    charging = state or ("charging" if on_ac else "discharging")
    return (
        f"Now drawing from '{source}'\n"
        f" -InternalBattery-0 (id=20316259)\t{percent}%; {charging}; 1:00 remaining present: true\n"
    )


def _runner_returning(stdout: str, *, returncode: int = 0, stderr: str = ""):
    def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        assert list(cmd) == ["/usr/bin/pmset", "-g", "batt"]
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return _run


@pytest.mark.parametrize("percent", [0, 5, 10])
def test_battery_fixture_at_or_below_10_forces_sleep(percent: int) -> None:
    raw = _pmset_batt_fixture(on_ac=False, percent=percent)
    batt = get_battery_status(runner=_runner_returning(raw))
    assert batt.on_ac_power is False
    assert batt.percent == percent

    for only_plugged in (True, False):
        for mode in (Mode.AUTOMATIC, Mode.HOLD_2H, Mode.OFF):
            decision = decide(
                PolicyState(
                    any_agent_working=True,
                    on_ac_power=batt.on_ac_power,
                    battery_percent=batt.percent,
                    hold_active=True,
                    mode=mode,
                    settings=PolicySettings(only_while_plugged_in=only_plugged),
                )
            )
            assert decision.kind is ActionType.SLEEP_NOW


@pytest.mark.parametrize("percent", [11, 20, 24])
def test_battery_fixture_below_25_refuses_new_hold_on_battery(percent: int) -> None:
    raw = _pmset_batt_fixture(on_ac=False, percent=percent)
    batt = get_battery_status(runner=_runner_returning(raw))
    assert batt.on_ac_power is False
    assert batt.percent == percent

    settings = PolicySettings(only_while_plugged_in=False)
    for mode in (Mode.AUTOMATIC, Mode.HOLD_2H):
        decision = decide(
            PolicyState(
                any_agent_working=True,
                on_ac_power=batt.on_ac_power,
                battery_percent=batt.percent,
                hold_active=False,
                mode=mode,
                settings=settings,
            )
        )
        assert decision.kind is ActionType.NOOP
        assert f"{percent}%" in decision.reason


@pytest.mark.parametrize("percent", [26, 50, 75, 100])
def test_battery_fixture_26_to_100_allows_hold_plugged_and_unplugged(percent: int) -> None:
    # Unplugged with only_while_plugged_in=False -> HOLD works
    raw_batt = _pmset_batt_fixture(on_ac=False, percent=percent)
    batt_unplugged = get_battery_status(runner=_runner_returning(raw_batt))
    assert batt_unplugged.on_ac_power is False
    assert batt_unplugged.percent == percent

    res_unplugged = decide(
        PolicyState(
            any_agent_working=True,
            on_ac_power=batt_unplugged.on_ac_power,
            battery_percent=batt_unplugged.percent,
            hold_active=False,
            settings=PolicySettings(only_while_plugged_in=False),
        )
    )
    assert res_unplugged.kind is ActionType.HOLD

    # Default only_while_plugged_in=True -> refused on battery, allowed on AC
    res_default_batt = decide(
        PolicyState(
            any_agent_working=True,
            on_ac_power=batt_unplugged.on_ac_power,
            battery_percent=batt_unplugged.percent,
            hold_active=False,
            settings=PolicySettings(only_while_plugged_in=True),
        )
    )
    assert res_default_batt.kind is ActionType.NOOP

    raw_ac = _pmset_batt_fixture(
        on_ac=True,
        percent=percent,
        state="charged" if percent == 100 else "charging",
    )
    batt_ac = get_battery_status(runner=_runner_returning(raw_ac))
    assert batt_ac.on_ac_power is True
    assert batt_ac.percent == percent

    res_ac = decide(
        PolicyState(
            any_agent_working=True,
            on_ac_power=batt_ac.on_ac_power,
            battery_percent=batt_ac.percent,
            hold_active=False,
            settings=PolicySettings(only_while_plugged_in=True),
        )
    )
    assert res_ac.kind is ActionType.HOLD


def test_plugged_in_vs_on_battery_transitions_in_engine(tmp_path: Path) -> None:
    clock = FakeClock(initial=1_700_000_000.0)
    current_raw = [_pmset_batt_fixture(on_ac=True, percent=80)]

    room_dir = tmp_path / "room"
    pdir = room_dir / "presence"
    pdir.mkdir(parents=True)
    (pdir / "claude.json").write_text(
        '{"agent":"claude","status":"working","task":"test","pid":999,"updated_at":"2023-11-14T22:13:20Z"}',
        encoding="utf-8",
    )

    eng = PowerEngine(
        state_dir=tmp_path / "state",
        room_home=room_dir,
        clock=clock,
        battery_reader=lambda: get_battery_status(runner=_runner_returning(current_raw[0])),
        dry_run=True,
    )

    # Plugged in at 80% -> acquires hold
    t1 = eng.step()
    assert t1.action.kind is ActionType.HOLD
    assert t1.hold_active is True

    # Unplugged at 80% with default only_while_plugged_in=True -> releases hold
    current_raw[0] = _pmset_batt_fixture(on_ac=False, percent=80)
    t2 = eng.step()
    assert t2.action.kind is ActionType.RELEASE
    assert t2.hold_active is False

    # Allow unplugged holds -> acquires hold at 80%
    t3 = eng.set_only_while_plugged_in(False)
    assert t3.action.kind is ActionType.HOLD
    assert t3.hold_active is True

    # Battery drops to 10% on battery -> forces SLEEP_NOW
    current_raw[0] = _pmset_batt_fixture(on_ac=False, percent=10)
    t4 = eng.step()
    assert t4.action.kind is ActionType.SLEEP_NOW
    assert t4.hold_active is False


@pytest.mark.parametrize(
    "bad_output",
    [
        "",
        "   \n",
        "pmset: unable to read power sources\n",
        "Now drawing from 'Battery Power'\n",
        "Now drawing from 'Battery Power'\n -InternalBattery-0\t(no percentage)\n",
    ],
)
def test_unreadable_pmset_output_fails_safe(bad_output: str) -> None:
    batt = parse_pmset_batt(bad_output)
    assert batt.on_ac_power is False
    assert batt.percent is None

    for only_plugged in (True, False):
        for mode in (Mode.AUTOMATIC, Mode.HOLD_2H):
            # Must refuse new hold
            new_res = decide(
                PolicyState(
                    any_agent_working=True,
                    on_ac_power=batt.on_ac_power,
                    battery_percent=batt.percent,
                    hold_active=False,
                    mode=mode,
                    settings=PolicySettings(only_while_plugged_in=only_plugged),
                )
            )
            assert new_res.kind is ActionType.NOOP

            # Must release existing hold
            active_res = decide(
                PolicyState(
                    any_agent_working=True,
                    on_ac_power=batt.on_ac_power,
                    battery_percent=batt.percent,
                    hold_active=True,
                    mode=mode,
                    settings=PolicySettings(only_while_plugged_in=only_plugged),
                )
            )
            assert active_res.kind is ActionType.RELEASE


def test_unreadable_pmset_nonzero_exit_and_exception_fail_safe() -> None:
    # Non-zero exit code from pmset -g batt (even if stdout mentions AC Power)
    batt_rc = get_battery_status(
        runner=_runner_returning("Now drawing from 'AC Power'\n", returncode=1, stderr="iokit error")
    )
    assert batt_rc.on_ac_power is False
    assert batt_rc.percent is None

    # Subprocess exception
    def raising_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, timeout=5.0)

    batt_exc = get_battery_status(runner=raising_runner)
    assert batt_exc.on_ac_power is False
    assert batt_exc.percent is None

    # Policy refuses new hold and releases active hold even when only_while_plugged_in=False
    for batt in (batt_rc, batt_exc):
        res_new = decide(
            PolicyState(
                any_agent_working=True,
                on_ac_power=batt.on_ac_power,
                battery_percent=batt.percent,
                hold_active=False,
                settings=PolicySettings(only_while_plugged_in=False),
            )
        )
        assert res_new.kind is ActionType.NOOP

        res_active = decide(
            PolicyState(
                any_agent_working=True,
                on_ac_power=batt.on_ac_power,
                battery_percent=batt.percent,
                hold_active=True,
                settings=PolicySettings(only_while_plugged_in=False),
            )
        )
        assert res_active.kind is ActionType.RELEASE


def test_get_battery_status_retries_once_on_transient_flap() -> None:
    """A single unreadable/failing pmset -g batt call is retried once and recovers if the retry succeeds."""
    responses = [
        subprocess.CompletedProcess(["/usr/bin/pmset", "-g", "batt"], 1, stdout="", stderr="transient error"),
        subprocess.CompletedProcess(
            ["/usr/bin/pmset", "-g", "batt"],
            0,
            stdout=_pmset_batt_fixture(on_ac=True, percent=88),
            stderr="",
        ),
    ]
    calls = 0

    def flapping_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return responses[calls - 1]

    batt = get_battery_status(runner=flapping_runner)
    assert calls == 2
    assert batt.on_ac_power is True
    assert batt.percent == 88

    # Two consecutive unreadable reads return unreadable after exactly 2 calls (initial + 1 retry)
    calls_fail = 0

    def always_empty_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        nonlocal calls_fail
        calls_fail += 1
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    batt_fail = get_battery_status(runner=always_empty_runner)
    assert calls_fail == 2
    assert batt_fail.on_ac_power is False
    assert batt_fail.percent is None


def test_engine_unreadable_battery_flap_hysteresis_and_reset(tmp_path: Path) -> None:
    """1-2 unreadable ticks keep an active hold, 3 consecutive release, a good read resets counter, and new holds refuse immediately."""
    clock = FakeClock(initial=1_760_000_000.0)
    room_dir = tmp_path / "room"
    pdir = room_dir / "presence"
    pdir.mkdir(parents=True)

    def write_working() -> None:
        from datetime import datetime, timezone

        (pdir / "claude.json").write_text(
            f'{{"agent":"claude","status":"working","task":"build","pid":999,"updated_at":"{datetime.fromtimestamp(clock.now, tz=timezone.utc).isoformat()}"}}',
            encoding="utf-8",
        )

    good_ac = _pmset_batt_fixture(on_ac=True, percent=90)
    current_stdout = [""]  # Start unreadable

    eng = PowerEngine(
        state_dir=tmp_path / "state",
        room_home=room_dir,
        clock=clock,
        battery_reader=lambda: get_battery_status(runner=_runner_returning(current_stdout[0])),
        dry_run=True,
    )

    # 1. New hold is refused IMMEDIATELY on first unreadable tick
    write_working()
    t_new_refused = eng.step()
    assert t_new_refused.action.kind is ActionType.NOOP
    assert t_new_refused.hold_active is False

    # 2. Acquire active hold on good AC reading
    current_stdout[0] = good_ac
    clock.advance(5.0)
    write_working()
    t_hold = eng.step()
    assert t_hold.action.kind is ActionType.HOLD
    assert t_hold.hold_active is True

    # 3. Single unreadable flap keeps the active hold (even with default only_while_plugged_in=True)
    current_stdout[0] = ""
    clock.advance(5.0)
    write_working()
    t_flap1 = eng.step()
    assert t_flap1.action.kind is ActionType.NOOP
    assert t_flap1.hold_active is True

    # 4. Second consecutive unreadable tick still keeps active hold
    clock.advance(5.0)
    write_working()
    t_flap2 = eng.step()
    assert t_flap2.action.kind is ActionType.NOOP
    assert t_flap2.hold_active is True

    # 5. Successful read resets the unreadable counter
    current_stdout[0] = good_ac
    clock.advance(5.0)
    write_working()
    t_ok = eng.step()
    assert t_ok.hold_active is True

    # 6. After reset, 2 unreadable ticks still keep hold, and the 3rd consecutive releases it
    current_stdout[0] = ""
    clock.advance(5.0)
    write_working()
    assert eng.step().hold_active is True  # streak 1
    clock.advance(5.0)
    write_working()
    assert eng.step().hold_active is True  # streak 2
    clock.advance(5.0)
    write_working()
    t_release = eng.step()  # streak 3 -> RELEASE!
    assert t_release.action.kind is ActionType.RELEASE
    assert t_release.hold_active is False
