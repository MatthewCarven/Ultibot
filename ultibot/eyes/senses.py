"""LiveSenses — the real-world Senses adapter (Phase 2).

One screen grab per tick, reused by every image/pixel question that tick
(20 Hz means one grab, many matches). Stat sensors arrive in Phase 5 and
Atlas in Phase 7; until then those questions answer None/False.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .capture import Screen
from .match import ImageFinder, Match


class LiveSenses:
    def __init__(self, assets_dir: str | Path, monitor: int = 1):
        self.screen = Screen(monitor)
        self.finder = ImageFinder(assets_dir)
        self.frame = None
        self._warned: set[str] = set()

    def begin_tick(self) -> None:
        self.frame = self.screen.grab()

    def image(self, name: str, *, confidence: float, region, grayscale: bool,
              scales: tuple, variants: tuple) -> Match | None:
        try:
            return self.finder.find_in_image(
                self.frame, name,
                confidence=confidence, region=region,
                grayscale=grayscale, scales=scales, variants=variants,
            )
        except FileNotFoundError as exc:
            # A missing template is "not found", never a crash — warn once.
            if name not in self._warned:
                self._warned.add(name)
                print(f"warn: {exc}  (treating as not-found)", file=sys.stderr)
            return None

    def stat(self, name: str):
        return None  # Phase 5 (OCR sensors)

    def pixel(self, x: int, y: int):
        if self.frame is None:
            return None
        h, w = self.frame.shape[:2]
        if not (0 <= x < w and 0 <= y < h):
            return None
        b, g, r = (int(v) for v in self.frame[y, x])
        return (r, g, b)  # profiles speak RGB; OpenCV stores BGR

    def at(self, place: str) -> bool:
        return False  # Phase 7 (Atlas)

    def near(self, place: str, radius: float) -> bool:
        return False  # Phase 7 (Atlas)
