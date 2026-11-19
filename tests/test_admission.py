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


TIER_MAP = json.load(open(os.path.join(ROOT, "tier-map.json"), encoding="utf-8"))


def levels_comment(payload):
    return "编排写入的等级\n<!-- autopilot-levels:v1 " + json.dumps(payload) + " -->\n"


# Both plugin tiers at the loosest value, so conclusion-only tests see the conclusion cap unchanged.
PERMISSIVE = levels_comment({"spec_guard": {"module": "m", "tier": "relaxed"}, "module_crew": {"effective": "relaxed"}})

SCHEMA = json.load(open(os.path.join(ROOT, "schemas", "vendor", "repo-readiness", "conclusion.v1.json"), encoding="utf-8"))


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
        plugin_limits=d.plugin_limits([PERMISSIVE], TIER_MAP),
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
        c["basis"]["tool"]["commit"] = "3" * 40
        self.assertCap(run(conclusion=c), "L0", "允许列表")

    def test_tool_version_and_commit_must_match_together(self):
        c = load("conclusion.basic.json")
        c["basis"]["tool"]["version"] = "0.2.0"  # same commit, different version
        self.assertCap(run(conclusion=c), "L0", "允许列表")

    def test_missing_allowed_list_is_L0(self):
        self.assertCap(run(allowed_tools=None), "L0", "允许列表")

    def test_schema_invalid_is_L0(self):
        c = load("conclusion.basic.json")
        c["tier"] = "basic"  # v1 requires an object
        self.assertCap(run(conclusion=c), "L0", "格式")

    def test_unknown_field_is_L0(self):
        c = load("conclusion.basic.json")
        c["extra"] = 1  # additionalProperties: false
        self.assertCap(run(conclusion=c), "L0", "格式")

    def test_bad_head_sha_is_L0(self):
        c = load("conclusion.basic.json")
        c["head"] = "abc"  # $ref sha pattern
        self.assertCap(run(conclusion=c), "L0", "格式")

    def test_wrong_schema_const_is_L0(self):
        c = load("conclusion.basic.json")
        c["schema"] = "repo-readiness.conclusion/v2"
        self.assertCap(run(conclusion=c), "L0", "格式")

    def test_empty_rulesets_is_L0(self):
        c = load("conclusion.basic.json")
        c["basis"]["rulesets"] = []  # minItems 1
        self.assertCap(run(conclusion=c), "L0", "格式")

    def test_bad_date_is_L0(self):
        c = load("conclusion.basic.json")
        c["declarations_expire_at"] = "soon"  # format: date
        self.assertCap(run(conclusion=c), "L0", "格式")

    def test_null_declarations_expiry_is_valid(self):
        c = load("conclusion.basic.json")
        c["declarations_expire_at"] = None
        self.assertCap(run(conclusion=c), "L2")

    def test_null_repo_cap_is_L0(self):
        c = load("conclusion.basic.json")
        c["tier"], c["trust_cap"] = {"id": None, "name": "未达标"}, None
        self.assertCap(run(conclusion=c), "L0")

    def test_non_level_repo_cap_is_L0(self):
        c = load("conclusion.basic.json")
        c["trust_cap"] = "L9"  # any string passes the schema; admission fails closed
        self.assertCap(run(conclusion=c), "L0")

    def test_null_app_cap_is_L0_for_that_app(self):
        c = load("conclusion.basic.json")
        c["apps"][0]["trust_cap"] = None
        self.assertCap(run(conclusion=c, changed_files=["src/core/a.py"]), "L0")
        self.assertCap(run(conclusion=c, changed_files=["src/cli/x.py"]), "L2")

    def test_file_in_two_apps_takes_lowest(self):
        c = load("conclusion.basic.json")
        c["apps"][0]["paths"].append("shared/**")
        c["apps"][1]["paths"].append("shared/**")
        self.assertCap(run(conclusion=c, changed_files=["src/core/a.py", "shared/x.py"]), "L2")

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

    def limits(self, *comments):
        return d.plugin_limits(list(comments), TIER_MAP)

    def test_no_levels_comment_defaults_both_plugins_to_L2(self):
        r = run(changed_files=["src/core/a.py"], plugin_limits=self.limits())
        self.assertCap(r, "L2")
        self.assertIn("缺少", " ".join(s["detail"] for s in r["steps"]))

    def test_spec_guard_strict_lowers_app_cap(self):
        c = levels_comment({"spec_guard": {"module": "m", "tier": "strict"}, "module_crew": {"effective": "relaxed"}})
        self.assertCap(run(changed_files=["src/core/a.py"], plugin_limits=self.limits(c)), "L2", "spec-guard")

    def test_module_crew_effective_lowers_app_cap(self):
        c = levels_comment({"spec_guard": {"module": "m", "tier": "relaxed"}, "module_crew": {"effective": "strict"}})
        self.assertCap(run(changed_files=["src/core/a.py"], plugin_limits=self.limits(c)), "L2", "module-crew")

    def test_plugin_tier_cannot_raise_above_conclusion(self):
        self.assertCap(run(changed_files=["README.md"]), "L2")  # relaxed=L4 but repo cap is L2

    def test_standard_maps_to_L3(self):
        c = levels_comment({"spec_guard": {"module": "m", "tier": "standard"}, "module_crew": {"effective": "standard"}})
        self.assertCap(run(changed_files=["src/core/a.py"], plugin_limits=self.limits(c)), "L3")

    def test_unknown_tier_is_L0(self):
        c = levels_comment({"spec_guard": {"module": "m", "tier": "yolo"}, "module_crew": {"effective": "relaxed"}})
        self.assertCap(run(plugin_limits=self.limits(c)), "L0")

    def test_missing_one_source_defaults_that_source_to_L2(self):
        c = levels_comment({"spec_guard": {"module": "m", "tier": "relaxed"}})
        self.assertCap(run(changed_files=["src/core/a.py"], plugin_limits=self.limits(c)), "L2", "module-crew")

    def test_unparseable_levels_block_is_L0(self):
        self.assertCap(run(plugin_limits=self.limits("<!-- autopilot-levels:v1 {oops} -->")), "L0")

    def test_latest_levels_comment_wins(self):
        old = levels_comment({"spec_guard": {"module": "m", "tier": "strict"}, "module_crew": {"effective": "strict"}})
        r = run(changed_files=["src/core/a.py"], plugin_limits=self.limits(old, PERMISSIVE))
        self.assertCap(r, "L3")

    def test_unrelated_comments_are_ignored(self):
        r = run(changed_files=["src/core/a.py"], plugin_limits=self.limits("looks good", PERMISSIVE, "<!-- other -->"))
        self.assertCap(r, "L3")

    def test_open_degrade_issue_lowers_cap(self):
        self.assertCap(run(degrade_levels=["L0"]), "L0", "降级")

    def test_unmet_conclusion_is_L0(self):
        c = load("conclusion.basic.json")
        c["tier"], c["trust_cap"] = {"id": None, "name": "未达标"}, "L0"
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
        return {"body": "降级说明\n<!-- autopilot-degrade:v1 " + json.dumps(payload) + " -->\n"}

    def test_parses_level_for_this_repo(self):
        iss = [self.issue({"scope": {"repo": "haigeerlab/agent-relay", "tier": "*"}, "level": "L0", "reason": "revert"})]
        self.assertEqual(d.degrade_levels(iss, "haigeerlab/agent-relay"), ["L0"])

    def test_ignores_other_repo(self):
        iss = [self.issue({"scope": {"repo": "haigeerlab/tier-guard", "tier": "*"}, "level": "L0"})]
        self.assertEqual(d.degrade_levels(iss, "haigeerlab/agent-relay"), [])

    def test_unparseable_block_fails_closed(self):
        iss = [{"body": "<!-- autopilot-degrade:v1 {not json} -->"}]
        self.assertEqual(d.degrade_levels(iss, "haigeerlab/agent-relay"), ["L0"])

    def test_issue_without_block_is_ignored(self):
        self.assertEqual(d.degrade_levels([{"body": "随便写的"}], "haigeerlab/agent-relay"), [])


