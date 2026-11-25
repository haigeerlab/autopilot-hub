"""Checkup tests: C1-C14, draft fixes, exit codes and the takeover conclusion.

Run: python3 -B -m unittest discover -s tests -v
No test touches the network: GitHub answers come from an injected fake transport.
"""
import contextlib
import io
import json
import os
import subprocess
import unittest

from _checkup_util import (INTERFACE, PR_CI, FakeTransport, commit_on_branch, git, good_github, make_repo,
                           repo_meta, ROOT)
from checkup import checks, cli, remedy
from checkup.ghprobe import GitHub, GhResponse
from risk import tiers

NAME = "o/n"


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def run_checks(root, gh_data=None, base="main", **kw):
    gh = GitHub(FakeTransport(gh_data)) if gh_data is not None else None
    findings, ctx = checks.run_checks(root, base, gh, NAME if gh else None, **kw)
    return {f["id"]: f for f in findings}, ctx


def status(root, cid, gh_data=None, **kw):
    return run_checks(root, gh_data, **kw)[0][cid]["status"]


GOOD = {".github/workflows/ci.yml": PR_CI, ".github/autopr/interface.yml": INTERFACE}


class C1Test(unittest.TestCase):
    def test_interface_check_wins(self):
        f = run_checks(make_repo(self, GOOD))[0]["C1"]
        self.assertEqual(f["status"], "ok")
        self.assertIn("interface.yml", f["detail"])

    def test_inferred_from_pull_request_workflow(self):
        f = run_checks(make_repo(self, {".github/workflows/ci.yml": PR_CI}))[0]["C1"]
        self.assertEqual(f["status"], "ok")
        self.assertIn("python3 -B -m unittest discover -s tests", f["detail"])
        self.assertIn("请确认", f["detail"])

    def test_missing(self):
        root = make_repo(self, {"a.txt": "x", ".github/workflows/push.yml": "on: push\njobs:\n  j:\n    steps:\n      - run: make test\n"})
        f = run_checks(root)[0]["C1"]
        self.assertEqual(f["status"], "missing")
        self.assertFalse(f["fix"]["can_generate"])
        self.assertIn("不能开自动合并", f["impact"])

    def test_unreadable_interface_is_unknown(self):
        self.assertEqual(status(make_repo(self, {".github/autopr/interface.yml": "a: &x 1\n"}), "C1"), "unknown")


class C2Test(unittest.TestCase):
    def test_not_run_by_default(self):
        f = run_checks(make_repo(self, GOOD))[0]["C2"]
        self.assertTrue(f["skipped"])
        self.assertIsNone(f["fix"])

    def test_passes_and_fails_with_run_check(self):
        root = make_repo(self, {".github/autopr/interface.yml": 'entries:\n  check:\n    run: "exit 0"\n'})
        self.assertEqual(status(root, "C2", run_check=True), "ok")
        root = make_repo(self, {".github/autopr/interface.yml": 'entries:\n  check:\n    run: "echo boom; exit 4"\n'})
        f = run_checks(root, run_check=True)[0]["C2"]
        self.assertEqual(f["status"], "missing")
        self.assertEqual(f["evidence"], ["boom"])

    def test_no_command_is_unknown(self):
        self.assertEqual(status(make_repo(self, {"a": "x"}), "C2", run_check=True), "unknown")


