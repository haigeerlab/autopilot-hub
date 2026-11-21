"""Unit tests for slice-7 admission: can the PR be merged automatically? (report-only)

Run: python3 -B -m unittest discover -s tests -v
Stdlib only; must pass on macOS /usr/bin/python3 (3.9) and ubuntu python3.
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from admission import decide as d  # noqa: E402
from risk import tiers  # noqa: E402

REPO = "haigeerlab/agent-relay"
TEMPLATES = tiers.load_templates()


def risk_of(name="docs/a.md", patch="@@ -1 +1 @@\n+x\n", config=None):
    f = {"filename": name, "status": "modified", "additions": 1, "deletions": 0, "patch": patch}
    return tiers.classify([f], TEMPLATES, config or {"extends": "plugin"})


def takeover(merge, extra=None):
    payload = {"module": "m", "spec": "human", "plan": "human", "merge": merge, "release": "human"}
    payload.update(extra or {})
    return "编排写入的接管档位\n<!-- autopilot-takeover:v1 " + json.dumps(payload) + " -->\n"


def degrade_issue(number, repo=REPO, level="L2"):
    block = json.dumps({"scope": {"repo": repo, "tier": "*"}, "level": level, "reason": "revert"})
    return {"number": number, "body": "降级\n<!-- autopilot-degrade:v1 " + block + " -->\n"}


def run(risk=None, comments=(), issues=()):
    return d.decide(risk=risk or risk_of(), comments=list(comments), degrade_issues=list(issues), repo=REPO)


def item(result, item_id):
    return next(i for i in result["items"] if i["id"] == item_id)


class RiskItemTest(unittest.TestCase):
    def test_low_tier_is_ok(self):
        res = run(risk_of("docs/a.md"))
        self.assertTrue(item(res, "risk")["ok"])
        self.assertIn("可自动合", item(res, "risk")["detail"])

    def test_mid_tier_is_not_ok_and_says_human_merges(self):
        res = run(risk_of("src/app.py", config={"extends": "plugin", "T3": ["src/**"]}))
        self.assertFalse(item(res, "risk")["ok"])
        self.assertIn("要你合", item(res, "risk")["detail"])

    def test_stop_tier_is_not_ok(self):
        res = run(risk_of(".github/workflows/x.yml"))
        self.assertFalse(item(res, "risk")["ok"])
        self.assertIn("停下交你", item(res, "risk")["detail"])
        self.assertIn("风险档 T4", res["headline"])


class TakeoverItemTest(unittest.TestCase):
    def test_auto_is_ok(self):
        self.assertTrue(item(run(comments=[takeover("auto")]), "takeover")["ok"])

    def test_human_is_not_ok(self):
        res = run(comments=[takeover("human")])
        self.assertFalse(item(res, "takeover")["ok"])
        self.assertIn("合并由人", res["headline"])

    def test_missing_comment_means_human_merges(self):
        res = run(comments=["随便一条评论"])
        self.assertFalse(item(res, "takeover")["ok"])
        self.assertIn("没有接管评论，合并按“人合”处理", item(res, "takeover")["detail"])

    def test_unparseable_block_is_not_ok_with_reason(self):
        res = run(comments=["<!-- autopilot-takeover:v1 {not json} -->"])
        self.assertFalse(item(res, "takeover")["ok"])
        self.assertIn("读不懂", item(res, "takeover")["detail"])

    def test_non_object_block_is_not_ok(self):
        self.assertFalse(item(run(comments=["<!-- autopilot-takeover:v1 [1] -->"]), "takeover")["ok"])

    def test_latest_block_wins(self):
        self.assertTrue(item(run(comments=[takeover("human"), "x", takeover("auto")]), "takeover")["ok"])
        self.assertFalse(item(run(comments=[takeover("auto"), takeover("human")]), "takeover")["ok"])

    def test_old_levels_comment_is_not_read(self):
        old = '<!-- autopilot-' + 'levels:v1 {"spec_guard":{"tier":"relaxed"}} -->'
        self.assertFalse(item(run(comments=[old]), "takeover")["ok"])


class GateItemTest(unittest.TestCase):
    def test_gate_is_never_ok_yet(self):
        res = run(comments=[takeover("auto")])
        self.assertFalse(item(res, "gate")["ok"])
        self.assertIn("第二批", item(res, "gate")["detail"])


class DegradeItemTest(unittest.TestCase):
    def test_no_issue_is_ok(self):
        self.assertTrue(item(run(), "degrade")["ok"])

    def test_open_issue_blocks_and_is_listed(self):
        res = run(issues=[degrade_issue(186), degrade_issue(190)])
        self.assertFalse(item(res, "degrade")["ok"])
        self.assertIn("#186", item(res, "degrade")["detail"])
        self.assertIn("#190", item(res, "degrade")["detail"])

    def test_level_is_ignored(self):
        self.assertFalse(item(run(issues=[degrade_issue(1, level="L5")]), "degrade")["ok"])
        self.assertFalse(item(run(issues=[degrade_issue(1, level="bogus")]), "degrade")["ok"])

    def test_other_repo_is_ignored(self):
        self.assertTrue(item(run(issues=[degrade_issue(1, repo="haigeerlab/tier-guard")]), "degrade")["ok"])

    def test_unparseable_block_blocks(self):
        res = run(issues=[{"number": 7, "body": "<!-- autopilot-degrade:v1 {not json} -->"}])
        self.assertFalse(item(res, "degrade")["ok"])
        self.assertIn("#7", item(res, "degrade")["detail"])

    def test_issue_without_block_is_ignored(self):
        self.assertTrue(item(run(issues=[{"number": 2, "body": "随便写的"}]), "degrade")["ok"])


class HeadlineTest(unittest.TestCase):
    def test_four_items_in_order(self):
        self.assertEqual([i["id"] for i in run()["items"]], ["risk", "takeover", "gate", "degrade"])

    def test_headline_never_says_ok_while_gate_is_missing(self):
        # Every other item satisfied; the gate evidence does not exist until batch 2.
        res = run(comments=[takeover("auto")])
        self.assertFalse(res["auto_merge"])
        self.assertEqual(res["headline"], "不会自动合并（硬卡：还没有证据）")

    def test_headline_ok_only_when_all_four_ok(self):
        gate = {"id": "gate", "ok": True, "detail": "有证据"}
        res = d.decide(risk=risk_of(), comments=[takeover("auto")], degrade_issues=[], repo=REPO, gate=gate)
        self.assertTrue(res["auto_merge"])
        self.assertEqual(res["headline"], "可以自动合并")
        # Dropping any single item flips it.
        cases = {
            "risk": dict(risk=risk_of(".github/workflows/x.yml")),
            "takeover": dict(comments=[takeover("human")]),
            "degrade": dict(degrade_issues=[degrade_issue(3)]),
        }
        for name, change in cases.items():
            args = dict(risk=risk_of(), comments=[takeover("auto")], degrade_issues=[], repo=REPO, gate=gate)
            args.update(change)
            res = d.decide(**args)
            self.assertFalse(res["auto_merge"], name)
            self.assertTrue(res["headline"].startswith("不会自动合并（"), name)
        res = d.decide(risk=risk_of(), comments=[takeover("auto")], degrade_issues=[], repo=REPO,
                       gate={"id": "gate", "ok": False, "detail": "x"})
        self.assertFalse(res["auto_merge"])

    def test_reasons_are_listed(self):
        res = run(risk_of(".github/workflows/x.yml"), issues=[degrade_issue(5)])
        self.assertTrue(res["headline"].startswith("不会自动合并（"))
        for part in ("风险档 T4", "没有接管评论", "硬卡", "#5"):
            self.assertIn(part, res["headline"])

    def test_author_note_and_risk_notes_present(self):
        res = run()
        self.assertIn(d.AUTHOR_NOTE, res["notes"])

    def test_inputs_not_mutated(self):
        risk = risk_of()
        before = json.dumps(risk, sort_keys=True)
        run(risk)
        self.assertEqual(json.dumps(risk, sort_keys=True), before)


class GlobTest(unittest.TestCase):
    def test_double_star_matches_nested(self):
        self.assertTrue(d.glob_match("src/core/**", "src/core/a/b/c.py"))

    def test_single_star_does_not_cross_slash(self):
        self.assertTrue(d.glob_match("bin/*", "bin/relay"))
        self.assertFalse(d.glob_match("bin/*", "bin/sub/relay"))

    def test_no_partial_prefix_match(self):
        self.assertFalse(d.glob_match("src/core/**", "src/core-extra/a.py"))


if __name__ == "__main__":
    unittest.main()
