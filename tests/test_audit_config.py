"""Slice 18a: compliance baseline and configuration checks (research audit-spec, M3/M4).

Run: python3 -B -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from audit import baselines as b
from audit import config_check as c
from risk import tiers

PIN = "7" * 40
OTHER = "8" * 40
REPO = "haigeerlab/agent-relay"


def baseline(**over):
    data = {"since": "2026-11-23", "approval": "agent-relay#194", "hub_pin": PIN,
            "takeover": {"project": "plan", "modules": {}},
            "checkup": {"C1": "ok", "C3": "ok", "C13": "missing"}}
    data.update(over)
    return data


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def caller(kind, pin_uses, pin_ref):
    return (f"jobs:\n  {kind}:\n    uses: haigeerlab/autopilot-hub/.github/workflows/{kind}.yml@{pin_uses}\n"
            f"    with:\n      hub_ref: {pin_ref}\n")


class BaselinesTest(unittest.TestCase):
    def test_load_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "baselines.json")
            with open(path, "w") as fh:
                json.dump({"schema": "autopilot.audit-baselines/v1", "repos": {REPO: baseline()}}, fh)
            self.assertEqual(b.load(path)[REPO]["hub_pin"], PIN)

    def test_problems_are_all_listed(self):
        bad = {"schema": "autopilot.audit-baselines/v1", "repos": {
            "nope": baseline(),
            REPO: baseline(since="yesterday", hub_pin="abc", checkup={"C1": "great"},
                           takeover={"project": "everything", "modules": {"m": 5}})}}
        with self.assertRaises(b.BaselineError) as ctx:
            b.validate(bad)
        text = "\n".join(ctx.exception.problems)
        for part in ("nope", "since", "hub_pin", "C1", "project", "m"):
            self.assertIn(part, text)

    def test_wrong_schema(self):
        with self.assertRaises(b.BaselineError):
            b.validate({"schema": "x", "repos": {}})

    def test_hub_file_is_t4(self):
        res = tiers.classify([{"filename": "audit/baselines.json", "status": "modified", "additions": 1,
                               "deletions": 0, "patch": "@@ -1 +1 @@\n+x\n"}], tiers.load_templates(), None)
        self.assertEqual(res["tier"], "T4")


class TakeoverItemsTest(unittest.TestCase):
    def test_presets_and_objects(self):
        self.assertEqual(c.items_of("none"), {"spec": "human", "plan": "human", "merge": "human", "release": "human"})
        self.assertEqual(c.items_of("plan")["plan"], "codex")
        self.assertEqual(c.items_of("merge"), {"spec": "codex", "plan": "codex", "merge": "auto", "release": "human"})
        self.assertEqual(c.items_of({"release": "prepare"})["release"], "prepare")
        self.assertIsNone(c.items_of("bogus"))
        self.assertIsNone(c.items_of({"merge": "yes"}))

    def test_wider_lists_each_item(self):
        self.assertEqual(c.wider(c.items_of("merge"), c.items_of("plan")),
                         [("spec", "human", "codex"), ("merge", "human", "auto")])
        self.assertEqual(c.wider(c.items_of("none"), c.items_of("plan")), [])
        self.assertEqual(c.wider(c.items_of({"release": "auto"}), c.items_of({"release": "auto-confirm"})),
                         [("release", "auto-confirm", "auto")])


class ReadTakeoverTest(unittest.TestCase):
    def test_spec_guard_project(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, "spec/CAPABILITY-MAP.md", "# map\n")
            write(root, ".agent/config.json", json.dumps({"takeover": "plan", "moduleTakeover": {"m": "merge"}}))
            self.assertEqual(c.read_takeover(root), {"project": "plan", "modules": {"m": "merge"}})

    def test_bad_module_entry_invalidates_the_key(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, "spec/CAPABILITY-MAP.md", "# map\n")
            write(root, ".agent/config.json", json.dumps({"takeover": "plan", "moduleTakeover": {"m": "merge", "x": 1}}))
            self.assertEqual(c.read_takeover(root), {"project": "plan", "modules": {}})

    def test_plain_repo_reads_takeover_yml(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, ".github/autopr/takeover.yml", "merge: auto\nrelease: prepare\n")
            self.assertEqual(c.read_takeover(root), {"project": {"merge": "auto", "release": "prepare"}, "modules": {}})

    def test_nothing_configured_is_none_preset(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(c.read_takeover(root), {"project": "none", "modules": {}})


class CheckTakeoverTest(unittest.TestCase):
    def test_same_as_baseline_is_clean(self):
        self.assertEqual(c.check_takeover({"project": "plan", "modules": {}}, baseline()), [])

    def test_project_widened_is_severe(self):
        problems = c.check_takeover({"project": "merge", "modules": {}}, baseline())
        self.assertEqual({p["severity"] for p in problems}, {"严重"})
        self.assertEqual(len(problems), 2)  # spec human→codex and merge human→auto
        self.assertTrue(any("merge" in p["detail"] for p in problems))

    def test_new_module_entry_wider_than_project_baseline(self):
        problems = c.check_takeover({"project": "plan", "modules": {"m": "merge"}}, baseline())
        self.assertTrue(problems and "m" in problems[0]["detail"])

    def test_narrower_is_clean(self):
        self.assertEqual(c.check_takeover({"project": "none", "modules": {}}, baseline()), [])

    def test_unreadable_current_value_is_reported(self):
        problems = c.check_takeover({"project": "bogus", "modules": {}}, baseline())
        self.assertEqual(problems[0]["kind"], "takeover-unreadable")


class PinsTest(unittest.TestCase):
    def test_matching_pins(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, ".github/workflows/autopilot-admission.yml", caller("admission", PIN, PIN))
            write(root, ".github/workflows/autopilot-degrade.yml", caller("degrade", PIN, PIN))
            self.assertEqual(c.check_pins(c.read_pins(root), baseline()), [])

    def test_changed_or_inconsistent_pin(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, ".github/workflows/autopilot-admission.yml", caller("admission", PIN, OTHER))
            write(root, ".github/workflows/autopilot-degrade.yml", caller("degrade", OTHER, OTHER))
            problems = c.check_pins(c.read_pins(root), baseline())
            self.assertTrue(any(p["kind"] == "pin-changed" for p in problems))

    def test_missing_caller_files(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(c.check_pins(c.read_pins(root), baseline())[0]["kind"], "pin-missing")


class CheckupAndDegradeTest(unittest.TestCase):
    def test_regression_only_from_ok(self):
        problems = c.check_checkup({"C1": "missing", "C3": "ok", "C13": "missing", "C14": "missing"}, baseline())
        self.assertEqual([p["detail"].split("：")[0] for p in problems], ["C1"])

    def test_unknown_counts_as_regression(self):
        self.assertEqual(len(c.check_checkup({"C1": "unknown", "C3": "ok"}, baseline())), 1)

    def test_open_degrade_issue(self):
        block = '<!-- autopilot-degrade:v1 {"scope": {"repo": "%s"}} -->' % REPO
        problems = c.check_degrade([{"number": 7, "body": block}], REPO)
        self.assertEqual(problems[0]["kind"], "degrade-open")
        self.assertIn("#7", problems[0]["detail"])
        self.assertEqual(c.check_degrade([], REPO), [])


if __name__ == "__main__":
    unittest.main()
