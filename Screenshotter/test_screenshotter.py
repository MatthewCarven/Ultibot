"""Self-test for screenshotter.py. Needs a real desktop: it briefly shows the overlay and injects F12 / Ctrl+Shift+F12 keystrokes. Run: python test_screenshotter.py"""
import ctypes
import struct
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshotter as ss  # noqa: E402
import tkinter as tk  # noqa: E402
from PIL import Image  # noqa: E402

u, k = ss.user32, ss.kernel32
u.GetClipboardData.restype = ctypes.c_void_p
u.GetClipboardData.argtypes = [wintypes.UINT]
k.GlobalSize.restype = ctypes.c_size_t
k.GlobalSize.argtypes = [ctypes.c_void_p]
CF_UNICODETEXT = 13

failures = 0


def check(name, ok, extra=""):
    global failures
    failures += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra else ""))


# --- DPI / grab geometry -----------------------------------------------------
left, top, w, h = ss.virtual_screen()
shot = ss.ImageGrab.grab(all_screens=True)
check("grab size matches virtual screen", shot.size == (w, h), f"grab={shot.size} metrics={(w, h)} origin={(left, top)}")
print(f"     dpi scale {ss.dpi_scale():.2f}, primary {ss.primary_screen()}")

# --- hotkey parsing ----------------------------------------------------------
check("parse ctrl+shift+f12", ss.parse_hotkey("ctrl+shift+f12") == (ss.MOD_CONTROL | ss.MOD_SHIFT, 0x7B))
check("parse F12 (case-insensitive)", ss.parse_hotkey("F12") == (0, 0x7B))
check("parse print", ss.parse_hotkey("print") == (0, 0x2C))
check("parse alt+a", ss.parse_hotkey("alt+a") == (0x0001, ord("A")))
for bad in ("", "+", "bogus", "ctrl+", "meta+f1"):
    try:
        ss.parse_hotkey(bad)
        check(f"parse rejects {bad!r}", False)
    except ValueError:
        check(f"parse rejects {bad!r}", True)
try:
    ss.main(["--key", "bogus"])
    check("--key bogus rejected by CLI", False)
except SystemExit as exc:
    check("--key bogus rejected by CLI", exc.code == 2, str(exc.code))


# --- hotkey thread: default f12 (hook) + ctrl+shift+f12 (system hotkey) --------
KEYEVENTF_KEYUP = 0x0002
u.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]


def press(*vks):
    for vk in vks:
        u.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        u.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def drain(q, timeout=1.0):
    items, deadline = [], time.time() + timeout
    while time.time() < deadline:
        try:
            items.append(q.get(timeout=0.05))
        except ss.queue.Empty:
            pass
    return items


q = ss.queue.Queue()
hk = ss.HotkeyListener(q)
hk.start()
hk.ready.wait(3)
check("listener starts with default keys", hk.error is None, hk.error or "")
check("f12 served by keyboard hook", hk.mechanism.get(ss.ID_CAPTURE) == "keyboard hook", str(hk.mechanism))
check("ctrl+shift+f12 served by system hotkey", hk.mechanism.get(ss.ID_QUIT) == "system hotkey", str(hk.mechanism))
dup = u.RegisterHotKey(None, 99, ss.MOD_CONTROL | ss.MOD_SHIFT | ss.MOD_NOREPEAT, 0x7B)
check("ctrl+shift+f12 held by listener (duplicate refused)", not dup)
if dup:
    u.UnregisterHotKey(None, 99)

