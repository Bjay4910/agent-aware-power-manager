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

import ctypes
import ctypes.util
from dataclasses import dataclass
import time
from typing import Any, Callable
import rumps

from agent_power.battery_viewer import (
    BatteryProcessInfo,
    quit_user_process,
    sample_battery_processes,
)
from agent_power.engine import LOOP_INTERVAL_SECONDS, EngineTickResult, PowerEngine
from agent_power.policy import Mode
from agent_power.runaway_guard import send_macos_notification

ICON_STATE_TITLES: dict[str, str] = {
    "awake": "⚡",
    "normal": "🌙",
    "off": "⏸",
    "problem": "⚠️",
}

HIDDEN_MIN_CONSECUTIVE_CHECKS: int = 3
HIDDEN_MIN_SPAN_SECONDS: float = 30.0
HIDDEN_ICON_NOTIFICATION_TITLE: str = "Agent Power"
HIDDEN_ICON_NOTIFICATION_BODY: str = (
    "Agent Power's icon is hidden by the menu bar. Free up space or use More Space."
)
_NS_WINDOW_OCCLUSION_STATE_VISIBLE: int = 1 << 1  # NSWindowOcclusionStateVisible


def read_status_item_visible(nsstatusitem: Any) -> bool | None:
    """Return True if an NSStatusItem's window is visible, False if occluded, None if unavailable."""
    if nsstatusitem is None:
        return None
    try:
        button_fn = getattr(nsstatusitem, "button", None)
        if not callable(button_fn):
            return None
        button = button_fn()
        if button is None:
            return None
        window_fn = getattr(button, "window", None)
        if not callable(window_fn):
            return None
        window = window_fn()
        if window is None:
            return None
        occlusion_fn = getattr(window, "occlusionState", None)
        if not callable(occlusion_fn):
            return None
        occlusion_state = int(occlusion_fn())
    except Exception:
        return None

    visible_mask = _NS_WINDOW_OCCLUSION_STATE_VISIBLE
    try:
        import AppKit

        visible_mask = int(getattr(AppKit, "NSWindowOcclusionStateVisible", visible_mask))
    except Exception:
        pass
    return bool(occlusion_state & visible_mask)


def is_screen_locked() -> bool:
    """Return True if the macOS GUI session screen is locked or switched away from console."""
    try:
        import objc

        cg_path = ctypes.util.find_library("CoreGraphics")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not cg_path or not cf_path:
            return False
        cg = ctypes.CDLL(cg_path)
        cf = ctypes.CDLL(cf_path)
        cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        ptr = cg.CGSessionCopyCurrentDictionary()
        if not ptr:
            return False
        try:
            session_dict = objc.objc_object(c_void_p=ptr)
            locked = bool(session_dict.get("CGSSessionScreenIsLocked", False))
            on_console = session_dict.get("kCGSSessionOnConsoleKey", True)
            del session_dict
            return locked or (on_console is False)
        finally:
            cf.CFRelease(ptr)
    except Exception:
        return False


def is_display_asleep() -> bool:
    """Return True if the main macOS display is currently asleep."""
    try:
        cg_path = ctypes.util.find_library("CoreGraphics")
        if not cg_path:
            return False
        cg = ctypes.CDLL(cg_path)
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayIsAsleep.argtypes = [ctypes.c_uint32]
        cg.CGDisplayIsAsleep.restype = ctypes.c_bool
        return bool(cg.CGDisplayIsAsleep(cg.CGMainDisplayID()))
    except Exception:
        return False


def is_menubar_hidden_or_fullscreen() -> bool:
    """Return True if the macOS menu bar is hidden (e.g. native full-screen or auto-hide)."""
    try:
        import AppKit

        return not bool(AppKit.NSMenu.menuBarVisible())
    except Exception:
        return False


@dataclass(frozen=True)
class IconVisibilityCheckResult:
    """Result of a single menu bar icon visibility evaluation."""

    visible: bool | None
    raw_visible: bool | None
    skipped_reason: str | None
    confirmed_hidden: bool
    notified: bool
    checked_at: float


