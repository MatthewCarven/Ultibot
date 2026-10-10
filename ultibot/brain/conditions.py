"""Condition evaluation — Phase 2.

and/or/not combinators over the leaf vocabulary (PLAN §2). Leaves come in
two families:

- SENSE leaves (image, stat, pixel, at, near) ask the world via a `Senses`
  object (LiveSenses for the real screen, fakes in tests).
- STATE leaves (cooldown_ready, timer, once, chance) ask the `EngineState`
  (cooldowns, timers, once-flags, the dice).

Both are injected, so evaluation is pure and fully testable.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Protocol


class Senses(Protocol):
    """What the Brain may ask the world. One frame per tick (begin_tick)."""

    def begin_tick(self) -> None: ...
    def image(self, name: str, *, confidence: float, region, grayscale: bool,
              scales: tuple, variants: tuple): ...
    def stat(self, name: str): ...          # -> number | str | None
    def pixel(self, x: int, y: int): ...    # -> (r, g, b) | None
    def at(self, place: str) -> bool: ...
    def near(self, place: str, radius: float) -> bool: ...


_UNSET = object()


class EngineState:
    """Everything the Brain remembers between ticks. Clock and dice injected."""

    def __init__(self, *, clock=time.monotonic, rng: random.Random | None = None,
                 cooldowns: dict[str, float] | None = None):
        self.clock = clock
        self.rng = rng or random.Random()
        self.cooldowns = cooldowns or {}          # group -> cooldown_ms
        self.last_fired: dict[str, float] = {}    # group -> timestamp
        self.fire_counts: dict[str, int] = {}
        self.timers: dict[str, float] = {}        # key -> last fire timestamp
        self.onces: set[str] = set()
        self.vetoes: dict[str, float] = {}        # group -> suppressed-until timestamp
        self.stats: dict[str, Any] = {}           # sensor -> last value

    def now(self) -> float:
        return self.clock()

    # dice -----------------------------------------------------------------
    def roll(self, p: float) -> bool:
        return self.rng.random() < p

    # cooldowns --------------------------------------------------------------
    def cooldown_ready(self, group: str) -> bool:
        last = self.last_fired.get(group)
        if last is None:
            return True
        return (self.now() - last) * 1000 >= self.cooldowns.get(group, 0)

    def cooldown_left_ms(self, group: str) -> float:
        last = self.last_fired.get(group, float("-inf"))
        return max(0.0, self.cooldowns.get(group, 0) - (self.now() - last) * 1000)

    def note_fired(self, group: str) -> None:
        self.last_fired[group] = self.now()
        self.fire_counts[group] = self.fire_counts.get(group, 0) + 1

    def reached_max(self, group: str, max_fires: int | None) -> bool:
        return max_fires is not None and self.fire_counts.get(group, 0) >= max_fires

    # timers / onces -----------------------------------------------------------
    def timer_due(self, key: str, ms: float) -> bool:
        """True every `ms` for this key. The first check fires immediately."""
        last = self.timers.get(key)
        if last is None or (self.now() - last) * 1000 >= ms:
            self.timers[key] = self.now()
            return True
        return False

    def take_once(self, key: str) -> bool:
        if key in self.onces:
            return False
        self.onces.add(key)
        return True

    # vetoes (Conduct Layer, PLAN §4a) -----------------------------------------
    def apply_veto(self, groups: list[str], ms: float) -> None:
        until = self.now() + ms / 1000
        for g in groups:
            self.vetoes[g] = max(self.vetoes.get(g, 0.0), until)

    def vetoed(self, group: str) -> bool:
        return self.vetoes.get(group, 0.0) > self.now()

    # stats ----------------------------------------------------------------------
    def stat_changed(self, name: str, value: Any) -> bool:
        last = self.stats.get(name, _UNSET)
        self.stats[name] = value
        return last is not _UNSET and last != value


# ---------------------------------------------------------------- evaluate --

def _key(cond: Any) -> str:
    """Stable identity for anonymous timers/onces: the condition itself."""
    return json.dumps(cond, sort_keys=True)


def evaluate(cond: dict, senses: Senses, state: EngineState) -> bool:
    if "and" in cond:
        return all(evaluate(c, senses, state) for c in cond["and"])
    if "or" in cond:
        return any(evaluate(c, senses, state) for c in cond["or"])
    if "not" in cond:
        return not evaluate(cond["not"], senses, state)

    if "image" in cond:
        match = senses.image(
            cond["image"],
            confidence=cond.get("confidence", 0.8),
            region=tuple(cond["region"]) if "region" in cond else None,
            grayscale=cond.get("grayscale", False),
            scales=tuple(cond.get("scales", (1.0,))),
            variants=tuple(cond.get("variants", ())),
        )
        return bool(match and match.found)

    if "stat" in cond:
        name, op = cond["stat"][0], cond["stat"][1]
        value = senses.stat(name)
        if value is None:
            return False
        if op == "changed":
            return state.stat_changed(name, value)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        threshold = cond["stat"][2]
        return {"<": value < threshold, ">": value > threshold,
                "<=": value <= threshold, ">=": value >= threshold,
                "==": value == threshold, "!=": value != threshold}[op]

    if "pixel" in cond:
        spec = cond["pixel"]
        got = senses.pixel(int(spec["point"][0]), int(spec["point"][1]))
        if got is None:
            return False
        tolerance = spec.get("tolerance", 0)
        return all(abs(g - c) <= tolerance for g, c in zip(got, spec["color"]))

    if "cooldown_ready" in cond:
        return state.cooldown_ready(cond["cooldown_ready"])

    if "timer" in cond:
        return state.timer_due(_key(cond), cond["timer"])

    if "once" in cond:
        value = cond["once"]
        return state.take_once(value if isinstance(value, str) else _key(cond))

    if "chance" in cond:
        return state.roll(cond["chance"])

    if "at" in cond:
        return senses.at(cond["at"])

    if "near" in cond:
        place, radius = cond["near"]
        return senses.near(place, radius)

    raise ValueError(f"unrecognized condition: {cond!r} (profile should have been validated)")
