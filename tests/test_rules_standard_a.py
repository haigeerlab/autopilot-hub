"""Plugin standard A1 (local paths / user names) and A2 (secrets): rules/local_paths.py and rules/secrets.py.

Run: python3 -B -m unittest discover -s tests -v
Every sample path, user name and key below is assembled from parts, so scanning this repository with the
rules themselves never trips over the fixtures.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rules import local_paths as lp
from rules import no_secrets as sc
from rules.diffs import unchecked

U = "/" + "Users/"
H = "/" + "home/"


def pr_file(name, *added, status="modified", removed=()):
    lines = ["@@ -1,%d +1,%d @@" % (len(removed), len(added))]
    lines += ["-" + r for r in removed] + ["+" + a for a in added]
    return {"filename": name, "status": status, "patch": "\n".join(lines)}


class AddedLinesTest(unittest.TestCase):
    def test_line_numbers_follow_the_hunk_header(self):
        f = {"filename": "a.txt", "patch": "@@ -10,3 +20,4 @@\n ctx\n-old\n+new1\n+new2\n ctx"}
        self.assertEqual(list(lp.added_lines(f)), [(21, "new1"), (22, "new2")])

    def test_unified_diff_from_git(self):
        diff = ("diff --git a/x.md b/x.md\n--- a/x.md\n+++ b/x.md\n@@ -1 +1,2 @@\n line\n+added\n"
                "diff --git a/y.md b/y.md\nnew file mode 100644\n--- /dev/null\n+++ b/y.md\n@@ -0,0 +1 @@\n+fresh\n")
        files = lp.files_from_diff(diff)
        self.assertEqual([f["filename"] for f in files], ["x.md", "y.md"])
        self.assertEqual(list(lp.added_lines(files[1])), [(1, "fresh")])

    def test_binary_files_in_a_local_diff_are_unchecked(self):
        diff = ("diff --git a/logo.png b/logo.png\nindex 1111111..2222222 100644\n"
                "Binary files a/logo.png and b/logo.png differ\n"
                "diff --git a/new.bin b/new.bin\nnew file mode 100644\nindex 0000000..3333333\n"
                "GIT binary patch\nliteral 3\nKcmZ?wWB>pF0RRC2\n\nliteral 0\nHcmV?d00001\n\n"
                "diff --git a/x.md b/x.md\n--- a/x.md\n+++ b/x.md\n@@ -1 +1,2 @@\n line\n+added\n")
        files = lp.files_from_diff(diff)
        self.assertEqual([(f["filename"], f["status"]) for f in files],
                         [("logo.png", "modified"), ("new.bin", "added"), ("x.md", "modified")])
        self.assertEqual(unchecked(files), ["logo.png", "new.bin"])
        self.assertEqual(list(lp.added_lines(files[2])), [(2, "added")])

    def test_binary_rename_and_quoted_names_from_real_git_output(self):
        # Copied from `git diff --cached -M` (git quotes non-ASCII names; renames have no ---/+++ lines).
        diff = ('diff --git a/big.png "b/moved \\303\\251.png"\nsimilarity index 99%\nrename from big.png\n'
                'rename to "moved \\303\\251.png"\nindex c97f4c5..c54798c 100644\n'
                'Binary files a/big.png and "b/moved \\303\\251.png" differ\n'
                'diff --git "a/caf\\303\\251.bin" "b/caf\\303\\251.bin"\nindex a903574..00ffac9 100644\n'
                'Binary files "a/caf\\303\\251.bin" and "b/caf\\303\\251.bin" differ\n'
                "diff --git a/my file.png b/my file.png\nindex 8835708..d0c3599 100644\n"
                "Binary files a/my file.png and b/my file.png differ\n"
                "diff --git a/new.png b/new.png\nnew file mode 100644\nindex 0000000..47c2dd5\n"
                "Binary files /dev/null and b/new.png differ\n")
        files = lp.files_from_diff(diff)
        self.assertEqual(unchecked(files), ["moved é.png", "café.bin", "my file.png", "new.png"])

    def test_unparseable_binary_header_is_still_listed(self):
        diff = 'diff --git a/x b/y and b/z\nindex 1..2 100644\nGIT binary patch\nliteral 1\nIcmZ?wWB>p\n'
        self.assertEqual(unchecked(lp.files_from_diff(diff)), ["a/x b/y and b/z"])

    def test_removed_binary_file_in_a_local_diff_is_not_unchecked(self):
        diff = ("diff --git a/old.png b/old.png\ndeleted file mode 100644\nindex 1111111..0000000\n"
                "Binary files a/old.png and /dev/null differ\n")
        files = lp.files_from_diff(diff)
        self.assertEqual([(f["filename"], f["status"]) for f in files], [("old.png", "removed")])
        self.assertEqual(unchecked(files), [])

    def test_empty_added_file_without_patch_is_checkable(self):
        files = [{"filename": "empty.txt", "status": "added", "patch": None, "additions": 0}]
        self.assertEqual(unchecked(files), [])

    def test_nonempty_added_file_without_patch_is_unchecked(self):
        files = [{"filename": "large.txt", "status": "added", "patch": None, "additions": 10000}]
        self.assertEqual(unchecked(files), ["large.txt"])


class LocalPathsTest(unittest.TestCase):
    def run_rule(self, files, login=None):
        return lp.check(files, login=login)

    def test_real_home_paths_are_caught(self):
        for text in ("see " + U + "zhangsan" + "/repo", "cd " + H + "dev.user" + "/x",
                     "~/.claude/projects/-" + "Users-" + "zhangsan" + "-Documents-x"):
            with self.subTest(text=text):
                self.assertEqual(len(self.run_rule([pr_file("doc.md", text)])), 1)

    def test_placeholders_pass(self):
        for name in ("me", "someone", "example", "alice", "bob", "x", "u", "private", "fixture"):
            with self.subTest(name=name):
                self.assertEqual(self.run_rule([pr_file("doc.md", "path " + U + name + "/repo")]), [])
        self.assertEqual(self.run_rule([pr_file("doc.md", U + "<user>/repo", U + "<名>/x", U + "$USER/x")]), [])

    def test_placeholder_match_is_exact(self):
        self.assertEqual(len(self.run_rule([pr_file("doc.md", U + "alice2" + "/repo")])), 1)

    def test_only_added_lines_count(self):
        self.assertEqual(self.run_rule([pr_file("doc.md", "clean", removed=(U + "zhangsan" + "/old",))]), [])

    def test_login_name_whole_word_case_insensitive(self):
        login = "zhang" + "san"
        self.assertEqual(len(self.run_rule([pr_file("a.py", "owner = 'ZhangSan'")], login=login)), 1)
        self.assertEqual(self.run_rule([pr_file("a.py", "owner = 'zhangsanfeng'")], login=login), [])

    def test_generic_or_short_login_is_ignored(self):
        self.assertEqual(self.run_rule([pr_file("a.py", "runner here")], login="runner"), [])
        self.assertEqual(self.run_rule([pr_file("a.py", "abc")], login="abc"), [])

    def test_exempt_files_are_skipped(self):
        self.assertEqual(self.run_rule([pr_file("rules/local_paths.py", U + "zhangsan" + "/x")]), [])

    def test_patchless_file_is_reported_as_unchecked(self):
        out = self.run_rule([{"filename": "big.bin", "status": "added", "patch": None}])
        self.assertEqual(out, [])
        self.assertIn("big.bin", lp.unchecked([{"filename": "big.bin", "status": "added", "patch": None}]))

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "files.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([pr_file("doc.md", U + "zhangsan" + "/x")], fh)
            r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "rules", "local_paths.py"), "--pr-files", path],
                               capture_output=True, text=True, env=dict(os.environ, GITHUB_ACTIONS="true"))
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("doc.md:1", r.stdout)
            r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "rules", "local_paths.py"), "--pr-files",
                                os.path.join(tmp, "missing.json")], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)


GH = "gh" + "p_"
KEYS = {
    "private-key": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "github-token": GH + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "github-pat": "github" + "_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRS",
    "aws-access-key": "AK" + "IA" + "Q3EGRIFS7UVWXYZ2",
    "anthropic-key": "sk-" + "ant-" + "api03-" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tV9wX1yZ",
    "openai-key": "sk-" + "proj-" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tV9wX1yZ",
}


class SecretsTest(unittest.TestCase):
    def test_each_pattern_is_caught(self):
        for kind, key in KEYS.items():
            with self.subTest(kind=kind):
                found = sc.check([pr_file("conf.env", "TOKEN=" + key)])
                self.assertEqual([f["kind"] for f in found], [kind])

    def test_message_never_repeats_the_secret(self):
        found = sc.check([pr_file("conf.env", "TOKEN=" + KEYS["github-token"])])
        self.assertNotIn(KEYS["github-token"], json.dumps(found))
        self.assertNotIn(KEYS["github-token"], sc.render(found))

    def test_exact_fake_literals_pass(self):
        for fake in sc.ALLOWED_LITERALS:
            with self.subTest(fake=fake[:8]):
                self.assertEqual(sc.check([pr_file("README.md", "example: " + fake)]), [])

    def test_example_word_is_not_a_blanket_waiver(self):
        line = "EXAMPLE " + KEYS["aws-access-key"] + "  # EXAMPLE"
        self.assertEqual(len(sc.check([pr_file("README.md", line)])), 1)

    def test_short_or_prose_prefixes_pass(self):
        self.assertEqual(sc.check([pr_file("doc.md", "tokens start with " + GH + " or sk-ant-, see docs")]), [])

    def test_only_added_lines(self):
        self.assertEqual(sc.check([pr_file("conf.env", "TOKEN=${{ secrets.X }}", removed=("TOKEN=" + KEYS["github-token"],))]), [])

    def test_patterns_are_versioned(self):
        self.assertRegex(sc.PATTERNS_VERSION, r"^\d+$")

    def test_cli_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "files.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([pr_file("conf.env", "TOKEN=" + KEYS["aws-access-key"])], fh)
            r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "rules", "no_secrets.py"), "--pr-files", path],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertNotIn(KEYS["aws-access-key"], r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
