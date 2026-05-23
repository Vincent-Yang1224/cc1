#!/usr/bin/env python3
"""Pomodoro Timer - Desktop focus timer for Windows. Zero dependencies, stdlib only."""

import tkinter as tk
from tkinter import ttk, font
import threading
import queue
import time
import ctypes
from ctypes import wintypes
import sys
import atexit
import os
import winsound

# ============================================================================
# CONSTANTS
# ============================================================================

WORK_MINUTES = 25
SHORT_BREAK_MINUTES = 5
LONG_BREAK_MINUTES = 15
WORK_SESSIONS_BEFORE_LONG_BREAK = 4

# Color palette (Catppuccin Mocha inspired dark theme)
BG = "#1e1e2e"
SURFACE = "#313244"
TEXT = "#cdd6f4"
SUBTEXT = "#a6adc8"
ACCENT = "#cba6f7"
WORK_COLOR = "#f38ba8"
BREAK_COLOR = "#a6e3a1"
BUTTON_BG = "#45475a"
BUTTON_HOVER = "#585b70"
PROGRESS_BG = "#45475a"

# Win32 constants
WM_TRAY_CALLBACK = 0x8001
NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 1
NIF_ICON = 2
NIF_TIP = 4
NIF_INFO = 16
NIIF_INFO = 1
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONUP = 0x0205
TPM_LEFTALIGN = 0
TPM_TOPALIGN = 0
TPM_RIGHTBUTTON = 2
MF_STRING = 0
MF_SEPARATOR = 0x800
IDI_APPLICATION = 32512
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
GWL_WNDPROC = -4

# Context menu IDs
IDM_SHOW = 1001
IDM_START = 1002
IDM_RESET = 1003
IDM_EXIT = 1004


# ============================================================================
# WIN32 HELPERS
# ============================================================================

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


# ============================================================================
# POMODORO TIMER ENGINE
# ============================================================================

class PomodoroTimer:
    """Pure timer logic with background daemon thread. Thread-safe."""

    IDLE = 0
    RUNNING = 1
    PAUSED = 2

    def __init__(self):
        self._work_sec = WORK_MINUTES * 60
        self._short_break_sec = SHORT_BREAK_MINUTES * 60
        self._long_break_sec = LONG_BREAK_MINUTES * 60

        # Session cycle: 4×(Work→ShortBreak) then Work→LongBreak
        self._sessions = []
        for i in range(WORK_SESSIONS_BEFORE_LONG_BREAK - 1):
            self._sessions.append(("work", self._work_sec))
            self._sessions.append(("short_break", self._short_break_sec))
        self._sessions.append(("work", self._work_sec))
        self._sessions.append(("long_break", self._long_break_sec))

        self._session_index = 0
        self._session_type, self._session_duration = self._sessions[0]
        self._remaining = self._session_duration
        self._completed_count = 0
        self._state = self.IDLE

        self._lock = threading.Lock()
        self._pause_event = threading.Event()
        self._stop_event = threading.Event()
        self._queue = queue.Queue()
        self._thread = None
        self._thread_started = False
        self._on_complete = None

    # --- Public API ---

    def set_on_complete(self, callback):
        self._on_complete = callback

    @property
    def queue(self):
        return self._queue

    def start(self):
        with self._lock:
            if self._state == self.RUNNING:
                return
            self._state = self.RUNNING
            self._pause_event.set()
            if not self._thread_started:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
                self._thread_started = True

    def pause(self):
        with self._lock:
            if self._state != self.RUNNING:
                return
            self._state = self.PAUSED
            self._pause_event.clear()

    def reset(self):
        with self._lock:
            self._state = self.IDLE
            self._pause_event.clear()
            self._remaining = self._session_duration
            self._queue.put(("tick", self._remaining))

    def get_remaining(self):
        return self._remaining

    def get_session_type(self):
        return self._session_type

    def get_completed_count(self):
        return self._completed_count

    def get_state(self):
        return self._state

    def get_total_duration(self):
        return self._session_duration

    def shutdown(self):
        self._stop_event.set()
        self._pause_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)

    # --- Internal ---

    def _run(self):
        while not self._stop_event.is_set():
            self._pause_event.wait()
            if self._stop_event.is_set():
                break
            time.sleep(1)
            if self._stop_event.is_set():
                break
            with self._lock:
                if self._state == self.RUNNING:
                    self._remaining -= 1
                    self._queue.put(("tick", self._remaining))
                    if self._remaining <= 0:
                        self._state = self.IDLE
                        self._pause_event.clear()
                        prev_type = self._session_type
                        if prev_type == "work":
                            self._completed_count += 1
                        self._advance_session()
                        self._queue.put(("complete", prev_type, self._completed_count))
                        if self._on_complete:
                            self._on_complete(prev_type)

    def _advance_session(self):
        self._session_index = (self._session_index + 1) % len(self._sessions)
        self._session_type, self._session_duration = self._sessions[self._session_index]
        self._remaining = self._session_duration


