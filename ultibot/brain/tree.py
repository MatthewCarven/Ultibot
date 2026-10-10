"""The tree walker — Phase 2.

Locked semantics (PLAN §2, decisions 1, 2, 8):

- Depth-first, top to bottom. The FIRST action group that fires stops the
  walk for the tick.
- The failsafe-first entry of a level is NOT walked during normal descent —
  it is the landing pad for the default else. It runs when a question with
  no explicit else fails ("bubble up one level, run the first entry",
  ascending until a level with a failsafe entry is found — the root always
  qualifies), or when a whole walk fires nothing.
- A group does not fire while it's vetoed (Conduct Layer), on cooldown, or
  past max_fires — the walk simply continues to the next node.
- The failsafe ignores cooldowns/vetoes: the landing pad must always land.

Phase 2 is dry-run only: "firing" means recording what WOULD happen to the
tick's event list (chance rolled, choose resolved, vetoes applied). The
Hands arrive in Phase 3 and subscribe to the very same events.
"""

from __future__ import annotations

from typing import Any

from .conditions import EngineState, Senses, evaluate

_DEFAULT = object()  # marker: question node had no explicit else


class Walker:
    def __init__(self, profile: dict, senses: Senses, state: EngineState):
        self.profile = profile
        self.senses = senses
        self.state = state
        # Groups may be bare step lists or {"steps": ..., ...} objects
        # (validator accepts both) — normalize to the object form.
        self.groups: dict[str, dict] = {
            name: ({"steps": g} if isinstance(g, list) else g)
            for name, g in profile.get("groups", {}).items()
        }
        self.events: list[dict] = []

    # -- one tick -----------------------------------------------------------
    def tick(self) -> list[dict]:
        self.events = []
        self.senses.begin_tick()
        fired = self._walk_level(self.profile["tree"], up_chain=())
        if not fired:
            self._bubble((self.profile["tree"],), why="nothing matched")
        return self.events

    # -- walking --------------------------------------------------------------
    def _is_failsafe_ref(self, node: Any) -> bool:
        return (
            isinstance(node, dict) and set(node) == {"$ref"}
            and self.groups.get(node["$ref"], {}).get("failsafe", False)
        )

    def _walk_level(self, nodes: list, up_chain: tuple) -> bool:
        """Walk one sibling list. Returns True if something fired (stop)."""
        for i, node in enumerate(nodes):
            if i == 0 and self._is_failsafe_ref(node):
                continue  # landing pad, not walked (decision 8)

            if not isinstance(node, dict):
                continue
            # chance gate on any node (PLAN §2)
            if "chance" in node and not self.state.roll(node["chance"]):
                self.events.append({"kind": "skip", "target": _label(node),
                                    "why": f"chance {node['chance']}"})
                continue

            if "$ref" in node:
                if self._fire_group(node["$ref"]):
                    return True
            elif "if" in node:
                if self._question(node, up_chain=(nodes, *up_chain)):
                    return True
            elif "type" in node:
                self._resolve_steps([node], fired=[])
                return True  # a bare action step always counts as fired
        return False

    def _question(self, node: dict, up_chain: tuple) -> bool:
        try:
            result = evaluate(node["if"], self.senses, self.state)
        except Exception as exc:  # one bad question must never kill the bot
            self.events.append({"kind": "error", "why": f"question error: {exc}"})
            result = False
        if result:
            branch = node.get("then", "continue")
        else:
            branch = node.get("else", _DEFAULT)

        if branch is _DEFAULT:
            self._bubble(up_chain, why="default else")
            return True
        if branch == "continue":
            return False
        return self._walk_level(branch, up_chain)

    def _bubble(self, chain: tuple, why: str) -> None:
        """Ascend levels until one starts with a failsafe entry; run it."""
        for level in chain:
            if level and self._is_failsafe_ref(level[0]):
                self._fire_failsafe(level[0]["$ref"], why)
                return
        # Unreachable for validated profiles: the root always has one.
        self.events.append({"kind": "error", "why": "no failsafe level found"})

    # -- firing -------------------------------------------------------------
    def _fire_group(self, name: str) -> bool:
        group = self.groups[name]
        if self.state.vetoed(name):
            self.events.append({"kind": "skip", "target": name, "why": "vetoed"})
            return False
        if not self.state.cooldown_ready(name):
            left = self.state.cooldown_left_ms(name)
            self.events.append({"kind": "skip", "target": name,
                                "why": f"cooldown {left:.0f}ms left"})
            return False
        if self.state.reached_max(name, group.get("max_fires")):
            self.events.append({"kind": "skip", "target": name, "why": "max_fires"})
            return False

        fired: list = []
        self._resolve_steps(group["steps"], fired)
        self.state.note_fired(name)
        self.events.append({"kind": "fire", "group": name, "steps": fired})
        return True

    def _fire_failsafe(self, name: str, why: str) -> None:
        """The landing pad always lands: no cooldown/veto/max checks."""
        fired: list = []
        self._resolve_steps(self.groups[name]["steps"], fired)
        self.state.note_fired(name)
        self.events.append({"kind": "failsafe", "group": name, "why": why, "steps": fired})

    def _resolve_steps(self, steps: list, fired: list) -> None:
        """Dry-run resolution of a step list: roll chance gates, pick choose
        options, apply vetoes, inline nested group refs."""
        for step in steps:
            if "chance" in step and not self.state.roll(step["chance"]):
                self.events.append({"kind": "skip", "target": step.get("type", "$ref"),
                                    "why": f"chance {step['chance']}"})
                continue
            if set(step) == {"$ref"}:
                self._resolve_steps(self.groups[step["$ref"]]["steps"], fired)
            elif step["type"] == "choose":
                options = step["options"]
                weights = [opt.get("weight", 1) for opt in options]
                picked = self.state.rng.choices(range(len(options)), weights=weights)[0]
                self.events.append({"kind": "choose", "picked": picked,
                                    "of": len(options)})
                self._resolve_steps(options[picked]["actions"], fired)
            elif step["type"] == "veto":
                ms = step.get("ms", 5000)
                self.state.apply_veto(step["groups"], ms)
                self.events.append({"kind": "veto", "groups": step["groups"],
                                    "reason": step["reason"], "ms": ms})
                fired.append(step)
            else:
                fired.append(step)


def _label(node: dict) -> str:
    if "$ref" in node:
        return node["$ref"]
    if "if" in node:
        return "question"
    return node.get("type", "?")
