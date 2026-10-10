# Worklog: Screenshotter

## 2026-09-06

**Built** `screenshotter.py`: F12 opens a frozen, dimmed overlay across every monitor; drag a marquee; the crop is saved to `Pictures\Screenshotter` as PNG and copied to the clipboard. Only dependency is Pillow (already installed); everything else is Win32 via ctypes and tkinter.

**Decisions**
- Global hotkey through Win32 `RegisterHotKey` rather than the `keyboard` / `pynput` packages (neither installed; keeps the script dependency-free).
- Finding: Windows reserves plain F12 as the debugger break key (`HKLM\...\AeDebug\UserDebuggerHotKey = 0`), so `RegisterHotKey` returns error 1409 for it on every default install. PrintScreen is also refused on this machine because ShareX holds it. Added a low-level keyboard hook (`WH_KEYBOARD_LL`) fallback for any key `RegisterHotKey` refuses; the startup banner shows which mechanism each key uses. Hook limitation: can't see keys while an elevated window has the foreground (documented in README).
- Overlay shows a frozen screenshot (dimmed) instead of a transparent window: the overlay can never appear in the capture, what you see is exactly what you get, and the bright selection is a Tk-side `photo copy` (C speed, ~17 ms per redraw on the 3520x1080 desktop here).
- Process is per-monitor-v2 DPI aware so Tk coordinates equal Pillow's physical pixels.
- Overlay forces itself to the foreground with `AttachThreadInput` (the hook path doesn't grant foreground rights) so Esc works.
- Toast bubble uses `WS_EX_NOACTIVATE` so it never steals focus from the app you were in.
- A click without a drag keeps the overlay open rather than cancelling.

**Tested** with `test_screenshotter.py` (synthetic Tk events plus injected keystrokes; flashes the overlay briefly and needs a real desktop): grab geometry on this two-monitor setup (3520x1080, origin -1600,0), hotkey parsing and CLI validation, hook and RegisterHotKey paths, auto-repeat suppression, clipboard DIB round trip, overlay drag / reversed drag / tiny drag / Esc / right-click, toast non-activation. 41/41 pass. Then an end-to-end subprocess run: F12 opens the overlay in the foreground, Esc cancels, Ctrl+Shift+F12 quits cleanly.

**Flagged for meatthread0**: folder is not a git repo yet; `git init` when ready.