press(0x7B)                                   # F12 -> hook path
got_keys = drain(q)
check("injected F12 reaches queue exactly once", got_keys == [ss.ID_CAPTURE], str(got_keys))
u.keybd_event(0x7B, 0, 0, 0)                  # held key auto-repeats: down, down, up
u.keybd_event(0x7B, 0, 0, 0)
u.keybd_event(0x7B, 0, KEYEVENTF_KEYUP, 0)
got_keys = drain(q)
check("auto-repeat while held fires once", got_keys == [ss.ID_CAPTURE], str(got_keys))
press(0x11, 0x10, 0x7B)                       # Ctrl+Shift+F12 -> RegisterHotKey path
got_keys = drain(q)
check("injected Ctrl+Shift+F12 reaches queue as quit", got_keys == [ss.ID_QUIT], str(got_keys))
press(0x11, 0x7B)                             # Ctrl+F12 matches nothing -> passes through, no event
got_keys = drain(q, 0.5)
check("Ctrl+F12 ignored", got_keys == [], str(got_keys))

hk.stop()
hk.join(3)
check("listener thread stops", not hk.is_alive())
dup = u.RegisterHotKey(None, 99, ss.MOD_CONTROL | ss.MOD_SHIFT | ss.MOD_NOREPEAT, 0x7B)
check("ctrl+shift+f12 released after stop", bool(dup))
if dup:
    u.UnregisterHotKey(None, 99)
press(0x7B)
check("F12 no longer intercepted after stop", drain(q, 0.5) == [])

# a key another program owns (PrintScreen, held by ShareX here) also falls back to the hook
q2 = ss.queue.Queue()
hk2 = ss.HotkeyListener(q2, "print", "ctrl+shift+f24")
hk2.start()
hk2.ready.wait(3)
check("key owned by another app falls back to hook", hk2.error is None and hk2.mechanism.get(ss.ID_CAPTURE) == "keyboard hook", hk2.error or str(hk2.mechanism))
hk2.stop()
hk2.join(3)


# --- clipboard (only if we can put the user's clipboard back afterwards) -----
def read_text():
    if not u.OpenClipboard(None):
        return None
    try:
        if not u.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        hnd = u.GetClipboardData(CF_UNICODETEXT)
        if not hnd:
            return None
        p = k.GlobalLock(hnd)
        try:
            return ctypes.wstring_at(p)
        finally:
            k.GlobalUnlock(hnd)
    finally:
        u.CloseClipboard()


def write_text(s):
    data = s.encode("utf-16-le") + b"\x00\x00"
    hnd = k.GlobalAlloc(ss.GMEM_MOVEABLE, len(data))
    p = k.GlobalLock(hnd)
    ctypes.memmove(p, data, len(data))
    k.GlobalUnlock(hnd)
    u.OpenClipboard(None)
    u.EmptyClipboard()
    u.SetClipboardData(CF_UNICODETEXT, hnd)
    u.CloseClipboard()


saved_text = read_text()
nformats = u.CountClipboardFormats()
if nformats == 0 or saved_text is not None:
    ok = ss.copy_image_to_clipboard(Image.new("RGB", (37, 21), (255, 0, 0)))
    u.OpenClipboard(None)
    has = u.IsClipboardFormatAvailable(ss.CF_DIB)
    hdr = None
    if has:
        hnd = u.GetClipboardData(ss.CF_DIB)
        p = k.GlobalLock(hnd)
        data = ctypes.string_at(p, k.GlobalSize(hnd))
        k.GlobalUnlock(hnd)
        hdr = struct.unpack_from("<IiiHH", data, 0)  # biSize, biWidth, biHeight, biPlanes, biBitCount
    u.CloseClipboard()
    check("clipboard holds a 37x21 DIB", ok and has and hdr and hdr[1] == 37 and abs(hdr[2]) == 21, str(hdr))
    if saved_text is not None:
        write_text(saved_text)
        check("clipboard text restored", read_text() == saved_text)
    else:
        u.OpenClipboard(None)
        u.EmptyClipboard()
        u.CloseClipboard()
else:
    check("clipboard test skipped (clipboard holds non-text data, left untouched)", True, f"{nformats} formats")

# --- unique_path -------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "Shot.png"
    p.write_bytes(b"x")
    (Path(d) / "Shot (2).png").write_bytes(b"x")
    check("unique_path skips existing names", ss.unique_path(p).name == "Shot (3).png", ss.unique_path(p).name)

