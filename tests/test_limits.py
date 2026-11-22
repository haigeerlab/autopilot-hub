"""Kernel release caps and conservative policy loading."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from pipeline.limits import effective_level, load_limits
from pipeline.model import LEVELS, resolve


OWN = ["haigeerlab/spec-guard-plugin", "haigeerlab/module-crew",
       "haigeerlab/delegate-plugin", "haigeerlab/agent-relay"]
POLICY = {"schema": "autopilot.release-limits/v1", "pipeline_own": OWN,
          "repo_caps": {"haigeerlab/kb-plugin": "auto-confirm"},
          "sandbox_repos": ["haigeerlab/autopilot-sandbox"]}


def pipeline(category="sandbox"):
    return {"template": "example", "class": category, "rollback": True,
            "steps": [{"id": "publish"}, {"id": "rollback"}]}


class ReleaseLimitsTest(unittest.TestCase):
    def test_hub_policy_and_default_path_do_not_depend_on_checkout(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                self.assertEqual(load_limits(), POLICY)
            finally:
                os.chdir(original)

    def test_all_own_plugins_are_capped_regardless_of_template(self):
        for repo in OWN:
            for raw in LEVELS:
                with self.subTest(repo=repo, raw=raw):
                    level, reason = effective_level(repo, raw, pipeline(), POLICY)
                    self.assertEqual(level, raw if raw in ("human", "prepare") else "prepare")
                    if raw in ("human", "prepare"):
                        self.assertIsNone(reason)
                    else:
                        self.assertIn(repo.split("/")[1], reason)
                        self.assertIn("流水线自用插件", reason)
                        self.assertIn("prepare", reason)
                        self.assertIn(raw, reason)

    def test_repository_name_case_cannot_bypass_own_plugin_cap(self):
        level, reason = effective_level("Haigeerlab/Delegate-Plugin", "auto", pipeline(), POLICY)
        self.assertEqual(level, "prepare")
        self.assertIn("流水线自用插件", reason)

    def test_other_classes_cannot_auto_release(self):
        for category in ("plugin", "npm"):
            level, reason = effective_level("haigeerlab/other", "auto", pipeline(category), POLICY)
            self.assertEqual(level, "auto-confirm")
            self.assertIn("auto-confirm", reason)

    def test_kb_plugin_cannot_bypass_repo_cap_with_template(self):
        for category in ("sandbox", "site"):
            with self.subTest(category=category):
                value = resolve({"extends": "sandbox"}) if category == "sandbox" else pipeline(category)
                level, reason = effective_level("Haigeerlab/KB-Plugin", "auto", value, POLICY)
                self.assertEqual(level, "auto-confirm")
                self.assertIn("仓库上限", reason)
                if category == "sandbox":
                    self.assertIn("sandbox 模板只给沙盒仓库", reason)

    def test_sandbox_template_only_grants_auto_to_sandbox_repositories(self):
        value = resolve({"extends": "sandbox"})
        level, reason = effective_level("haigeerlab/other", "auto", value, POLICY)
        self.assertEqual(level, "auto-confirm")
        self.assertIn("sandbox 模板只给沙盒仓库", reason)
        self.assertEqual(effective_level("Haigeerlab/Autopilot-Sandbox", "auto", value, POLICY),
                         ("auto", None))

    def test_repo_caps_apply_the_stricter_level(self):
        repo = "haigeerlab/autopilot-sandbox"
        for category in ("site", "sandbox"):
            for cap in LEVELS:
                policy = dict(POLICY, repo_caps={repo: cap})
                for raw in LEVELS:
                    with self.subTest(category=category, cap=cap, raw=raw):
                        level, reason = effective_level(repo, raw, pipeline(category), policy)
                        self.assertEqual(level, min((raw, cap), key=LEVELS.index))
                        if LEVELS.index(raw) > LEVELS.index(cap):
                            self.assertIn("仓库上限", reason)
                        else:
                            self.assertIsNone(reason)

    def test_repo_cap_auto_does_not_bypass_auto_requirements(self):
        repo = "haigeerlab/other"
        policy = dict(POLICY, repo_caps={repo: "auto"})
        for value in (pipeline(), pipeline("plugin"), dict(pipeline("site"), rollback=False)):
            with self.subTest(value=value):
                self.assertEqual(effective_level(repo, "auto", value, policy)[0], "auto-confirm")

    def test_case_variant_repo_caps_take_the_stricter_cap(self):
        policy = dict(POLICY, repo_caps={"haigeerlab/kb-plugin": "prepare",
                                        "Haigeerlab/KB-Plugin": "auto"})
        self.assertEqual(effective_level("HAIGEERLAB/KB-PLUGIN", "auto", pipeline("site"),
                                         policy)[0], "prepare")

    def test_auto_needs_allowed_class_true_rollback_and_rollback_step(self):
        for category in ("site", "sandbox"):
            repo = "haigeerlab/autopilot-sandbox" if category == "sandbox" else "haigeerlab/other"
            for identifier in ("rollback", "rollback-check"):
                value = pipeline(category)
                value["steps"][-1]["id"] = identifier
                with self.subTest(category=category, identifier=identifier):
                    self.assertEqual(effective_level(repo, "auto", value, POLICY),
                                     ("auto", None))
            for change in ({"rollback": False}, {"rollback": None}, {"rollback": "true"},
                           {"steps": [{"id": "publish"}]}, {"steps": []}):
                value = dict(pipeline(category), **change)
                with self.subTest(category=category, change=change):
                    level, reason = effective_level(repo, "auto", value, POLICY)
                    self.assertEqual(level, "auto-confirm")
                    self.assertTrue(reason)
            value = pipeline(category)
            del value["rollback"]
            self.assertEqual(effective_level(repo, "auto", value, POLICY)[0],
                             "auto-confirm")

    def test_lower_levels_are_never_promoted(self):
        for category in ("plugin", "site", "sandbox"):
            for raw in LEVELS[:-1]:
                self.assertEqual(effective_level("haigeerlab/other", raw, pipeline(category), POLICY),
                                 (raw, None))

    def test_removed_rollback_steps_in_resolved_pipeline_prevent_auto(self):
        value = resolve({"extends": "sandbox", "remove": ["rollback", "rollback-check"]})
        self.assertEqual(effective_level("haigeerlab/autopilot-sandbox", "auto", value, POLICY)[0],
                         "auto-confirm")

    def test_policy_allowlist_is_used_and_inputs_are_not_mutated(self):
        policy = dict(POLICY, sandbox_repos=[])
        value = pipeline()
        before = copy.deepcopy((policy, value))
        self.assertEqual(effective_level("haigeerlab/autopilot-sandbox", "auto", value, policy)[0],
                         "auto-confirm")
        self.assertEqual((policy, value), before)

    def test_unreadable_policy_caps_at_prepare_with_explanation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release-limits.json"
            for content in (None, b"{", b"\xff", b"[]", b"null",
                            b'{"schema": "unknown", "pipeline_own": [], "repo_caps": {}, "sandbox_repos": []}',
                            b'{"schema": "autopilot.release-limits/v1"}'):
                with self.subTest(content=content):
                    if content is not None:
                        path.write_bytes(content)
                    policy = load_limits(path)
                    for raw in LEVELS:
                        level, reason = effective_level("haigeerlab/other", raw, pipeline(), policy)
                        self.assertEqual(level, raw if raw in ("human", "prepare") else "prepare")
                        if raw in ("human", "prepare"):
                            self.assertIsNone(reason)
                        else:
                            self.assertIn("release-limits.json", reason)
                            self.assertIn("prepare", reason)
            self.assertIsNone(load_limits(directory))

    def test_unreadable_policy_with_human_level_has_no_explanation(self):
        self.assertEqual(effective_level("haigeerlab/other", "human", pipeline(), None),
                         ("human", None))

    def test_invalid_policy_lists_also_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release-limits.json"
            for changes in ({"pipeline_own": "haigeerlab/other"}, {"pipeline_own": [None]},
                            {"repo_caps": []}, {"repo_caps": None},
                            {"repo_caps": {"haigeerlab/other": "unknown"}},
                            {"repo_caps": {"haigeerlab/other": []}},
                            {"repo_caps": {"": "auto"}},
                            {"sandbox_repos": "haigeerlab/other"}, {"sandbox_repos": [None]},
                            {"sandbox_repos": [[]]}, {"sandbox_repos": [" "]}):
                with self.subTest(changes=changes):
                    path.write_text(json.dumps(dict(POLICY, **changes)), encoding="utf-8")
                    level, reason = effective_level("haigeerlab/other", "auto", pipeline(),
                                                    load_limits(path))
                    self.assertEqual(level, "prepare")
                    self.assertTrue(reason)

    def test_missing_new_policy_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release-limits.json"
            for key in ("repo_caps", "sandbox_repos"):
                policy = dict(POLICY)
                del policy[key]
                path.write_text(json.dumps(policy), encoding="utf-8")
                with self.subTest(key=key):
                    self.assertIsNone(load_limits(path))


if __name__ == "__main__":
    unittest.main()