class GithubChecksTest(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self, GOOD)

    def data(self, **override):
        d = good_github()
        d.update(override)
        return d

    def test_all_ok(self):
        found = run_checks(self.root, self.data())[0]
        for cid in ("C3", "C4", "C6", "C7"):
            self.assertEqual(found[cid]["status"], "ok", cid)

    def test_c3_missing_without_pr_requirement(self):
        d = self.data(**{"repos/o/n/rules/branches/main": []})
        f = run_checks(self.root, d)[0]["C3"]
        self.assertEqual(f["status"], "missing")
        self.assertTrue(f["fix"]["can_generate"])

    def test_c3_classic_protection_counts(self):
        d = self.data(**{
            "repos/o/n/rules/branches/main": [],
            "repos/o/n/branches/main/protection": {
                "enforce_admins": {"enabled": True}, "required_pull_request_reviews": {"required_approving_review_count": 0},
                "allow_force_pushes": {"enabled": False}, "allow_deletions": {"enabled": False}}})
        self.assertEqual(status(self.root, "C3", d), "ok")

    def test_c3_bypass_actor_that_could_be_an_agent_does_not_count(self):
        d = self.data(**{"repos/o/n/rulesets/7": {"name": "gate", "enforcement": "active",
                                                  "bypass_actors": [{"actor_type": "Integration", "actor_id": 9}]}})
        f = run_checks(self.root, d)[0]["C3"]
        self.assertEqual(f["status"], "missing")
        self.assertIn("可被绕过", f["detail"])

    def test_c3_inactive_ruleset_and_unreadable_bypass_list(self):
        d = self.data(**{"repos/o/n/rulesets/7": {"name": "gate", "enforcement": "evaluate", "bypass_actors": []}})
        self.assertEqual(status(self.root, "C3", d), "missing")
        d = self.data(**{"repos/o/n/rulesets/7": {"name": "gate", "enforcement": "active"}})
        self.assertEqual(status(self.root, "C3", d), "unknown")

    def test_c4_failure_and_no_runs(self):
        path = "repos/o/n/actions/runs?branch=main&status=completed&per_page=30"
        run = {"event": "push", "head_branch": "main", "conclusion": "failure", "created_at": "2026-11-20T00:00:00Z"}
        self.assertEqual(status(self.root, "C4", self.data(**{path: {"workflow_runs": [run]}})), "missing")
        cancelled = dict(run, conclusion="cancelled", created_at="2026-11-21T00:00:00Z")
        older_ok = dict(run, conclusion="success", created_at="2026-11-19T00:00:00Z")
        self.assertEqual(status(self.root, "C4", self.data(**{path: {"workflow_runs": [cancelled, older_ok]}})), "ok")
        pr = dict(run, event="pull_request")
        self.assertEqual(status(self.root, "C4", self.data(**{path: {"workflow_runs": [pr]}})), "missing")

    def test_c6_disabled_and_not_reported(self):
        off = repo_meta(security_and_analysis={"secret_scanning": {"status": "enabled"},
                                               "secret_scanning_push_protection": {"status": "disabled"}})
        f = run_checks(self.root, self.data(**{"repos/o/n": off}))[0]["C6"]
        self.assertEqual(f["status"], "missing")
        self.assertIn("推送保护", f["detail"])
        self.assertEqual(status(self.root, "C6", self.data(**{"repos/o/n": repo_meta(security_and_analysis=None)})), "unknown")

    def test_c7_write_default_and_forbidden(self):
        perms = "repos/o/n/actions/permissions/workflow"
        self.assertEqual(status(self.root, "C7", self.data(**{perms: {"default_workflow_permissions": "write"}})), "missing")
        self.assertEqual(status(self.root, "C7", self.data(**{perms: GhResponse("forbidden", 403, {}, "Must have admin rights")})), "unknown")

    def test_without_github_remote_or_repo_all_four_are_unknown(self):
        found = run_checks(self.root)[0]
        for cid in ("C3", "C4", "C6", "C7"):
            self.assertEqual(found[cid]["status"], "unknown", cid)
        found = run_checks(self.root, {})[0]  # repo answers 404
        for cid in ("C3", "C4", "C6", "C7"):
            self.assertEqual(found[cid]["status"], "unknown", cid)

    def test_default_branch_comes_from_github(self):
        d = good_github(branch="trunk")
        d["repos/o/n"] = repo_meta(default_branch="trunk")
        self.assertEqual(status(self.root, "C4", d), "ok")


