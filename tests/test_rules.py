"""Unit tests for the slice-6 rule checks in rules/ (each: one passing and one failing case, plus waivers).

Run: python3 -B -m unittest discover -s tests -v
Stdlib only. Literal waiver / suppression strings are assembled from parts so that scanning this repo with
the rules themselves does not trip over the fixtures.
"""
import contextlib
import datetime
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from risk.tiers import load_templates
from rules import chinese_commit as cc
from rules import no_weakened_assertion as nwa
from rules import suppression_budget as sb
from rules.common import waiver_state

TEMPLATES = load_templates()
TODAY = datetime.date(2026, 11, 21)
ALLOW = "autopilot" + "-allow"
NOQA = "# no" + "qa"


def waiver(rule, until="2027-01-31", who="vilin", token=ALLOW, reason="历史遗留"):
    return f"# {token} {rule}: {reason} until {until} approved-by {who}"


def pfile(name, patch, add=1, dele=1, status="modified"):
    return {"filename": name, "status": status, "additions": add, "deletions": dele, "patch": patch}


def run_cli(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = module.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def write_json(directory, name, data):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return path


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)


class NoWeakenedAssertionTest(TempDirCase):
    REMOVE = "@@ -1,3 +1,2 @@\n def t():\n-    self.assertEqual(a, 1)\n+    pass\n"
    KEEP = "@@ -1,3 +1,3 @@\n def t():\n-    self.assertEqual(a, 1)\n+    self.assertEqual(a, 2)\n"

    def test_passes_when_assertions_are_kept(self):
        self.assertEqual(nwa.check([pfile("tests/test_a.py", self.KEEP)], TEMPLATES, TODAY), [])

    def test_fails_when_assertion_removed_with_six_line_message(self):
        errors = nwa.check([pfile("tests/test_a.py", self.REMOVE)], TEMPLATES, TODAY)
        self.assertEqual(len(errors), 1)
        lines = errors[0].splitlines()
        self.assertEqual(len(lines), 6)
        self.assertTrue(lines[0].startswith("no-weakened-assertion: tests/test_a.py"))
        for prefix, line in zip(("为什么：", "改成：", "不要：", "例外：", "详见：docs/spec/slice-6.md"), lines[1:]):
            self.assertTrue(line.startswith(prefix), line)

    def test_non_test_files_are_ignored(self):
        self.assertEqual(nwa.check([pfile("src/a.py", self.REMOVE)], TEMPLATES, TODAY), [])

    def test_missing_patch_on_test_file_with_deletions_fails(self):
        self.assertEqual(len(nwa.check([pfile("tests/test_a.py", None, 0, 5)], TEMPLATES, TODAY)), 1)
        self.assertEqual(nwa.check([pfile("tests/test_a.py", None, 5, 0)], TEMPLATES, TODAY), [])

    def test_valid_waiver_on_added_line_clears_the_file(self):
        patch = self.REMOVE + "+    " + waiver("no-weakened-assertion") + "\n"
        self.assertEqual(nwa.check([pfile("tests/test_a.py", patch)], TEMPLATES, TODAY), [])

    def test_expired_malformed_and_foreign_waivers_do_not_clear(self):
        for line in (waiver("no-weakened-assertion", until="2026-11-20"),
                     f"# {ALLOW} no-weakened-assertion: 理由",
                     waiver("suppression-budget")):
            patch = self.REMOVE + "+    " + line + "\n"
            self.assertEqual(len(nwa.check([pfile("tests/test_a.py", patch)], TEMPLATES, TODAY)), 1, line)

    def test_cli_exit_codes(self):
        bad = write_json(self.tmp, "bad.json", [pfile("tests/test_a.py", self.REMOVE)])
        good = write_json(self.tmp, "good.json", [pfile("tests/test_a.py", self.KEEP)])
        code, out, _ = run_cli(nwa, ["--pr-files", bad, "--today", "2026-11-21"])
        self.assertEqual(code, 1)
        self.assertIn("为什么：", out)
        self.assertEqual(run_cli(nwa, ["--pr-files", good])[0], 0)
        self.assertEqual(run_cli(nwa, ["--pr-files", os.path.join(self.tmp, "missing.json")])[0], 2)
        md = os.path.join(self.tmp, "o.md")
        run_cli(nwa, ["--pr-files", bad, "--out-md", md])
        with open(md, encoding="utf-8") as fh:
            self.assertIn("改成：", fh.read())


