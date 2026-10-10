r"""
Screenshotter
=============

Press F12 anywhere, drag a marquee over the part of the screen you want,
release, and the selection is saved as a PNG and copied to the clipboard.

    python screenshotter.py          (or pythonw screenshotter.py to hide the console)

Keys
    F12                 start a capture (spans every monitor)
    drag                select an area
    Esc / right-click   cancel the capture
    Ctrl+Shift+F12      quit Screenshotter (Ctrl+C in the console also works)

Hotkeys go through RegisterHotKey when Windows allows it. Windows reserves plain
F12 for debuggers (and other apps may own other keys), so keys it refuses are
picked up with a low-level keyboard hook instead. Either way, pick a different
key with --key if you prefer:

    python screenshotter.py --key f11
    python screenshotter.py --key ctrl+f12

Requires Windows and Pillow (pip install pillow). Nothing else.
"""

from __future__ import annotations

import ctypes
import datetime
import io
import queue
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

if sys.platform != "win32":
    sys.exit("Screenshotter uses Win32 APIs and only runs on Windows.")

# ------------------------------------------------------------------ settings --
CAPTURE_KEY = "f12"                            # global hotkey that starts a capture (override with --key)
QUIT_KEY = "ctrl+shift+f12"                    # global hotkey that quits (override with --quit-key)
SAVE_DIR = Path.home() / "Pictures" / "Screenshotter"
COPY_TO_CLIPBOARD = True
DIM = 0.35                                     # brightness outside the selection (0 = black, 1 = untouched)
ACCENT = "#4cc2ff"                             # marquee colour
MIN_SELECTION = 3                              # drags smaller than this (px) count as an accidental click
TOAST_MS = 2500                                # how long the "Saved" bubble stays up
POLL_MS = 40                                   # how often the Tk thread checks for hotkey presses
# ------------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def make_dpi_aware() -> None:
    """Put the process in per-monitor DPI mode so Tk's pixels are the screen's pixels.

    Pillow grabs the screen at physical resolution. Without this, a scaled
    display (125 %, 150 %...) would make Tk report shrunken coordinates and the
    marquee would not line up with what gets saved. Must run before tkinter
    creates its first window.
    """
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
    except AttributeError:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except (AttributeError, OSError):
        pass
    user32.SetProcessDPIAware()


make_dpi_aware()

import tkinter as tk  # noqa: E402  (imported after DPI setup on purpose)
from PIL import Image, ImageEnhance, ImageGrab, ImageTk  # noqa: E402

# ------------------------------------------------------------- Win32 plumbing --
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
ID_CAPTURE, ID_QUIT = 1, 2
CF_DIB = 8
GMEM_MOVEABLE = 0x0002
GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000
SPI_GETWORKAREA = 0x0030
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
user32.GetParent.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]

# SetWindowLongPtr only exists as an export on 64-bit Windows.
_GetWindowLong = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
_SetWindowLong = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
_GetWindowLong.restype = ctypes.c_void_p
_GetWindowLong.argtypes = [wintypes.HWND, ctypes.c_int]
_SetWindowLong.restype = ctypes.c_void_p
_SetWindowLong.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]


def virtual_screen() -> tuple[int, int, int, int]:
    """(left, top, width, height) of the rectangle spanning every monitor, in physical pixels."""
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
    return tuple(  # type: ignore[return-value]
        user32.GetSystemMetrics(i)
        for i in (SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN)
    )


def primary_screen() -> tuple[int, int]:
    """(width, height) of the primary monitor in physical pixels."""
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def work_area() -> wintypes.RECT:
    """Primary monitor minus the taskbar."""
    rect = wintypes.RECT()
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
    return rect


def dpi_scale() -> float:
    try:
        return user32.GetDpiForSystem() / 96.0
    except AttributeError:
        return 1.0


def hwnd_of(widget: tk.Misc) -> int:
    """The real top-level HWND of a Tk toplevel (Tk wraps its own window in a frame)."""
    inner = widget.winfo_id()
    return user32.GetParent(inner) or inner


