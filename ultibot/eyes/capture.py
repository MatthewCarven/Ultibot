"""Screen capture via mss — Phase 1.

Full-monitor and region grabs, fast enough for the 20 Hz tick. Coordinates
are in the chosen monitor's pixel space, origin top-left — the same space
pynput clicks in (for the primary display), so a match's position can be
handed straight to the Hands later.
"""

from __future__ import annotations

import mss
import numpy as np


class Screen:
    """Grabs frames from one monitor.

    monitor=1 is the primary display (default). monitor=0 is the whole
    virtual desktop spanning every monitor — note its origin can be at
    negative coordinates on multi-monitor layouts, which complicates
    clicking, so it's opt-in.
    """

    def __init__(self, monitor: int = 1):
        self._mss = mss.mss()
        count = len(self._mss.monitors) - 1  # index 0 is the virtual desktop
        if not 0 <= monitor <= count:
            raise ValueError(f"no monitor {monitor} (have 0..{count})")
        self.monitor = monitor
        self._box = self._mss.monitors[monitor]

    @property
    def size(self) -> tuple[int, int]:
        """(width, height) of this monitor in pixels."""
        return self._box["width"], self._box["height"]

    def grab(self, region: tuple[int, int, int, int] | None = None) -> np.ndarray:
        """Grab the monitor (or a region of it) as a BGR image.

        region = (x, y, width, height) in this monitor's coordinate space.
        """
        if region is None:
            box = self._box
        else:
            x, y, w, h = region
            box = {"left": self._box["left"] + x, "top": self._box["top"] + y,
                   "width": w, "height": h}
        shot = self._mss.grab(box)
        frame = np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        return frame[:, :, :3]  # BGRA -> BGR (exactly what OpenCV wants)
