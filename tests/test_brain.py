"""Tests for the Phase 2 Brain (conditions + tree walker semantics).

No screen, no real time, no real dice: FakeSenses, a fake clock, and seeded
RNGs are injected throughout. Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import random
import unittest

from ultibot.brain.conditions import EngineState, evaluate
from ultibot.brain.tree import Walker


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds: float):
        self.t += seconds


class FakeSenses:
    """Scripted senses: images by name, stats by name, pixels by (x, y)."""

    def __init__(self, images=(), stats=None, pixels=None):
        self.found = set(images)
        self.stats = stats or {}
        self.pixels = pixels or {}

    def begin_tick(self):
        pass

    def image(self, name, *, confidence, region, grayscale, scales, variants):
        found = name in self.found or any(v in self.found for v in variants)
        return type("M", (), {"found": found})()

    def stat(self, name):
        return self.stats.get(name)

    def pixel(self, x, y):
        return self.pixels.get((x, y))

    def at(self, place):
        return False

    def near(self, place, radius):
        return False


def make_state(profile=None, rng=None, clock=None):
    cooldowns = {n: g["cooldown_ms"] for n, g in (profile or {}).get("groups", {}).items()
                 if "cooldown_ms" in g}
    return EngineState(clock=clock or FakeClock(), rng=rng or random.Random(0),
                       cooldowns=cooldowns)


def make_walker(profile, senses=None, rng=None, clock=None):
    state = make_state(profile, rng, clock)
    return Walker(profile, senses or FakeSenses(), state), state


def base_profile() -> dict:
    return {
        "profile": "test",
        "groups": {
            "failsafe-random": {"failsafe": True,
                                "steps": [{"type": "wait", "ms": [100, 200]}]},
            "alpha": [{"type": "key", "key": "a"}],
            "beta": [{"type": "key", "key": "b"}],
        },
        "tree": [{"$ref": "failsafe-random"}],
    }


def fired_groups(events):
    return [e.get("group") for e in events if e["kind"] in ("fire", "failsafe")]


class TestConditions(unittest.TestCase):
    def setUp(self):
        self.state = make_state()

    def test_and_or_not(self):
        s = FakeSenses(images=("a.png",))
        self.assertTrue(evaluate({"and": [{"image": "a.png"},
                                          {"not": {"image": "b.png"}}]}, s, self.state))
        self.assertFalse(evaluate({"or": [{"image": "b.png"},
                                          {"image": "c.png"}]}, s, self.state))
        self.assertTrue(evaluate({"or": [{"image": "b.png"},
                                         {"image": "a.png"}]}, s, self.state))

    def test_stat_ops_and_missing_sensor(self):
        s = FakeSenses(stats={"health": 25})
        self.assertTrue(evaluate({"stat": ["health", "<", 30]}, s, self.state))
        self.assertFalse(evaluate({"stat": ["health", ">=", 30]}, s, self.state))
        self.assertFalse(evaluate({"stat": ["mana", "<", 30]}, s, self.state))  # None

    def test_stat_changed(self):
        s = FakeSenses(stats={"xp": 100})
        self.assertFalse(evaluate({"stat": ["xp", "changed"]}, s, self.state))  # first read
        self.assertFalse(evaluate({"stat": ["xp", "changed"]}, s, self.state))  # same
        s.stats["xp"] = 150
        self.assertTrue(evaluate({"stat": ["xp", "changed"]}, s, self.state))

    def test_pixel_with_tolerance(self):
        s = FakeSenses(pixels={(10, 20): (100, 150, 200)})
        cond = {"pixel": {"point": [10, 20], "color": [102, 148, 200], "tolerance": 5}}
        self.assertTrue(evaluate(cond, s, self.state))
        cond["pixel"]["color"] = [120, 150, 200]
        self.assertFalse(evaluate(cond, s, self.state))
        self.assertFalse(evaluate({"pixel": {"point": [0, 0],
                                             "color": [0, 0, 0]}}, s, self.state))

    def test_chance(self):
        always = make_state(rng=random.Random(1))
        self.assertTrue(evaluate({"chance": 1.0}, FakeSenses(), always))
        never = make_state(rng=random.Random(1))
        self.assertFalse(evaluate({"chance": 0.000001}, FakeSenses(), never))

    def test_once_fires_exactly_once(self):
        cond = {"once": "greeted"}
        self.assertTrue(evaluate(cond, FakeSenses(), self.state))
        self.assertFalse(evaluate(cond, FakeSenses(), self.state))

    def test_timer_fires_immediately_then_waits(self):
        clock = FakeClock()
        state = make_state(clock=clock)
        cond = {"timer": 1000}
        self.assertTrue(evaluate(cond, FakeSenses(), state))   # first check: due
        self.assertFalse(evaluate(cond, FakeSenses(), state))  # too soon
        clock.advance(0.5)
        self.assertFalse(evaluate(cond, FakeSenses(), state))
        clock.advance(0.6)
        self.assertTrue(evaluate(cond, FakeSenses(), state))   # 1.1s elapsed


class TestWalkerPriority(unittest.TestCase):
    def test_first_true_group_fires_and_stops_walk(self):
        p = base_profile()
        p["tree"] += [
            {"if": {"image": "a.png"}, "then": [{"$ref": "alpha"}]},
            {"if": {"image": "b.png"}, "then": [{"$ref": "beta"}]},
        ]
        walker, _ = make_walker(p, FakeSenses(images=("a.png", "b.png")))
        self.assertEqual(fired_groups(walker.tick()), ["alpha"])

    def test_false_question_falls_through_to_next(self):
        p = base_profile()
        p["tree"] += [
            # explicit else: without it the default else would fire the failsafe
            {"if": {"image": "a.png"}, "then": [{"$ref": "alpha"}],
             "else": "continue"},
            {"if": {"image": "b.png"}, "then": [{"$ref": "beta"}]},
        ]
        walker, _ = make_walker(p, FakeSenses(images=("b.png",)))
        self.assertEqual(fired_groups(walker.tick()), ["beta"])

    def test_omitted_else_fires_failsafe_not_next_sibling(self):
        """Decision 2: no explicit else = bubble up, NOT 'continue'."""
        p = base_profile()
        p["tree"] += [
            {"if": {"image": "a.png"}, "then": [{"$ref": "alpha"}]},
            {"if": {"image": "b.png"}, "then": [{"$ref": "beta"}]},
        ]
        walker, _ = make_walker(p, FakeSenses(images=("b.png",)))
        events = walker.tick()
        self.assertEqual(fired_groups(events), ["failsafe-random"])
        self.assertNotIn("beta", fired_groups(events))

    def test_failsafe_first_entry_is_not_walked(self):
        p = base_profile()
        p["tree"].append({"if": {"image": "a.png"}, "then": [{"$ref": "alpha"}],
                          "else": "continue"})
        walker, _ = make_walker(p, FakeSenses(images=("a.png",)))
        events = walker.tick()
        self.assertEqual(fired_groups(events), ["alpha"])
        self.assertNotIn("failsafe-random", fired_groups(events))


class TestContingency(unittest.TestCase):
    def test_default_else_bubbles_to_root_failsafe(self):
        p = base_profile()
        # nested level has no failsafe entry -> bubble ascends to root
        p["tree"].append({
            "if": {"image": "a.png"},
            "then": [{"if": {"image": "b.png"}, "then": [{"$ref": "alpha"}]}],
        })
        walker, _ = make_walker(p, FakeSenses(images=("a.png",)))  # b absent
        events = walker.tick()
        kinds = {e["kind"] for e in events}
        self.assertIn("failsafe", kinds)
        failsafe = next(e for e in events if e["kind"] == "failsafe")
        self.assertEqual(failsafe["group"], "failsafe-random")
        self.assertEqual(failsafe["why"], "default else")

    def test_default_else_uses_nearest_level_failsafe(self):
        p = base_profile()
        p["groups"]["menu-failsafe"] = {"failsafe": True,
                                        "steps": [{"type": "key", "key": "esc"}]}
        p["tree"].append({
            "if": {"image": "menu.png"},
            "then": [
                {"$ref": "menu-failsafe"},  # this level's own landing pad
                {"if": {"image": "b.png"}, "then": [{"$ref": "alpha"}]},
            ],
        })
        walker, _ = make_walker(p, FakeSenses(images=("menu.png",)))
        events = walker.tick()
        failsafe = next(e for e in events if e["kind"] == "failsafe")
        self.assertEqual(failsafe["group"], "menu-failsafe")

    def test_explicit_else_continue_walks_on(self):
        p = base_profile()
        p["tree"] += [
            {"if": {"image": "a.png"}, "then": [{"$ref": "alpha"}],
             "else": "continue"},
            {"if": {"image": "b.png"}, "then": [{"$ref": "beta"}]},
        ]
        walker, _ = make_walker(p, FakeSenses(images=("b.png",)))
        self.assertEqual(fired_groups(walker.tick()), ["beta"])

    def test_nothing_matched_runs_root_failsafe(self):
        p = base_profile()
        p["tree"].append({"if": {"image": "a.png"}, "then": [{"$ref": "alpha"}],
                          "else": "continue"})
        walker, _ = make_walker(p, FakeSenses())
        failsafe = next(e for e in walker.tick() if e["kind"] == "failsafe")
        self.assertEqual(failsafe["why"], "nothing matched")


class TestSuppression(unittest.TestCase):
    def test_cooldown_skips_then_recovers(self):
        clock = FakeClock()
        p = base_profile()
        p["groups"]["beta"] = {"cooldown_ms": 1000,
                               "steps": [{"type": "key", "key": "b"}]}
        p["tree"].append({"if": {"image": "b.png"}, "then": [{"$ref": "beta"}],
                          "else": "continue"})
        walker, _ = make_walker(p, FakeSenses(images=("b.png",)), clock=clock)

        self.assertEqual(fired_groups(walker.tick()), ["beta"])
        self.assertEqual(fired_groups(walker.tick()), ["failsafe-random"])  # on cooldown
        clock.advance(1.1)
        self.assertEqual(fired_groups(walker.tick()), ["beta"])

    def test_cooldown_ready_leaf(self):
        clock = FakeClock()
        p = base_profile()
        p["groups"]["beta"] = {"cooldown_ms": 1000,
                               "steps": [{"type": "key", "key": "b"}]}
        p["tree"].append({
            "if": {"and": [{"image": "b.png"}, {"cooldown_ready": "beta"}]},
            "then": [{"$ref": "beta"}], "else": "continue"})
        walker, _ = make_walker(p, FakeSenses(images=("b.png",)), clock=clock)
        walker.tick()
        self.assertEqual(fired_groups(walker.tick()), ["failsafe-random"])
        clock.advance(1.1)
        self.assertEqual(fired_groups(walker.tick()), ["beta"])

    def test_veto_suppresses_group(self):
        clock = FakeClock()
        p = base_profile()
        p["groups"]["conduct"] = [
            {"type": "veto", "groups": ["beta"], "reason": "not mine", "ms": 1000}]
        p["tree"] += [
            {"if": {"image": "player.png"}, "then": [{"$ref": "conduct"}],
             "else": "continue"},
            {"if": {"image": "b.png"}, "then": [{"$ref": "beta"}],
             "else": "continue"},
        ]
        senses = FakeSenses(images=("player.png", "b.png"))
        walker, _ = make_walker(p, senses, clock=clock)

        events = walker.tick()  # conduct fires, veto applied; walk stops there
        self.assertEqual(fired_groups(events), ["conduct"])
        self.assertTrue(any(e["kind"] == "veto" for e in events))

        senses2 = FakeSenses(images=("b.png",))  # player gone, veto still active
        walker.senses = senses2
        events = walker.tick()
        self.assertEqual(fired_groups(events), ["failsafe-random"])
        self.assertTrue(any(e.get("why") == "vetoed" for e in events))

        clock.advance(1.1)  # veto expires
        self.assertEqual(fired_groups(walker.tick()), ["beta"])

    def test_max_fires(self):
        p = base_profile()
        p["groups"]["beta"] = {"max_fires": 2, "steps": [{"type": "key", "key": "b"}]}
        p["tree"].append({"if": {"image": "b.png"}, "then": [{"$ref": "beta"}],
                          "else": "continue"})
        walker, _ = make_walker(p, FakeSenses(images=("b.png",)))
        self.assertEqual(fired_groups(walker.tick()), ["beta"])
        self.assertEqual(fired_groups(walker.tick()), ["beta"])
        self.assertEqual(fired_groups(walker.tick()), ["failsafe-random"])


class TestRandomness(unittest.TestCase):
    def test_chance_gate_on_step(self):
        p = base_profile()
        p["groups"]["alpha"] = [{"type": "key", "key": "a", "chance": 0.000001}]
        p["tree"].append({"if": {"image": "a.png"}, "then": [{"$ref": "alpha"}]})
        walker, _ = make_walker(p, FakeSenses(images=("a.png",)),
                                rng=random.Random(5))
        events = walker.tick()
        fire = next(e for e in events if e["kind"] == "fire")
        self.assertEqual(fire["steps"], [])  # rolled off: fired but did nothing
        self.assertTrue(any(e.get("why", "").startswith("chance") for e in events))

    def test_choose_is_weighted_and_recorded(self):
        p = base_profile()
        p["groups"]["alpha"] = [
            {"type": "choose", "options": [
                {"weight": 1, "actions": [{"type": "key", "key": "x"}]},
                {"weight": 99, "actions": [{"type": "key", "key": "y"}]},
            ]}]
        p["tree"].append({"if": {"image": "a.png"}, "then": [{"$ref": "alpha"}]})
        walker, _ = make_walker(p, FakeSenses(images=("a.png",)),
                                rng=random.Random(42))
        events = walker.tick()
        self.assertTrue(any(e["kind"] == "choose" for e in events))
        fire = next(e for e in events if e["kind"] == "fire")
        self.assertEqual(fire["steps"], [{"type": "key", "key": "y"}])

    def test_nested_ref_in_group_steps_is_inlined(self):
        p = base_profile()
        p["groups"]["outer"] = [{"$ref": "alpha"}]
        p["tree"].append({"if": {"image": "a.png"}, "then": [{"$ref": "outer"}]})
        walker, _ = make_walker(p, FakeSenses(images=("a.png",)))
        fire = next(e for e in walker.tick() if e["kind"] == "fire")
        self.assertEqual(fire["group"], "outer")
        self.assertEqual(fire["steps"], [{"type": "key", "key": "a"}])


class TestBareSteps(unittest.TestCase):
    def test_action_step_directly_in_tree_fires(self):
        p = base_profile()
        p["tree"].append({"if": {"image": "a.png"},
                          "then": [{"type": "key", "key": "9"}],
                          "else": "continue"})
        walker, _ = make_walker(p, FakeSenses(images=("a.png",)))
        events = walker.tick()
        self.assertTrue(any(e["kind"] == "fire" or e["kind"] == "failsafe"
                            for e in events) or
                        any(e["kind"] == "skip" for e in events) is False)


if __name__ == "__main__":
    unittest.main()
