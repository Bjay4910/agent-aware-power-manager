"""Tests for CLI parsing/commands (cli.py) and menu bar icon states (menubar.py)."""

from __future__ import annotations

from pathlib import Path
import pytest

from agent_power.cli import build_parser, main
from agent_power.engine import PowerEngine
from agent_power.menubar import ICON_STATE_TITLES
from agent_power.policy import FakeClock
from agent_power.power import BatteryStatus


def _make_test_engine(tmp_path: Path) -> PowerEngine:
    return PowerEngine(
        state_dir=tmp_path / "state",
        room_home=tmp_path / "room",
        clock=FakeClock(initial=1_700_000_000.0),
        battery_reader=lambda: BatteryStatus(on_ac_power=True, percent=95, charging_state="charged"),
        dry_run=True,
    )


def test_cli_parser_subcommands() -> None:
    parser = build_parser()
    for sub in ("status", "hold", "release", "sleep-now", "guard-run", "guard-status"):
        ns = parser.parse_args([sub])
        assert ns.command == sub

    for target in ("auto", "hold", "off"):
        ns = parser.parse_args(["mode", target])
        assert ns.command == "mode"
        assert ns.target_mode == target

    with pytest.raises(SystemExit):
        parser.parse_args(["mode", "invalid_mode"])


def test_cli_commands_in_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AGENT_POWER_DRY_RUN", "1")
    eng = _make_test_engine(tmp_path)

    # 1. status
    assert main(["status"], engine=eng, state_dir=tmp_path / "state") == 0
    out = capsys.readouterr().out
    assert "Icon state:             normal" in out
    assert "Only while plugged in:  ON" in out

    # 2. hold
    assert main(["hold"], engine=eng, state_dir=tmp_path / "state") == 0
    out = capsys.readouterr().out
    assert "[DRY-RUN] sudo -n /usr/bin/pmset -a disablesleep 1" in out
    assert "HOLD:" in out

    # 3. release
    assert main(["release"], engine=eng, state_dir=tmp_path / "state") == 0
    out = capsys.readouterr().out
    assert "[DRY-RUN] sudo -n /usr/bin/pmset -a disablesleep 0" in out
    assert "RELEASE:" in out

    # 4. mode auto / hold / off
    assert main(["mode", "off"], engine=eng, state_dir=tmp_path / "state") == 0
    out = capsys.readouterr().out
    assert "Mode set to Off (off)." in out

    assert main(["mode", "auto"], engine=eng, state_dir=tmp_path / "state") == 0
    out = capsys.readouterr().out
    assert "Mode set to Automatic (auto)." in out

    # 5. sleep-now
    assert main(["sleep-now"], engine=eng, state_dir=tmp_path / "state") == 0
    out = capsys.readouterr().out
    assert "[DRY-RUN] sudo -n /usr/bin/pmset -a disablesleep 0" in out
    assert "[DRY-RUN] /usr/bin/pmset sleepnow" in out
    assert "SLEEP_NOW:" in out

    # 6. guard-status & guard-run
    cdir = tmp_path / "config"
    cdir.mkdir()
    (cdir / "guard_disabled").write_text("", encoding="utf-8")
    assert (
        main(
            ["guard-run"],
            state_dir=tmp_path / "state",
            config_dir=cdir,
            log_path=tmp_path / "guard.log",
        )
        == 0
    )
    assert (
        main(
            ["guard-status"],
            state_dir=tmp_path / "state",
            config_dir=cdir,
            log_path=tmp_path / "guard.log",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "DISABLED" in out


def test_menubar_icon_states_defined() -> None:
    assert set(ICON_STATE_TITLES.keys()) == {"awake", "normal", "off", "problem"}