class SchemaValidatorTest(unittest.TestCase):
    def test_reports_path_of_error(self):
        c = load("conclusion.basic.json")
        c["apps"][0]["trust_cap"] = 3  # must be string or null
        errors = d.validate(c, SCHEMA)
        self.assertTrue(any("apps[0].trust_cap" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()


class VendoredSchemaTest(unittest.TestCase):
    def test_vendored_schema_matches_recorded_sha256(self):
        import hashlib
        base = os.path.join(ROOT, "schemas", "vendor", "repo-readiness")
        source = json.load(open(os.path.join(base, "SOURCE.json"), encoding="utf-8"))
        data = open(os.path.join(base, "conclusion.v1.json"), "rb").read()
        self.assertEqual(hashlib.sha256(data).hexdigest(), source["sha256"])
        self.assertRegex(source["commit"], r"^[0-9a-f]{40}$")

    def test_allowed_list_pins_the_vendored_release(self):
        source = json.load(open(os.path.join(ROOT, "schemas", "vendor", "repo-readiness", "SOURCE.json"), encoding="utf-8"))
        allowed = json.load(open(os.path.join(ROOT, "allowed-tools.json"), encoding="utf-8"))
        self.assertIn({"name": "repo-readiness", "version": source["tag"].lstrip("v"), "commit": source["commit"]},
                      allowed["tools"])