def force_foreground(hwnd: int) -> bool:
    """Bring a window to the front even though this process didn't get the last keystroke.

    Windows only lets the process that owns the current input set the foreground
    window. When the hotkey arrives through the keyboard hook that isn't us, so
    we briefly attach our input queue to the foreground thread's, which is the
    long-standing way around the restriction.
    """
    current = user32.GetForegroundWindow()
    if current == hwnd:
        return True
    fg_thread = user32.GetWindowThreadProcessId(current, None) if current else 0
    our_thread = kernel32.GetCurrentThreadId()
    attached = bool(fg_thread) and fg_thread != our_thread and bool(user32.AttachThreadInput(our_thread, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(our_thread, fg_thread, False)
    return user32.GetForegroundWindow() == hwnd


def copy_image_to_clipboard(image: Image.Image) -> bool:
    """Put an image on the Windows clipboard as a device-independent bitmap."""
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "BMP")
    dib = buffer.getvalue()[14:]  # CF_DIB is a BMP file minus its 14-byte file header

    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
    if not handle:
        return False
    pointer = kernel32.GlobalLock(handle)
    ctypes.memmove(pointer, dib, len(dib))
    kernel32.GlobalUnlock(handle)

    for _ in range(10):  # another app may hold the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        kernel32.GlobalFree(handle)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_DIB, handle):
            kernel32.GlobalFree(handle)
            return False
        return True  # the clipboard now owns the memory
    finally:
        user32.CloseClipboard()


def unique_path(path: Path) -> Path:
    candidate, n = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


# ------------------------------------------------------------------- hotkeys --
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
MODIFIERS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "shift": MOD_SHIFT, "alt": MOD_ALT, "win": MOD_WIN}
KEY_CODES = {
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
    "print": 0x2C, "printscreen": 0x2C, "prtsc": 0x2C, "prtscn": 0x2C,
    "pause": 0x13, "scrolllock": 0x91, "scroll": 0x91, "numlock": 0x90, "capslock": 0x14,
    "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "space": 0x20, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+shift+f12' -> (RegisterHotKey modifier flags, virtual-key code)."""
    parts = [p.strip().lower() for p in spec.split("+")]
    if not parts or not all(parts):
        raise ValueError(f"invalid hotkey {spec!r}")
    *mod_names, key = parts
    mods = 0
    for name in mod_names:
        if name not in MODIFIERS:
            raise ValueError(f"unknown modifier {name!r} in hotkey {spec!r} (use ctrl, shift, alt, win)")
        mods |= MODIFIERS[name]
    if key in KEY_CODES:
        vk = KEY_CODES[key]
    elif len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key.upper())  # letters and digits map straight onto their virtual-key codes
    else:
        raise ValueError(f"unknown key {key!r} in hotkey {spec!r}")
    return mods, vk


