"""Plugin standard B1 (versions agree), B5 (release tags) and F1 (commit subject format).

rules/version_consistency.py, rules/release_tag.py, rules/commit_format.py.
Run: python3 -B -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rules import commit_format as cf
from rules import release_tag as rt
from rules import version_consistency as vc


def write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data))


def plugin_repo(root, claude="1.2.0", codex="1.2.0", market="1.2.0", name="demo", sub="plugins/demo"):
    base = sub + "/" if sub else ""
    if claude is not None:
        write(root, base + ".claude-plugin/plugin.json", {"name": name, "version": claude})
    if codex is not None:
        write(root, base + ".codex-plugin/plugin.json", {"name": name, "version": codex})
    entry = {"name": name, "source": "./" + sub if sub else "./"}
    if market is not None:
        entry["version"] = market
    write(root, ".claude-plugin/marketplace.json", {"name": "m", "plugins": [entry]})


class VersionConsistencyTest(unittest.TestCase):
    def test_all_equal_passes(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_repo(root)
            self.assertEqual(vc.check(root), [])

    def test_mismatch_fails_and_names_every_place(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_repo(root, codex="1.1.0")
            errors = vc.check(root)
            self.assertEqual(len(errors), 1)
            self.assertIn("1.1.0", errors[0])
            self.assertIn(".codex-plugin/plugin.json", errors[0])

    def test_missing_places_do_not_count(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_repo(root, codex=None, market=None)
            self.assertEqual(vc.check(root), [])

    def test_raw_string_comparison(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_repo(root, market="v1.2.0")
            self.assertEqual(len(vc.check(root)), 1)

    def test_duplicate_marketplace_entry_fails(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_repo(root)
            write(root, ".claude-plugin/marketplace.json", {"plugins": [{"name": "demo", "source": "./plugins/demo"},
                                                                         {"name": "demo", "source": "./x"}]})
            self.assertTrue(any("出现 2 次" in e for e in vc.check(root)))

    def test_root_layout(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_repo(root, sub="", codex="0.9.0")
            self.assertEqual(len(vc.check(root)), 1)

    def test_scope_only_when_a_declaration_changed(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_repo(root, codex="1.1.0")
            self.assertEqual(vc.check(root, changed=["README.md"]), [])
            self.assertEqual(len(vc.check(root, changed=["plugins/demo/.claude-plugin/plugin.json"])), 1)

    def test_unreadable_manifest_is_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_repo(root)
            write(root, "plugins/demo/.codex-plugin/plugin.json", "{broken")
            self.assertTrue(any("读不懂" in e for e in vc.check(root)))

    def test_claude_worktrees_are_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_repo(root)
            write(root, ".claude/worktrees/x/plugins/demo/.claude-plugin/plugin.json", {"name": "demo", "version": "0.1.0"})
            self.assertEqual(vc.check(root), [])

    def test_version_of(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_repo(root)
            self.assertEqual(vc.versions_by_plugin(root)["demo"], "1.2.0")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")).stdout.strip()


class ReleaseTagTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        origin = os.path.join(self.tmp.name, "origin.git")
        git(self.tmp.name, "init", "-q", "--bare", "-b", "main", origin)
        self.repo = os.path.join(self.tmp.name, "work")
        git(self.tmp.name, "clone", "-q", origin, self.repo)
        for k, v in (("maintenance.auto", "false"), ("gc.auto", "0"), ("commit.gpgsign", "false"), ("tag.gpgsign", "false")):
            git(self.repo, "config", k, v)
        git(self.repo, "checkout", "-q", "-b", "main")
        plugin_repo(self.repo)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "chore(release): 发布 v1.2.0")
        git(self.repo, "push", "-q", "origin", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_annotated_tag_on_default_branch_passes(self):
        git(self.repo, "tag", "-a", "v1.2.0", "-m", "v1.2.0")
        self.assertEqual(rt.check(self.repo, "v1.2.0"), [])

    def test_lightweight_tag_fails(self):
        git(self.repo, "tag", "v1.2.0")
        self.assertTrue(any("附注" in e for e in rt.check(self.repo, "v1.2.0")))

    def test_name_must_match_version(self):
        git(self.repo, "tag", "-a", "v1.3.0", "-m", "x")
        self.assertTrue(any("1.2.0" in e for e in rt.check(self.repo, "v1.3.0")))

    def test_commit_not_on_default_branch_fails(self):
        git(self.repo, "checkout", "-q", "-b", "side")
        write(self.repo, "x.txt", "x")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "feat: 未合并")
        git(self.repo, "tag", "-a", "v1.2.0", "-m", "x")
        self.assertTrue(any("默认分支" in e for e in rt.check(self.repo, "v1.2.0")))

    def test_versions_are_read_at_the_tag_not_the_working_tree(self):
        git(self.repo, "tag", "-a", "v1.2.0", "-m", "v1.2.0")
        plugin_repo(self.repo, claude="1.3.0", codex="1.3.0", market="1.3.0")  # uncommitted bump after tagging
        write(self.repo, ".claude/worktrees/old/plugins/demo/.claude-plugin/plugin.json", {"name": "demo", "version": "0.1.0"})
        self.assertEqual(rt.check(self.repo, "v1.2.0"), [])

    def test_missing_tag(self):
        self.assertTrue(any("找不到" in e for e in rt.check(self.repo, "v1.2.0")))

    def test_cli(self):
        git(self.repo, "tag", "v1.2.0")
        r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "rules", "release_tag.py"), "--repo", self.repo,
                            "--tag", "v1.2.0"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)


class CommitFormatTest(unittest.TestCase):
    def errors(self, *messages, parents=1):
        return cf.check([{"message": m, "parents": parents} for m in messages])

    def test_dependabot_pr_and_commit_authors_are_exempt(self):
        for message in ("chore(deps): Bump actions/setup-python from 7.0.0 to 7.1.0", "Bump dependency"):
            with self.subTest(message=message):
                self.assertEqual(cf.check([{"message": message, "parents": 1, "author": "dependabot[bot]"}],
                                          pr_author="dependabot[bot]"), [])

    def test_forged_dependabot_commit_author_is_not_exempt(self):
        for pr_author in ("someone", "Dependabot[bot]", "dependabot[bot] "):
            with self.subTest(pr_author=pr_author):
                errors = cf.check([{"message": "chore(deps): Bump dependency", "author": "dependabot[bot]"}],
                                  pr_author=pr_author)
                self.assertEqual(len(errors), 1)

    def test_dependabot_commit_without_pr_author_is_not_exempt(self):
        self.assertEqual(len(cf.check([{"message": "chore(deps): Bump dependency",
                                        "author": "dependabot[bot]"}])), 1)

    def test_other_authors_are_not_exempt(self):
        for author in ("someone", None, "Dependabot[bot]", "dependabot[bot] "):
            with self.subTest(author=author):
                self.assertEqual(len(cf.check([{"message": "chore(deps): Bump dependency", "parents": 1,
                                               "author": author}], pr_author="dependabot[bot]")), 1)

    def test_dependabot_message_does_not_grant_exemption(self):
        message = "chore(deps): Bump dependency by dependabot[bot]"
        for commit in (message, {"message": message, "parents": 1},
                       {"message": message, "parents": 1, "author": "someone"}):
            with self.subTest(commit=commit):
                self.assertEqual(len(cf.check([commit], pr_author="dependabot[bot]")), 1)

    def test_dependabot_exemption_only_applies_to_its_commit(self):
        commits = [{"message": "chore(deps): Bump dependency", "author": "dependabot[bot]"},
                   {"message": "fix: handle overflow", "author": "someone"}]
        errors = cf.check(commits, pr_author="dependabot[bot]")
        self.assertEqual(len(errors), 1)
        self.assertIn("handle overflow", errors[0])

    def test_cli_dependabot_exemption_requires_both_authors(self):
        cases = [({"pr_author": "dependabot[bot]"}, "dependabot[bot]", 0),
                 ({"pr_author": "someone"}, "dependabot[bot]", 1),
                 ({"pr_author": "dependabot[bot]"}, "someone", 1),
                 ({}, "dependabot[bot]", 1)]
        with tempfile.TemporaryDirectory() as tmp:
            for metadata, author, expected in cases:
                with self.subTest(metadata=metadata, author=author):
                    data = dict(metadata, commits=[{"message": "chore(deps): Bump dependency", "parents": 1,
                                                    "author": author}])
                    write(tmp, "commits.json", data)
                    r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "rules", "commit_format.py"),
                                        "--input", os.path.join(tmp, "commits.json")], capture_output=True, text=True)
                    self.assertEqual(r.returncode, expected, r.stdout + r.stderr)

    def test_good_subjects(self):
        for m in ("feat: 加了一个功能", "fix(rules): 修正误报", "chore(release): 发布 v0.94.0",
                  "refactor!: 改了接口", "docs: 更新 `README.md` 的说明\n\nbody in English is fine"):
            with self.subTest(m=m):
                self.assertEqual(self.errors(m), [])

    def test_missing_or_unknown_type(self):
        for m in ("加了一个功能", "feature: 加了功能", "Feat: 加了功能", "fix 修了", "fix:修了"):
            with self.subTest(m=m):
                self.assertEqual(len(self.errors(m)), 1)

    def test_subject_needs_chinese_outside_code_paths_and_urls(self):
        for m in ("fix: handle overflow", "docs: `中文标识符`", "fix: docs/说明/a.md", "feat: https://例子.com/中文"):
            with self.subTest(m=m):
                self.assertEqual(len(self.errors(m)), 1)

    def test_only_the_first_line_counts(self):
        self.assertEqual(len(self.errors("fix: handle overflow\n\n修了溢出")), 1)

    def test_merge_commits_pass_by_parent_count(self):
        self.assertEqual(self.errors("Merge branch 'x' into main", parents=2), [])
        self.assertEqual(len(self.errors("Merge branch 'x' into main", parents=1)), 1)

    def test_standard_revert_passes(self):
        self.assertEqual(self.errors('Revert "fix: 修正误报"\n\nThis reverts commit abc.'), [])
        self.assertEqual(len(self.errors("revert: 撤回误报修正")), 1)  # revert is not an allowed type

    def test_plain_string_commits_are_accepted(self):
        self.assertEqual(cf.check(["feat: 加了功能"]), [])

    def test_cli_message_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "MSG")
            write(tmp, "MSG", "update stuff\n")
            r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "rules", "commit_format.py"), "--message-file", path],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
