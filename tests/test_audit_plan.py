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
           route=None, reasons=(), rounds=None):
    return {"reviewRounds": rounds if rounds is not None else {"spec": [], "plan": []},"id": mid, "status": status, "step": "build", "progress": {"done": done, "total": done + 2},
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
        self.assertEqual(run(module(), module("n", plan="codex-approved", by_plan="codex",
                                              rounds={"spec": [], "plan": rnd(0)}))["problems"], [])

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

    def test_many_unmarked_modules_make_one_note(self):
        result = run(*[module("m%d" % i, plan="none", by_plan=None) for i in range(12)])
        unjudged = [n for n in result["notes"] if "判不了" in n]
        self.assertEqual(len(unjudged), 1)
        self.assertIn("12 个", unjudged[0])


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
        self.assertTrue(any("未独立核验" in n for n in run(old_view_module())["notes"]))

    def test_unreadable_view(self):
        result = p.check_plan(None)
        self.assertEqual(kinds(result), ["map-unavailable"])


if __name__ == "__main__":
    unittest.main()


def old_view_module():
    m = module()
    del m["reviewRounds"]
    return m


def rnd(*blocking):
    return [{"round": i + 1, "blocking": b, "sha256": "%012x" % i} for i, b in enumerate(blocking)]


class ReviewRoundsTest(unittest.TestCase):
    def codex(self, plan_rounds, spec_rounds=()):
        return run(module(plan="codex-approved", by_plan="codex", rounds={"spec": rnd(*spec_rounds), "plan": rnd(*plan_rounds)}))

    def test_clean_codex_approval(self):
        self.assertEqual(kinds(self.codex([1, 0])), [])

    def test_approval_round_with_blocking_is_severe(self):
        result = self.codex([0, 2])
        self.assertEqual(kinds(result), ["codex-approved-with-blocking"])
        self.assertEqual(result["problems"][0]["severity"], "严重")

    def test_two_blocking_rounds_must_go_to_user(self):
        self.assertEqual(kinds(self.codex([1, 1, 0])), ["rounds-not-escalated"])

    def test_blocking_rounds_count_across_the_file_like_review_mark(self):
        # review_mark counts every blocking round in the file, also before a revision
        self.assertEqual(kinds(self.codex([1, 0, 1, 0])), ["rounds-not-escalated"])
        self.assertEqual(kinds(self.codex([1, 1, 0, 0])), ["rounds-not-escalated"])  # round 3 should not exist
        self.assertEqual(kinds(self.codex([1, 0, 0])), [])

    def test_codex_approval_without_rounds_is_a_reminder(self):
        result = self.codex([])
        self.assertEqual((kinds(result), result["problems"][0]["severity"]), (["codex-approved-without-rounds"], "提醒"))

    def test_spec_rounds_are_checked_too(self):
        m = module(spec="codex-approved", by_spec="codex", rounds={"spec": rnd(1, 1, 0), "plan": []})
        self.assertEqual(kinds(run(m)), ["rounds-not-escalated"])

    def test_user_approval_ignores_rounds(self):
        m = module(rounds={"spec": [], "plan": rnd(1, 1, 3)})
        self.assertEqual(kinds(run(m)), [])

    def test_rounds_present_drop_the_unverified_note(self):
        self.assertFalse(any("未独立核验" in n for n in run(module())["notes"]))


class FlowV2ModulesTest(unittest.TestCase):
    """New-flow modules (tier set by spec-guard flow v2) auto-approve their Plan; S110's new-flow checks judge them."""

    def flow_v2(self, **kw):
        m = module(plan="pending", by_plan="auto", spec="pending", by_spec=None, **kw)
        m["tier"] = "normal"
        return m

    def test_flow_v2_module_is_not_judged_by_plan_approval(self):
        result = run(self.flow_v2())
        self.assertEqual(result["problems"], [])

    def test_flow_v2_module_is_noted(self):
        notes = run(self.flow_v2())["notes"]
        self.assertTrue(any("新流程模块改由新流程检查判断（需求确认、重要需求技术方案、合并前验证）" in n for n in notes))
        self.assertFalse(any("尚未接入" in n for n in notes))

    def test_flow_v2_skips_important_spec_and_rounds_too(self):
        m = module(spec="codex-approved", by_spec="codex", plan="codex-approved", by_plan="codex",
                   reasons=("新模块",), rounds={"spec": rnd(1, 1, 0), "plan": rnd(2)})
        m["tier"] = "important"
        self.assertEqual(run(m)["problems"], [])

    def test_flow_v2_note_truncates_after_eight(self):
        mods = []
        for i in range(9):
            m = self.flow_v2()
            m["id"] = "f%d" % i
            mods.append(m)
        note = [n for n in run(*mods)["notes"] if "新流程" in n][0]
        self.assertIn("9 个", note)
        self.assertTrue(note.endswith("等"))

    def test_old_module_without_tier_is_still_checked(self):
        m = module(plan="pending", by_plan=None)
        m["tier"] = None
        self.assertEqual(kinds(run(m)), ["plan-not-approved"])