class SuppressionBudgetPatchTest(TempDirCase):
    def patch(self, added=(), removed=()):
        return "@@ -1,2 +1,2 @@\n" + "".join(f"-{x}\n" for x in removed) + "".join(f"+{x}\n" for x in added)

    def check(self, **kw):
        return sb.check_patches([pfile("src/a.py", self.patch(**kw))], TEMPLATES, TODAY)

    def test_passes_when_not_increased(self):
        self.assertEqual(self.check(added=[f"x = 1  {NOQA}"], removed=[f"y = 2  {NOQA}"]), [])
        self.assertEqual(self.check(added=["x = 1"]), [])

    def test_fails_when_suppressions_increase(self):
        errors = self.check(added=[f"x = 1  {NOQA}"])
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("suppression-budget: 压制注释净增加 1"))
        self.assertEqual(len(errors[0].splitlines()), 6)

    def test_valid_waiver_on_the_same_line_is_not_counted(self):
        self.assertEqual(self.check(added=[f"x = 1  {NOQA}  " + waiver("suppression-budget")]), [])

    def test_waiver_for_another_rule_does_not_waive_this_one(self):
        self.assertEqual(len(self.check(added=[f"x = 1  {NOQA}  " + waiver("no-weakened-assertion")])), 1)

    def test_expired_or_malformed_waiver_is_reported_and_does_not_waive(self):
        for line in (f"x = 1  {NOQA}  " + waiver("suppression-budget", until="2026-01-01"),
                     f"x = 1  {NOQA}  # {ALLOW} suppression-budget: 理由 until 2027-01-01",
                     f"x = 1  {NOQA}  " + waiver("suppression-budget", until="2027-02-30")):
            errors = self.check(added=[line])
            self.assertTrue(any("豁免" in e for e in errors), line)
            self.assertTrue(any("净增加" in e for e in errors), line)

    def test_legacy_prefix_is_accepted(self):
        self.assertEqual(self.check(added=[f"x = 1  {NOQA}  " + waiver("suppression-budget", token="autopr-allow")]), [])

    def test_waiver_line_alone_is_not_a_suppression(self):
        self.assertEqual(self.check(added=[waiver("no-weakened-assertion")]), [])

    def test_missing_patch_is_skipped(self):
        self.assertEqual(sb.check_patches([pfile("a.png", None)], TEMPLATES, TODAY), [])


class SuppressionBudgetScanTest(TempDirCase):
    def repo(self, files):
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        for name, text in files.items():
            with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        subprocess.run(["git", "-C", self.tmp, "add", "-A"], check=True)

    def test_scan_passes_on_valid_unexpired_waivers_and_prose(self):
        self.repo({"a.py": "x = 1  " + waiver("suppression-budget") + "\n",
                   "doc.md": f"格式：`# {ALLOW} <规则名>: <理由> until <日期> approved-by <人>`，见 {ALLOW} 一节\n"})
        self.assertEqual(sb.scan(self.tmp, TODAY), [])

    def test_scan_fails_on_expired_waiver_with_location(self):
        self.repo({"a.py": "x = 1\ny = 2  " + waiver("suppression-budget", until="2026-11-20") + "\n"})
        errors = sb.scan(self.tmp, TODAY)
        self.assertEqual(len(errors), 1)
        self.assertIn("a.py:2", errors[0])
        self.assertIn("已过期", errors[0])

    def test_scan_fails_on_malformed_waiver(self):
        self.repo({"a.py": f"x = 1  # {ALLOW} some-rule: 没有日期和审批人\n"})
        errors = sb.scan(self.tmp, TODAY)
        self.assertEqual(len(errors), 1)
        self.assertIn("格式不完整", errors[0])

    def test_scan_only_looks_at_tracked_files_and_skips_binary(self):
        self.repo({"a.py": "x = 1\n"})
        with open(os.path.join(self.tmp, "untracked.py"), "w") as fh:
            fh.write(waiver("r", until="2020-01-01") + "\n")
        with open(os.path.join(self.tmp, "b.bin"), "wb") as fh:
            fh.write(b"\xff\xfe\x00" + waiver("r", until="2020-01-01").encode())
        subprocess.run(["git", "-C", self.tmp, "add", "b.bin"], check=True)
        self.assertEqual(sb.scan(self.tmp, TODAY), [])

    def test_scan_legacy_prefix_is_checked_too(self):
        self.repo({"a.py": waiver("r", until="2020-01-01", token="autopr-allow") + "\n"})
        self.assertEqual(len(sb.scan(self.tmp, TODAY)), 1)

    def test_cli_scan_exit_codes_and_today(self):
        self.repo({"a.py": "x = 1  " + waiver("suppression-budget", until="2026-12-01") + "\n"})
        self.assertEqual(run_cli(sb, ["--scan", self.tmp, "--today", "2026-11-21"])[0], 0)
        self.assertEqual(run_cli(sb, ["--scan", self.tmp, "--today", "2026-12-02"])[0], 1)
        self.assertEqual(run_cli(sb, [])[0], 2)
        self.assertEqual(run_cli(sb, ["--scan", self.tmp, "--today", "not-a-date"])[0], 2)

    def test_cli_pr_files_mode(self):
        path = write_json(self.tmp, "f.json", [pfile("a.py", f"@@ -1 +1 @@\n+x = 1  {NOQA}\n")])
        self.assertEqual(run_cli(sb, ["--pr-files", path])[0], 1)


