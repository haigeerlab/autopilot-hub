"""Plugin standard C7 (frontmatter of skills, commands, agents) and C8 (workflow security, static part).

rules/frontmatter.py, rules/workflow_audit.py. Run: python3 -B -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rules import frontmatter as fm
from rules import workflow_audit as wa


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


SKILL = "---\nname: ask\ndescription: 查知识库\n---\n正文\n"


class FrontmatterTest(unittest.TestCase):
    def run_on(self, files):
        with tempfile.TemporaryDirectory() as root:
            for rel, text in files.items():
                write(root, rel, text)
            return fm.check(root)

    def test_good_files_pass(self):
        self.assertEqual(self.run_on({
            "plugins/kb/skills/ask/SKILL.md": SKILL,
            "plugins/kb/agents/helper.md": "---\nname: helper\ndescription: >\n  帮手\n---\n",
            "plugins/kb/commands/save.md": "---\ndescription: 保存\nargument-hint: \"[text]\"\n---\n"}), [])

    def test_unclosed_flow_is_a_yaml_error(self):
        errors = self.run_on({"plugins/kb/skills/ask/SKILL.md": "---\nname: [ask\ndescription: x\n---\n"})
        self.assertEqual(len(errors), 1)
        self.assertIn("YAML", errors[0])

    def test_skill_and_agent_need_name_and_description(self):
        errors = self.run_on({"skills/a/SKILL.md": "---\ndescription: x\n---\n",
                              "agents/b.md": "---\nname: b\n---\n"})
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("name" in e for e in errors) and any("description" in e for e in errors))

    def test_command_needs_only_description(self):
        self.assertEqual(self.run_on({"commands/go.md": "---\ndescription: 出发\n---\n"}), [])
        self.assertEqual(len(self.run_on({"commands/go.md": "---\nname: go\n---\n"})), 1)

    def test_missing_frontmatter(self):
        self.assertEqual(len(self.run_on({"skills/a/SKILL.md": "# 没有元信息\n"})), 1)

    def test_empty_values_fail(self):
        self.assertEqual(len(self.run_on({"skills/a/SKILL.md": "---\nname: ''\ndescription: x\n---\n"})), 1)

    def test_other_markdown_is_ignored(self):
        self.assertEqual(self.run_on({"docs/commands/x.md": "no frontmatter", "skills/a/README.md": "x",
                                      ".claude/worktrees/w/skills/a/SKILL.md": "broken"}), [])

    def test_cli(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, "skills/a/SKILL.md", "---\nname: a\n---\n")
            r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "rules", "frontmatter.py"), "--repo", root],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)


GOOD_WF = """name: CI
on: [pull_request]
permissions: {}
jobs:
  test:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
"""


def changed(name, status="modified"):
    return {"filename": name, "status": status, "patch": "@@ -1 +1 @@\n+x"}


class WorkflowAuditTest(unittest.TestCase):
    def run_on(self, text, name=".github/workflows/ci.yml", status="modified"):
        with tempfile.TemporaryDirectory() as root:
            write(root, name, text)
            return wa.check(root, [changed(name, status)])

    def test_good_workflow_passes(self):
        self.assertEqual(self.run_on(GOOD_WF), [])

    def test_top_level_permissions_must_be_empty(self):
        for text in (GOOD_WF.replace("permissions: {}\n", ""), GOOD_WF.replace("permissions: {}", "permissions:\n  contents: read")):
            with self.subTest(text=text[:40]):
                self.assertTrue(any("permissions" in e for e in self.run_on(text)))

    def test_checkout_must_not_keep_credentials(self):
        text = GOOD_WF.replace("        with:\n          persist-credentials: false\n", "")
        self.assertTrue(any("persist-credentials" in e for e in self.run_on(text)))
        self.assertTrue(any("persist-credentials" in e for e in self.run_on(GOOD_WF.replace("false", "true"))))

    def test_kept_credentials_with_a_reason_pass(self):
        text = GOOD_WF.replace("persist-credentials: false", "persist-credentials: true  # 原因：这一步要推送标签")
        self.assertEqual(self.run_on(text), [])

    def test_unparseable_workflow_fails(self):
        self.assertTrue(any("读不懂" in e for e in self.run_on("jobs: [unclosed\n")))

    def test_only_changed_workflow_files(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, ".github/workflows/old.yml", GOOD_WF.replace("permissions: {}\n", ""))
            write(root, ".github/workflows/ci.yml", GOOD_WF)
            self.assertEqual(wa.check(root, [changed(".github/workflows/ci.yml"), changed("README.md"),
                                             changed(".github/workflows/gone.yml", "removed")]), [])

    def test_changed_paths(self):
        files = [changed(".github/workflows/a.yml"), changed(".github/workflows/b.yaml", "added"),
                 changed(".github/workflows/c.yml", "removed"), changed(".github/actions/x/action.yml"), changed("a.yml")]
        self.assertEqual(wa.changed_workflows(files), [".github/workflows/a.yml", ".github/workflows/b.yaml"])

    def test_cli_lists_changed_files_for_zizmor(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "files.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([changed(".github/workflows/a.yml"), changed("x.md")], fh)
            r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "rules", "workflow_audit.py"), "--pr-files", path,
                                "--list-changed"], capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stdout.split()), (0, [".github/workflows/a.yml"]))


if __name__ == "__main__":
    unittest.main()
