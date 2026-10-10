# Screenshotter

Press **F12**, drag a marquee over any part of your screen, release. The selection is saved as a PNG and copied to the clipboard.

- Works across all monitors (the overlay spans the whole virtual desktop).
- DPI-aware: what you select is what you get, even at 125 % / 150 % scaling.
- The screen is frozen while you select, so the overlay never appears in the capture.
- No dependencies beyond Pillow. The global hotkey is a plain Win32 `RegisterHotKey` via ctypes.

## Run

```bash
pip install pillow
```

```bash
python screenshotter.py
```

Use `pythonw screenshotter.py` instead to run without a console window (quit with Ctrl+Shift+F12).

## Keys

| Key | Action |
| --- | --- |
| F12 | Start a capture |
| Drag | Select an area (a size readout follows the box) |
| Esc / right-click | Cancel |
| Ctrl+Shift+F12 | Quit (Ctrl+C in the console also works) |

A click without a drag does nothing and leaves the overlay open, so a slip of the mouse doesn't cost you the capture.

## Options

```text
--key KEY          capture hotkey (default f12). Examples: f11, ctrl+f12, print, ctrl+shift+s
--quit-key KEY     quit hotkey (default ctrl+shift+f12)
--dir FOLDER       where to save (default %USERPROFILE%\Pictures\Screenshotter)
--no-clipboard     save only, don't touch the clipboard
```

Key names: `f1`..`f24`, `print`, `pause`, `scroll`, `insert`, `delete`, `home`, `end`, `pageup`, `pagedown`, `space`, `tab`, `esc`, arrow keys, or any single letter/digit. Modifiers: `ctrl`, `shift`, `alt`, `win`.

## How the hotkey is caught

Each key is registered with Windows' `RegisterHotKey` when possible. Windows refuses plain F12 on every machine (it is reserved as the debugger break key), and refuses any key another program already owns (PrintScreen when ShareX is running, for example). Keys it refuses are caught with a low-level keyboard hook instead; the startup banner tells you which mechanism each key ended up with.

The one practical difference: the keyboard hook can't see keystrokes while a program running **as administrator** has the foreground. If you need captures over elevated windows, run Screenshotter as administrator too, or pick a key `RegisterHotKey` accepts, such as `--key ctrl+f12` or `--key f11`.

While Screenshotter is running, other apps don't see the capture key at all (browser DevTools won't open on F12, for example).

## Start with Windows

Create a shortcut to `pythonw.exe "C:\path\to\screenshotter.py"` and drop it in the Startup folder (`Win+R`, `shell:startup`).

## Files

Captures land in `Pictures\Screenshotter\Screenshot YYYY-MM-DD HH-MM-SS.png`. Duplicates in the same second get a ` (2)` suffix.