class HotkeyListener(threading.Thread):
    """Owns the Win32 message loop that receives hotkey presses.

    Each binding is registered with RegisterHotKey when Windows allows it. Keys
    it refuses (plain F12 is reserved for debuggers; other programs may own
    other keys) are served by a low-level keyboard hook instead, which sees the
    key anyway and swallows it so the foreground app doesn't also react.

    Both mechanisms deliver to the thread that set them up, and Tk owns the main
    thread's message loop, so this runs on a thread of its own. Presses are
    handed to the Tk thread through a queue (Tk is not thread-safe).
    """

    def __init__(self, events: queue.Queue[int], capture_key: str = CAPTURE_KEY, quit_key: str = QUIT_KEY):
        super().__init__(daemon=True, name="hotkeys")
        self.events = events
        self.capture_key, self.quit_key = capture_key, quit_key
        # Parse up front so a bad key name fails loudly in the main thread.
        self.bindings = [(ID_CAPTURE, capture_key, *parse_hotkey(capture_key)),
                         (ID_QUIT, quit_key, *parse_hotkey(quit_key))]
        self.mechanism: dict[int, str] = {}        # hotkey id -> "system hotkey" | "keyboard hook"
        self.hooked: list[tuple[int, str, int, int]] = []
        self.ready = threading.Event()
        self.error: str | None = None
        self.thread_id: int | None = None
        self._hook: int | None = None
        self._hook_proc = None
        self._held: set[int] = set()

    def run(self) -> None:
        self.thread_id = kernel32.GetCurrentThreadId()
        registered: list[int] = []
        for hotkey_id, spec, mods, vk in self.bindings:
            if user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, vk):
                registered.append(hotkey_id)
                self.mechanism[hotkey_id] = "system hotkey"
                continue
            err = ctypes.get_last_error()
            if err != ERROR_HOTKEY_ALREADY_REGISTERED:
                self.error = f"Could not register hotkey '{spec}' (Win32 error {err})."
                break
            self.hooked.append((hotkey_id, spec, mods, vk))
            self.mechanism[hotkey_id] = "keyboard hook"

        if not self.error and self.hooked:
            self._hook_proc = HOOKPROC(self._on_key)  # keep a reference or ctypes frees the callback
            self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_proc, kernel32.GetModuleHandleW(None), 0)
            if not self._hook:
                self.error = f"Could not install the keyboard hook (Win32 error {ctypes.get_last_error()})."

        self.ready.set()
        if self.error:
            self._teardown(registered)
            return

        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self.events.put(int(msg.wParam))
                # Keyboard-hook callbacks are dispatched to this thread by GetMessage itself.
        finally:
            self._teardown(registered)

    def _teardown(self, registered: list[int]) -> None:
        for hotkey_id in registered:
            user32.UnregisterHotKey(None, hotkey_id)
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def stop(self) -> None:
        if self.thread_id is not None:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)

    @staticmethod
    def _modifiers() -> int:
        def down(vk: int) -> bool:
            return bool(user32.GetAsyncKeyState(vk) & 0x8000)

        mods = 0
        if down(VK_CONTROL):
            mods |= MOD_CONTROL
        if down(VK_SHIFT):
            mods |= MOD_SHIFT
        if down(VK_MENU):
            mods |= MOD_ALT
        if down(VK_LWIN) or down(VK_RWIN):
            mods |= MOD_WIN
        return mods

    def _on_key(self, code: int, w_param: int, l_param: int) -> int:
        """Low-level keyboard hook. Must return fast, and must never raise."""
        try:
            if code == HC_ACTION:
                vk = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode
                if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    for hotkey_id, _spec, mods, want_vk in self.hooked:
                        if vk == want_vk and self._modifiers() == mods:
                            if vk not in self._held:  # ignore auto-repeat while the key stays down
                                self._held.add(vk)
                                self.events.put(hotkey_id)
                            return 1  # swallow: the foreground app never sees the key
                elif w_param in (WM_KEYUP, WM_SYSKEYUP) and vk in self._held:
                    self._held.discard(vk)
                    return 1
        except Exception:  # noqa: BLE001  (a raise here would eat the keystroke)
            pass
        return user32.CallNextHookEx(None, code, w_param, l_param)