class C5Test(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(status(make_repo(self, GOOD), "C5"), "ok")

    def test_no_pull_request_workflow(self):
        root = make_repo(self, {**GOOD, ".github/workflows/ci.yml": PR_CI.replace("  pull_request:\n", "")})
        self.assertEqual(status(root, "C5"), "missing")

    def test_workflow_does_not_run_the_registered_command(self):
        root = make_repo(self, {**GOOD, ".github/autopr/interface.yml": INTERFACE.replace("unittest discover -s tests", "validate.sh")})
        f = run_checks(root)[0]["C5"]
        self.assertEqual(f["status"], "missing")
        self.assertIn("validate.sh", f["detail"])

    def test_no_workflows_at_all(self):
        self.assertEqual(status(make_repo(self, {".github/autopr/interface.yml": INTERFACE}), "C5"), "missing")

    def test_wrapper_counts(self):
        root = make_repo(self, {**GOOD, ".github/workflows/ci.yml": PR_CI.replace(
            "python3 -B -m unittest discover -s tests", "autopr-entry run --entry check")})
        self.assertEqual(status(root, "C5"), "ok")


class IncrementalTest(unittest.TestCase):
    """C8-C10 only look at what changed against --base."""

    def test_unchanged_old_problems_are_not_reported(self):
        old = PR_CI.replace("@" + "a" * 40, "@v4")
        root = make_repo(self, {".github/workflows/ci.yml": old, "package.json": '{"dependencies": {"a": "1"}}'})
        for cid in ("C8", "C9", "C10"):
            self.assertEqual(status(root, cid), "ok", cid)

    def test_c8_unpinned_action_in_changed_workflow(self):
        root = make_repo(self, GOOD)
        commit_on_branch(root, {".github/workflows/ci.yml": PR_CI.replace("@" + "a" * 40, "@v4")})
        f = run_checks(root)[0]["C8"]
        self.assertEqual(f["status"], "missing")
        self.assertIn("actions/checkout@v4", f["detail"])

    def test_c8_c9_ok_when_changed_workflow_is_clean(self):
        root = make_repo(self, GOOD)
        commit_on_branch(root, {".github/workflows/ci.yml": PR_CI + "      - run: echo more\n"})
        found = run_checks(root)[0]
        self.assertEqual((found["C8"]["status"], found["C9"]["status"]), ("ok", "ok"))
        self.assertIn("1 个 workflow", found["C8"]["detail"])

    def test_c9_dangerous_trigger(self):
        root = make_repo(self, GOOD)
        bad = ("on: pull_request_target\njobs:\n  j:\n    steps:\n      - uses: actions/checkout@%s\n"
               "        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n" % ("c" * 40))
        commit_on_branch(root, {".github/workflows/pr-target.yml": bad})
        f = run_checks(root)[0]["C9"]
        self.assertEqual(f["status"], "missing")
        self.assertIn("pr-target.yml", f["detail"])

    def test_uncommitted_and_untracked_changes_count(self):
        root = make_repo(self, GOOD)
        with open(os.path.join(root, ".github/workflows/new.yml"), "w") as fh:
            fh.write("on: push\njobs:\n  j:\n    steps:\n      - uses: a/b@main\n")
        self.assertEqual(status(root, "C8"), "missing")
        git(root, "add", "-A")
        self.assertEqual(status(root, "C8"), "missing")

    def test_c10_changed_manifest_without_lock(self):
        root = make_repo(self, GOOD)
        commit_on_branch(root, {"package.json": '{"dependencies": {"a": "1"}}'})
        f = run_checks(root)[0]["C10"]
        self.assertEqual(f["status"], "missing")
        self.assertIn("没有锁文件", f["detail"])

    def test_c10_with_lock_and_frozen_install(self):
        files = {**GOOD, ".github/workflows/ci.yml": PR_CI + "      - run: npm ci\n"}
        root = make_repo(self, files)
        commit_on_branch(root, {"package.json": '{"dependencies": {"a": "1"}}', "package-lock.json": "{}"})
        self.assertEqual(status(root, "C10"), "ok")

    def test_unknown_base_makes_the_three_unknown(self):
        root = make_repo(self, GOOD)
        found = run_checks(root, base="nope")[0]
        for cid in ("C8", "C9", "C10"):
            self.assertEqual(found[cid]["status"], "unknown", cid)

    def test_unreadable_changed_workflow_is_unknown(self):
        root = make_repo(self, GOOD)
        commit_on_branch(root, {".github/workflows/odd.yml": "a: &x 1\n"})
        found = run_checks(root)[0]
        self.assertEqual((found["C8"]["status"], found["C9"]["status"]), ("unknown", "unknown"))


class C11C12Test(unittest.TestCase):
    def test_c11_missing_present_and_broken(self):
        self.assertEqual(status(make_repo(self, {"a": "x"}), "C11"), "missing")
        root = make_repo(self, {".github/autopr/risk-tiers.json": '{"extends": "plugin"}\n'})
        self.assertEqual(status(root, "C11"), "ok")
        root = make_repo(self, {".github/autopr/risk-tiers.json": "{oops"})
        self.assertEqual(status(root, "C11"), "unknown")

    def test_c12_missing_and_present(self):
        self.assertEqual(status(make_repo(self, {"CLAUDE.md": "# Project\n\nhello autopilot\n"}), "C12"), "missing")
        self.assertEqual(status(make_repo(self, {"a": "x"}), "C12"), "missing")
        self.assertEqual(status(make_repo(self, {"CLAUDE.md": "# P\n\n## autopilot（自动维护）\n\nx\n"}), "C12"), "ok")
        self.assertEqual(status(make_repo(self, {"AGENTS.md": "## Autopilot rules\n"}), "C12"), "ok")


class C13RiskPathsTest(unittest.TestCase):
    """C13: .github/autopr/risk-paths.json, read by spec-guard 0.91, equals `risk/cli.py paths --json`."""

    def current(self, config):
        return tiers.risk_paths_text(tiers.load_templates(), config)

    def test_missing(self):
        root = make_repo(self, {"a": "x"})
        self.assertEqual(status(root, "C13"), "missing")

    def test_up_to_date_with_yml_config(self):
        cfg = "extends: plugin\nT4:\n  - bridge/**\n"
        root = make_repo(self, {".github/autopr/risk-tiers.yml": cfg,
                                ".github/autopr/risk-paths.json": self.current({"extends": "plugin", "T4": ["bridge/**"]})})
        self.assertEqual(status(root, "C13"), "ok")

    def test_stale_after_repo_rules_change(self):
        root = make_repo(self, {".github/autopr/risk-tiers.yml": "extends: plugin\nT4:\n  - bridge/**\n",
                                ".github/autopr/risk-paths.json": self.current({"extends": "plugin"})})
        found = run_checks(root)[0]["C13"]
        self.assertEqual(found["status"], "missing")
        self.assertIn("过期", found["detail"])

    def test_unreadable(self):
        root = make_repo(self, {".github/autopr/risk-paths.json": "{oops"})
        self.assertEqual(status(root, "C13"), "unknown")

    def test_patch_creates_and_updates_the_file(self):
        for files in ({".github/autopr/risk-tiers.yml": "extends: plugin\n"},
                      {".github/autopr/risk-tiers.yml": "extends: plugin\nT3:\n  - x/**\n",
                       ".github/autopr/risk-paths.json": self.current({"extends": "plugin"})}):
            root = make_repo(self, files)
            import tempfile
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            found = run_checks(root, {"repos/o/n": repo_meta()})[0]
            fixes = {f["id"]: f for f in remedy.build_fixes(root, NAME, "main", list(found.values()), "make test", tmp.name)}
            patch = os.path.join(tmp.name, fixes["F4"]["files"][0])
            subprocess.run(["git", "-C", root, "apply", patch], check=True)
            self.assertEqual(status(root, "C13"), "ok")
            data = json.loads(read(os.path.join(root, ".github", "autopr", "risk-paths.json")))
            self.assertEqual(data["schema"], "autopilot.risk-paths/v1")


class C14PipelineTest(unittest.TestCase):
    def test_missing_and_valid_pipeline(self):
        root = make_repo(self, {"a": "x"})
        self.assertEqual(status(root, "C14"), "missing")
        for template in ("plugin", "npm", "site", "sandbox"):
            root = make_repo(self, {".github/autopr/pipeline.yml": "extends: " + template})
            found = run_checks(root)[0]
            self.assertEqual(list(found)[-2:], ["C13", "C14"])
            self.assertEqual(found["C14"]["title"], "流程文件")
            self.assertEqual(found["C14"]["status"], "ok")

    def test_invalid_pipeline_is_unknown_with_at_most_five_problems(self):
        cases = (("extends: absent\n", "不存在"), ("extends: [\n", "YAML"), ("", "映射"),
                 ("extends: sandbox\n" + "".join("wrong%d: true\n" % i for i in range(6)), "wrong0"))
        for content, detail in cases:
            with self.subTest(content=content):
                root = make_repo(self, {".github/autopr/pipeline.yml": content})
                finding = run_checks(root)[0]["C14"]
                self.assertEqual(finding["status"], "unknown")
                self.assertIn(detail, finding["detail"])
                self.assertNotIn("wrong5", finding["detail"])
                if "wrong0" in content:
                    self.assertIn("wrong4", finding["detail"])

    def test_f5_patch_uses_risk_category_then_nonprivate_package(self):
        import tempfile
        cases = (({".github/autopr/risk-tiers.yml": "extends: plugin"}, "plugin"),
                 ({".github/autopr/risk-tiers.json": '{"extends":"frontend"}',
                   "package.json": '{"private":false}'}, "site"),
                 ({"package.json": '{}'}, "npm"),
                 ({"package.json": '{"private":false}'}, "npm"),
                 ({".github/autopr/risk-tiers.yml": "extends: content", "package.json": '{}'}, "npm"))
        for files, template in cases:
            with self.subTest(template=template, files=files), tempfile.TemporaryDirectory() as out:
                root = make_repo(self, files)
                fixes = remedy.build_fixes(root, NAME, "main", [{"id": "C14", "status": "missing"}], None, out)
                self.assertEqual(len(fixes), 1)
                fix = fixes[0]
                self.assertEqual((fix["id"], fix["finding"], fix["files"]), ("F5", "C14", ["pipeline.patch"]))
                self.assertIn("T4", fix["note"])
                path = os.path.join(root, ".github", "autopr", "pipeline.yml")
                self.assertFalse(os.path.exists(path))
                patch = os.path.join(out, "pipeline.patch")
                subprocess.run(["git", "-C", root, "apply", "--check", patch], check=True)
                subprocess.run(["git", "-C", root, "apply", patch], check=True)
                self.assertEqual(read(path), "# 改这个文件算 T4，由人审后合并\nextends: %s\n" % template)
                self.assertEqual(status(root, "C14"), "ok")

    def test_unknown_category_generates_only_a_note_and_existing_file_is_not_replaced(self):
        import tempfile
        for files in ({"a": "x"}, {"package.json": '{"private":true}'}, {"package.json": "[]"},
                      {"package.json": "{oops"}, {".github/autopr/risk-tiers.yml": "["}):
            with self.subTest(files=files), tempfile.TemporaryDirectory() as out:
                root = make_repo(self, files)
                fix = remedy.build_fixes(root, NAME, "main", [{"id": "C14", "status": "missing"}], None, out)[0]
                self.assertEqual((fix["id"], fix["files"]), ("F5", []))
                self.assertEqual(fix["note"], "看不出类别（plugin / npm / site），请告诉我")
                self.assertEqual(os.listdir(out), [])
                for state in ("ok", "unknown"):
                    self.assertEqual(remedy.build_fixes(root, NAME, "main", [{"id": "C14", "status": state}],
                                                       None, out), [])


class FixesTest(unittest.TestCase):
    def build(self, root, out, extra=None):
        found = run_checks(root, {"repos/o/n": repo_meta(), **(extra or {})})[0]
        return remedy.build_fixes(root, NAME, "main", list(found.values()), "make test", out)

    def out_dir(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return os.path.realpath(tmp.name)

    def test_ruleset_json_and_command(self):
        root, out = make_repo(self, {"a": "x"}), self.out_dir()
        fixes = {f["id"]: f for f in self.build(root, out)}
        body = json.loads(read(os.path.join(out, "ruleset-main.json")))
        self.assertEqual({r["type"] for r in body["rules"]}, {"deletion", "non_fast_forward", "pull_request"})
        self.assertEqual(body["enforcement"], "active")
        self.assertEqual(body["conditions"]["ref_name"]["include"], ["refs/heads/main"])
        self.assertEqual(body["bypass_actors"], [])
        self.assertEqual(fixes["F1"]["commands"],
                         ["gh api --method POST repos/o/n/rulesets --input %s" % os.path.join(out, "ruleset-main.json")])
        text = read(os.path.join(out, "ruleset-command.txt"))
        self.assertIn("--method POST repos/o/n/rulesets", text)
        self.assertEqual(len([c for c in text.splitlines() if not c.startswith("#")]), 1)

    def test_no_files_without_out(self):
        root = make_repo(self, {".claude-plugin/plugin.json": "{}"})
        fixes = self.build(root, None)
        self.assertEqual({f["id"] for f in fixes}, {"F1", "F2", "F3", "F4", "F5"})
        self.assertTrue(all(f["files"] == [] for f in fixes))
        self.assertFalse(os.path.exists(os.path.join(root, ".github/autopr/risk-tiers.json")))

    def test_risk_tiers_guess_plugin_frontend_or_ask(self):
        cases = [({".claude-plugin/plugin.json": "{}"}, "plugin"),
                 ({"package.json": '{"devDependencies": {"vite": "5"}}'}, "frontend"),
                 ({"package.json": '{"dependencies": {"@angular/core": "17"}}'}, "frontend")]
        for files, want in cases:
            root, out = make_repo(self, files), self.out_dir()
            self.build(root, out)
            path = os.path.join(out, ".github", "autopr", "risk-tiers.yml")
            text = read(path)
            self.assertEqual(len(text.strip().splitlines()), 1)
            cfg = tiers.read_repo_config_yml(path)  # readable by admission (slice 7)
            self.assertEqual(cfg, {"extends": want})
            classified = tiers.classify([{"filename": "README.md", "status": "modified", "additions": 1,
                                          "deletions": 0, "patch": "@@ -1 +1 @@\n+x\n"}], tiers.load_templates(), cfg)
            self.assertIn("tier", classified)

    def test_risk_tiers_not_guessed(self):
        root, out = make_repo(self, {"package.json": '{"dependencies": {"lodash": "4"}}'}), self.out_dir()
        fixes = {f["id"]: f for f in self.build(root, out)}
        self.assertEqual(fixes["F2"]["files"], [])
        self.assertIn("告诉我是哪一类", fixes["F2"]["note"])
        self.assertFalse(os.path.exists(os.path.join(out, ".github", "autopr", "risk-tiers.yml")))

    def test_instructions_patch_applies_to_new_and_existing_file(self):
        for files in ({"a": "x"}, {"CLAUDE.md": "# Proj\n\nold text\n"}, {"AGENTS.md": "# Agents\nno newline at end"}):
            root, out = make_repo(self, files), self.out_dir()
            self.build(root, out)
            patches = [f for f in os.listdir(out) if f.startswith("instructions-") and f.endswith(".patch")]
            self.assertEqual(len(patches), 1)
            patch = os.path.join(out, patches[0])
            subprocess.run(["git", "-C", root, "apply", "--check", patch], check=True)
            subprocess.run(["git", "-C", root, "apply", patch], check=True)
            target = "AGENTS.md" if "AGENTS.md" in files else "CLAUDE.md"
            text = read(os.path.join(root, target))
            self.assertIn("## autopilot（自动维护）", text)
            self.assertIn("`autopilot/*` 分支上提交、推送并开 PR", text)
            self.assertIn("main、tag、发版仍然要用户本人来", text)
            self.assertIn("`make test`", text)
            self.assertEqual(status(root, "C12"), "ok")  # the draft satisfies C12

    def test_nothing_generated_when_everything_is_fine(self):
        root = make_repo(self, {**GOOD, "CLAUDE.md": "## autopilot\n", ".github/autopr/risk-tiers.json": "{}\n",
                                ".github/autopr/pipeline.yml": "extends: sandbox\n",
                                ".github/autopr/risk-paths.json": tiers.risk_paths_text(tiers.load_templates(), {})})
        out = self.out_dir()
        found = run_checks(root, good_github())[0]
        self.assertEqual(remedy.build_fixes(root, NAME, "main", list(found.values()), None, out), [])
        self.assertEqual(os.listdir(out), [])


class CliTest(unittest.TestCase):
    def go(self, root, *args, transport=None):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["--repo", root] + list(args), transport=transport, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def good_repo(self):
        return make_repo(self, {**GOOD, "CLAUDE.md": "## autopilot\n", ".github/autopr/risk-tiers.json": '{"extends": "plugin"}\n'},
                         remote=NAME)

    def test_exit_0_with_findings_and_report_shape(self):
        root = make_repo(self, {"a": "x"}, remote=NAME)
        code, out, err = self.go(root, transport=FakeTransport(good_github()))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("[缺] C1", out)
        self.assertIn("缺了会怎样：", out)
        self.assertIn("要不要帮你补：", out)
        self.assertTrue(out.rstrip().endswith("。"))
        self.assertIn("结论：现在只能开到「只接管 Plan」", out.splitlines()[-1])

    def test_exit_0_when_all_good(self):
        code, out, _ = self.go(self.good_repo(), transport=FakeTransport(good_github()))
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[-1].startswith("结论：现在可以开到「接管到合并」。"), out.splitlines()[-1])

    def test_exit_2_for_bad_input(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.go(d)[0], 2)  # not a git repo
        self.assertEqual(self.go("/nonexistent/path/xyz")[0], 2)
        empty = make_repo(self, {"a": "x"})
        git(empty, "checkout", "-q", "--orphan", "fresh")
        git(empty, "rm", "-rqf", ".")
        git(empty, "branch", "-D", "main")
        self.assertEqual(self.go(empty)[0], 2)  # no commits
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            cli.main([])
        self.assertEqual(cm.exception.code, 2)

    def test_exit_3_when_gh_not_logged_in_or_network_down(self):
        root = make_repo(self, {"a": "x"}, remote=NAME)
        for st in ("unauthenticated", "network", "gh_missing", "rate_limited"):
            t = FakeTransport({"repos/o/n": GhResponse(st, message="x")})
            code, out, err = self.go(root, transport=t)
            self.assertEqual((code, out), (3, ""), st)
            self.assertIn("gh auth login", err)

    def test_no_remote_still_exit_0(self):
        code, out, _ = self.go(make_repo(self, GOOD))
        self.assertEqual(code, 0)
        self.assertIn("没找到 GitHub 远端", out)

    def test_json_output_and_out_dir(self):
        import tempfile
        root = make_repo(self, {"a": "x"}, remote=NAME)
        with tempfile.TemporaryDirectory() as out_dir:
            code, out, _ = self.go(root, "--json", "--out", out_dir, transport=FakeTransport(good_github()))
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual([f["id"] for f in data["findings"]],
                         ["C%d" % i for i in range(1, 15)] + ["规范" + n for n in (
                             "A3", "B2", "B3", "B4", "C1", "C2", "C3", "C4", "C5", "C6", "C9", "C10",
                             "D1", "D2", "D3", "E1", "F2", "G1", "G2", "H1", "H2", "H4")])
            for f in data["findings"]:
                self.assertIn(f["status"], ("ok", "missing", "unknown"))
            self.assertEqual(data["repo"], NAME)
            self.assertEqual(data["conclusion"]["level"], "只接管 Plan")
            self.assertEqual({f["id"] for f in data["fixes"]}, {"F2", "F3", "F4", "F5"})
            self.assertTrue(os.path.exists(os.path.join(out_dir, "instructions-claude-md.patch")))
            self.assertTrue(os.path.exists(os.path.join(out_dir, "risk-paths.patch")))

    def test_run_check_flag(self):
        root = make_repo(self, {".github/autopr/interface.yml": 'entries:\n  check:\n    run: "exit 1"\n'}, remote=NAME)
        code, out, _ = self.go(root, "--run-check", transport=FakeTransport(good_github()))
        self.assertEqual(code, 0)  # a failing check is a finding, not a failure
        self.assertIn("[缺] C2", out)
        self.assertIn("C2（没跑通）", out.splitlines()[-1])

    def test_never_writes_to_github(self):
        t = FakeTransport(good_github())
        self.go(self.good_repo(), transport=t)
        self.assertTrue(t.paths)
        self.assertTrue(all(p.startswith("repos/") for p in t.paths))

    def test_runs_as_script(self):
        root = make_repo(self, {"a": "x"})
        r = subprocess.run(["python3", "-B", os.path.join(ROOT, "checkup", "cli.py"), "--repo", root, "--json"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["findings"][0]["id"], "C1")


class ConclusionTest(unittest.TestCase):
    def fs(self, **st):
        base = {"C%d" % i: "ok" for i in range(1, 15)}
        base.update(st)
        return [{"id": k, "status": v, "skipped": False} for k, v in sorted(base.items(), key=lambda kv: int(kv[0][1:]))]

    def test_merge_level_needs_c1_and_c12(self):
        c = checks.conclusion(self.fs(C3="missing"), False)
        self.assertEqual(c["level"], "接管到合并")
        self.assertEqual(c["reminders"], ["C3"])
        self.assertIn("其余提醒", c["text"])
        self.assertIn("机器人身份", c["text"])
        c = checks.conclusion(self.fs(C12="missing"), False)
        self.assertEqual((c["level"], c["missing"]), ("只接管 Plan", ["C12"]))
        self.assertIn("autopilot 例外", c["text"])

    def test_missing_c1_leaves_plan_only(self):
        c = checks.conclusion(self.fs(C1="missing"), False)
        self.assertEqual((c["level"], c["missing"]), ("只接管 Plan", ["C1"]))
        self.assertIn("还差 C1（没找到）", c["text"])

    def test_c2_counts_only_when_run(self):
        self.assertEqual(checks.conclusion(self.fs(C2="missing"), False)["level"], "接管到合并")
        c = checks.conclusion(self.fs(C2="missing"), True)
        self.assertEqual((c["level"], c["missing"]), ("只接管 Plan", ["C2"]))
        c = checks.conclusion(self.fs(C1="unknown"), False)
        self.assertIn("C1（判不了）", c["text"])

    def test_c14_is_only_a_reminder(self):
        for state in ("missing", "unknown"):
            result = checks.conclusion(self.fs(C14=state), True)
            self.assertEqual((result["level"], result["missing"], result["reminders"]),
                             ("接管到合并", [], ["C14"]))


if __name__ == "__main__":
    unittest.main()


class LegacyHeadingTest(unittest.TestCase):
    def test_autopr_heading_counts_for_c12(self):
        self.assertTrue(checks._AUTOPILOT_HEADING.search("# 项目\n\n## autopr（自动维护）\n内容\n"))
        self.assertFalse(checks._AUTOPILOT_HEADING.search("# 项目\n\n## 发版\n"))


class RiskConfigYmlTest(unittest.TestCase):
    def test_c11_accepts_yml_and_prefers_it(self):
        from checkup import checks as ck
        self.assertEqual(ck.RISK_CONFIGS[0], ".github/autopr/risk-tiers.yml")
