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

from admission import decide as d
from risk import tiers

REPO = "haigeerlab/agent-relay"
TEMPLATES = tiers.load_templates()


def risk_of(name="docs/a.md", patch="@@ -1 +1 @@\n+x\n", config=None):
    f = {"filename": name, "status": "modified", "additions": 1, "deletions": 0, "patch": patch}
    return tiers.classify([f], TEMPLATES, config or {"extends": "plugin"})


BOT = {"app_slug": "haigeerlab-autopilot", "login": "haigeerlab-autopilot[bot]", "id": 359574582}
T0 = "2026-11-22T01:00:00Z"


def bot(body, *, edited=False, user=None, app="haigeerlab-autopilot"):
    """A PR comment as the workflow passes it: posted by the App bot unless `user`/`app` say otherwise."""
    return {"id": 1, "body": body, "user": user or {"login": BOT["login"], "id": BOT["id"], "type": "Bot"},
            "app": app, "created_at": T0, "updated_at": "2026-11-22T02:00:00Z" if edited else T0}


def takeover_body(merge, extra=None):
    payload = {"module": "m", "spec": "human", "plan": "human", "merge": merge, "release": "human"}
    payload.update(extra or {})
    return "编排写入的接管档位\n<!-- autopilot-takeover:v1 " + json.dumps(payload) + " -->\n"


def takeover(merge, extra=None, **kw):
    return bot(takeover_body(merge, extra), **kw)


def degrade_issue(number, repo=REPO, level="L2"):
    block = json.dumps({"scope": {"repo": repo, "tier": "*"}, "level": level, "reason": "revert"})
    return {"number": number, "body": "降级\n<!-- autopilot-degrade:v1 " + block + " -->\n"}


HEAD = "a" * 40


def evidence_body(sha=HEAD, exit_code=0):
    p = {"head_sha": sha, "runner": "autopilot-orchestrator",
         "checks": [{"cmd": "make test", "exit": exit_code, "output_sha256": "c" * 64}],
         "review": {"backend": "codex", "status": "completed", "fingerprint": "f" * 16, "blocking": 0, "rounds": 1},
         "acceptance": {"marker": "codex-approved", "sha": sha}}
    return "<!-- autopilot-evidence:v1 " + json.dumps(p) + " -->"


def run(risk=None, comments=(), issues=(), bot_identity=BOT, head=HEAD):
    return d.decide(risk=risk or risk_of(), comments=list(comments), degrade_issues=list(issues), repo=REPO,
                    bot=bot_identity, head_sha=head)


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
        res = run(comments=[bot("<!-- autopilot-takeover:v1 {not json} -->")])
        self.assertFalse(item(res, "takeover")["ok"])
        self.assertIn("读不懂", item(res, "takeover")["detail"])

    def test_non_object_block_is_not_ok(self):
        self.assertFalse(item(run(comments=[bot("<!-- autopilot-takeover:v1 [1] -->")]), "takeover")["ok"])

    def test_latest_block_wins(self):
        self.assertTrue(item(run(comments=[takeover("human"), "x", takeover("auto")]), "takeover")["ok"])
        self.assertFalse(item(run(comments=[takeover("auto"), takeover("human")]), "takeover")["ok"])

    def test_old_levels_comment_is_not_read(self):
        old = '<!-- autopilot-' + 'levels:v1 {"spec_guard":{"tier":"relaxed"}} -->'
        self.assertFalse(item(run(comments=[bot(old)]), "takeover")["ok"])


class BotIdentityTest(unittest.TestCase):
    """Slice 8: only takeover comments posted by the App bot and never edited count."""

    def test_human_comment_is_ignored_and_counted(self):
        human = {"login": "haigeerlab", "id": 1, "type": "User"}
        res = run(comments=[takeover("auto", user=human, app=None)])
        self.assertFalse(item(res, "takeover")["ok"])
        self.assertIn("没有接管评论", item(res, "takeover")["detail"])
        self.assertTrue(any("忽略了 1 条" in n for n in res["notes"]), res["notes"])

    def test_plain_string_comment_is_ignored(self):
        res = run(comments=[takeover_body("auto")])
        self.assertFalse(item(res, "takeover")["ok"])
        self.assertTrue(any("忽略了 1 条" in n for n in res["notes"]), res["notes"])

    def test_edited_bot_comment_is_ignored(self):
        res = run(comments=[takeover("auto", edited=True)])
        self.assertFalse(item(res, "takeover")["ok"])
        self.assertTrue(any("忽略了 1 条" in n for n in res["notes"]), res["notes"])

    def test_missing_timestamps_count_as_edited(self):
        c = takeover("auto")
        del c["updated_at"]
        self.assertFalse(item(run(comments=[c]), "takeover")["ok"])

    def test_other_bot_is_ignored(self):
        other = {"login": "github-actions[bot]", "id": 41898282, "type": "Bot"}
        self.assertFalse(item(run(comments=[takeover("auto", user=other, app="github-actions")]), "takeover")["ok"])

    def test_same_login_other_id_is_ignored(self):
        fake = {"login": BOT["login"], "id": 7, "type": "Bot"}
        self.assertFalse(item(run(comments=[takeover("auto", user=fake)]), "takeover")["ok"])

    def test_renamed_login_same_id_still_counts(self):
        renamed = {"login": "renamed[bot]", "id": BOT["id"], "type": "Bot"}
        self.assertTrue(item(run(comments=[takeover("auto", user=renamed)]), "takeover")["ok"])

    def test_wrong_app_slug_is_ignored(self):
        self.assertFalse(item(run(comments=[takeover("auto", app="someone-else")]), "takeover")["ok"])

    def test_forged_human_comment_after_bot_does_not_override(self):
        human = {"login": "haigeerlab", "id": 1, "type": "User"}
        res = run(comments=[takeover("human"), takeover("auto", user=human, app=None)])
        self.assertFalse(item(res, "takeover")["ok"])
        self.assertIn("合并 'human'", item(res, "takeover")["detail"])

    def test_no_bot_identity_never_auto_merges(self):
        res = run(comments=[takeover("auto"), bot(evidence_body())], bot_identity=None)
        self.assertFalse(res["auto_merge"])
        self.assertIn("没配机器人身份", item(res, "takeover")["detail"])
        self.assertIn("没配机器人身份", res["headline"])

    def test_load_bot_identity(self):
        self.assertEqual(d.load_bot_identity(os.path.join(ROOT, "bot-identity.json"))["id"], BOT["id"])
        self.assertIsNone(d.load_bot_identity(os.path.join(ROOT, "no-such-file.json")))

    def test_load_bot_identity_rejects_bad_shape(self):
        import tempfile
        for bad in ({"id": "359574582", "app_slug": "x"}, {"id": 1}, [1], {"id": True, "app_slug": "x"}):
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                json.dump(bad, fh)
            try:
                self.assertIsNone(d.load_bot_identity(fh.name), bad)
            finally:
                os.unlink(fh.name)