# ============================================================================
# SYSTEM TRAY
# ============================================================================

# Set up Win32 function signatures used by SystemTray
_ctypes_setup_done = False


def _setup_win32_signatures():
    global _ctypes_setup_done
    if _ctypes_setup_done:
        return
    ctypes.windll.user32.SetWindowLongPtrW.argtypes = [
        wintypes.HWND, ctypes.c_int, ctypes.c_longlong
    ]
    ctypes.windll.user32.SetWindowLongPtrW.restype = ctypes.c_longlong
    ctypes.windll.user32.GetWindowLongPtrW.argtypes = [
        wintypes.HWND, ctypes.c_int
    ]
    ctypes.windll.user32.GetWindowLongPtrW.restype = ctypes.c_longlong
    ctypes.windll.user32.CallWindowProcW.argtypes = [
        ctypes.c_longlong, ctypes.c_longlong, ctypes.c_uint,
        ctypes.c_ulonglong, ctypes.c_longlong
    ]
    ctypes.windll.user32.CallWindowProcW.restype = ctypes.c_longlong
    _ctypes_setup_done = True


class SystemTray:
    """Windows system tray icon with context menu and balloon notifications."""

    def __init__(self, app):
        self._app = app
        self._hwnd = None
        self._icon_added = False
        self._nid = None
        self._original_wndproc = None
        self._subclass_ref = None

    def setup(self, hwnd):
        self._hwnd = hwnd
        _setup_win32_signatures()
        self._add_icon()
        self._subclass_window()

    def _add_icon(self):
        hicon = ctypes.windll.user32.LoadIconW(0, ctypes.cast(IDI_APPLICATION, ctypes.c_wchar_p))

        self._nid = NOTIFYICONDATAW()
        self._nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        self._nid.hWnd = self._hwnd
        self._nid.uID = 1
        self._nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
        self._nid.uCallbackMessage = WM_TRAY_CALLBACK
        self._nid.hIcon = hicon
        self._nid.szTip = "Pomodoro Timer"

        ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid))
        self._icon_added = True

    def _subclass_window(self):
        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, ctypes.c_longlong, ctypes.c_uint,
            ctypes.c_ulonglong, ctypes.c_longlong
        )

        @WNDPROC
        def proc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY_CALLBACK:
                if lparam == WM_LBUTTONDOWN:
                    self._app.restore_window()
                elif lparam == WM_RBUTTONUP:
                    self._show_menu()
                return 0
            return ctypes.windll.user32.CallWindowProcW(
                self._original_wndproc, hwnd, msg, wparam, lparam
            )

        self._subclass_ref = proc
        proc_addr = ctypes.cast(proc, ctypes.c_void_p).value
        self._original_wndproc = ctypes.windll.user32.SetWindowLongPtrW(
            self._hwnd, GWL_WNDPROC, proc_addr
        )

    def _show_menu(self):
        hmenu = ctypes.windll.user32.CreatePopupMenu()

        state = self._app.timer.get_state()
        if state == PomodoroTimer.RUNNING:
            start_text = "Pause"
        elif state == PomodoroTimer.PAUSED:
            start_text = "Resume"
        else:
            start_text = "Start"

        ctypes.windll.user32.AppendMenuW(hmenu, MF_STRING, IDM_SHOW, "Show Window")
        ctypes.windll.user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, "")
        ctypes.windll.user32.AppendMenuW(hmenu, MF_STRING, IDM_START, start_text)
        ctypes.windll.user32.AppendMenuW(hmenu, MF_STRING, IDM_RESET, "Reset")
        ctypes.windll.user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, "")
        ctypes.windll.user32.AppendMenuW(hmenu, MF_STRING, IDM_EXIT, "Exit")

        pt = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))

        cmd = ctypes.windll.user32.TrackPopupMenu(
            hmenu, TPM_LEFTALIGN | TPM_TOPALIGN | TPM_RIGHTBUTTON,
            pt.x, pt.y, 0, self._hwnd, 0
        )

        ctypes.windll.user32.DestroyMenu(hmenu)

        if cmd == IDM_SHOW:
            self._app.restore_window()
        elif cmd == IDM_START:
            self._app.toggle_timer()
        elif cmd == IDM_RESET:
            self._app.reset_timer()
        elif cmd == IDM_EXIT:
            self._app.quit_app()

    def show_notification(self, title, message):
        if not self._icon_added:
            return
        self._nid.uFlags = NIF_INFO
        self._nid.szInfoTitle = title
        self._nid.szInfo = message
        self._nid.dwInfoFlags = NIIF_INFO
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    def update_tooltip(self, text):
        if not self._icon_added:
            return
        self._nid.uFlags = NIF_TIP
        self._nid.szTip = text
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    def remove_icon(self):
        if self._icon_added and self._nid:
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._icon_added = False

    def destroy(self):
        """Restore original wndproc, remove icon, prepare for shutdown."""
        self.remove_icon()
        if self._original_wndproc is not None and self._hwnd is not None:
            try:
                ctypes.windll.user32.SetWindowLongPtrW(
                    self._hwnd, GWL_WNDPROC, self._original_wndproc
                )
            except Exception:
                pass
            self._original_wndproc = None
            self._subclass_ref = None


