"""The engine loop — Phase 2 (dry-run).

Ties profile + senses + walker together and ticks at the profile's rate.
Every tick's events are printed (and, in Phase 4, written to the audit
log). Phase 2 sends NO input: it logs what it *would* do.
"""

from __future__ import annotations

import sys
import time
from typing import Callable, TextIO

from .conditions import EngineState, Senses
from .tree import Walker


class Engine:
    def __init__(self, profile: dict, senses: Senses, *,
                 state: EngineState | None = None):
        self.profile = profile
        self.senses = senses
        cooldowns = {name: g["cooldown_ms"]
                     for name, g in profile.get("groups", {}).items()
                     if "cooldown_ms" in g}
        self.state = state or EngineState(cooldowns=cooldowns)
        self.state.cooldowns = {**cooldowns, **self.state.cooldowns}
        self.walker = Walker(profile, senses, self.state)
        settings = profile.get("settings", {})
        self.tick_rate = float(settings.get("tick_rate_hz", 20))

    def tick(self) -> list[dict]:
        return self.walker.tick()

    def run(self, *, ticks: int | None = None, seconds: float | None = None,
            verbose: bool = False, out: TextIO = sys.stdout,
            sleeper: Callable[[float], None] = time.sleep) -> None:
        period = 1.0 / self.tick_rate
        started = time.monotonic()
        count = 0
        print(f"Ultibot DRY-RUN — profile \"{self.profile['profile']}\" @ "
              f"{self.tick_rate:g} Hz. No input will be sent. Ctrl+C to stop.",
              file=out)
        try:
            while True:
                if ticks is not None and count >= ticks:
                    break
                if seconds is not None and time.monotonic() - started >= seconds:
                    break
                t0 = time.monotonic()
                count += 1
                for line in format_events(self.tick(), verbose=verbose):
                    print(f"[{t0 - started:7.2f}s] tick {count:<5} {line}", file=out)
                sleeper(max(0.0, period - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            pass
        print(f"stopped after {count} tick(s).", file=out)


# ------------------------------------------------------------- formatting --

def _step_summary(step: dict) -> str:
    t = step["type"]
    if t in ("click", "right_click", "double_click", "move_to"):
        return f"{t} {step.get('target', 'match')}"
    if t == "key":
        return f"key {step['key']}"
    if t == "key_hold":
        return f"key {step['key']} held {step['ms']}ms"
    if t == "type":
        return f'type "{step["text"]}"'
    if t == "wait":
        return f"wait {step['ms']}ms"
    if t == "run_macro":
        return f"macro {step['name']}"
    if t == "random_key":
        return f"random_key {step['keys']} held {step.get('hold_ms', '?')}ms"
    if t == "random_mouse":
        return "random_mouse"
    if t == "veto":
        return f"veto {step['groups']}"
    return t


def format_events(events: list[dict], *, verbose: bool = False) -> list[str]:
    lines = []
    for ev in events:
        kind = ev["kind"]
        if kind == "fire":
            steps = "; ".join(_step_summary(s) for s in ev["steps"]) or "(no-op)"
            lines.append(f"FIRE {ev['group']}: {steps}")
        elif kind == "failsafe":
            steps = "; ".join(_step_summary(s) for s in ev["steps"]) or "(no-op)"
            lines.append(f"FAILSAFE ({ev['why']}) {ev['group']}: {steps}")
        elif kind == "veto":
            lines.append(f"VETO {ev['groups']} — \"{ev['reason']}\" ({ev['ms']}ms)")
        elif verbose and kind == "choose":
            lines.append(f"  choose → option {ev['picked'] + 1} of {ev['of']}")
        elif verbose and kind == "skip":
            lines.append(f"  skip {ev['target']} ({ev['why']})")
        elif kind == "error":
            lines.append(f"ERROR: {ev['why']}")
    return lines