class VerifiedArtifactsFromEvidenceTest(unittest.TestCase):
    def test_current_evidence_lists_verified_artifacts(self):
        body = evidence_body().replace('"acceptance":', '"artifacts": {"module": "m", "verified": ["spec/m.md"]}, "acceptance":')
        self.assertEqual(d.verified_artifacts([bot(body)], BOT, HEAD), {"spec/m.md"})

    def test_v2_verified_artifacts(self):
        body = evidence_body().replace("autopilot-evidence:v1", "autopilot-evidence:v2").replace(
            '"runner":', '"artifacts": {"verified": ["spec/m.md", "tasks/m/plan.md", "src/a.py"]}, "runner":')
        self.assertEqual(d.verified_artifacts([bot(body)], BOT, HEAD), {"spec/m.md", "tasks/m/plan.md"})

    def test_untrusted_or_stale_evidence_verifies_nothing(self):
        body = evidence_body().replace('"acceptance":', '"artifacts": {"module": "m", "verified": ["spec/m.md"]}, "acceptance":')
        human = {"login": "haigeerlab", "id": 1, "type": "User"}
        self.assertEqual(d.verified_artifacts([bot(body, user=human, app=None)], BOT, HEAD), set())
        self.assertEqual(d.verified_artifacts([bot(body)], BOT, "b" * 40), set())
        self.assertEqual(d.verified_artifacts([bot(body)], None, HEAD), set())


class GateItemTest(unittest.TestCase):
    def test_no_evidence_is_not_ok(self):
        res = run(comments=[takeover("auto")])
        self.assertFalse(item(res, "gate")["ok"])
        self.assertIn("还没有", item(res, "gate")["detail"])

    def test_bot_evidence_on_head_is_ok(self):
        self.assertTrue(item(run(comments=[takeover("auto"), bot(evidence_body())]), "gate")["ok"])

    def test_takeover_module_requires_acceptance(self):
        no_acc = bot(evidence_body().replace('"acceptance": {"marker": "codex-approved", "sha": "' + HEAD + '"}',
                                             '"acceptance": null'))
        self.assertFalse(item(run(comments=[takeover("auto"), no_acc]), "gate")["ok"])
        plain = bot(takeover_body("auto").replace('"module": "m"', '"module": null'))
        self.assertTrue(item(run(comments=[plain, no_acc]), "gate")["ok"])


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

    def test_headline_without_evidence(self):
        res = run(comments=[takeover("auto")])
        self.assertFalse(res["auto_merge"])
        self.assertEqual(res["headline"], "不会自动合并（硬卡：还没有证据）")

    def test_headline_ok_only_when_all_four_ok(self):
        good = [takeover("auto"), bot(evidence_body())]
        res = run(comments=good)
        self.assertTrue(res["auto_merge"])
        self.assertEqual(res["headline"], "可以自动合并")
        # Dropping any single item flips it.
        cases = {
            "risk": dict(risk=risk_of(".github/workflows/x.yml")),
            "takeover": dict(comments=[takeover("human"), bot(evidence_body())]),
            "gate": dict(comments=[takeover("auto"), bot(evidence_body(exit_code=1))]),
            "stale": dict(comments=good, head="b" * 40),
            "degrade": dict(issues=[degrade_issue(3)]),
        }
        for name, change in cases.items():
            args = dict(comments=good)
            args.update(change)
            res = run(**args)
            self.assertFalse(res["auto_merge"], name)
            self.assertTrue(res["headline"].startswith("不会自动合并（"), name)

    def test_reasons_are_listed(self):
        res = run(risk_of(".github/workflows/x.yml"), issues=[degrade_issue(5)])
        self.assertTrue(res["headline"].startswith("不会自动合并（"))
        for part in ("风险档 T4", "没有接管评论", "硬卡", "#5"):
            self.assertIn(part, res["headline"])

    def test_no_author_note_any_more(self):
        self.assertFalse(hasattr(d, "AUTHOR_NOTE"))
        self.assertEqual(run()["notes"], [])

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
