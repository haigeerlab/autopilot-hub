"""CLI tests: files in, JSON + Markdown out; exit codes. Run as part of `python3 -B -m unittest discover -s tests`."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "admission", "cli.py")
FILE_DOC = {"filename": "docs/a.md", "status": "modified", "additions": 1, "deletions": 0, "patch": "@@ -1 +1 @@\n+x\n"}
FILE_CI = {"filename": ".github/workflows/ci.yml", "status": "modified", "additions": 1, "deletions": 0,
           "patch": "@@ -1 +1 @@\n+x\n"}
TAKEOVER_AUTO = '<!-- autopilot-takeover:v1 {"module": "m", "merge": "auto"} -->'


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, name, value, raw=False):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(value if raw else json.dumps(value))
        return path

    def run_cli(self, files, extra=()):
        out_json = os.path.join(self.tmp, "out.json")
        out_md = os.path.join(self.tmp, "out.md")
        args = [sys.executable, "-B", CLI, "--pr-files", files, "--repo", "haigeerlab/agent-relay",
                "--out-json", out_json, "--out-md", out_md, *extra]
        proc = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        result = None
        if os.path.exists(out_json):
            with open(out_json, encoding="utf-8") as f:
                result = json.load(f)
        md = ""
        if os.path.exists(out_md):
            with open(out_md, encoding="utf-8") as f:
                md = f.read()
        return proc, result, md

    def test_markdown_shape(self):
        files = self.write("files.json", [FILE_CI])
        proc, result, md = self.run_cli(files, ["--risk-config-yml", self.write("r.yml", "extends: plugin\n", raw=True)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = md.splitlines()
        self.assertEqual(lines[0], "<!-- autopilot-admission -->")
        self.assertTrue(lines[1].startswith("### autopilot/admission：不会自动合并（风险档 T4"), lines[1])
        self.assertIn("| 项 | 结果 | 说明 |", md)
        self.assertRegex(md, r"\| risk \| ⛔ \|")
        self.assertRegex(md, r"\| takeover \| — \| 没有接管评论")
        self.assertRegex(md, r"\| gate \| — \|")
        self.assertRegex(md, r"\| degrade \| ✅ \| 没有未关闭的降级 issue")
        self.assertIn("本检查只报告，不拦截、不合并（切片 7，见 autopilot-hub `docs/spec/slice-7.md`）。", md)
        self.assertEqual(result["risk"]["tier"], "T4")

    def test_all_but_gate_ok_still_not_auto_merge(self):
        files = self.write("files.json", [FILE_DOC])
        comments = self.write("c.json", [TAKEOVER_AUTO])
        cfg = self.write("r.yml", "extends: plugin\n", raw=True)
        proc, result, md = self.run_cli(files, ["--comments", comments, "--risk-config-yml", cfg])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(result["auto_merge"])
        self.assertEqual(result["headline"], "不会自动合并（硬卡：还没有证据）")
        self.assertRegex(md, r"\| risk \| ✅ \|")
        self.assertRegex(md, r"\| takeover \| ✅ \|")

    def test_open_degrade_issue_listed(self):
        files = self.write("files.json", [FILE_DOC])
        block = json.dumps({"scope": {"repo": "haigeerlab/agent-relay", "tier": "*"}, "level": "L2"})
        issues = self.write("i.json", [{"number": 186, "body": "<!-- autopilot-degrade:v1 " + block + " -->"}])
        proc, result, md = self.run_cli(files, ["--degrade-issues", issues])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(md, r"\| degrade \| — \| 有未关闭的降级 issue：#186")

    def test_yml_preferred_over_json_with_mismatch_note(self):
        files = self.write("files.json", [FILE_DOC])
        yml = self.write("r.yml", "extends: plugin\n", raw=True)
        js = self.write("r.json", {"extends": "plugin", "T4": ["docs/**"]})
        proc, result, md = self.run_cli(files, ["--risk-config-yml", yml, "--risk-config-json", js])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["risk"]["tier"], "T0")  # the .json would have made it T4
        self.assertIn("risk-tiers.yml 和 .json 不一致，以 .yml 为准", result["notes"])

    def test_same_yml_and_json_have_no_mismatch_note(self):
        files = self.write("files.json", [FILE_DOC])
        yml = self.write("r.yml", "extends: plugin\n", raw=True)
        js = self.write("r.json", {"extends": "plugin"})
        _, result, _ = self.run_cli(files, ["--risk-config-yml", yml, "--risk-config-json", js])
        self.assertFalse(any("不一致" in n for n in result["notes"]), result["notes"])

    def test_json_used_when_no_yml(self):
        files = self.write("files.json", [FILE_DOC])
        js = self.write("r.json", {"extends": "plugin"})
        _, result, _ = self.run_cli(files, ["--risk-config-yml", os.path.join(self.tmp, "none.yml"),
                                            "--risk-config-json", js])
        self.assertEqual(result["risk"]["tier"], "T0")

    def test_risk_config_flag_picks_by_suffix(self):
        files = self.write("files.json", [FILE_DOC])
        _, result, _ = self.run_cli(files, ["--risk-config", self.write("r.yml", "extends: plugin\n", raw=True)])
        self.assertEqual(result["risk"]["tier"], "T0")
        _, result, _ = self.run_cli(files, ["--risk-config", self.write("r.json", {"extends": "plugin"})])
        self.assertEqual(result["risk"]["tier"], "T0")

    def test_no_config_is_generic_t3(self):
        _, result, _ = self.run_cli(self.write("files.json", [FILE_DOC]))
        self.assertEqual(result["risk"]["tier"], "T3")

    def test_unreadable_json_config_is_generic_with_note(self):
        files = self.write("files.json", [FILE_DOC])
        bad = self.write("r.json", "extends: plugin", raw=True)
        proc, result, _ = self.run_cli(files, ["--risk-config-json", bad])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["risk"]["tier"], "T3")
        self.assertTrue(any("risk-tiers" in n for n in result["notes"]), result["notes"])

    def test_unreadable_yml_config_is_generic_with_note(self):
        files = self.write("files.json", [FILE_DOC])
        bad = self.write("r.yml", "extends: [unclosed\n", raw=True)
        _, result, _ = self.run_cli(files, ["--risk-config-yml", bad])
        self.assertEqual(result["risk"]["tier"], "T3")
        self.assertTrue(any("risk-tiers.yml" in n for n in result["notes"]), result["notes"])

    def test_old_flags_are_gone(self):
        files = self.write("files.json", [FILE_DOC])
        removed = ("conclusion", "schema", "allowed-" + "tools", "tier-" + "map", "levels-comments", "changed-files")
        for flag in ("--" + name for name in removed):
            proc, _, _ = self.run_cli(files, [flag, "x"])
            self.assertEqual(proc.returncode, 2, flag)

    def test_missing_pr_files_exits_2(self):
        proc, _, _ = self.run_cli(os.path.join(self.tmp, "none.json"))
        self.assertEqual(proc.returncode, 2)

    def test_pr_files_not_array_exits_2(self):
        proc, _, _ = self.run_cli(self.write("files.json", {"a": 1}))
        self.assertEqual(proc.returncode, 2)


class WorkflowLabelTest(unittest.TestCase):
    """The label step lives in the reusable workflow's shell; check what it selects."""

    def setUp(self):
        with open(os.path.join(ROOT, ".github", "workflows", "admission.yml"), encoding="utf-8") as f:
            self.text = f.read()

    def test_sets_one_risk_label_and_no_cap_label(self):
        self.assertIn('labels[]=autopilot:risk-${TIER}', self.text)
        self.assertNotIn("autopilot:cap-", self.text)
        self.assertEqual(len(re.findall(r'-f "labels\[\]=', self.text)), 1)

    def test_removes_the_other_risk_labels(self):
        self.assertIn("for tier in T0 T1 T2 T3 T4", self.text)
        self.assertIn('[ "${tier}" = "${TIER}" ] && continue', self.text)
        self.assertIn("labels/autopilot:risk-${tier}", self.text)

    def test_reads_base_branch_yml_and_json_and_comments(self):
        for part in ("risk-tiers.yml", "risk-tiers.json", "--comments", "--risk-config-yml", "--risk-config-json"):
            self.assertIn(part, self.text)

    def test_check_name_and_conclusion_unchanged(self):
        self.assertIn('"name": "autopilot/admission"', self.text)
        self.assertIn('"conclusion": "neutral"', self.text)


if __name__ == "__main__":
    unittest.main()
