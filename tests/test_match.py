"""Tests for the Phase 1 Eyes (template matching).

No screen needed: frames are synthesized with numpy, templates pasted in at
known positions. Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np

from ultibot.eyes.match import ImageFinder

RNG = np.random.default_rng(42)


def make_frame(w: int = 400, h: int = 300) -> np.ndarray:
    """A noisy BGR 'game screen'."""
    return RNG.integers(0, 255, (h, w, 3), dtype=np.uint8)


def make_patch(w: int = 40, h: int = 30) -> np.ndarray:
    """A distinctive BGR 'template'."""
    return RNG.integers(0, 255, (h, w, 3), dtype=np.uint8)


class FinderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.assets = Path(self.tmp.name)
        self.finder = ImageFinder(self.assets)

    def save(self, name: str, image: np.ndarray) -> None:
        assert cv2.imwrite(str(self.assets / name), image)

    def test_missing_assets_dir_raises(self):
        with self.assertRaises(FileNotFoundError):
            ImageFinder(self.assets / "nope")

    def test_exact_match_found_at_position(self):
        frame, patch = make_frame(), make_patch()
        frame[100:130, 150:190] = patch
        self.save("patch.png", patch)

        m = self.finder.find_in_image(frame, "patch.png", confidence=0.99)
        self.assertTrue(m.found)
        self.assertAlmostEqual(m.confidence, 1.0, places=2)
        self.assertEqual(m.center, (170, 115))
        self.assertEqual(m.rect, (150, 100, 40, 30))

    def test_absent_template_not_found(self):
        frame, patch = make_frame(), make_patch()
        self.save("patch.png", patch)
        m = self.finder.find_in_image(frame, "patch.png", confidence=0.999)
        self.assertFalse(m.found)
        self.assertLess(m.confidence, 0.999)

    def test_region_offsets_coordinates(self):
        frame, patch = make_frame(), make_patch()
        frame[100:130, 150:190] = patch
        self.save("patch.png", patch)

        m = self.finder.find_in_image(frame, "patch.png", confidence=0.99,
                                      region=(100, 50, 300, 250))
        self.assertTrue(m.found)
        self.assertEqual(m.center, (170, 115))  # frame coords, not region coords

    def test_region_excludes_the_hit(self):
        frame, patch = make_frame(), make_patch()
        frame[100:130, 150:190] = patch
        self.save("patch.png", patch)

        m = self.finder.find_in_image(frame, "patch.png", confidence=0.99,
                                      region=(0, 0, 100, 100))
        self.assertFalse(m.found)

    def test_grayscale_survives_hue_shift(self):
        frame, patch = make_frame(), make_patch()
        frame[100:130, 150:190] = patch
        # Recolor the frame (same luminance-ish structure, different hue):
        shifted = cv2.cvtColor(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY),
                               cv2.COLOR_GRAY2BGR)
        shifted[:, :, 0] = 255 - shifted[:, :, 0]
        self.save("patch.png", cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY))

        m = self.finder.find_in_image(shifted, "patch.png", confidence=0.95,
                                      grayscale=True)
        self.assertTrue(m.found)
        self.assertEqual(m.center, (170, 115))

    def test_multiscale_finds_resized_template(self):
        frame, patch = make_frame(), make_patch()
        bigger = cv2.resize(patch, (50, 38), interpolation=cv2.INTER_LINEAR)  # 1.25x
        frame[100:138, 150:200] = bigger
        self.save("patch.png", patch)

        one_scale = self.finder.find_in_image(frame, "patch.png", confidence=0.99)
        self.assertFalse(one_scale.found)

        m = self.finder.find_in_image(frame, "patch.png", confidence=0.95,
                                      scales=(1.0, 1.25))
        self.assertTrue(m.found)
        self.assertAlmostEqual(m.scale, 1.25)
        self.assertEqual(m.center, (175, 119))

    def test_variants_any_hit_wins(self):
        frame = make_frame()
        patch_a, patch_b = make_patch(), make_patch()
        frame[100:130, 150:190] = patch_b
        self.save("a.png", patch_a)
        self.save("b.png", patch_b)

        m = self.finder.find_in_image(frame, "a.png", confidence=0.99,
                                      variants=("b.png",))
        self.assertTrue(m.found)
        self.assertEqual(m.template, "b.png")

    def test_alpha_mask_ignores_transparent_pixels(self):
        frame, patch = make_frame(), make_patch()
        frame[100:130, 150:190] = patch
        # Same patch with alpha: only the left half is solid.
        rgba = cv2.cvtColor(patch, cv2.COLOR_BGR2BGRA)
        rgba[:, 20:, 3] = 0
        self.save("masked.png", rgba)
        # Scribble over the right half of the frame's patch — a naive matcher
        # would fail, but those pixels are transparent in the mask.
        frame[100:130, 170:190] = 0

        m = self.finder.find_in_image(frame, "masked.png", confidence=0.95)
        self.assertTrue(m.found)
        self.assertEqual(m.rect, (150, 100, 40, 30))

    def test_template_bigger_than_region_is_skipped(self):
        frame, patch = make_frame(), make_patch()
        self.save("patch.png", patch)
        m = self.finder.find_in_image(frame, "patch.png", region=(0, 0, 20, 20))
        self.assertFalse(m.found)
        self.assertEqual(m.confidence, 0.0)

    def test_missing_template_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.finder.find_in_image(make_frame(), "ghost.png")


if __name__ == "__main__":
    unittest.main()