# ------------------------------------------------------------------- overlay --
class SelectionOverlay(tk.Toplevel):
    """A window covering every monitor that shows a frozen, dimmed screenshot.

    Dragging reveals the bright pixels under the marquee. The crop is taken from
    the frozen screenshot, so the overlay itself never ends up in the capture.
    """

    def __init__(self, master: tk.Misc, screenshot: Image.Image, origin: tuple[int, int], on_done):
        super().__init__(master)
        self.screenshot = screenshot
        self.on_done = on_done
        self.width, self.height = screenshot.size
        self.start: tuple[int, int] | None = None
        self.cursor = (0, 0)
        self.redraw_pending = False
        self.finished = False

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{self.width}x{self.height}+{origin[0]}+{origin[1]}")

        c = self.canvas = tk.Canvas(
            self, width=self.width, height=self.height,
            bg="black", bd=0, highlightthickness=0, cursor="crosshair",
        )
        c.pack(fill="both", expand=True)

        # Two full-size copies of the screen: the dimmed one is the backdrop, the
        # bright one is the source we copy the selected region out of. The copy
        # happens inside Tk (C code), so dragging stays smooth even on big displays.
        self.dark = ImageTk.PhotoImage(ImageEnhance.Brightness(screenshot).enhance(DIM), master=self)
        self.bright = ImageTk.PhotoImage(screenshot, master=self)
        self.selection = tk.PhotoImage(master=self)

        c.create_image(0, 0, image=self.dark, anchor="nw")
        self.sel_item = c.create_image(0, 0, image=self.selection, anchor="nw", state="hidden")
        self.rect = c.create_rectangle(0, 0, 0, 0, outline=ACCENT, width=1, state="hidden")
        self.hline = c.create_line(0, 0, self.width, 0, fill=ACCENT, dash=(6, 4))
        self.vline = c.create_line(0, 0, 0, self.height, fill=ACCENT, dash=(6, 4))

        s = dpi_scale()
        self.pad = max(4, int(6 * s))
        self.font = ("Segoe UI", -max(12, int(14 * s)))
        self.size_bg = c.create_rectangle(0, 0, 0, 0, fill="#202020", outline="", state="hidden")
        self.size_text = c.create_text(0, 0, text="", fill="white", font=self.font, anchor="nw", state="hidden")

        # Hint sits at the top of the primary monitor (which may not be at the
        # virtual screen's origin on multi-monitor layouts).
        hint_x = -origin[0] + primary_screen()[0] // 2
        hint_y = -origin[1] + int(28 * s)
        self.hint_text = c.create_text(
            hint_x, hint_y, anchor="n", fill="white", font=self.font,
            text="Drag to select an area   ·   Esc or right-click to cancel",
        )
        x1, y1, x2, y2 = c.bbox(self.hint_text)
        self.hint_bg = c.create_rectangle(
            x1 - 2 * self.pad, y1 - self.pad, x2 + 2 * self.pad, y2 + self.pad, fill="#202020", outline="",
        )
        c.tag_lower(self.hint_bg, self.hint_text)

        c.bind("<Motion>", self._on_motion)
        c.bind("<ButtonPress-1>", self._on_press)
        c.bind("<B1-Motion>", self._on_motion)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<ButtonPress-3>", lambda _e: self.cancel())
        self.bind("<Escape>", lambda _e: self.cancel())
        self.protocol("WM_DELETE_WINDOW", self.cancel)

        self.after(20, self._take_focus)

    # -- helpers ---------------------------------------------------------------
    def _take_focus(self) -> None:
        """Make sure Esc reaches us even though the hotkey fired in another app."""
        if self.finished:
            return
        self.lift()
        force_foreground(hwnd_of(self))
        self.focus_force()
        self.canvas.focus_set()

    def _clamp(self, event: tk.Event) -> tuple[int, int]:
        return (min(max(event.x, 0), self.width), min(max(event.y, 0), self.height))

    def _box(self) -> tuple[int, int, int, int]:
        (sx, sy), (cx, cy) = self.start, self.cursor  # type: ignore[misc]
        return min(sx, cx), min(sy, cy), max(sx, cx), max(sy, cy)

    def _set_hint(self, visible: bool) -> None:
        state = "normal" if visible else "hidden"
        self.canvas.itemconfigure(self.hint_text, state=state)
        self.canvas.itemconfigure(self.hint_bg, state=state)

    def _set_selection_visible(self, visible: bool) -> None:
        state = "normal" if visible else "hidden"
        for item in (self.sel_item, self.rect, self.size_bg, self.size_text):
            self.canvas.itemconfigure(item, state=state)

    # -- events ----------------------------------------------------------------
    def _on_press(self, event: tk.Event) -> None:
        self.start = self.cursor = self._clamp(event)
        self._set_hint(False)
        self._schedule_redraw()

    def _on_motion(self, event: tk.Event) -> None:
        self.cursor = self._clamp(event)
        self._schedule_redraw()

    def _on_release(self, event: tk.Event) -> None:
        if self.start is None or self.finished:
            return
        self.cursor = self._clamp(event)
        x1, y1, x2, y2 = self._box()
        if x2 - x1 < MIN_SELECTION or y2 - y1 < MIN_SELECTION:
            # Treat as a stray click: stay open and let them try again.
            self.start = None
            self._set_selection_visible(False)
            self._set_hint(True)
            return
        self._finish((x1, y1, x2, y2))

    def cancel(self) -> None:
        self._finish(None)

    def _finish(self, box: tuple[int, int, int, int] | None) -> None:
        if self.finished:
            return
        self.finished = True
        image = self.screenshot.crop(box) if box else None
        self.destroy()
        self.on_done(image)

    # -- drawing ---------------------------------------------------------------
    def _schedule_redraw(self) -> None:
        # Coalesce bursts of motion events into one redraw per idle cycle.
        if not self.redraw_pending:
            self.redraw_pending = True
            self.after_idle(self._redraw)

    def _redraw(self) -> None:
        self.redraw_pending = False
        if self.finished:
            return
        c = self.canvas
        x, y = self.cursor
        c.coords(self.hline, 0, y, self.width, y)
        c.coords(self.vline, x, 0, x, self.height)
        if self.start is None:
            return

        x1, y1, x2, y2 = self._box()
        if x2 - x1 < 1 or y2 - y1 < 1:
            self._set_selection_visible(False)
            return

        # Copy the bright pixels under the marquee into the selection image (in C).
        self.tk.call(str(self.selection), "copy", str(self.bright),
                     "-from", x1, y1, x2, y2, "-to", 0, 0, "-shrink")
        c.coords(self.sel_item, x1, y1)
        c.coords(self.rect, x1 - 1, y1 - 1, x2, y2)  # 1 px border just outside the selection

        self._set_selection_visible(True)  # bbox() ignores hidden items, so show before measuring

        # Size label: below the box if there's room, else above, else inside.
        c.itemconfigure(self.size_text, text=f"{x2 - x1} × {y2 - y1}")
        c.coords(self.size_text, x1, y2 + self.pad)
        bx1, by1, bx2, by2 = c.bbox(self.size_text)
        text_h = by2 - by1
        if y2 + self.pad + text_h + self.pad > self.height:
            ty = y1 - self.pad - text_h if y1 - 2 * self.pad - text_h >= 0 else y1 + self.pad
            c.coords(self.size_text, x1, ty)
        bx1, by1, bx2, by2 = c.bbox(self.size_text)
        if bx2 + self.pad > self.width:
            c.move(self.size_text, self.width - self.pad - bx2, 0)
            bx1, by1, bx2, by2 = c.bbox(self.size_text)
        c.coords(self.size_bg, bx1 - self.pad, by1 - self.pad // 2, bx2 + self.pad, by2 + self.pad // 2)


# --------------------------------------------------------------------- toast --
class Toast(tk.Toplevel):
    """A small, non-activating bubble in the bottom-right corner that goes away by itself."""

    def __init__(self, master: tk.Misc, lines: list[str], ms: int = TOAST_MS):
        super().__init__(master)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.94)
        s = dpi_scale()
        self.configure(bg="#202020")
        tk.Label(
            self, text="\n".join(lines), bg="#202020", fg="white", justify="left",
            font=("Segoe UI", -max(12, int(14 * s))), padx=int(16 * s), pady=int(10 * s),
        ).pack()
        self.update_idletasks()

        area = work_area()
        margin = int(16 * s)
        self.geometry(f"+{area.right - self.winfo_reqwidth() - margin}"
                      f"+{area.bottom - self.winfo_reqheight() - margin}")

        # Don't steal keyboard focus from whatever the user was working in.
        previous = user32.GetForegroundWindow()
        hwnd = hwnd_of(self)
        _SetWindowLong(hwnd, GWL_EXSTYLE, (_GetWindowLong(hwnd, GWL_EXSTYLE) or 0) | WS_EX_NOACTIVATE)
        self.deiconify()
        self.update_idletasks()
        if previous and user32.GetForegroundWindow() == hwnd_of(self):
            user32.SetForegroundWindow(previous)
        self.after(ms, self.destroy)


