"""Unit tests for slice-4 risk tiers (docs/spec/slice-4.md).

Run: python3 -B -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from risk import tiers as r  # noqa: E402

T = r.load_templates()


def f(name, additions=1, deletions=0, patch="@@ -1 +1 @@\n+x\n", status="modified"):
    return {"filename": name, "status": status, "additions": additions, "deletions": deletions, "patch": patch}


def tier_of(files, config=None):
    return r.classify(files, T, config)


class FileTierTest(unittest.TestCase):
    def one(self, name, config=None):
        return tier_of([f(name)], config)["tier"]

    def test_plugin_template(self):
        cfg = {"extends": "plugin"}
        self.assertEqual(self.one(".claude-plugin/plugin.json", cfg), "T4")
        self.assertEqual(self.one("skills/x/SKILL.md", cfg), "T2")
        self.assertEqual(self.one("tests/test_a.py", cfg), "T1")
        self.assertEqual(self.one("docs/guide.md", cfg), "T0")

    def test_plugin_template_covers_subdirectory_layout(self):
        # agent-relay#188: plugins/<name>/ layout (agent-relay, spec-guard, delegate) and root layout (module-crew)
        cfg = {"extends": "plugin"}
        for path in ["plugins/agent-relay/.claude-plugin/plugin.json", "plugins/spec-guard/.codex-plugin/plugin.json",
                     "plugins/delegate/interface.json", "interface.json", ".claude-plugin/marketplace.json",
                     "RELEASING.md", "scripts/validate.sh", "plugins/x/scripts/validate.sh"]:
            self.assertEqual(self.one(path, cfg), "T4", path)
        self.assertEqual(self.one("plugins/agent-relay/skills/x/SKILL.md", cfg), "T2")
        # R4：Licenses and unmatched plugin paths use the ordinary T2 default.
        self.assertEqual(self.one("plugins/agent-relay/bridge/LICENSE", cfg), "T2")
        self.assertEqual(self.one("plugins/delegate/scripts/review.py", cfg), "T2")

    def test_contract_is_all_T4(self):
        self.assertEqual(self.one("spec/messages.md", {"extends": "contract"}), "T4")
        self.assertEqual(self.one("anything.txt", {"extends": "contract"}), "T4")

    def test_frontend_template(self):
        cfg = {"extends": "frontend"}
        self.assertEqual(self.one("src/auth/login.ts", cfg), "T4")
        self.assertEqual(self.one("src/sw.ts", cfg), "T3")
        self.assertEqual(self.one("src/util/date.ts", cfg), "T2")
        self.assertEqual(self.one("src/util/date.test.ts", cfg), "T1")

    def test_content_template(self):
        cfg = {"extends": "content"}
        self.assertEqual(self.one("REQUIREMENTS.md", cfg), "T4")
        self.assertEqual(self.one("maps/graph.json", cfg), "T3")
        self.assertEqual(self.one("nodes/a.md", cfg), "T2")

    def test_common_rules(self):
        cfg = {"extends": "plugin"}
        self.assertEqual(self.one(".github/workflows/ci.yml", cfg), "T4")
        self.assertEqual(self.one("CLAUDE.md", cfg), "T3")
        self.assertEqual(self.one("README.md", cfg), "T0")

    def test_common_T4_covers_the_four_R4_groups(self):
        groups = {
            "workflows": [".github/workflows/ci.yml"],
            "release": [".claude-plugin/plugin.json", ".codex-plugin/plugin.json",
                        "package.json", "pyproject.toml", "RELEASING.md"],
            "credentials": [".github/autopr/takeover.yml", ".github/autopr/risk-tiers.json",
                            "keys/archive/signing.pem", "signing.pem", ".env", ".env.local"],
            "hub configuration": [".agent/config.json", "audit/baselines.json", "release-limits.json",
                                  "bot-identity.json", "standard-target.json"],
        }
        for group, paths in groups.items():
            for path in paths:
                with self.subTest(group=group, path=path):
                    res = tier_of([f(path)])
                    self.assertEqual(res["tier"], "T4")
                    self.assertTrue(res["stop"])
                    self.assertFalse(res["auto_merge_eligible"])

    def test_other_common_paths_use_the_default(self):
        for path in ("LICENSE", ".agent/state.json", ".github/dependabot.yml", ".github/CODEOWNERS"):
            with self.subTest(path=path):
                self.assertEqual(self.one(path), "T2")

    def test_unmatched_is_T2_without_a_category_or_with_an_ordinary_category(self):
        for config in (None, {}, {"extends": "plugin"}, {"extends": "frontend"}, {"extends": "content"}):
            with self.subTest(config=config):
                # R4：An unmatched path uses T2 outside the contract category.
                self.assertEqual(self.one("unmatched.py", config), "T2")

    def test_unknown_category_falls_back_to_generic(self):
        res = tier_of([f("src/x.py")], {"extends": "nope"})
        # R4：An unknown category uses the generic T2 default.
        self.assertEqual(res["tier"], "T2")
        self.assertTrue(any("nope" in n for n in res["notes"]), res["notes"])

    def test_repo_paths_can_lower_unmatched_but_not_common(self):
        cfg = {"extends": "plugin", "T1": ["fixtures/**", ".github/**"]}
        self.assertEqual(self.one("fixtures/a.json", cfg), "T1")
        self.assertEqual(self.one(".github/workflows/ci.yml", cfg), "T4")

    def test_repo_paths_can_raise(self):
        self.assertEqual(self.one("skills/x/SKILL.md", {"extends": "plugin", "T4": ["skills/x/**"]}), "T4")

    def test_highest_file_wins(self):
        # R4：Use a workflow path to provide the common T4 rule.
        res = tier_of([f("docs/a.md"), f("skills/b.md"), f(".github/workflows/x.yml")], {"extends": "plugin"})
        self.assertEqual(res["tier"], "T4")

    def test_common_and_category_protection_win_over_ordinary_rules(self):
        cfg = {"extends": "frontend", "T0": [".github/workflows/**", "src/**"]}
        self.assertEqual(self.one(".github/workflows/ci.yml", cfg), "T4")
        self.assertEqual(self.one("src/auth/login.ts", cfg), "T4")
        self.assertEqual(self.one("src/sw.ts", cfg), "T3")

    def test_hub_repo_declares_its_own_T4_paths(self):
        cfg = r.read_repo_config_at(ROOT)
        for path in ("orchestrator/token.py", "risk/templates.json"):
            with self.subTest(path=path):
                self.assertEqual(self.one(path), "T2")
                self.assertEqual(self.one(path, cfg), "T4")
                self.assertIn(path, r.risk_paths(T, cfg)["T4"])


class ConfigKeysTest(unittest.TestCase):
    """Old `T4_paths` style keys still count; unknown keys are reported, never silently dropped."""

    def test_paths_suffix_keys_count_like_tier_keys(self):
        cfg = {"extends": "plugin", "T4_paths": ["bridge/**"], "T1_paths": ["fixtures/**"]}
        res = tier_of([f("bridge/src/version.ts")], cfg)
        self.assertEqual(res["tier"], "T4")
        self.assertEqual(self.one_tier("fixtures/a.json", cfg), "T1")
        self.assertTrue(any("T4_paths" in n for n in res["notes"]), res["notes"])

    def test_both_spellings_are_merged(self):
        cfg = {"extends": "plugin", "T4": ["a/**"], "T4_paths": ["b/**"]}
        self.assertEqual(self.one_tier("a/x.py", cfg), "T4")
        self.assertEqual(self.one_tier("b/x.py", cfg), "T4")

    def test_unknown_key_is_reported(self):
        res = tier_of([f("docs/a.md")], {"extends": "plugin", "T5": ["x/**"], "tier4": ["y/**"]})
        self.assertTrue(any("T5" in n for n in res["notes"]), res["notes"])
        self.assertTrue(any("tier4" in n for n in res["notes"]), res["notes"])

    def test_known_meta_keys_are_quiet(self):
        res = tier_of([f("docs/a.md")], {"extends": "plugin", "version": 1, "$comment": "x", "T4": []})
        self.assertEqual(res["notes"], [])

    def test_risk_paths_also_reads_old_keys(self):
        out = r.risk_paths(T, {"extends": "plugin", "T4_paths": ["bridge/**"]})
        self.assertIn("bridge/**", out["T4"])

    @staticmethod
    def one_tier(name, cfg):
        return tier_of([f(name)], cfg)["tier"]


class BumpTest(unittest.TestCase):
    cfg = {"extends": "plugin"}

    def test_many_lines_bumps_one(self):
        res = tier_of([f("skills/a.md", additions=150, deletions=60)], self.cfg)
        self.assertEqual(res["tier"], "T3")

    def test_test_lines_do_not_count(self):
        res = tier_of([f("skills/a.md", additions=10), f("tests/test_a.py", additions=500)], self.cfg)
        self.assertEqual(res["tier"], "T2")

    def test_many_files_bumps_one(self):
        res = tier_of([f(f"skills/s{i}.md") for i in range(9)], self.cfg)
        self.assertEqual(res["tier"], "T3")

    def test_both_bumps_add_up_but_cap_at_T4(self):
        res = tier_of([f(f"skills/s{i}.md", additions=30) for i in range(9)], self.cfg)
        self.assertEqual(res["tier"], "T4")

    def test_dependency_file_at_least_T3(self):
        # R4：The package manifest is common T4, so use a lockfile to test the dependency floor.
        res = tier_of([f("docs/a.md"), f("package-lock.json")], {"extends": "plugin", "T1": ["package-lock.json"]})
        self.assertEqual(res["tier"], "T3")

    def assert_human_only(self, res, tier, reason):
        self.assertEqual(res["tier"], tier)
        self.assertTrue(res["human_only"])
        self.assertTrue(any(reason in item for item in res["human_reasons"]), res)
        self.assertFalse(res["auto_merge_eligible"])
        self.assertFalse(res["stop"])

    def test_weakened_assertion_requires_a_human(self):
        patch = "@@ -1,2 +1 @@\n-    assert x == 1\n-    assert y == 2\n+    pass\n"
        res = tier_of([f("tests/test_a.py", additions=1, deletions=2, patch=patch)], self.cfg)
        # R4：Net assertion deletion requires a human and keeps the test file's T1.
        self.assert_human_only(res, "T1", "删除或削弱了测试断言")

    def test_rewritten_assertion_is_not_weakened(self):
        patch = "@@ -1 +1 @@\n-    assert x == 1\n+    assert x == 2\n"
        res = tier_of([f("tests/test_a.py", additions=1, deletions=1, patch=patch)], self.cfg)
        self.assertEqual(res["tier"], "T1")
        self.assertFalse(res["human_only"])

    def test_removed_test_file_counts_its_assertions(self):
        patch = "@@ -1,2 +0,0 @@\n-def test_a():\n-    assert 1\n"
        res = tier_of([f("tests/test_a.py", additions=0, deletions=2, patch=patch, status="removed")], self.cfg)
        # R4：Assertions removed with a test file still require a human at T1.
        self.assert_human_only(res, "T1", "删除或削弱了测试断言")

    def test_missing_patch_on_test_file_with_deletions_requires_a_human(self):
        res = tier_of([f("tests/test_a.py", additions=0, deletions=5, patch=None)], self.cfg)
        # R4：Unreadable test deletions require a human and keep the test file's T1.
        self.assert_human_only(res, "T1", "补丁读不到")

    def test_added_suppression_requires_a_human(self):
        patch = "@@ -1 +1 @@\n-x = 1\n+x = 1  # no" + "qa\n"
        res = tier_of([f("skills/a.py", patch=patch)], self.cfg)
        # R4：A net increase in suppressions requires a human and keeps the file's T2.
        self.assert_human_only(res, "T2", "压制注释净增加")

    def test_marker_in_docs_is_not_a_suppression(self):
        patch = "@@ -1 +1 @@\n+| rule | counts `# no" + "qa` and `eslint-disable` |\n"
        res = tier_of([f("docs/spec/rules.md", patch=patch)], self.cfg)
        self.assertEqual(res["tier"], "T0")
        self.assertFalse(res["human_only"])

    def test_removed_suppression_is_fine(self):
        patch = "@@ -1 +1 @@\n-x = 1  # no" + "qa\n+x = 1\n"
        res = tier_of([f("skills/a.py", patch=patch)], self.cfg)
        self.assertEqual(res["tier"], "T2")
        self.assertFalse(res["human_only"])

    def test_missing_patch_on_code_file_requires_a_human(self):
        res = tier_of([f("skills/a.py", additions=3, patch=None)], self.cfg)
        # R4：Unreadable code additions require a human and keep the file's T2.
        self.assert_human_only(res, "T2", "补丁读不到")

    def test_human_reasons_are_kept_alongside_T4(self):
        files = [f(".github/workflows/ci.yml"), f("skills/a.py", patch=None),
                 f("tests/test_a.py", additions=0, deletions=1, patch="@@ -1 +0,0 @@\n-assert 1\n")]
        res = tier_of(files, self.cfg)
        self.assertEqual(res["tier"], "T4")
        self.assertTrue(res["stop"])
        self.assertTrue(res["human_only"])
        self.assertEqual(len(res["human_reasons"]), 2)


class SpecGuardArtifactTest(unittest.TestCase):
    cfg = {"extends": "plugin"}

    def test_todo_ticks_only_are_T1(self):
        patch = "@@ -3 +3 @@\n- - [ ] T1 do it\n+ - [x] T1 do it\n"
        res = tier_of([f("tasks/m/todo.md", additions=1, deletions=1, patch=patch)], self.cfg)
        # The slice 12 approval guard exempts checkbox-only changes.
        self.assertEqual(res["tier"], "T1")
        self.assertFalse(res["human_only"])
        self.assertTrue(res["auto_merge_eligible"])

    def test_todo_text_change_uses_ordinary_rules(self):
        patch = "@@ -3 +3 @@\n- - [ ] T1 do it\n+ - [ ] T1 do something else\n"
        res = tier_of([f("tasks/m/todo.md", additions=1, deletions=1, patch=patch)], self.cfg)
        # R4 keeps the ordinary tier, and the slice 12 guard requires human approval for content changes.
        self.assertEqual(res["tier"], "T2")
        self.assertTrue(res["human_only"])
        self.assertFalse(res["auto_merge_eligible"])
        self.assertIn("spec-guard 产物没核对到有效的批准：tasks/m/todo.md", res["human_reasons"])

    def test_spec_review_mark_only_is_T1(self):
        patch = ("@@ -1 +1 @@\n-<!-- spec-guard: review pending -->\n"
                 "+<!-- spec-guard: review approved abc123 -->\n")
        res = tier_of([f("spec/m.md", additions=1, deletions=1, patch=patch)], self.cfg)
        # The slice 12 approval guard exempts approval marker-only changes.
        self.assertEqual(res["tier"], "T1")
        self.assertFalse(res["human_only"])
        self.assertTrue(res["auto_merge_eligible"])

    def test_spec_content_change_uses_ordinary_rules(self):
        patch = "@@ -9 +9 @@\n-- must do A\n+- must do B\n"
        res = tier_of([f("spec/m.md", additions=1, deletions=1, patch=patch)], self.cfg)
        # R4 keeps the ordinary tier, and the slice 12 guard blocks auto-merge through human_only.
        self.assertEqual(res["tier"], "T2")
        self.assertFalse(res["stop"])
        self.assertTrue(res["human_only"])
        self.assertFalse(res["auto_merge_eligible"])
        self.assertIn("spec-guard 产物没核对到有效的批准：spec/m.md", res["human_reasons"])

    def test_content_changes_use_repo_rules(self):
        cfg = {"extends": "plugin", "T0": ["spec/**", "tasks/**"]}
        for path in ("spec/m.md", "tasks/m/plan.md"):
            with self.subTest(path=path):
                self.assertEqual(tier_of([f(path)], cfg)["tier"], "T0")

    def test_capability_map_needs_user_approval(self):
        for patch in ("@@ -9 +9 @@\n-- must do A\n+- must do B\n",
                      "@@ -1 +1 @@\n-<!-- spec-guard: review pending -->\n"
                      "+<!-- spec-guard: review approved abc123 -->\n"):
            with self.subTest(patch=patch):
                res = tier_of([f(r.CAPABILITY_MAP, patch=patch)], self.cfg)
                self.assertTrue(res["human_only"])
                self.assertIn("能力图改动要用户批", res["human_reasons"])
                self.assertFalse(res["auto_merge_eligible"])
                self.assertFalse(res["stop"])


class VerifiedArtifactTest(unittest.TestCase):
    """Verification evidence satisfies the slice 12 approval guard for module artifacts."""
    cfg = {"extends": "plugin"}
    patch = "@@ -9 +9 @@\n-- must do A\n+- must do B\n"

    def test_verified_spec_and_tasks_use_ordinary_rules_without_human_only(self):
        files = [f("spec/m.md", patch=self.patch), f("tasks/m/plan.md", patch=self.patch)]
        res = r.classify(files, T, self.cfg, verified={"spec/m.md", "tasks/m/plan.md"})
        # R4 uses the ordinary tier, and verified paths satisfy the slice 12 approval guard.
        self.assertEqual(res["tier"], "T2", res["reasons"])
        self.assertFalse(res["stop"])
        self.assertFalse(res["human_only"])
        self.assertEqual(res["human_reasons"], [])
        self.assertTrue(res["auto_merge_eligible"])

    def test_partial_verification_only_exempts_the_verified_path(self):
        files = [f("spec/m.md", patch=self.patch), f("tasks/m/plan.md", patch=self.patch)]
        res = r.classify(files, T, self.cfg, verified={"spec/m.md"})
        # The slice 12 guard checks approval for each artifact separately.
        self.assertTrue(res["human_only"])
        self.assertEqual(res["human_reasons"], ["spec-guard 产物没核对到有效的批准：tasks/m/plan.md"])
        self.assertFalse(res["auto_merge_eligible"])

    def test_agent_config_is_always_T4(self):
        res = r.classify([f(".agent/config.json")], T, self.cfg, verified={".agent/config.json"})
        self.assertEqual(res["tier"], "T4")

    def test_capability_map_never_verified(self):
        res = r.classify([f("spec/CAPABILITY-MAP.md", patch=self.patch)], T, self.cfg,
                         verified={"spec/CAPABILITY-MAP.md"})
        # R4 keeps the capability map at the ordinary tier and always requires human approval.
        self.assertEqual(res["tier"], "T2")
        self.assertTrue(res["human_only"])
        self.assertEqual(res["human_reasons"], ["能力图改动要用户批"])
        self.assertFalse(res["auto_merge_eligible"])

    def test_verified_does_not_lower_other_protective_rules(self):
        res = r.classify([f(".github/workflows/ci.yml")], T, self.cfg, verified={".github/workflows/ci.yml"})
        self.assertEqual(res["tier"], "T4")


class OutputTest(unittest.TestCase):
    def test_eligibility(self):
        low = tier_of([f("docs/a.md")], {"extends": "plugin"})
        self.assertTrue(low["auto_merge_eligible"])
        self.assertFalse(low["stop"])
        mid = tier_of([f("CLAUDE.md")], {"extends": "plugin"})
        self.assertFalse(mid["auto_merge_eligible"])
        self.assertFalse(mid["stop"])

    def test_reasons_name_the_rule(self):
        res = tier_of([f(".github/workflows/ci.yml")], {"extends": "plugin"})
        # R4：The reason names the narrower workflow rule.
        self.assertTrue(any(".github/workflows/**" in x for x in res["reasons"]), res["reasons"])

    def test_no_files_is_T0(self):
        res = tier_of([], {"extends": "plugin"})
        self.assertEqual(res["tier"], "T0")
        self.assertFalse(res["human_only"])
        self.assertEqual(res["human_reasons"], [])


class PathsCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_paths(self):
        proc = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "risk", "cli.py"), "paths", "--repo", self.tmp, "--json"],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        return proc

    def test_paths_for_plugin_repo(self):
        os.makedirs(os.path.join(self.tmp, ".github", "autopr"))
        with open(os.path.join(self.tmp, ".github", "autopr", "risk-tiers.json"), "w") as fh:
            json.dump({"extends": "plugin", "T4": ["bridge/**"]}, fh)
        proc = self.run_paths()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["schema"], "autopilot.risk-paths/v1")
        self.assertEqual(out["category"], "plugin")
        # R4：Export workflow protection and omit the former broad GitHub and artifact globs.
        self.assertIn(".github/workflows/**", out["T4"])
        self.assertNotIn(".github/**", out["T4"])
        self.assertIn("bridge/**", out["T4"])
        self.assertIn("CLAUDE.md", out["T3"])
        self.assertNotIn("spec/**", out["T4"])
        self.assertNotIn("tasks/**", out["T4"])

    def test_paths_reads_yml_first(self):
        os.makedirs(os.path.join(self.tmp, ".github", "autopr"))
        with open(os.path.join(self.tmp, ".github", "autopr", "risk-tiers.yml"), "w") as fh:
            fh.write("extends: plugin\nT4:\n  - from-yml/**\n")
        with open(os.path.join(self.tmp, ".github", "autopr", "risk-tiers.json"), "w") as fh:
            json.dump({"extends": "plugin", "T4": ["from-json/**"]}, fh)
        proc = self.run_paths()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, r.risk_paths_text(T, {"extends": "plugin", "T4": ["from-yml/**"]}))
        self.assertIn("from-yml/**", json.loads(proc.stdout)["T4"])

    def test_paths_without_config_is_generic(self):
        proc = self.run_paths()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertIsNone(out["category"])
        # R4：The exported default without a category is T2.
        self.assertEqual(out["default"], "T2")


if __name__ == "__main__":
    unittest.main()
