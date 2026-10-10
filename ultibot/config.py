"""Profile loading and validation — Phase 0.

Enforces the tree's laws from PLAN.md (v5, locked):

1. STRUCTURE — the root object needs "profile" (a name) and "tree" (a list of
   nodes). "groups" / "macros" are dicts of named step lists.
2. FAILSAFE-FIRST-ENTRY — the root level's first node must be a $ref to a
   group marked "failsafe": true. That's the landing pad for the default else
   (PLAN §2, decision 2). Nested lists containing questions that don't start
   with a failsafe ref get a *warning*: their default else will bubble all
   the way up to the root failsafe.
3. $REF INTEGRITY — every group/macro referenced actually exists.
4. VOCABULARY — conditions and actions use known types with their required
   fields present (PLAN §2).

Stdlib only, on purpose: the validator must run anywhere, no venv required.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ------------------------------------------------------------- vocabulary --

COMBINATORS = ("and", "or", "not")

CONDITION_LEAVES = (
    "image",           # template found on screen?
    "stat",            # OCR sensor comparison (Phase 5)
    "pixel",           # color at a point
    "cooldown_ready",  # a group's cooldown expired?
    "timer",           # true every N ms
    "once",            # true exactly once per session
    "chance",          # true with probability p
    "at",              # Atlas: at a named place (Phase 7)
    "near",            # Atlas: within radius of a named place (Phase 7)
)

ACTION_TYPES = (
    "click", "right_click", "double_click", "move_to",
    "key", "key_hold", "type", "wait",
    "run_macro", "choose", "random_key", "random_mouse", "veto",
)

STAT_OPS = ("<", ">", "<=", ">=", "==", "!=", "changed")

# Optional fields any action step may carry (PLAN §2: chance gates,
# verify/retry/on_fail contingencies).
STEP_EXTRAS = ("chance", "verify", "retry", "on_fail", "then")

SETTING_KEYS = (
    "tick_rate_hz", "kill_hotkey", "max_actions_per_minute",
    "humanize", "match_mode",
)


# --------------------------------------------------------------- results --

@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def warn(self, path: str, message: str) -> None:
        self.warnings.append(f"{path}: {message}")


class ProfileError(Exception):
    """Raised by load_profile when a profile fails validation."""

    def __init__(self, result: ValidationResult, source: str):
        self.result = result
        self.source = source
        lines = [f"Invalid profile {source}:"]
        lines += [f"  ERROR {e}" for e in result.errors]
        lines += [f"  warn  {w}" for w in result.warnings]
        super().__init__("\n".join(lines))


# --------------------------------------------------------------- helpers --

def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_ms(v: Any) -> bool:
    """A duration: positive number, or [min, max] with 0 < min <= max."""
    if _is_num(v):
        return v > 0
    return (
        isinstance(v, list) and len(v) == 2
        and all(_is_num(x) for x in v) and 0 < v[0] <= v[1]
    )


def _is_point(v: Any) -> bool:
    return isinstance(v, list) and len(v) == 2 and all(_is_num(x) for x in v)


def _is_probability(v: Any) -> bool:
    return _is_num(v) and 0 < v <= 1


# --------------------------------------------------------- the validator --

class _Validator:
    def __init__(self, data: Any):
        self.data = data
        self.r = ValidationResult()
        self.groups: dict[str, dict] = {}   # name -> {"steps": [...], "failsafe": bool}
        self.macros: set[str] = set()

    # -- entry point --------------------------------------------------------
    def run(self) -> ValidationResult:
        r, data = self.r, self.data
        if not isinstance(data, dict):
            r.error("$", "profile must be a JSON object")
            return r

        name = data.get("profile")
        if not isinstance(name, str) or not name.strip():
            r.error("$.profile", "must be a non-empty string (the game's name)")

        self._settings(data.get("settings"))
        # Two passes: collect every group/macro NAME first so that forward
        # references (a group $ref'ing a group defined later in the file)
        # validate cleanly, THEN validate the step bodies.
        pending = self._collect_groups(data.get("groups"))
        pending += self._collect_macros(data.get("macros"))
        for steps, path in pending:
            self._steps(steps, path)
        self._sensors(data.get("sensors"))
        self._tree(data.get("tree"))
        return r

    # -- settings -----------------------------------------------------------
    def _settings(self, s: Any) -> None:
        if s is None:
            return
        if not isinstance(s, dict):
            return self.r.error("$.settings", "must be an object")
        for key in s:
            if key not in SETTING_KEYS:
                self.r.warn(f"$.settings.{key}", "unknown setting (ignored)")
        if "tick_rate_hz" in s and not (_is_num(s["tick_rate_hz"]) and s["tick_rate_hz"] > 0):
            self.r.error("$.settings.tick_rate_hz", "must be a positive number")
        if "max_actions_per_minute" in s and not (
            isinstance(s["max_actions_per_minute"], int) and s["max_actions_per_minute"] > 0
        ):
            self.r.error("$.settings.max_actions_per_minute", "must be a positive integer")
        if "kill_hotkey" in s and not isinstance(s["kill_hotkey"], str):
            self.r.error("$.settings.kill_hotkey", "must be a string like \"f9\"")
        if "match_mode" in s and s["match_mode"] not in ("first_match", "all_matching"):
            self.r.error("$.settings.match_mode", "must be \"first_match\" or \"all_matching\"")
        h = s.get("humanize")
        if h is not None:
            if not isinstance(h, dict):
                return self.r.error("$.settings.humanize", "must be an object")
            if "click_jitter_px" in h and not (_is_num(h["click_jitter_px"]) and h["click_jitter_px"] >= 0):
                self.r.error("$.settings.humanize.click_jitter_px", "must be a number >= 0")
            if "action_delay_ms" in h and not _is_ms(h["action_delay_ms"]):
                self.r.error("$.settings.humanize.action_delay_ms", "must be ms or [min, max]")

    # -- groups / macros / sensors ------------------------------------------
    def _read_steps(self, value: Any, path: str) -> tuple[list, dict]:
        """A group is a bare list of steps, or an object:
        {"steps": [...], "failsafe": bool, "cooldown_ms": ms, "max_fires": n}."""
        meta: dict = {"failsafe": False}
        if isinstance(value, list):
            return value, meta
        if isinstance(value, dict):
            steps = value.get("steps")
            if not isinstance(steps, list):
                self.r.error(path, 'group object needs a "steps" list')
                return [], meta
            failsafe = value.get("failsafe", False)
            if not isinstance(failsafe, bool):
                self.r.error(f"{path}.failsafe", "must be true or false")
            meta["failsafe"] = failsafe
            if "cooldown_ms" in value:
                if not _is_ms(value["cooldown_ms"]):
                    self.r.error(f"{path}.cooldown_ms", "must be ms (positive number)")
                else:
                    meta["cooldown_ms"] = value["cooldown_ms"]
            if "max_fires" in value:
                if not (isinstance(value["max_fires"], int) and value["max_fires"] > 0):
                    self.r.error(f"{path}.max_fires", "must be a positive integer")
                else:
                    meta["max_fires"] = value["max_fires"]
            for key in set(value) - {"steps", "failsafe", "cooldown_ms", "max_fires"}:
                self.r.warn(f"{path}.{key}", "unknown group option (ignored)")
            return steps, meta
        self.r.error(path, "must be a list of steps or an object")
        return [], meta

    def _collect_groups(self, g: Any) -> list[tuple[list, str]]:
        if g is None:
            self.r.error("$.groups", "is required (at least a failsafe group)")
            return []
        if not isinstance(g, dict) or not g:
            self.r.error("$.groups", "must be a non-empty object")
            return []
        pending = []
        for name, value in g.items():
            steps, meta = self._read_steps(value, f"$.groups.{name}")
            self.groups[name] = {"steps": steps, **meta}
            pending.append((steps, f"$.groups.{name}"))
        if not any(gr["failsafe"] for gr in self.groups.values()):
            self.r.error(
                "$.groups",
                'no group is marked "failsafe": true — the default else needs '
                "a landing pad (PLAN §2, decision 2)",
            )
        return pending

    def _collect_macros(self, m: Any) -> list[tuple[list, str]]:
        if m is None:
            return []
        if not isinstance(m, dict):
            self.r.error("$.macros", "must be an object of name -> step list")
            return []
        pending = []
        for name, steps in m.items():
            if not isinstance(steps, list):
                self.r.error(f"$.macros.{name}", "must be a list of steps")
                continue
            self.macros.add(name)
            pending.append((steps, f"$.macros.{name}"))
        return pending

    def _sensors(self, s: Any) -> None:
        if s is None:
            return
        if not isinstance(s, dict):
            return self.r.error("$.sensors", "must be an object of name -> sensor definition")
        for name, spec in s.items():
            path = f"$.sensors.{name}"
            if not isinstance(spec, dict):
                self.r.error(path, "must be an object")
                continue
            if "offset" in spec and not _is_point(spec["offset"]):
                self.r.error(f"{path}.offset", "must be [x, y]")
            if "size" in spec and not _is_point(spec["size"]):
                self.r.error(f"{path}.size", "must be [width, height]")
            if "anchor" in spec and not isinstance(spec["anchor"], str):
                self.r.error(f"{path}.anchor", "must be an image filename")
            if "type" in spec and spec["type"] not in ("number", "text"):
                self.r.error(f"{path}.type", 'must be "number" or "text"')

    # -- the tree -------------------------------------------------------------
    def _tree(self, t: Any) -> None:
        if not isinstance(t, list) or not t:
            return self.r.error("$.tree", "must be a non-empty list of nodes")
        self._level(t, "$.tree", is_root=True)

    def _level(self, nodes: list, path: str, *, is_root: bool) -> None:
        """Validate one level (a sibling list of nodes).

        LAW (decision 2): the root level's first entry must be a $ref to a
        failsafe group — the landing pad for the default else. Nested levels
        that contain questions but lack the failsafe entry only warn, since
        their bubble-up will ascend to the root failsafe instead.
        """
        first = nodes[0] if nodes else None
        has_failsafe_first = (
            isinstance(first, dict) and set(first) == {"$ref"}
            and isinstance(first["$ref"], str)
            and self.groups.get(first["$ref"], {}).get("failsafe", False)
        )
        contains_question = any(isinstance(n, dict) and "if" in n for n in nodes)

        if is_root and not has_failsafe_first:
            self.r.error(
                f"{path}[0]",
                'the first entry of the tree must be {"$ref": <failsafe group>} '
                "— the default else lands here (PLAN §2, decision 2)",
            )
        elif not is_root and contains_question and not has_failsafe_first:
            self.r.warn(
                f"{path}[0]",
                "this level contains questions but no failsafe-first entry; "
                "a default else here bubbles all the way to the root failsafe",
            )

        for i, node in enumerate(nodes):
            self._node(node, f"{path}[{i}]")

    def _node(self, node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return self.r.error(path, "node must be an object")
        if "$ref" in node:
            if set(node) != {"$ref"}:
                return self.r.error(path, 'a $ref node must contain only "$ref"')
            target = node["$ref"]
            if not isinstance(target, str):
                return self.r.error(f"{path}.$ref", "must be a group name")
            if target not in self.groups:
                self.r.error(f"{path}.$ref", f'unknown group "{target}" ($ref integrity)')
            return
        if "if" in node:
            self._question(node, path)
            return
        if "type" in node:
            self._step(node, path)
            return
        self.r.error(path, 'unrecognized node (needs "if", "$ref", or "type")')

    def _question(self, node: dict, path: str) -> None:
        self._condition(node["if"], f"{path}.if")
        then = node.get("then")
        if then is None:
            self.r.error(f"{path}.then", 'a question needs a "then" branch')
        elif then != "continue":
            if not isinstance(then, list) or not then:
                self.r.error(f"{path}.then", 'must be "continue" or a non-empty list of nodes')
            else:
                self._level(then, f"{path}.then", is_root=False)
        else_ = node.get("else")
        if else_ is None:
            pass  # omitted = default else: bubble up to the nearest failsafe level
        elif else_ == "continue":
            pass
        elif isinstance(else_, list) and else_:
            self._level(else_, f"{path}.else", is_root=False)
        else:
            self.r.error(f"{path}.else", 'must be "continue" or a non-empty list of nodes')

    # -- conditions -----------------------------------------------------------
    def _condition(self, cond: Any, path: str) -> None:
        r = self.r
        if not isinstance(cond, dict):
            return r.error(path, "condition must be an object")

        if "and" in cond or "or" in cond:
            key = "and" if "and" in cond else "or"
            subs = cond[key]
            extra = set(cond) - {key}
            if extra:
                r.error(path, f'combinator "{key}" cannot be mixed with {sorted(extra)}')
            if not isinstance(subs, list) or len(subs) < 2:
                return r.error(f"{path}.{key}", "needs a list of at least 2 conditions")
            for i, c in enumerate(subs):
                self._condition(c, f"{path}.{key}[{i}]")
            return
        if "not" in cond:
            if set(cond) != {"not"}:
                r.error(path, '"not" cannot be combined with other keys')
            return self._condition(cond["not"], f"{path}.not")

        leaves = [k for k in cond if k in CONDITION_LEAVES]
        if len(leaves) != 1:
            return r.error(
                path,
                f"condition needs exactly one leaf of {list(CONDITION_LEAVES)} "
                f"(or a combinator), found keys {sorted(cond)}",
            )
        leaf = leaves[0]
        value = cond[leaf]
        lp = f"{path}.{leaf}"

        if leaf == "image":
            if not isinstance(value, str) or not value:
                r.error(lp, "must be an image filename (e.g. \"loot_icon.png\")")
            if "confidence" in cond and not _is_probability(cond["confidence"]):
                r.error(f"{lp}: confidence", "must be in (0, 1]")
            if "region" in cond and not (
                isinstance(cond["region"], list) and len(cond["region"]) == 4
                and all(_is_num(x) for x in cond["region"])
            ):
                r.error(f"{lp}: region", "must be [x, y, width, height]")
            if "anchor" in cond and not isinstance(cond["anchor"], str):
                r.error(f"{lp}: anchor", "must be an image filename")
            if "variants" in cond and not (
                isinstance(cond["variants"], list) and all(isinstance(v, str) for v in cond["variants"])
            ):
                r.error(f"{lp}: variants", "must be a list of image filenames")
            if "scales" in cond and not (
                isinstance(cond["scales"], list) and cond["scales"]
                and all(_is_num(s) and s > 0 for s in cond["scales"])
            ):
                r.error(f"{lp}: scales", "must be a list of positive numbers")
        elif leaf == "stat":
            ok = (
                isinstance(value, list) and len(value) in (2, 3)
                and isinstance(value[0], str) and value[1] in STAT_OPS
                and (len(value) == 2) == (value[1] == "changed")
                and (len(value) == 2 or _is_num(value[2]))
            )
            if not ok:
                r.error(lp, f'must be ["sensor", op, number] with op in {STAT_OPS} (or ["sensor", "changed"])')
        elif leaf == "pixel":
            if not isinstance(value, dict):
                r.error(lp, 'must be {"point": [x, y], "color": [r, g, b], "tolerance": n?}')
            else:
                if not _is_point(value.get("point")):
                    r.error(f"{lp}.point", "must be [x, y]")
                color = value.get("color")
                if not (isinstance(color, list) and len(color) == 3
                        and all(isinstance(c, int) and 0 <= c <= 255 for c in color)):
                    r.error(f"{lp}.color", "must be [r, g, b] with channels 0–255")
                if "tolerance" in value and not (_is_num(value["tolerance"]) and value["tolerance"] >= 0):
                    r.error(f"{lp}.tolerance", "must be a number >= 0")
        elif leaf == "cooldown_ready":
            if not isinstance(value, str):
                r.error(lp, "must be a group name")
            elif value not in self.groups:
                r.error(lp, f'unknown group "{value}" ($ref integrity)')
        elif leaf == "timer":
            if not _is_ms(value):
                r.error(lp, "must be ms (positive number)")
        elif leaf == "once":
            if not (isinstance(value, (bool, str))):
                r.error(lp, 'must be true or a name string (e.g. "once": "looted-chest")')
        elif leaf == "chance":
            if not _is_probability(value):
                r.error(lp, "must be in (0, 1]")
        elif leaf in ("at", "near"):
            if leaf == "at" and not isinstance(value, str):
                r.error(lp, "must be a place name")
            if leaf == "near" and not (
                isinstance(value, list) and len(value) == 2
                and isinstance(value[0], str) and _is_num(value[1]) and value[1] >= 0
            ):
                r.error(lp, 'must be ["place", radius]')

        unknown = set(cond) - {leaf} - {
            "confidence", "region", "anchor", "variants", "grayscale", "scales"
        }
        for key in unknown:
            r.warn(f"{lp}.{key}", "unknown condition option (ignored)")

    # -- action steps ---------------------------------------------------------
    def _steps(self, steps: list, path: str) -> None:
        if not steps:
            self.r.warn(path, "empty step list")
        for i, step in enumerate(steps):
            self._step(step, f"{path}[{i}]")

    def _step(self, step: Any, path: str) -> None:
        r = self.r
        if isinstance(step, dict) and set(step) == {"$ref"}:
            target = step["$ref"]
            if target not in self.groups:
                r.error(f"{path}.$ref", f'unknown group "{target}" ($ref integrity)')
            return
        if not isinstance(step, dict) or "type" not in step:
            return r.error(path, 'step must be an object with a "type"')

        t = step["type"]
        if t not in ACTION_TYPES:
            return r.error(f"{path}.type", f'unknown action type "{t}" (known: {list(ACTION_TYPES)})')
        sp = f"{path}.{t}"

        if t in ("key", "key_hold"):
            if not isinstance(step.get("key"), str) or not step.get("key"):
                r.error(f"{sp}.key", "must be a key name string")
            if t == "key_hold" and not _is_ms(step.get("ms")):
                r.error(f"{sp}.ms", "must be ms or [min, max]")
        elif t == "type":
            if not isinstance(step.get("text"), str):
                r.error(f"{sp}.text", "must be a string")
        elif t == "wait":
            if not _is_ms(step.get("ms")):
                r.error(f"{sp}.ms", "must be ms or [min, max]")
        elif t == "run_macro":
            name = step.get("name")
            if not isinstance(name, str):
                r.error(f"{sp}.name", "must be a macro name")
            elif name not in self.macros:
                r.error(f"{sp}.name", f'unknown macro "{name}" ($ref integrity)')
        elif t == "choose":
            options = step.get("options")
            if not isinstance(options, list) or not options:
                r.error(f"{sp}.options", "must be a non-empty list of weighted choices")
            else:
                for j, opt in enumerate(options):
                    op = f"{sp}.options[{j}]"
                    if not isinstance(opt, dict):
                        r.error(op, "must be an object")
                        continue
                    if "weight" in opt and not (_is_num(opt["weight"]) and opt["weight"] > 0):
                        r.error(f"{op}.weight", "must be a positive number")
                    actions = opt.get("actions")
                    if not isinstance(actions, list) or not actions:
                        r.error(f"{op}.actions", "must be a non-empty list of steps")
                    else:
                        self._steps(actions, f"{op}.actions")
        elif t == "random_key":
            keys = step.get("keys")
            if not (isinstance(keys, list) and keys and all(isinstance(k, str) for k in keys)):
                r.error(f"{sp}.keys", "must be a non-empty list of key names")
            if "hold_ms" in step and not _is_ms(step["hold_ms"]):
                r.error(f"{sp}.hold_ms", "must be ms or [min, max]")
        elif t == "random_mouse":
            if "radius_px" in step and not (_is_num(step["radius_px"]) and step["radius_px"] > 0):
                r.error(f"{sp}.radius_px", "must be a positive number")
        elif t == "veto":
            groups = step.get("groups")
            if not (isinstance(groups, list) and groups and all(isinstance(g, str) for g in groups)):
                r.error(f"{sp}.groups", "must be a non-empty list of group names")
            else:
                for g in groups:
                    if g not in self.groups:
                        r.error(f"{sp}.groups", f'unknown group "{g}" ($ref integrity)')
            if not isinstance(step.get("reason"), str) or not step.get("reason"):
                r.error(f"{sp}.reason", "a veto must state its reason (audit log)")
            if "ms" in step and not _is_ms(step["ms"]):
                r.error(f"{sp}.ms", "must be ms (positive number)")
        elif t == "move_to" and "target" not in step:
            r.error(f"{sp}.target", 'required ("match", {"x": .., "y": ..}, or {"image": ..})')

        # Contingency extras any step may carry (PLAN §2).
        if "chance" in step and not _is_probability(step["chance"]):
            r.error(f"{sp}.chance", "must be in (0, 1]")
        if "verify" in step:
            self._condition(step["verify"], f"{sp}.verify")
        if "retry" in step:
            retry = step["retry"]
            if not isinstance(retry, dict) or not (
                isinstance(retry.get("attempts"), int) and retry.get("attempts", 0) > 0
            ):
                r.error(f"{sp}.retry", 'must be {"attempts": n>0, "backoff_ms"?: ms}')
            elif "backoff_ms" in retry and not _is_ms(retry["backoff_ms"]):
                r.error(f"{sp}.retry.backoff_ms", "must be ms or [min, max]")
        if "on_fail" in step:
            on_fail = step["on_fail"]
            if not isinstance(on_fail, list) or not on_fail:
                r.error(f"{sp}.on_fail", "must be a non-empty list of steps")
            else:
                self._steps(on_fail, f"{sp}.on_fail")


# ------------------------------------------------------------ public API --

def validate_profile(data: Any) -> ValidationResult:
    """Validate a parsed profile dict against every law. Never raises."""
    return _Validator(data).run()


def load_profile(path: str | Path) -> dict:
    """Load a JSON profile and validate it. Raises ProfileError if invalid."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        result = ValidationResult()
        result.error("$", f"JSON syntax error: {exc}")
        raise ProfileError(result, str(path)) from None
    result = validate_profile(data)
    if not result.ok:
        raise ProfileError(result, str(path))
    return data
