"""CLI tests: files in, JSON + Markdown out; exit codes. Run as part of `python3 -B -m unittest discover -s tests`."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
CLI = os.path.join(ROOT, "admission", "cli.py")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, conclusion, extra=()):
        out_json = os.path.join(self.tmp, "out.json")
        out_md = os.path.join(self.tmp, "out.md")
        args = [sys.executable, "-B", CLI,
                "--conclusion", conclusion,
                "--schema", os.path.join(ROOT, "schemas", "vendor", "repo-readiness", "conclusion.v1.json"),
                "--allowed-tools", os.path.join(FIX, "allowed-tools.json"),
                "--repo", "haigeerlab/agent-relay", "--default-branch", "main",
                "--now", "2026-11-20T12:00:00Z",
                "--out-json", out_json, "--out-md", out_md, *extra]
        proc = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        result = None
        if os.path.exists(out_json):
            with open(out_json, encoding="utf-8") as f:
                result = json.load(f)
        md = open(out_md, encoding="utf-8").read() if os.path.exists(out_md) else ""
        return proc, result, md

    def test_valid_conclusion(self):
        proc, result, md = self.run_cli(os.path.join(FIX, "conclusion.basic.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["cap"], "L2")
        self.assertEqual(len(result["conclusion_sha256"]), 64)
        self.assertTrue(md.startswith("<!-- autopilot-admission -->"))
        self.assertIn("信任上限 L2（只报告）", md)

    def test_missing_conclusion_still_exits_zero_with_L0(self):
        proc, result, _ = self.run_cli(os.path.join(self.tmp, "nope.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["cap"], "L0")
        self.assertIsNone(result["conclusion_sha256"])

    def test_unparseable_conclusion_is_L0(self):
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as f:
            f.write("{not json")
        proc, result, _ = self.run_cli(bad)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["cap"], "L0")
        self.assertIn("格式", result["reason"])

    def test_changed_files_and_degrade_issues(self):
        changed = os.path.join(self.tmp, "changed.txt")
        with open(changed, "w") as f:
            f.write("src/core/a.py\n")
        issues = os.path.join(self.tmp, "issues.json")
        with open(issues, "w") as f:
            json.dump([{"body": '<!-- autopilot-degrade:v1 {"scope":{"repo":"haigeerlab/agent-relay","tier":"*"},"level":"L2"} -->'}], f)
        proc, result, _ = self.run_cli(os.path.join(FIX, "conclusion.basic.json"),
                                       ["--changed-files", changed, "--degrade-issues", issues])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["cap"], "L2")  # app cap L3, lowered by degrade issue to L2

    def test_levels_comments_file(self):
        changed = os.path.join(self.tmp, "changed.txt")
        with open(changed, "w") as f:
            f.write("src/core/a.py\n")
        comments = os.path.join(self.tmp, "comments.json")
        with open(comments, "w") as f:
            json.dump(['<!-- autopilot-levels:v1 {"spec_guard":{"module":"m","tier":"standard"},"module_crew":{"effective":"relaxed"}} -->'], f)
        proc, result, md = self.run_cli(os.path.join(FIX, "conclusion.basic.json"),
                                        ["--changed-files", changed, "--levels-comments", comments])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["cap"], "L3")
        self.assertIn("spec-guard", md)

    def test_no_levels_comments_defaults_to_L2(self):
        changed = os.path.join(self.tmp, "changed.txt")
        with open(changed, "w") as f:
            f.write("src/core/a.py\n")
        proc, result, _ = self.run_cli(os.path.join(FIX, "conclusion.basic.json"), ["--changed-files", changed])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["cap"], "L2")

    def test_trust_flag_is_gone(self):
        proc, _, _ = self.run_cli(os.path.join(FIX, "conclusion.basic.json"), ["--trust", "x"])
        self.assertEqual(proc.returncode, 2)

    def test_bad_tier_map_exits_2(self):
        bad = os.path.join(self.tmp, "map.json")
        with open(bad, "w") as f:
            f.write("{}")
        proc, _, _ = self.run_cli(os.path.join(FIX, "conclusion.basic.json"), ["--tier-map", bad])
        self.assertEqual(proc.returncode, 2)

    def test_missing_schema_exits_2(self):
        out = os.path.join(self.tmp, "o.json")
        proc = subprocess.run([sys.executable, "-B", CLI, "--conclusion", "x", "--schema", os.path.join(self.tmp, "none.json"),
                               "--allowed-tools", "x", "--repo", "a/b", "--default-branch", "main",
                               "--out-json", out, "--out-md", out + ".md"],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