class WaiverStateTest(unittest.TestCase):
    def test_states(self):
        self.assertEqual(waiver_state(waiver("r"), "r", TODAY), "ok")
        self.assertEqual(waiver_state(waiver("r", until="2026-11-21"), "r", TODAY), "ok")
        self.assertEqual(waiver_state(waiver("r", until="2026-11-20"), "r", TODAY), "expired")
        self.assertEqual(waiver_state(f"# {ALLOW} r: x until 2027-01-01", "r", TODAY), "malformed")
        self.assertIsNone(waiver_state(waiver("r"), "other", TODAY))
        self.assertIsNone(waiver_state("plain line", "r", TODAY))
        self.assertIsNone(waiver_state(f"# {ALLOW} <规则名>: <理由> until <日期> approved-by <人>", "r", TODAY))


class ChineseCommitTest(TempDirCase):
    def data(self, **kw):
        base = {"title": "fix(api): 修好分页越界", "body": "把偏移量限制在总数内。", "commits": ["fix: 修好分页越界\n\n原因见 src/a.py"]}
        base.update(kw)
        return base

    def test_passes_with_chinese_everywhere(self):
        self.assertEqual(cc.check(self.data()), [])

    def test_english_title_fails_with_six_lines(self):
        errors = cc.check(self.data(title="fix: handle pagination overflow"))
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("chinese-commit: PR 标题没有中文"))
        self.assertEqual(len(errors[0].splitlines()), 6)

    def test_english_commit_and_body_fail(self):
        errors = cc.check(self.data(body="Limits offset to total.", commits=["feat(x): add thing"]))
        self.assertEqual(len(errors), 2)

    def test_prefix_path_url_and_code_do_not_count(self):
        self.assertFalse(cc.has_chinese("feat(新): add `中文标识符` at https://例子.com/中文 in docs/说明/a.md"))
        self.assertTrue(cc.has_chinese("feat: 增/删 两处"))

    def test_merge_commits_are_exempt(self):
        commits = ["Merge pull request #12 from a/b\n\nfix thing", "Merge branch 'main' into x", "fix: 修复"]
        self.assertEqual(cc.check(self.data(commits=commits)), [])

    def test_empty_body_is_not_a_violation(self):
        self.assertEqual(cc.check(self.data(body="")), [])
        self.assertEqual(cc.check(self.data(body=None)), [])

    def test_trailer_alone_is_not_chinese(self):
        self.assertTrue(cc.check(self.data(commits=["fix: x\n\nCo-Authored-By: 张三 <a@b.c>"])))

    def test_cli_opt_out_and_default(self):
        inp = write_json(self.tmp, "in.json", self.data(title="fix: english only"))
        off = write_json(self.tmp, "rules.json", {"chinese_commit": False})
        on = write_json(self.tmp, "rules-on.json", {"chinese_commit": True})
        self.assertEqual(run_cli(cc, ["--input", inp])[0], 1)
        self.assertEqual(run_cli(cc, ["--input", inp, "--config", os.path.join(self.tmp, "absent.json")])[0], 1)
        self.assertEqual(run_cli(cc, ["--input", inp, "--config", on])[0], 1)
        self.assertEqual(run_cli(cc, ["--input", inp, "--config", off])[0], 0)

    def test_cli_bad_input_exit_2(self):
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as fh:
            fh.write("[")
        self.assertEqual(run_cli(cc, ["--input", bad])[0], 2)
        broken = write_json(self.tmp, "rules.json", [1])
        ok = write_json(self.tmp, "in.json", self.data())
        self.assertEqual(run_cli(cc, ["--input", ok, "--config", broken])[0], 2)


if __name__ == "__main__":
    unittest.main()
