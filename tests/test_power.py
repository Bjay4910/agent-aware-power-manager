"""Tests for power.py: pmset output parsing, dry-run mode, and error handling."""

from __future__ import annotations

import subprocess
from typing import Sequence
import pytest

from agent_power.power import (
    is_dry_run,
    is_sleep_disabled,
    parse_disablesleep,
    parse_pmset_batt,
    set_disable_sleep,
    sleep_now,
)


SAMPLE_AC_CHARGING = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=20316259)	84%; charging; 0:45 remaining present: true
"""

SAMPLE_AC_CHARGED = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=20316259)	100%; charged; 0:00 remaining present: true
"""

SAMPLE_BATTERY_DISCHARGING = """Now drawing from 'Battery Power'
 -InternalBattery-0 (id=20316259)	23%; discharging; 1:12 remaining present: true
"""

SAMPLE_DESKTOP_AC_ONLY = """Now drawing from 'AC Power'
"""

SAMPLE_PMSET_G_DISABLED = """System-wide power settings:
Currently in use:
 standby              1
 SleepDisabled        1
 displaysleep         10
"""

SAMPLE_PMSET_G_ENABLED = """System-wide power settings:
Currently in use:
 standby              1
 SleepDisabled        0
 displaysleep         10
"""


def test_parse_pmset_batt_samples() -> None:
    ac_charging = parse_pmset_batt(SAMPLE_AC_CHARGING)
    assert ac_charging.on_ac_power is True
    assert ac_charging.percent == 84
    assert ac_charging.charging_state == "charging"

    ac_charged = parse_pmset_batt(SAMPLE_AC_CHARGED)
    assert ac_charged.on_ac_power is True
    assert ac_charged.percent == 100
    assert ac_charged.charging_state == "charged"

    batt_discharging = parse_pmset_batt(SAMPLE_BATTERY_DISCHARGING)
    assert batt_discharging.on_ac_power is False
    assert batt_discharging.percent == 23
    assert batt_discharging.charging_state == "discharging"

    desktop = parse_pmset_batt(SAMPLE_DESKTOP_AC_ONLY)
    assert desktop.on_ac_power is True
    assert desktop.percent is None
    assert desktop.charging_state == "ac_only"


def test_parse_disablesleep() -> None:
    assert parse_disablesleep(SAMPLE_PMSET_G_DISABLED) is True
    assert parse_disablesleep(SAMPLE_PMSET_G_ENABLED) is False
    assert parse_disablesleep("") is False


def test_is_sleep_disabled_with_fake_runner() -> None:
    def fake_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        assert list(cmd) == ["/usr/bin/pmset", "-g"]
        return subprocess.CompletedProcess(cmd, 0, stdout=SAMPLE_PMSET_G_DISABLED, stderr="")

    assert is_sleep_disabled(runner=fake_runner) is True


def test_set_disable_sleep_dry_run_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AGENT_POWER_DRY_RUN", "1")
    assert is_dry_run() is True

    called = False

    def fail_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError("Runner must not be invoked in dry-run mode")

    res_hold = set_disable_sleep(True, runner=fail_runner)
    assert res_hold.ok is True
    assert res_hold.dry_run is True
    assert res_hold.problem is None
    assert "sudo -n /usr/bin/pmset -a disablesleep 1" in res_hold.command

    res_release = set_disable_sleep(False, runner=fail_runner)
    assert res_release.ok is True
    assert "sudo -n /usr/bin/pmset -a disablesleep 0" in res_release.command
    assert called is False

    captured = capsys.readouterr().out
    assert "[DRY-RUN] sudo -n /usr/bin/pmset -a disablesleep 1" in captured
    assert "[DRY-RUN] sudo -n /usr/bin/pmset -a disablesleep 0" in captured


def test_set_disable_sleep_sudo_failure_reports_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_POWER_DRY_RUN", raising=False)

    def failing_sudo_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        assert list(cmd) == ["sudo", "-n", "/usr/bin/pmset", "-a", "disablesleep", "1"]
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="sudo: a password is required",
        )

    res = set_disable_sleep(True, dry_run=False, runner=failing_sudo_runner)
    assert res.ok is False
    assert res.dry_run is False
    assert res.problem is not None
    assert "Passwordless sudo is not configured" in res.problem
    assert "a password is required" in res.problem


def test_sleep_now_releases_first_and_sleeps_in_dry_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = sleep_now(release_first=True, dry_run=True)
    assert res.ok is True
    assert res.dry_run is True
    out = capsys.readouterr().out
    lines = [line.strip() for line in out.strip().splitlines()]
    assert lines == [
        "[DRY-RUN] sudo -n /usr/bin/pmset -a disablesleep 0",
        "[DRY-RUN] /usr/bin/pmset sleepnow",
    ]
