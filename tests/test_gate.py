"""Unit tests for slice-9 hard-gate evidence (docs/spec/slice-9.md).

Run: python3 -B -m unittest discover -s tests -v
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from admission import gate as g
from tests.test_admission import BOT, bot

HEAD = "a" * 40
OTHER = "b" * 40


def payload(**over):
    p = {
        "head_sha": HEAD,
        "runner": "autopilot-orchestrator",
        "checks": [{"cmd": "python3 -B -m unittest discover -s tests", "exit": 0, "output_sha256": "c" * 64}],
        "review": {"backend": "codex", "status": "completed", "fingerprint": "f" * 16, "blocking": 0, "rounds": 1},
        "acceptance": None,
    }
    p.update(over)
    return p


def evidence(p=None, **kw):
    return bot("硬卡证据\n<!-- autopilot-evidence:v1 " + json.dumps(p or payload()) + " -->\n", **kw)


def gate(comments, head=HEAD, module=None, bot_identity=BOT):
    notes = []
    item, _short = g.gate_item(list(comments), bot_identity, head, module, notes)
    return item, notes


class GateTest(unittest.TestCase):
    def test_good_evidence_is_ok(self):
        item, _ = gate([evidence()])
        self.assertTrue(item["ok"], item)
        self.assertIn(HEAD[:12], item["detail"])

    def test_no_evidence(self):
        item, _ = gate([])
        self.assertFalse(item["ok"])
        self.assertIn("还没有", item["detail"])

    def test_no_bot_identity(self):
        item, _ = gate([evidence()], bot_identity=None)
        self.assertFalse(item["ok"])
        self.assertIn("没配机器人身份", item["detail"])

    def test_unknown_head(self):
        item, _ = gate([evidence()], head=None)
        self.assertFalse(item["ok"])
        self.assertIn("当前提交", item["detail"])

    def test_human_or_edited_evidence_is_ignored_and_counted(self):
        human = {"login": "haigeerlab", "id": 1, "type": "User"}
        item, notes = gate([evidence(user=human, app=None), evidence(edited=True)])
        self.assertFalse(item["ok"])
        self.assertTrue(any("忽略了 2 条证据评论" in n for n in notes), notes)

    def test_other_sha_is_ignored_and_counted(self):
        item, notes = gate([evidence(payload(head_sha=OTHER))])
        self.assertFalse(item["ok"])
        self.assertIn("还没有", item["detail"])
        self.assertTrue(any("不是当前提交" in n for n in notes), notes)

    def test_latest_matching_evidence_wins(self):
        bad = payload(checks=[{"cmd": "x", "exit": 1, "output_sha256": "c" * 64}])
        self.assertTrue(gate([evidence(bad), evidence()])[0]["ok"])
        self.assertFalse(gate([evidence(), evidence(bad)])[0]["ok"])

    def test_failed_check(self):
        item, _ = gate([evidence(payload(checks=[{"cmd": "make test", "exit": 2, "output_sha256": "c" * 64}]))])
        self.assertFalse(item["ok"])
        self.assertIn("make test", item["detail"])

    def test_empty_or_malformed_checks(self):
        for checks in ([], None, [{"cmd": "", "exit": 0}], [{"cmd": "x", "exit": "0"}], [{"cmd": "x", "exit": True}], ["x"]):
            self.assertFalse(gate([evidence(payload(checks=checks))])[0]["ok"], checks)

    def test_review_must_be_completed_without_blocking(self):
        for review in ({"status": "completed", "fingerprint": "f", "blocking": 1},
                       {"status": "failed", "fingerprint": "f", "blocking": 0},
                       {"status": "completed", "fingerprint": "", "blocking": 0},
                       {"status": "completed", "fingerprint": "f", "blocking": "0"},
                       None):
            self.assertFalse(gate([evidence(payload(review=review))])[0]["ok"], review)

    def test_blocking_review_detail(self):
        item, _ = gate([evidence(payload(review={"status": "completed", "fingerprint": "f", "blocking": 2}))])
        self.assertIn("blocking", item["detail"])

    def test_spec_guard_module_needs_acceptance_on_same_sha(self):
        self.assertFalse(gate([evidence()], module="m")[0]["ok"])
        ok = payload(acceptance={"marker": "codex-approved", "sha": HEAD})
        self.assertTrue(gate([evidence(ok)], module="m")[0]["ok"])
        stale = payload(acceptance={"marker": "codex-approved", "sha": OTHER})
        self.assertFalse(gate([evidence(stale)], module="m")[0]["ok"])
        human = payload(acceptance={"marker": "approved", "sha": HEAD})
        self.assertTrue(gate([evidence(human)], module="m")[0]["ok"])
        other = payload(acceptance={"marker": "pending", "sha": HEAD})
        self.assertFalse(gate([evidence(other)], module="m")[0]["ok"])

    def test_no_module_skips_acceptance(self):
        self.assertTrue(gate([evidence()], module=None)[0]["ok"])

    def test_unparseable_block(self):
        item, _ = gate([bot("<!-- autopilot-evidence:v1 {nope} -->")])
        self.assertFalse(item["ok"])


class GateV2Test(unittest.TestCase):
    def payload(self, **over):
        value = payload(base_sha=OTHER, patch_id="c" * 40, module="m",
                        verification={"verdict": "pass", "report_sha256": "d" * 64,
                                      "regression_sha256": "e" * 64})
        del value["review"]
        del value["acceptance"]
        value.update(over)
        return value

    def evidence(self, value=None, **kw):
        return bot("<!-- autopilot-evidence:v2 " + json.dumps(value or self.payload()) + " -->", **kw)

    def test_good_verdicts_and_detail(self):
        for verdict in ("pass", "pass-with-notes"):
            value = self.payload()
            value["verification"]["verdict"] = verdict
            item, _ = gate([self.evidence(value)], module="m")
            self.assertTrue(item["ok"], item)
            self.assertIn("合并前验证 " + verdict, item["detail"])
            self.assertIn("c" * 12, item["detail"])

    def test_invalid_fields_and_fail_verdict(self):
        for key, values in {"verification": [None, {}, {"verdict": "fail"}, {"verdict": "unknown"}],
                            "base_sha": ["x" * 40, "a" * 39, None],
                            "patch_id": ["x" * 40, "a" * 41, None],
                            "module": [None, "", " ", 1]}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertFalse(gate([self.evidence(self.payload(**{key: value}))], module="m")[0]["ok"])
        for key in ("report_sha256", "regression_sha256"):
            for bad in (None, "f" * 63, "z" * 64):
                value = self.payload()
                value["verification"][key] = bad
                self.assertFalse(gate([self.evidence(value)], module="m")[0]["ok"])
        value = self.payload()
        value["verification"]["verdict"] = "fail"
        self.assertIn("合并前验证不通过", gate([self.evidence(value)], module="m")[0]["detail"])
        value = self.payload()
        del value["verification"]
        self.assertFalse(gate([self.evidence(value)], module="m")[0]["ok"])

    def test_checks_still_required(self):
        for checks in ([], [{"cmd": "x", "exit": 1}], [{"cmd": "x", "exit": True}]):
            self.assertFalse(gate([self.evidence(self.payload(checks=checks))], module="m")[0]["ok"])

    def test_module_binding(self):
        for module, detail in (("other", "证据的模块和接管评论不一致"),
                               (None, "v2 证据只用于 spec-guard 模块")):
            item, _ = gate([self.evidence()], module=module)
            self.assertFalse(item["ok"])
            self.assertIn(detail, item["detail"])

    def test_stale_edited_and_human_comments_are_ignored(self):
        for comment in (self.evidence(self.payload(head_sha=OTHER)), self.evidence(edited=True),
                        self.evidence(user={"id": 1, "type": "User"}, app=None)):
            item, notes = gate([comment], module="m")
            self.assertFalse(item["ok"])
            self.assertTrue(notes)

    def test_latest_comment_wins_across_versions(self):
        old = evidence(payload(acceptance={"marker": "approved", "sha": HEAD}))
        bad_old = evidence()
        new = self.evidence()
        bad_new = self.evidence(self.payload(verification={"verdict": "fail"}))
        for comments, ok in (([bad_old, new], True), ([new, bad_old], False),
                             ([bad_new, old], True), ([old, bad_new], False)):
            self.assertEqual(gate(comments, module="m")[0]["ok"], ok)


if __name__ == "__main__":
    unittest.main()