# ============================================================================
# MAIN APPLICATION GUI
# ============================================================================

class PomodoroApp:
    """Main tkinter GUI for the Pomodoro timer."""

    def __init__(self):
        self.timer = PomodoroTimer()
        self.tray = SystemTray(self)
        self._muted = False
        self._always_on_top = False
        self._setup_ui()
        self._setup_tray()
        self._start_polling()

    # --- UI Setup ---

    def _setup_ui(self):
        self.root = tk.Tk()
        self.root.title("Pomodoro Timer")
        self.root.geometry("380x460")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        # Center on screen
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - 380) // 2
        y = (sh - 460) // 2
        self.root.geometry(f"+{x}+{y}")

        # Close → minimize to tray
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Configure ttk style
        self._setup_style()

        # Build layout
        self._build_widgets()

    def _setup_style(self):
        style = ttk.Style()
        style.theme_use("clam")

        # Try to use Segoe UI (Windows 11 system font), fall back to default
        available_fonts = font.families()
        self._ui_font = "Segoe UI" if "Segoe UI" in available_fonts else "TkDefaultFont"

        style.configure(".", background=BG, foreground=TEXT, font=(self._ui_font, 10))
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Subtext.TLabel", foreground=SUBTEXT, font=(self._ui_font, 9))
        style.configure("Counter.TLabel", foreground=SUBTEXT, font=(self._ui_font, 10))
        style.configure("Session.TLabel", font=(self._ui_font, 12, "bold"))

        style.configure(
            "Start.TButton",
            background=ACCENT, foreground=BG, font=(self._ui_font, 13, "bold"),
            borderwidth=0, padding=(30, 12)
        )
        style.map("Start.TButton", background=[("active", "#ddb6f9")])

        style.configure(
            "Pause.TButton",
            background="#f9e2af", foreground=BG, font=(self._ui_font, 13, "bold"),
            borderwidth=0, padding=(30, 12)
        )
        style.map("Pause.TButton", background=[("active", "#fce9c0")])

        style.configure(
            "Secondary.TButton",
            background=BUTTON_BG, foreground=TEXT, font=(self._ui_font, 10),
            borderwidth=0, padding=(12, 6)
        )
        style.map("Secondary.TButton", background=[("active", BUTTON_HOVER)])

        style.configure(
            "TCheckbutton",
            background=BG, foreground=SUBTEXT, font=(self._ui_font, 9)
        )
        style.map("TCheckbutton", background=[("active", BG)])

        # Progress bar
        style.configure(
            "TProgressbar",
            background=WORK_COLOR, troughcolor=PROGRESS_BG,
            borderwidth=0, lightcolor=WORK_COLOR, darkcolor=WORK_COLOR
        )

    def _build_widgets(self):
        # Title
        title_frame = tk.Frame(self.root, bg=BG)
        title_frame.pack(pady=(24, 6))
        tk.Label(
            title_frame, text="Pomodoro", bg=BG, fg=TEXT,
            font=(self._ui_font, 16, "bold")
        ).pack()

        # Session counter
        self._counter_label = tk.Label(
            self.root, text="", bg=BG, fg=SUBTEXT,
            font=(self._ui_font, 10)
        )
        self._counter_label.pack(pady=(0, 12))

        # Timer display (large MM:SS)
        timer_frame = tk.Frame(self.root, bg=SURFACE, highlightthickness=0)
        timer_frame.pack(padx=30, pady=(0, 6), fill="x")

        self._timer_label = tk.Label(
            timer_frame, text="25:00", bg=SURFACE, fg=TEXT,
            font=(self._ui_font, 72, "bold")
        )
        self._timer_label.pack(pady=(20, 4))

        # Session type label
        self._session_label = tk.Label(
            timer_frame, text="Focus", bg=SURFACE, fg=WORK_COLOR,
            font=(self._ui_font, 13, "bold")
        )
        self._session_label.pack(pady=(0, 20))

        # Progress bar
        self._progress = ttk.Progressbar(
            self.root, style="TProgressbar", length=320, mode="determinate"
        )
        self._progress["maximum"] = 100
        self._progress["value"] = 100
        self._progress.pack(pady=(0, 16))

        # Main control button
        self._main_btn = ttk.Button(
            self.root, text="Start", style="Start.TButton",
            command=self.toggle_timer, width=20
        )
        self._main_btn.pack(pady=(0, 10))

        # Secondary buttons row
        btn_row = tk.Frame(self.root, bg=BG)
        btn_row.pack(pady=(0, 12))

        self._reset_btn = ttk.Button(
            btn_row, text="Reset", style="Secondary.TButton",
            command=self.reset_timer
        )
        self._reset_btn.pack(side="left", padx=4)

        self._mute_btn = ttk.Button(
            btn_row, text="Mute", style="Secondary.TButton",
            command=self.toggle_mute
        )
        self._mute_btn.pack(side="left", padx=4)

        # Always on top checkbox
        self._topmost_var = tk.BooleanVar(value=False)
        self._topmost_cb = ttk.Checkbutton(
            self.root, text="Always on top", variable=self._topmost_var,
            style="TCheckbutton", command=self._toggle_always_on_top
        )
        self._topmost_cb.pack(pady=(0, 12))

        # Tray hint
        tk.Label(
            self.root, text="Close to system tray", bg=BG, fg=SUBTEXT,
            font=(self._ui_font, 8)
        ).pack(side="bottom", pady=(0, 16))

    # --- Tray setup ---

    def _setup_tray(self):
        hwnd = self.root.winfo_id()
        self.tray.setup(hwnd)
        atexit.register(self._cleanup)

    def _cleanup(self):
        self.timer.shutdown()
        self.tray.destroy()

    # --- Queue polling ---

    def _start_polling(self):
        self._poll_queue()

    def _poll_queue(self):
        try:
            while True:
                msg = self.timer.queue.get_nowait()
                if msg[0] == "tick":
                    self._update_display(msg[1])
                elif msg[0] == "complete":
                    self._on_session_complete(msg[1], msg[2])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    # --- Display updates ---

    def _update_display(self, remaining):
        mm = remaining // 60
        ss = remaining % 60
        self._timer_label.config(text=f"{mm:02d}:{ss:02d}")

        session_type = self.timer.get_session_type()
        total = self.timer.get_total_duration()
        ratio = (remaining / total * 100) if total > 0 else 0
        self._progress["value"] = ratio

        if session_type == "work":
            self._session_label.config(text="Focus", fg=WORK_COLOR)
            self._set_progress_color(WORK_COLOR)
        elif session_type == "short_break":
            self._session_label.config(text="Short Break", fg=BREAK_COLOR)
            self._set_progress_color(BREAK_COLOR)
        else:
            self._session_label.config(text="Long Break", fg=BREAK_COLOR)
            self._set_progress_color(BREAK_COLOR)

        # Update tray tooltip
        self.tray.update_tooltip(
            f"Pomodoro Timer - {mm:02d}:{ss:02d} ({self._session_label.cget('text')})"
        )

    def _set_progress_color(self, color):
        style = ttk.Style()
        style.configure("TProgressbar", background=color, lightcolor=color, darkcolor=color)

    def _on_session_complete(self, prev_type, count):
        self._update_display(self.timer.get_remaining())
        self._update_counter_display()
        self._update_button_state()

        if prev_type == "work":
            self.tray.show_notification("Focus Session Complete!", "Time for a break.")
        else:
            self.tray.show_notification("Break Over!", "Time to focus.")

        if not self._muted:
            threading.Thread(target=self._play_complete_sound, args=(prev_type,), daemon=True).start()

    def _update_counter_display(self):
        count = self.timer.get_completed_count()
        if count == 0:
            self._counter_label.config(text="")
        elif count == 1:
            self._counter_label.config(text="1 pomodoro completed")
        else:
            self._counter_label.config(text=f"{count} pomodoros completed")

    def _update_button_state(self):
        state = self.timer.get_state()
        if state == PomodoroTimer.RUNNING:
            self._main_btn.config(text="Pause", style="Pause.TButton")
        elif state == PomodoroTimer.PAUSED:
            self._main_btn.config(text="Resume", style="Start.TButton")
        else:
            self._main_btn.config(text="Start", style="Start.TButton")

    # --- Button handlers ---

    def toggle_timer(self):
        state = self.timer.get_state()
        if state == PomodoroTimer.RUNNING:
            self.timer.pause()
        else:
            self.timer.start()
        self._update_button_state()

    def reset_timer(self):
        self.timer.reset()
        self._update_button_state()
        self._update_display(self.timer.get_remaining())

    def toggle_mute(self):
        self._muted = not self._muted
        self._mute_btn.config(text="Unmute" if self._muted else "Mute")

    def _toggle_always_on_top(self):
        self._always_on_top = self._topmost_var.get()
        hwnd = self.root.winfo_id()
        if self._always_on_top:
            ctypes.windll.user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE
            )
        else:
            ctypes.windll.user32.SetWindowPos(
                hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE
            )

    # --- Sound ---

    def _play_complete_sound(self, prev_type):
        if prev_type == "work":
            for _ in range(3):
                winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC)
                time.sleep(0.18)
        else:
            for _ in range(2):
                winsound.PlaySound("SystemDefault", winsound.SND_ALIAS | winsound.SND_ASYNC)
                time.sleep(0.18)

    # --- Window management ---

    def _on_close(self):
        self.root.withdraw()

    def restore_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit_app(self):
        self.timer.shutdown()
        self.tray.destroy()
        self.root.destroy()


# ============================================================================
# ENTRY POINT
# ============================================================================

def main():
    # Windows only: prevent multiple instances from messing up tray icon
    if sys.platform != "win32":
        print("This application requires Windows.", file=sys.stderr)
        sys.exit(1)

    app = PomodoroApp()
    app.root.mainloop()


if __name__ == "__main__":
    main()
