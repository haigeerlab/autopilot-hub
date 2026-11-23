"""Slice 18b: Plan-approval side of the compliance audit (research audit-spec M2).

Run: python3 -B -m unittest discover -s tests -v
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from audit import plan_check as p


def module(mid="m", status="in_progress", done=3, spec="approved", plan="approved", by_spec="user", by_plan="user",
           route=None, reasons=()):
    return {"id": mid, "status": status, "step": "build", "progress": {"done": done, "total": done + 2},
            "review": {"spec": spec, "plan": plan}, "reviewApprovedBy": {"spec": by_spec, "plan": by_plan},
            "codexRoute": route if route is not None else {"spec": None, "plan": None},
            "important": {"reasons": list(reasons)}}


def run(*modules):
    return p.check_plan({"modules": list(modules)})


def kinds(result):
    return [x["kind"] for x in result["problems"]]


class StartedTest(unittest.TestCase):
    def test_started_means_done_or_ticked(self):
        self.assertTrue(p.started(module(status="done", done=0)))
        self.assertTrue(p.started(module(done=1)))
        self.assertFalse(p.started(module(done=0)))


class PlanApprovedTest(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(run(module(), module("n", plan="codex-approved", by_plan="codex"))["problems"], [])

    def test_unapproved_plan_with_work_is_severe(self):
        result = run(module(plan="pending", by_plan=None))
        self.assertEqual(kinds(result), ["plan-not-approved"])
        self.assertEqual(result["problems"][0]["severity"], "严重")

    def test_not_started_is_ignored(self):
        self.assertEqual(run(module(done=0, plan="pending", by_plan=None))["problems"], [])

    def test_revised_after_approval_is_a_reminder(self):
        result = run(module(plan="revised", by_plan=None))
        self.assertEqual((kinds(result), result["problems"][0]["severity"]), (["plan-revised"], "提醒"))

    def test_no_markers_cannot_be_judged(self):
        result = run(module(plan="none", by_plan=None))
        self.assertEqual(result["problems"], [])
        self.assertTrue(any("判不了" in n for n in result["notes"]))


class CodexRouteTest(unittest.TestCase):
    def test_codex_approval_not_allowed_by_takeover_is_severe(self):
        result = run(module(plan="codex-approved", by_plan=None, route={"spec": "takeover", "plan": "takeover"}))
        self.assertEqual(kinds(result), ["codex-not-allowed"])
        self.assertEqual(result["problems"][0]["severity"], "严重")
        self.assertIn("takeover", result["problems"][0]["detail"])


class ImportantTest(unittest.TestCase):
    def test_important_spec_must_be_user(self):
        result = run(module(spec="codex-approved", by_spec=None, reasons=["cross-module"],
                            route={"spec": "important", "plan": None}))
        self.assertIn("important-not-user", kinds(result))

    def test_important_spec_revised_after_user_approval_is_not_flagged(self):
        # real module-crew data: approved by the user, edited later -> spec-guard shows revised, approver null
        self.assertEqual(run(module(spec="revised", by_spec=None, reasons=["multi-module"]))["problems"], [])

    def test_important_spec_never_approved(self):
        self.assertIn("important-not-user", kinds(run(module(spec="pending", by_spec=None, reasons=["risk-path"]))))

    def test_important_spec_by_user_is_clean(self):
        self.assertEqual(run(module(reasons=["risk-path"]))["problems"], [])


class NotesTest(unittest.TestCase):
    def test_rounds_not_independently_verified(self):
        self.assertTrue(any("未独立核验" in n for n in run(module())["notes"]))

    def test_unreadable_view(self):
        result = p.check_plan(None)
        self.assertEqual(kinds(result), ["map-unavailable"])


if __name__ == "__main__":
    unittest.main()
