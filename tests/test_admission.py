"""Unit tests for slice-1 admission (report-only trust cap from a readiness conclusion).

Run: python3 -B -m unittest discover -s tests -v
Stdlib only; must pass on macOS /usr/bin/python3 (3.9) and ubuntu python3.
"""
import copy
import datetime as dt
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from admission import decide as d  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


SCHEMA = json.load(open(os.path.join(ROOT, "schemas", "conclusion.v1.tmp.json"), encoding="utf-8"))


def run(conclusion="default", **kw):
    if conclusion == "default":
        conclusion = load("conclusion.basic.json")
    args = dict(
        conclusion=conclusion,
        schema=SCHEMA,
        repo="haigeerlab/agent-relay",
        default_branch="main",
        now=NOW,
        allowed_tools=load("allowed-tools.json"),
        changed_files=["README.md"],
        trust=None,
        degrade_levels=[],
    )
    args.update(kw)
    return d.decide(**args)


class CapTest(unittest.TestCase):
    def assertCap(self, result, cap, reason_part=None):
        self.assertEqual(result["cap"], cap, json.dumps(result, ensure_ascii=False, indent=1))
        self.assertTrue(result["report_only"])
        if reason_part:
            self.assertIn(reason_part, result["reason"])

    # --- acceptance §8.1 ---
    def test_valid_basic_conclusion_gives_L2(self):
        self.assertCap(run(), "L2")

    def test_missing_conclusion_is_L0(self):
        self.assertCap(run(conclusion=None), "L0", "没有接入结论")

    def test_failure_reason_states_the_cap(self):
        self.assertTrue(run(conclusion=None)["reason"].startswith("信任上限 L0（只报告）："))

    def test_expired_conclusion_is_L0(self):
        later = dt.datetime(2026, 12, 3, 0, 0, 0, tzinfo=UTC)  # == valid_until: expired
        self.assertCap(run(now=later), "L0", "过期")

    def test_tool_not_in_allowed_list_is_L0(self):
        c = load("conclusion.basic.json")
        c["basis"]["tool"]["sha"] = "3" * 40
        self.assertCap(run(conclusion=c), "L0", "允许列表")

    def test_tool_version_and_sha_must_match_together(self):
        c = load("conclusion.basic.json")
        c["basis"]["tool"]["version"] = "0.2.0"  # same SHA, different version
        self.assertCap(run(conclusion=c), "L0", "允许列表")

    def test_missing_allowed_list_is_L0(self):
        self.assertCap(run(allowed_tools=None), "L0", "允许列表")

    def test_schema_invalid_is_L0(self):
        c = load("conclusion.basic.json")
        c["trust_cap"] = "L9"
        self.assertCap(run(conclusion=c), "L0", "格式")

    def test_schema_missing_required_field_is_L0(self):
        c = load("conclusion.basic.json")
        del c["basis"]
        self.assertCap(run(conclusion=c), "L0", "格式")

    def test_repo_mismatch_is_L0(self):
        self.assertCap(run(repo="haigeerlab/tier-guard"), "L0", "仓库")

    def test_default_branch_mismatch_is_L0(self):
        self.assertCap(run(default_branch="master"), "L0", "默认分支")

    def test_single_app_uses_app_cap(self):
        self.assertCap(run(changed_files=["src/core/a.py", "src/core/sub/b.py"]), "L3")

    def test_cross_app_takes_lowest(self):
        self.assertCap(run(changed_files=["src/core/a.py", "src/cli/x.py"]), "L2")

    def test_root_path_uses_repo_cap(self):
        self.assertCap(run(changed_files=["src/core/a.py", ".github/workflows/ci.yml"]), "L2")

    def test_no_changed_files_uses_repo_cap(self):
        self.assertCap(run(changed_files=[]), "L2")

    def test_trust_lower_than_conclusion_wins(self):
        trust = {"version": 1, "levels": {"T0": "L2", "T1": "L2", "T2": "L0"}}
        self.assertCap(run(trust=trust), "L0", "trust")

    def test_trust_higher_than_conclusion_does_not_raise(self):
        trust = {"version": 1, "levels": {"T0": "L4", "T1": "L4"}}
        self.assertCap(run(trust=trust), "L2")

    def test_open_degrade_issue_lowers_cap(self):
        self.assertCap(run(degrade_levels=["L0"]), "L0", "降级")

    def test_unmet_conclusion_is_L0(self):
        c = load("conclusion.basic.json")
        c["tier"], c["trust_cap"] = "unmet", "L0"
        c["apps"] = []
        self.assertCap(run(conclusion=c), "L0")

    def test_bad_timestamp_is_L0(self):
        c = load("conclusion.basic.json")
        c["valid_until"] = "next week"
        self.assertCap(run(conclusion=c), "L0", "格式")

    def test_steps_are_reported_in_order(self):
        steps = [s["id"] for s in run()["steps"]]
        self.assertEqual(steps, ["exists", "schema", "repo", "valid_until", "tool", "app_cap", "limits"])

    def test_attestation_not_verified_is_flagged(self):
        self.assertIn("发布证明未校验", " ".join(run()["notes"]))

    def test_input_is_not_mutated(self):
        c = load("conclusion.basic.json")
        before = copy.deepcopy(c)
        run(conclusion=c)
        self.assertEqual(c, before)


class GlobTest(unittest.TestCase):
    def test_double_star_matches_nested(self):
        self.assertTrue(d.glob_match("src/core/**", "src/core/a/b/c.py"))

    def test_single_star_does_not_cross_slash(self):
        self.assertTrue(d.glob_match("bin/*", "bin/relay"))
        self.assertFalse(d.glob_match("bin/*", "bin/sub/relay"))

    def test_no_partial_prefix_match(self):
        self.assertFalse(d.glob_match("src/core/**", "src/core-extra/a.py"))


class DegradeParseTest(unittest.TestCase):
    def issue(self, payload):
        return {"body": "降级说明\n<!-- autopr-degrade:v1 " + json.dumps(payload) + " -->\n"}

    def test_parses_level_for_this_repo(self):
        iss = [self.issue({"scope": {"repo": "haigeerlab/agent-relay", "tier": "*"}, "level": "L0", "reason": "revert"})]
        self.assertEqual(d.degrade_levels(iss, "haigeerlab/agent-relay"), ["L0"])

    def test_ignores_other_repo(self):
        iss = [self.issue({"scope": {"repo": "haigeerlab/tier-guard", "tier": "*"}, "level": "L0"})]
        self.assertEqual(d.degrade_levels(iss, "haigeerlab/agent-relay"), [])

    def test_unparseable_block_fails_closed(self):
        iss = [{"body": "<!-- autopr-degrade:v1 {not json} -->"}]
        self.assertEqual(d.degrade_levels(iss, "haigeerlab/agent-relay"), ["L0"])

    def test_issue_without_block_is_ignored(self):
        self.assertEqual(d.degrade_levels([{"body": "随便写的"}], "haigeerlab/agent-relay"), [])


class SchemaValidatorTest(unittest.TestCase):
    def test_reports_path_of_error(self):
        c = load("conclusion.basic.json")
        c["apps"][0]["trust_cap"] = "LX"
        errors = d.validate(c, SCHEMA)
        self.assertTrue(any("apps[0].trust_cap" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