# --- overlay with synthetic events ------------------------------------------
root = tk.Tk()
root.withdraw()
got = {}


def done(img):
    got["img"] = img


t0 = time.perf_counter()
ov = ss.SelectionOverlay(root, shot, (left, top), done)
root.update()
print(f"     overlay build+show {1000 * (time.perf_counter() - t0):.0f} ms for {shot.size}")
time.sleep(0.05)
root.update()  # runs the delayed _take_focus
check("overlay becomes the foreground window", u.GetForegroundWindow() == ss.hwnd_of(ov), f"fg={u.GetForegroundWindow()} overlay={ss.hwnd_of(ov)}")
check("overlay canvas has keyboard focus", ov.focus_get() == ov.canvas, str(ov.focus_get()))
c = ov.canvas
c.event_generate("<ButtonPress-1>", x=100, y=100)
c.event_generate("<B1-Motion>", x=250, y=200)
root.update()
sel = (ov.selection.width(), ov.selection.height())
check("selection image copied at 150x100", sel == (150, 100), f"{sel}")
check("size label reads 150 x 100", c.itemcget(ov.size_text, "text") == "150 × 100", c.itemcget(ov.size_text, "text"))
check("hint hidden while dragging", c.itemcget(ov.hint_text, "state") == "hidden")
t0 = time.perf_counter()
for i in range(20):
    c.event_generate("<B1-Motion>", x=400 + i, y=300 + i)
    root.update()
print(f"     20 drag redraws {1000 * (time.perf_counter() - t0):.0f} ms")
c.event_generate("<B1-Motion>", x=400, y=300)
c.event_generate("<ButtonRelease-1>", x=400, y=300)
root.update()
img = got.get("img")
check("crop returned 300x200", img is not None and img.size == (300, 200), str(img and img.size))
if img:
    check("crop pixels equal source region", img.tobytes() == shot.crop((100, 100, 400, 300)).tobytes())
check("overlay destroyed after release", not ov.winfo_exists())

got.clear()
ov = ss.SelectionOverlay(root, shot, (left, top), done)
root.update()
c = ov.canvas
c.event_generate("<ButtonPress-1>", x=50, y=50)
c.event_generate("<ButtonRelease-1>", x=51, y=51)
root.update()
check("tiny drag keeps overlay open", ov.winfo_exists() and ov.start is None and "img" not in got)
check("hint back after tiny drag", c.itemcget(ov.hint_text, "state") == "normal")
ov.event_generate("<Escape>")
root.update()
check("Escape cancels with None", "img" in got and got["img"] is None and not ov.winfo_exists())

got.clear()
ov = ss.SelectionOverlay(root, shot, (left, top), done)
root.update()
ov.canvas.event_generate("<ButtonPress-3>", x=10, y=10)
root.update()
check("right-click cancels", "img" in got and got["img"] is None and not ov.winfo_exists())

# reversed drag (bottom-right to top-left) normalises
got.clear()
ov = ss.SelectionOverlay(root, shot, (left, top), done)
root.update()
ov.canvas.event_generate("<ButtonPress-1>", x=300, y=300)
ov.canvas.event_generate("<B1-Motion>", x=180, y=220)
ov.canvas.event_generate("<ButtonRelease-1>", x=180, y=220)
root.update()
check("reversed drag gives 120x80", got.get("img") is not None and got["img"].size == (120, 80), str(got.get("img") and got["img"].size))

# --- toast -------------------------------------------------------------------
toast = ss.Toast(root, ["Saved Screenshot test.png  (300×200)", "Copied to clipboard"], ms=400)
root.update()
fg = u.GetForegroundWindow()
check("toast does not take the foreground", fg != ss.hwnd_of(toast), f"fg={fg} toast={ss.hwnd_of(toast)}")
time.sleep(0.5)
root.update()
check("toast closes by itself", not toast.winfo_exists())

root.destroy()
print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
