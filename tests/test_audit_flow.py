"""S110 S5: new-flow compliance and read-only local verification records."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from audit import flow_check as f
from audit.cli import LiveIO
from orchestrator import cli as orchestrator_cli


def module(mid="m", **over):
    data = {"id": mid, "tier": "normal", "status": "in_progress", "progress": {"done": 1},
            "requirementConfirm": {"status": "confirmed", "by": "user"}, "designApproval": None}
    data.update(over)
    return data


def check(*modules, records=None):
    return f.check_flow({"modules": list(modules)}, records)


class FlowTest(unittest.TestCase):
    def test_unstarted_and_legacy_modules_are_ignored(self):
        result = check(module(progress={"done": 0}, requirementConfirm=None),
                       module(tier=None, status="done", requirementConfirm=None), records={})
        self.assertEqual(result, {"problems": [], "notes": []})

    def test_confirmed_by_user_is_clean(self):
        for tier in ("bug", "small", "normal"):
            with self.subTest(tier=tier):
                self.assertEqual(check(module(tier=tier))["problems"], [])

    def test_unconfirmed_requirement_is_severe(self):
        for confirmation in (None, [], {"status": "pending"}, {"status": "invalid"},
                             {"status": "confirmed", "by": "reviewer"}, {"status": "confirmed"}):
            with self.subTest(confirmation=confirmation):
                problems = check(module(requirementConfirm=confirmation))["problems"]
                self.assertEqual(len(problems), 1)
                self.assertEqual((problems[0]["kind"], problems[0]["severity"]),
                                 ("requirement-not-confirmed", "严重"))

    def test_revised_requirement_is_a_reminder(self):
        problems = check(module(requirementConfirm={"status": "revised"}))["problems"]
        self.assertEqual([(p["kind"], p["severity"]) for p in problems], [("requirement-revised", "提醒")])

    def test_important_design_requires_user(self):
        for approval in (None, [], {"status": "approved", "approvedBy": "reviewer"},
                         {"status": "pending-user"}, {"status": "approved"}):
            with self.subTest(approval=approval):
                problems = check(module(tier="important", designApproval=approval))["problems"]
                self.assertEqual([(p["kind"], p["severity"]) for p in problems],
                                 [("important-design-not-user", "严重")])
        self.assertEqual(check(module(tier="important", designApproval={
            "status": "approved", "approvedBy": "user", "at": "2026-11-26"}))["problems"], [])

    def test_normal_design_does_not_require_user(self):
        self.assertEqual(check(module(designApproval={"status": "approved", "approvedBy": "reviewer"}))["problems"], [])

    def test_done_with_passing_record_is_clean(self):
        for verdict in ("pass", "pass-with-notes"):
            with self.subTest(verdict=verdict):
                self.assertEqual(check(module(status="done", progress={"done": 0}),
                                       records={"m": {"verdict": verdict}})["problems"], [])

    def test_done_without_passing_record_is_severe(self):
        for records in ({}, {"m": None}, {"m": {"verdict": "fail"}}, {"m": {}}):
            with self.subTest(records=records):
                problems = check(module(status="done"), records=records)["problems"]
                self.assertEqual([(p["kind"], p["severity"]) for p in problems],
                                 [("merged-without-verification", "严重")])

    def test_missing_checkout_has_one_note_for_all_done_modules(self):
        result = check(module("a", status="done"), module("b", status="done"))
        self.assertEqual(result["problems"], [])
        notes = [n for n in result["notes"] if "判不了" in n]
        self.assertEqual(len(notes), 1)
        self.assertIn("a、b", notes[0])

    def test_unfinished_modules_skip_verification(self):
        result = check(module(), records={})
        self.assertEqual(result["problems"], [])
        self.assertFalse(any("判不了" in n for n in result["notes"]))

    def test_checked_modules_note_lists_first_eight(self):
        result = check(*[module("m%d" % i) for i in range(9)])
        self.assertEqual(len(result["notes"]), 1)
        self.assertIn("9 个", result["notes"][0])
        self.assertIn("、".join("m%d" % i for i in range(8)), result["notes"][0])
        self.assertNotIn("m8", result["notes"][0])
        self.assertTrue(result["notes"][0].endswith("等"))

    def test_unreadable_view(self):
        for view in (None, {}, {"modules": None}):
            with self.subTest(view=view):
                problems = f.check_flow(view, {})["problems"]
                self.assertEqual([(p["kind"], p["severity"]) for p in problems], [("map-unavailable", "提醒")])


class VerificationRecordsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = self.state / "config.json"
        self.state_patch = patch.object(orchestrator_cli, "STATE", str(self.state))
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.io = LiveIO()
        self.checkout = self.root / "checkout"

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.checkout)] + list(args),
                                       stderr=subprocess.PIPE, text=True).strip()

    def configure(self, checkout):
        self.config.write_text(json.dumps({"checkouts": {"owner/repo": str(checkout)}}), encoding="utf-8")

    def init_checkout(self):
        self.checkout.mkdir()
        self.git("init", "-q")
        self.configure(self.checkout)
        records = self.checkout / ".git" / "spec-guard" / "verification"
        records.mkdir(parents=True)
        return records

    def test_reads_common_git_directory_from_linked_worktree(self):
        records = self.init_checkout()
        record = {"verdict": "pass", "patchId": "abc"}
        (records / "m.json").write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self.io.verification_records("owner/repo", ["m"]), {"m": record})
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "test")
        linked = self.root / "linked"
        self.git("worktree", "add", "-q", "-b", "linked", str(linked))
        self.configure(linked)
        self.assertEqual(self.io.verification_records("owner/repo", ["m"]), {"m": record})

    def test_invalid_ids_are_not_read(self):
        records = self.init_checkout()
        (records.parent / "outside.json").write_text('{"verdict": "pass"}', encoding="utf-8")
        invalid = ["../outside", ".", "..", "m/x", "", "m\n", "m\\x"]
        with patch("builtins.open", wraps=open) as opened:
            self.assertEqual(self.io.verification_records("owner/repo", invalid), dict.fromkeys(invalid))
        self.assertEqual([str(c.args[0]) for c in opened.call_args_list], [str(self.config)])

    def test_missing_invalid_and_non_object_records_are_none(self):
        records = self.init_checkout()
        for mid, text in (("broken", "{"), ("list", "[]"), ("null", "null")):
            (records / (mid + ".json")).write_text(text, encoding="utf-8")
        ids = ["missing", "broken", "list", "null"]
        self.assertEqual(self.io.verification_records("owner/repo", ids), dict.fromkeys(ids))

    def test_missing_configuration_checkout_or_git_directory_is_unavailable(self):
        self.assertIsNone(self.io.verification_records("owner/repo", ["m"]))
        self.configure(self.checkout)
        self.assertIsNone(self.io.verification_records("owner/repo", ["m"]))
        self.checkout.mkdir()
        self.assertIsNone(self.io.verification_records("owner/repo", ["m"]))


if __name__ == "__main__":
    unittest.main()
