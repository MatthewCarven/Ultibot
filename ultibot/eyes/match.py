"""Template matching via OpenCV — Phase 1.

The bread and butter (PLAN §9): cv2.matchTemplate slides the template across
the frame and scores every position; TM_CCOEFF_NORMED yields 0..1, which IS
the profile's "confidence". Robustness options from PLAN §3:

- confidence threshold  (when is a score "found"?)
- region                (search only part of the frame: faster, fewer fakes)
- grayscale             (survives hue/lighting shifts)
- scales                (survives UI scaling / resolution differences)
- variants              (several templates per logical image: hover, themes)
- alpha masks           (a PNG with transparency matches only its solid part)

Best hit across all variants × scales wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass
class Match:
    found: bool
    confidence: float                     # best score seen, found or not
    center: tuple[int, int] | None        # frame coordinates (region-aware)
    rect: tuple[int, int, int, int] | None  # x, y, w, h of the hit
    template: str                         # which variant won
    scale: float = 1.0                    # at which scale

    def __str__(self) -> str:
        where = f" @ {self.center}" if self.center else ""
        verdict = "FOUND" if self.found else "not found"
        return f"{verdict}: {self.template}{where}  confidence {self.confidence:.3f} (scale {self.scale:g})"


@dataclass
class _Template:
    name: str
    image: np.ndarray                     # BGR (or gray if requested)
    mask: np.ndarray | None               # from the PNG alpha channel, if any


class ImageFinder:
    """Finds template images in frames. Templates are loaded once and cached."""

    def __init__(self, assets_dir: str | Path):
        self.assets_dir = Path(assets_dir)
        if not self.assets_dir.is_dir():
            raise FileNotFoundError(
                f"assets folder not found: {self.assets_dir}\n"
                "Create it and snip templates into it, e.g.:\n"
                f'  pythonw Screenshotter\\screenshotter.py --dir "{self.assets_dir}"'
            )
        self._cache: dict[tuple[str, bool], _Template] = {}

    # -- template loading -----------------------------------------------------
    def _load(self, name: str, grayscale: bool) -> _Template:
        key = (name, grayscale)
        if key in self._cache:
            return self._cache[key]
        path = self.assets_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"template not found: {path}")
        raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if raw is None:
            raise ValueError(f"not a readable image: {path}")

        mask = None
        if raw.ndim == 3 and raw.shape[2] == 4:      # PNG with alpha -> mask
            mask = raw[:, :, 3]
            raw = raw[:, :, :3]
        # Channel-count must match the search frame at match time. Normalize
        # here so grayscale-saved templates work in both modes.
        if grayscale and raw.ndim == 3:
            raw = cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY)
        elif not grayscale and raw.ndim == 2:
            raw = cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)
        if mask is not None and grayscale:
            mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)[1]

        templ = _Template(name, raw, mask)
        self._cache[key] = templ
        return templ

    # -- matching ---------------------------------------------------------------
    def find_in_image(
        self,
        frame: np.ndarray,
        name: str,
        *,
        confidence: float = 0.8,
        region: tuple[int, int, int, int] | None = None,
        grayscale: bool = False,
        scales: tuple[float, ...] = (1.0,),
        variants: tuple[str, ...] = (),
    ) -> Match:
        """Search `frame` (BGR) for `name`. Pure — no screen involved, so tests
        can feed synthesized images. All options mirror the profile vocabulary."""
        names = (name, *variants)
        templates = [self._load(n, grayscale) for n in names]

        ox, oy = 0, 0
        haystack = frame
        if region is not None:
            ox, oy, w, h = region
            haystack = frame[oy:oy + h, ox:ox + w]
        if grayscale and haystack.ndim == 3:
            haystack = cv2.cvtColor(haystack, cv2.COLOR_BGR2GRAY)

        best = Match(False, 0.0, None, None, names[0])
        for templ in templates:
            for scale in scales:
                needle, mask = self._scaled(templ, scale)
                if needle.shape[0] > haystack.shape[0] or needle.shape[1] > haystack.shape[1]:
                    continue  # template bigger than the search area: no result possible
                method = cv2.TM_CCORR_NORMED if mask is not None else cv2.TM_CCOEFF_NORMED
                result = cv2.matchTemplate(haystack, needle, method, mask=mask)
                _, score, _, loc = cv2.minMaxLoc(result)
                if score > best.confidence:
                    x, y = loc
                    h, w = needle.shape[:2]
                    best = Match(
                        found=score >= confidence,
                        confidence=float(score),
                        center=(ox + x + w // 2, oy + y + h // 2),
                        rect=(ox + x, oy + y, w, h),
                        template=templ.name,
                        scale=scale,
                    )
        best.found = best.confidence >= confidence
        return best

    @staticmethod
    def _scaled(templ: _Template, scale: float) -> tuple[np.ndarray, np.ndarray | None]:
        if scale == 1.0:
            return templ.image, templ.mask
        w = max(1, round(templ.image.shape[1] * scale))
        h = max(1, round(templ.image.shape[0] * scale))
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        image = cv2.resize(templ.image, (w, h), interpolation=interp)
        mask = None
        if templ.mask is not None:
            mask = cv2.resize(templ.mask, (w, h), interpolation=cv2.INTER_NEAREST)
        return image, mask
