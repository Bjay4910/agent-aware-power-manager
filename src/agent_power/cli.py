"""Command-line interface (`agent-power`) for Agent-Aware Power Manager.

Supported subcommands:
- status
- hold
- release
- sleep-now
- mode <auto|hold|off>
- guard-run
- guard-status
- menubar
- watchdog
- hook-event
- hook-heartbeat
- install-hooks (--print | --apply)
- uninstall-hooks
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from agent_power.engine import (
    PowerEngine,
    _atomic_write_json,
    get_state_dir,
    load_state_file,
)
from agent_power.hooks import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_HEARTBEAT_MAX_SECONDS,
    apply_install_hooks,
    apply_uninstall_hooks,
    format_install_hooks_print,
    handle_hook_event,
    run_heartbeat_loop,
)
from agent_power.policy import Mode
from agent_power.power import set_disable_sleep
from agent_power.runaway_guard import format_guard_status, run_guard_check
from agent_power.watchdog import run_watchdog_check


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser for `agent-power`."""
    parser = argparse.ArgumentParser(
        prog="agent-power",
        description=(
            "macOS menu-bar power manager that keeps your Mac awake with the lid "
            "closed while AI agents in a multi-agent room are working."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show current power hold, battery, and room agent status")
    subparsers.add_parser("hold", help="Activate a 2-hour sleep hold (subject to battery safety rules)")
    subparsers.add_parser("release", help="Release any active sleep hold immediately")
    subparsers.add_parser("sleep-now", help="Release any active sleep hold and put the Mac to sleep now")

    mode_parser = subparsers.add_parser("mode", help="Set operating mode (auto, hold, off)")
    mode_parser.add_argument(
        "target_mode",
        choices=["auto", "hold", "off"],
        help="Operating mode: auto (Automatic), hold (Hold 2 hours), or off (Off)",
    )

    subparsers.add_parser("guard-run", help="Run the runaway background script guard check once")
    subparsers.add_parser("guard-status", help="Show runaway guard status, tracked scripts, and recent log")
    subparsers.add_parser("menubar", help="Launch the macOS menu-bar application")
    subparsers.add_parser("watchdog", help="Run the 60-second watchdog check once")

    subparsers.add_parser("hook-event", help="Handle a Claude Code or Antigravity lifecycle hook event from stdin")

    hb_parser = subparsers.add_parser("hook-heartbeat", help="Run detached per-session presence heartbeat loop")
    hb_parser.add_argument("--file", required=True, help="Path to session presence JSON file")
    hb_parser.add_argument("--agent-pid", type=int, default=0, help="Agent PID to watch for liveness")
    hb_parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        help="Heartbeat refresh interval in seconds (default 30)",
    )
    hb_parser.add_argument(
        "--max-seconds",
        type=float,
        default=DEFAULT_HEARTBEAT_MAX_SECONDS,
        help="Maximum heartbeat lifetime in seconds (default 7200)",
    )

    install_parser = subparsers.add_parser(
        "install-hooks",
        help="Print (--print) or merge (--apply) lifecycle hooks for Claude Code and Antigravity",
    )
    install_group = install_parser.add_mutually_exclusive_group(required=False)
    install_group.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print exact JSON blocks for ~/.claude/settings.json and ~/.gemini/config/hooks.json (default)",
    )
    install_group.add_argument(
        "--apply",
        dest="apply_changes",
        action="store_true",
        help="Backup existing config files and merge agent-power hooks idempotently",
    )
    install_parser.add_argument("--claude-settings", type=Path, default=None, help="Override path to Claude settings.json")
    install_parser.add_argument("--agy-hooks", type=Path, default=None, help="Override path to Antigravity hooks.json")

    uninstall_parser = subparsers.add_parser(
        "uninstall-hooks",
        help="Backup and remove agent-power lifecycle hooks from Claude Code and Antigravity configs",
    )
    uninstall_parser.add_argument("--claude-settings", type=Path, default=None, help="Override path to Claude settings.json")
    uninstall_parser.add_argument("--agy-hooks", type=Path, default=None, help="Override path to Antigravity hooks.json")

    return parser


def _cmd_status(engine: PowerEngine) -> int:
    tick = engine.step()
    batt = tick.battery
    source = "AC Power" if batt.on_ac_power else "Battery Power"
    pct_str = f"{batt.percent}%" if batt.percent is not None else "n/a"

    print(f"Icon state:             {tick.icon_state}")
    print(f"Mode:                   {tick.mode.label} ({tick.mode.value})")
    print(f"Sleep hold active:      {'YES (disablesleep=1)' if tick.hold_active else 'NO (disablesleep=0)'}")
    print(f"Only while plugged in:  {'ON' if tick.only_while_plugged_in else 'OFF'}")
    print(f"Power source:           {source} ({pct_str}, {batt.charging_state})")
    print(f"Decision:               {tick.action.kind.value} — {tick.action.reason}")
    if tick.problem:
        print(f"Problem:                {tick.problem}")

    if tick.active_agents:
        print(f"Working agents ({len(tick.active_agents)}):")
        for agent in tick.active_agents:
            task_str = f" — '{agent.task}'" if agent.task else ""
            print(f"  - {agent.agent} (PID {agent.pid}, updated {agent.age_seconds:.0f}s ago){task_str}")
    else:
        print("Working agents:         none")

    recent = tick.events[-5:]
    if recent:
        print("What happened (last 5 events):")
        for ev in recent:
            print(f"  • {ev}")

    return 1 if tick.problem else 0


