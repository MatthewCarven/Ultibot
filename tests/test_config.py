"""Tests for the Phase 0 profile validator (PLAN §2 laws).

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from ultibot.config import load_profile, ProfileError, validate_profile

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "profiles" / "example-game.json"


def minimal_profile() -> dict:
    """The smallest profile that passes every law."""
    return {
        "profile": "mini",
        "groups": {
            "failsafe-random": {
                "failsafe": True,
                "steps": [{"type": "wait", "ms": [100, 200]}],
            },
        },
        "tree": [{"$ref": "failsafe-random"}],
    }


class TestExampleProfile(unittest.TestCase):
    def test_example_on_disk_is_valid(self):
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        result = validate_profile(data)
        self.assertEqual(result.errors, [], msg="\n".join(result.errors))

    def test_load_profile_returns_dict(self):
        data = load_profile(EXAMPLE)
        self.assertEqual(data["profile"], "example-game")


class TestStructureLaws(unittest.TestCase):
    def test_minimal_profile_passes(self):
        self.assertTrue(validate_profile(minimal_profile()).ok)

    def test_missing_tree_fails(self):
        p = minimal_profile()
        del p["tree"]
        self.assertFalse(validate_profile(p).ok)

    def test_missing_groups_fails(self):
        p = minimal_profile()
        del p["groups"]
        self.assertFalse(validate_profile(p).ok)

    def test_not_an_object_fails(self):
        self.assertFalse(validate_profile([1, 2, 3]).ok)


class TestFailsafeLaw(unittest.TestCase):
    def test_root_must_start_with_failsafe_ref(self):
        p = minimal_profile()
        p["groups"]["combat"] = [{"type": "key", "key": "1"}]
        p["tree"] = [{"$ref": "combat"}, {"$ref": "failsafe-random"}]
        errors = validate_profile(p).errors
        self.assertTrue(any("failsafe" in e for e in errors))

    def test_group_not_marked_failsafe_cannot_be_first(self):
        p = minimal_profile()
        p["groups"]["failsafe-random"]["failsafe"] = False
        self.assertFalse(validate_profile(p).ok)

    def test_some_group_must_be_failsafe(self):
        p = minimal_profile()
        p["groups"]["failsafe-random"] = [{"type": "wait", "ms": 100}]
        p["tree"] = [{"$ref": "failsafe-random"}]
        errors = validate_profile(p).errors
        self.assertTrue(any("failsafe" in e for e in errors))

    def test_nested_question_without_failsafe_only_warns(self):
        p = minimal_profile()
        p["groups"]["combat"] = [{"type": "key", "key": "1"}]
        p["tree"].append({
            "if": {"image": "a.png"},
            "then": [{"if": {"image": "b.png"}, "then": [{"$ref": "combat"}]}],
        })
        result = validate_profile(p)
        self.assertTrue(result.ok, msg="\n".join(result.errors))
        self.assertTrue(any("bubbles" in w for w in result.warnings))


class TestRefIntegrity(unittest.TestCase):
    def test_unknown_group_ref_fails(self):
        p = minimal_profile()
        p["tree"].append({"$ref": "nope"})
        self.assertFalse(validate_profile(p).ok)

    def test_unknown_macro_fails(self):
        p = minimal_profile()
        p["groups"]["failsafe-random"]["steps"].append(
            {"type": "run_macro", "name": "ghost"}
        )
        self.assertFalse(validate_profile(p).ok)

    def test_veto_unknown_group_fails(self):
        p = minimal_profile()
        p["groups"]["failsafe-random"]["steps"].append(
            {"type": "veto", "groups": ["ghost"], "reason": "test"}
        )
        self.assertFalse(validate_profile(p).ok)


class TestVocabulary(unittest.TestCase):
    def test_unknown_action_type_fails(self):
        p = minimal_profile()
        p["groups"]["failsafe-random"]["steps"].append({"type": "explode"})
        errors = validate_profile(p).errors
        self.assertTrue(any("unknown action" in e for e in errors))

    def test_chance_out_of_range_fails(self):
        p = minimal_profile()
        p["groups"]["failsafe-random"]["steps"].append(
            {"type": "key", "key": "1", "chance": 1.5}
        )
        self.assertFalse(validate_profile(p).ok)

    def test_bad_condition_leaf_fails(self):
        p = minimal_profile()
        p["tree"].append({"if": {"smell": "nice"}, "then": [{"$ref": "failsafe-random"}]})
        self.assertFalse(validate_profile(p).ok)

    def test_question_needs_then(self):
        p = minimal_profile()
        p["tree"].append({"if": {"image": "a.png"}})
        self.assertFalse(validate_profile(p).ok)

    def test_else_may_be_omitted(self):
        """Decision 2: no explicit else = default bubble-up. Not an error."""
        p = minimal_profile()
        p["tree"].append({"if": {"image": "a.png"}, "then": [{"$ref": "failsafe-random"}]})
        self.assertTrue(validate_profile(p).ok)

    def test_choose_needs_options(self):
        p = minimal_profile()
        p["groups"]["failsafe-random"]["steps"].append({"type": "choose", "options": []})
        self.assertFalse(validate_profile(p).ok)

    def test_verify_must_be_a_condition(self):
        p = minimal_profile()
        p["groups"]["failsafe-random"]["steps"].append(
            {"type": "key", "key": "1", "verify": {"bogus": True}}
        )
        self.assertFalse(validate_profile(p).ok)

    def test_load_profile_raises_with_all_errors(self):
        p = minimal_profile()
        del p["tree"]
        bad = ROOT / "profiles" / "_bad_test.json"
        try:
            bad.write_text(json.dumps(p), encoding="utf-8")
            with self.assertRaises(ProfileError) as ctx:
                load_profile(bad)
            self.assertTrue(ctx.exception.result.errors)
        finally:
            bad.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