# ----------------------------------------------------------------------- app --
class Screenshotter:
    def __init__(self, capture_key: str = CAPTURE_KEY, quit_key: str = QUIT_KEY,
                 save_dir: Path = SAVE_DIR, clipboard: bool = COPY_TO_CLIPBOARD) -> None:
        self.save_dir = Path(save_dir)
        self.clipboard = clipboard
        self.events: queue.Queue[int] = queue.Queue()
        self.hotkeys = HotkeyListener(self.events, capture_key, quit_key)  # validates the key names
        self.root = tk.Tk()
        self.root.withdraw()
        self.overlay: SelectionOverlay | None = None

    def run(self) -> None:
        self.hotkeys.start()
        self.hotkeys.ready.wait(3)
        if self.hotkeys.error:
            raise SystemExit(self.hotkeys.error)

        how = self.hotkeys.mechanism
        print("Screenshotter is running.")
        print(f"  {self.hotkeys.capture_key:<16}capture a region (Esc or right-click to cancel)  [{how[ID_CAPTURE]}]")
        print(f"  {self.hotkeys.quit_key:<16}quit (Ctrl+C here also works)  [{how[ID_QUIT]}]")
        print(f"  {'saving to':<16}{self.save_dir}")

        self.root.after(POLL_MS, self._poll)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self.hotkeys.stop()
            try:
                self.root.destroy()
            except tk.TclError:
                pass
        print("Screenshotter stopped.")

    def _poll(self) -> None:
        try:
            while True:
                hotkey_id = self.events.get_nowait()
                if hotkey_id == ID_CAPTURE:
                    self.begin_capture()
                elif hotkey_id == ID_QUIT:
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        except KeyboardInterrupt:
            self.root.quit()
            return
        self.root.after(POLL_MS, self._poll)

    def begin_capture(self) -> None:
        if self.overlay is not None:
            return  # a selection is already in progress
        left, top, width, height = virtual_screen()
        try:
            screenshot = ImageGrab.grab(all_screens=True)
        except OSError as exc:
            print(f"Screen capture failed: {exc}")
            return
        if screenshot.size != (width, height):
            print(f"Note: grabbed {screenshot.size} but Windows reports {(width, height)}; using the grab.")
        self.overlay = SelectionOverlay(self.root, screenshot, (left, top), self._on_selection)

    def _on_selection(self, image: Image.Image | None) -> None:
        self.overlay = None
        if image is None:
            print("Capture cancelled.")
            return

        self.save_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        path = unique_path(self.save_dir / f"Screenshot {stamp}.png")
        image.save(path, "PNG")
        copied = self.clipboard and copy_image_to_clipboard(image)

        print(f"Saved {path}  ({image.width}x{image.height}){'  + clipboard' if copied else ''}")
        Toast(self.root, [f"Saved {path.name}  ({image.width}×{image.height})",
                          "Copied to clipboard" if copied else f"in {self.save_dir}"])


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Press a hotkey, drag a marquee, get a screenshot.")
    parser.add_argument("--key", default=CAPTURE_KEY,
                        help=f"capture hotkey, e.g. f12, f11, ctrl+f12, print (default: {CAPTURE_KEY})")
    parser.add_argument("--quit-key", default=QUIT_KEY, help=f"quit hotkey (default: {QUIT_KEY})")
    parser.add_argument("--dir", type=Path, default=SAVE_DIR, help=f"folder to save into (default: {SAVE_DIR})")
    parser.add_argument("--no-clipboard", action="store_true", help="don't copy captures to the clipboard")
    args = parser.parse_args(argv)
    try:
        app = Screenshotter(args.key, args.quit_key, args.dir, not args.no_clipboard)
    except ValueError as exc:
        parser.error(str(exc))
    app.run()


if __name__ == "__main__":
    main()