def _cmd_hold(engine: PowerEngine) -> int:
    tick = engine.set_mode(Mode.HOLD_2H)
    print(f"{tick.action.kind.value}: {tick.action.reason}")
    if tick.problem:
        print(f"Problem: {tick.problem}", file=sys.stderr)
        return 1
    return 0


def _cmd_release(engine: PowerEngine, state_dir: Path | str | None = None) -> int:
    res = set_disable_sleep(False, dry_run=engine.dry_run)
    resolved_dir = get_state_dir(state_dir or engine.state_dir)
    state = load_state_file(resolved_dir)
    engine.hold_active = False
    engine.hold_started_at = None
    if engine.mode is Mode.HOLD_2H:
        engine.mode = Mode.AUTOMATIC
    msg = "Released sleep hold via CLI"
    engine.record_event(msg)

    if state:
        state["hold_active"] = False
        state["hold_started_at"] = None
        state["mode"] = engine.mode.value
        state["mode_label"] = engine.mode.label
        state["icon_state"] = "problem" if not res.ok else ("off" if engine.mode is Mode.OFF else "normal")
        state["last_action"] = "RELEASE"
        state["last_reason"] = msg
        state["events"] = list(engine.events)
        _atomic_write_json(resolved_dir / "state.json", state)

    if not res.ok:
        print(f"Problem releasing sleep hold: {res.problem}", file=sys.stderr)
        return 1
    print(f"RELEASE: {msg}.")
    return 0


def _cmd_sleep_now(engine: PowerEngine) -> int:
    tick = engine.request_sleep_now()
    print(f"{tick.action.kind.value}: {tick.action.reason}")
    if tick.problem:
        print(f"Problem: {tick.problem}", file=sys.stderr)
        return 1
    return 0


def _cmd_mode(engine: PowerEngine, target_mode: str) -> int:
    tick = engine.set_mode(target_mode)
    print(f"Mode set to {tick.mode.label} ({tick.mode.value}).")
    print(f"{tick.action.kind.value}: {tick.action.reason}")
    if tick.problem:
        print(f"Problem: {tick.problem}", file=sys.stderr)
        return 1
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    engine: PowerEngine | None = None,
    state_dir: Path | str | None = None,
    config_dir: Path | str | None = None,
    log_path: Path | str | None = None,
) -> int:
    """CLI entry point for `agent-power`."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    cmd = args.command
    if cmd == "guard-run":
        res = run_guard_check(state_dir=state_dir, config_dir=config_dir, log_path=log_path)
        if res.disabled_by_kill_switch:
            print("Runaway guard is disabled via ~/.config/agent-power/guard_disabled.")
            return 0
        print(
            f"Guard check complete: tracked={len(res.tracked_pids)}, "
            f"actions={len(res.actions)}, auto_stop={res.auto_stop_enabled}"
        )
        for act in res.actions:
            print(f"  - {act.message}")
        return 0

    if cmd == "guard-status":
        print(format_guard_status(state_dir=state_dir, config_dir=config_dir, log_path=log_path))
        return 0

    if cmd == "watchdog":
        wres = run_watchdog_check(state_dir=state_dir)
        print(wres.reason)
        return 0

    if cmd == "menubar":
        from agent_power.menubar import run_menubar_app

        run_menubar_app()
        return 0

    if cmd == "hook-event":
        try:
            raw_stdin = sys.stdin.read()
        except Exception:
            raw_stdin = ""
        resp = handle_hook_event(raw_stdin)
        print(json.dumps(resp))
        return 0

    if cmd == "hook-heartbeat":
        return run_heartbeat_loop(
            Path(args.file).expanduser(),
            agent_pid=int(args.agent_pid),
            interval_seconds=float(args.interval),
            max_seconds=float(args.max_seconds),
        )

    if cmd == "install-hooks":
        if args.apply_changes:
            c_bak, a_bak = apply_install_hooks(
                claude_settings_path=args.claude_settings,
                agy_hooks_path=args.agy_hooks,
            )
            print("Merged agent-power lifecycle hooks into Claude Code and Antigravity configs.")
            if c_bak:
                print(f"  Claude backup:      {c_bak}")
            if a_bak:
                print(f"  Antigravity backup: {a_bak}")
            return 0
        print(
            format_install_hooks_print(
                claude_settings_path=args.claude_settings,
                agy_hooks_path=args.agy_hooks,
            )
        )
        return 0

    if cmd == "uninstall-hooks":
        c_bak, a_bak = apply_uninstall_hooks(
            claude_settings_path=args.claude_settings,
            agy_hooks_path=args.agy_hooks,
        )
        print("Removed agent-power lifecycle hooks from Claude Code and Antigravity configs.")
        if c_bak:
            print(f"  Claude backup:      {c_bak}")
        if a_bak:
            print(f"  Antigravity backup: {a_bak}")
        return 0

    eng = engine or PowerEngine(state_dir=state_dir)

    if cmd == "status":
        return _cmd_status(eng)
    if cmd == "hold":
        return _cmd_hold(eng)
    if cmd == "release":
        return _cmd_release(eng, state_dir=state_dir)
    if cmd == "sleep-now":
        return _cmd_sleep_now(eng)
    if cmd == "mode":
        return _cmd_mode(eng, args.target_mode)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