class IconVisibilityMonitor:
    """Tracks menu bar icon visibility with anti-false-alarm hysteresis and one-time alert latch.

    Avoids false alarms by:
    - Skipping checks while the screen is locked, the system/display is asleep, or in full-screen.
    - Requiring at least `min_consecutive_checks` (3) consecutive not-visible checks spanning
      at least `min_span_seconds` (30s) before declaring the icon hidden.
    - Sending the hidden-icon notification once and latching until the icon is seen visible again.
    """

    def __init__(
        self,
        *,
        read_visible_fn: Callable[[], bool | None],
        is_screen_locked_fn: Callable[[], bool] = is_screen_locked,
        is_system_asleep_fn: Callable[[], bool] = is_display_asleep,
        is_fullscreen_fn: Callable[[], bool] = is_menubar_hidden_or_fullscreen,
        notify_fn: Callable[[str, str], bool] = send_macos_notification,
        clock: Callable[[], float] = time.time,
        min_consecutive_checks: int = HIDDEN_MIN_CONSECUTIVE_CHECKS,
        min_span_seconds: float = HIDDEN_MIN_SPAN_SECONDS,
    ) -> None:
        self._read_visible = read_visible_fn
        self._is_screen_locked = is_screen_locked_fn
        self._is_system_asleep = is_system_asleep_fn
        self._is_fullscreen = is_fullscreen_fn
        self._notify = notify_fn
        self._clock = clock
        self._min_consecutive_checks = max(1, int(min_consecutive_checks))
        self._min_span_seconds = max(0.0, float(min_span_seconds))

        self.last_visible: bool | None = None
        self.last_checked_at: float | None = None
        self._consecutive_hidden: int = 0
        self._first_hidden_at: float | None = None
        self._notified_hidden: bool = False

    def _reset_hidden_streak(self) -> None:
        self._consecutive_hidden = 0
        self._first_hidden_at = None

    def check(self) -> IconVisibilityCheckResult:
        """Run one visibility check and notify once if hidden is confirmed."""
        now = float(self._clock())

        if self._is_screen_locked():
            self._reset_hidden_streak()
            return IconVisibilityCheckResult(
                visible=self.last_visible,
                raw_visible=None,
                skipped_reason="screen_locked",
                confirmed_hidden=False,
                notified=False,
                checked_at=now,
            )

        if self._is_system_asleep():
            self._reset_hidden_streak()
            return IconVisibilityCheckResult(
                visible=self.last_visible,
                raw_visible=None,
                skipped_reason="system_asleep",
                confirmed_hidden=False,
                notified=False,
                checked_at=now,
            )

        if self._is_fullscreen():
            self._reset_hidden_streak()
            return IconVisibilityCheckResult(
                visible=self.last_visible,
                raw_visible=None,
                skipped_reason="fullscreen",
                confirmed_hidden=False,
                notified=False,
                checked_at=now,
            )

        raw_visible = self._read_visible()
        if raw_visible is None:
            return IconVisibilityCheckResult(
                visible=self.last_visible,
                raw_visible=None,
                skipped_reason="unavailable",
                confirmed_hidden=False,
                notified=False,
                checked_at=now,
            )

        self.last_checked_at = now
        if raw_visible:
            self._reset_hidden_streak()
            self._notified_hidden = False
            self.last_visible = True
            return IconVisibilityCheckResult(
                visible=True,
                raw_visible=True,
                skipped_reason=None,
                confirmed_hidden=False,
                notified=False,
                checked_at=now,
            )

        # Icon reported not-visible
        if self._first_hidden_at is None:
            self._first_hidden_at = now
        self._consecutive_hidden += 1
        span = now - self._first_hidden_at

        confirmed = (
            self._consecutive_hidden >= self._min_consecutive_checks
            and span >= self._min_span_seconds
        )
        notified = False
        if confirmed:
            self.last_visible = False
            if not self._notified_hidden:
                self._notify(HIDDEN_ICON_NOTIFICATION_TITLE, HIDDEN_ICON_NOTIFICATION_BODY)
                self._notified_hidden = True
                notified = True

        return IconVisibilityCheckResult(
            visible=self.last_visible,
            raw_visible=False,
            skipped_reason=None,
            confirmed_hidden=confirmed,
            notified=notified,
            checked_at=now,
        )


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
        self._system_asleep: bool = False

        if hasattr(rumps, "events"):
            rumps.events.on_sleep.register(self._on_system_sleep)
            rumps.events.on_wake.register(self._on_system_wake)

        self._visibility_monitor = IconVisibilityMonitor(
            read_visible_fn=self._read_own_status_item_visible,
            is_screen_locked_fn=is_screen_locked,
            is_system_asleep_fn=self._is_system_asleep,
            is_fullscreen_fn=is_menubar_hidden_or_fullscreen,
            notify_fn=send_macos_notification,
        )

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

    def _on_system_sleep(self) -> None:
        self._system_asleep = True

    def _on_system_wake(self) -> None:
        self._system_asleep = False

    def _is_system_asleep(self) -> bool:
        return self._system_asleep or is_display_asleep()

    def _read_own_status_item_visible(self) -> bool | None:
        nsapp = getattr(self, "_nsapp", None)
        nsstatusitem = getattr(nsapp, "nsstatusitem", None)
        return read_status_item_visible(nsstatusitem)

    def _check_icon_visibility(self) -> None:
        vis = self._visibility_monitor.check()
        if vis.skipped_reason is None and vis.raw_visible is not None:
            self.engine.set_icon_visibility(vis.visible, checked_at=vis.checked_at)
        if vis.notified:
            self.engine.record_event("Menu bar icon appears hidden behind the notch or crowded menu bar")

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
        self._check_icon_visibility()
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
