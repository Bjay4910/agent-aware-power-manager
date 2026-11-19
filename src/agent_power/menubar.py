"""macOS menu-bar application built with rumps.

Icon states: awake, normal, off, problem.
Menu structure:
- Mode: Automatic, Hold 2 hours, Off
- "Only while plugged in" toggle
- "Sleep now" (releases hold, then sleeps)
- "What happened" submenu (last 5 events in plain words)
- "Using the battery" submenu (on-demand 0.7s psutil CPU scan + Quit with confirmation)
- "Quit" (releases hold, then exits)

Idle CPU is kept well under 1% by running only lightweight file/pmset checks on
the 5-second timer and running the 0.7s process CPU sampler only on user click.
"""

from __future__ import annotations

from typing import Any
import rumps

from agent_power.battery_viewer import (
    BatteryProcessInfo,
    quit_user_process,
    sample_battery_processes,
)
from agent_power.engine import LOOP_INTERVAL_SECONDS, EngineTickResult, PowerEngine
from agent_power.policy import Mode

ICON_STATE_TITLES: dict[str, str] = {
    "awake": "⚡",
    "normal": "🌙",
    "off": "⏸",
    "problem": "⚠️",
}


class AgentPowerMenuBarApp(rumps.App):  # type: ignore[misc]
    """macOS status bar app for Agent-Aware Power Manager."""

    def __init__(self, engine: PowerEngine | None = None) -> None:
        super().__init__(
            name="Agent Power",
            title=ICON_STATE_TITLES["normal"],
            quit_button=None,
        )
        self.engine = engine or PowerEngine()
        self.engine.install_signal_handlers()

        # Status item (disabled, shows state in words)
        self.status_item = rumps.MenuItem("Status: Normal", callback=None)

        # Mode items
        self.mode_auto_item = rumps.MenuItem("Automatic", callback=self._on_mode_auto)
        self.mode_hold_item = rumps.MenuItem("Hold 2 hours", callback=self._on_mode_hold)
        self.mode_off_item = rumps.MenuItem("Off", callback=self._on_mode_off)

        # Toggle & actions
        self.plugged_in_item = rumps.MenuItem(
            "Only while plugged in",
            callback=self._on_toggle_plugged_in,
        )
        self.sleep_now_item = rumps.MenuItem("Sleep now", callback=self._on_sleep_now)

        # Submenus
        self.what_happened_menu = rumps.MenuItem("What happened")
        self.what_happened_menu.add(rumps.MenuItem("No events recorded yet"))

        self.using_battery_menu = rumps.MenuItem("Using the battery")
        self.refresh_battery_item = rumps.MenuItem(
            "Refresh high-CPU processes (>5%)...",
            callback=self._on_refresh_battery_processes,
        )
        self.using_battery_menu.add(self.refresh_battery_item)

        self.quit_item = rumps.MenuItem("Quit", callback=self._on_quit)

        self.menu = [
            self.status_item,
            None,
            self.mode_auto_item,
            self.mode_hold_item,
            self.mode_off_item,
            None,
            self.plugged_in_item,
            self.sleep_now_item,
            None,
            self.what_happened_menu,
            self.using_battery_menu,
            None,
            self.quit_item,
        ]

        self._timer = rumps.Timer(self._on_timer_tick, LOOP_INTERVAL_SECONDS)
        self._sync_ui_from_engine(self.engine.step())
        self._timer.start()

    def _sync_ui_from_engine(self, tick: EngineTickResult) -> None:
        self.title = ICON_STATE_TITLES.get(tick.icon_state, ICON_STATE_TITLES["normal"])
        self.status_item.title = f"Status: {tick.icon_state.capitalize()}"

        self.mode_auto_item.state = int(tick.mode is Mode.AUTOMATIC)
        self.mode_hold_item.state = int(tick.mode is Mode.HOLD_2H)
        self.mode_off_item.state = int(tick.mode is Mode.OFF)
        self.plugged_in_item.state = int(tick.only_while_plugged_in)

        self._rebuild_what_happened(tick.events)

    def _rebuild_what_happened(self, events: list[str]) -> None:
        if hasattr(self.what_happened_menu, "clear"):
            self.what_happened_menu.clear()
        last_five = events[-5:]
        if not last_five:
            self.what_happened_menu.add(rumps.MenuItem("No events recorded yet"))
            return
        for entry in reversed(last_five):
            self.what_happened_menu.add(rumps.MenuItem(entry))

    def _on_timer_tick(self, _sender: Any) -> None:
        tick = self.engine.step()
        self._sync_ui_from_engine(tick)

    def _on_mode_auto(self, _sender: Any) -> None:
        tick = self.engine.set_mode(Mode.AUTOMATIC)
        self._sync_ui_from_engine(tick)

    def _on_mode_hold(self, _sender: Any) -> None:
        tick = self.engine.set_mode(Mode.HOLD_2H)
        self._sync_ui_from_engine(tick)

    def _on_mode_off(self, _sender: Any) -> None:
        tick = self.engine.set_mode(Mode.OFF)
        self._sync_ui_from_engine(tick)

    def _on_toggle_plugged_in(self, _sender: Any) -> None:
        new_val = not self.engine.settings.only_while_plugged_in
        tick = self.engine.set_only_while_plugged_in(new_val)
        self._sync_ui_from_engine(tick)

    def _on_sleep_now(self, _sender: Any) -> None:
        tick = self.engine.request_sleep_now()
        self._sync_ui_from_engine(tick)

    def _on_refresh_battery_processes(self, _sender: Any) -> None:
        procs = sample_battery_processes()
        if hasattr(self.using_battery_menu, "clear"):
            self.using_battery_menu.clear()
        self.using_battery_menu.add(self.refresh_battery_item)

        if not procs:
            self.using_battery_menu.add(
                rumps.MenuItem("No user processes currently above 5% CPU")
            )
            return

        for proc_info in procs:
            sub = rumps.MenuItem(
                f"{proc_info.what_it_is} — {proc_info.cpu_percent:.1f}% CPU"
            )
            ports_desc = (
                ", ".join(str(p) for p in proc_info.listening_ports)
                if proc_info.listening_ports
                else "none"
            )
            sub.add(rumps.MenuItem(f"Folder: {proc_info.working_folder}"))
            sub.add(rumps.MenuItem(f"Listening ports: {ports_desc}"))
            sub.add(rumps.MenuItem(f"Started by: {proc_info.started_by}"))
            quit_action = rumps.MenuItem(
                f"Quit {proc_info.what_it_is} (PID {proc_info.pid})...",
                callback=self._make_quit_process_callback(proc_info),
            )
            sub.add(quit_action)
            self.using_battery_menu.add(sub)

    def _make_quit_process_callback(self, proc_info: BatteryProcessInfo) -> Any:
        def _callback(_sender: Any) -> None:
            def _confirm(info: BatteryProcessInfo) -> bool:
                response = rumps.alert(
                    title=f"Quit {info.what_it_is}?",
                    message=(
                        f"{info.summary}\n\n"
                        "This will send SIGTERM first, and SIGKILL after 5 seconds "
                        "if the process does not exit."
                    ),
                    ok="Quit Process",
                    cancel="Cancel",
                )
                return int(response) == 1

            res = quit_user_process(proc_info, confirm_fn=_confirm)
            self.engine.record_event(res.reason)
            self._rebuild_what_happened(self.engine.events)

        return _callback

    def _on_quit(self, _sender: Any) -> None:
        self._timer.stop()
        self.engine.shutdown("Released sleep hold: user quit menu bar app")
        rumps.quit_application()


def run_menubar_app() -> None:
    """Launch the macOS menu bar application."""
    app = AgentPowerMenuBarApp()
    app.run()


if __name__ == "__main__":
    run_menubar_app()
