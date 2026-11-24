"""Plugin-standard items in the checkup report (checkup/standard.py): A3, C1, C9, C10.

Run: python3 -B -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from checkup import standard as st
from checkup.ghprobe import GhResponse

MIT = open(os.path.join(ROOT, "LICENSE"), encoding="utf-8").read()
CI = open(os.path.join(ROOT, "examples", "caller-ci-standard.yml"), encoding="utf-8").read()
DEPENDABOT = open(os.path.join(ROOT, ".github", "dependabot.yml"), encoding="utf-8").read()


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class FakeGh:
    def __init__(self, classic=None, rules=None):
        self.classic, self.rules = classic, rules

    def get(self, path):
        if path.endswith("/protection"):
            if self.classic is None:
                return GhResponse("not_found", 404, None, "Branch not protected")
            return GhResponse("ok", 200, self.classic)
        if "/rules/branches/" in path:
            return GhResponse("ok", 200, self.rules or [])
        return GhResponse("not_found", 404, None, "Not Found")


def by_id(findings):
    return {f["id"]: f for f in findings}


class StandardItemsTest(unittest.TestCase):
    def run_on(self, files, gh=None):
        with tempfile.TemporaryDirectory() as root:
            for rel, text in files.items():
                write(root, rel, text)
            return by_id(st.findings(root, gh, "o/r", "main"))

    def test_ids_do_not_clash_with_checkup_numbers(self):
        out = self.run_on({})
        self.assertEqual(sorted(out), ["规范A3", "规范C1", "规范C10", "规范C9"])
        for f in out.values():
            self.assertTrue(f["title"] and f["impact"])

    def test_a3_mit_with_the_standard_copyright(self):
        self.assertEqual(self.run_on({"LICENSE": MIT})["规范A3"]["status"], "ok")
        self.assertEqual(self.run_on({})["规范A3"]["status"], "missing")
        self.assertEqual(self.run_on({"LICENSE": MIT.replace("haigeerlab", "someone")})["规范A3"]["status"], "missing")
        self.assertEqual(self.run_on({"LICENSE": "Apache License 2.0\nCopyright (c) 2026 haigeerlab"})["规范A3"]["status"], "missing")

    def test_a3_manifest_license_field(self):
        manifest = '{"name": "x", "version": "1", "license": "Apache-2.0"}'
        out = self.run_on({"LICENSE": MIT, "plugins/x/.claude-plugin/plugin.json": manifest})
        self.assertEqual(out["规范A3"]["status"], "missing")
        self.assertIn("plugin.json", out["规范A3"]["detail"])

    def test_c1_needs_workflow_aggregate_and_protection(self):
        gh_ok = FakeGh(rules=[{"type": "required_status_checks",
                               "parameters": {"required_status_checks": [{"context": "CI required"}]}}])
        self.assertEqual(self.run_on({".github/workflows/ci.yml": CI}, gh_ok)["规范C1"]["status"], "ok")
        classic_ok = FakeGh(classic={"required_status_checks": {"contexts": ["CI required"], "checks": []}})
        self.assertEqual(self.run_on({".github/workflows/ci.yml": CI}, classic_ok)["规范C1"]["status"], "ok")
        self.assertEqual(self.run_on({".github/workflows/ci.yml": CI}, FakeGh())["规范C1"]["status"], "missing")
        self.assertEqual(self.run_on({}, gh_ok)["规范C1"]["status"], "missing")

    def test_c1_without_github_is_unknown(self):
        self.assertEqual(self.run_on({".github/workflows/ci.yml": CI}, None)["规范C1"]["status"], "unknown")

    def test_c1_rejects_path_filters_and_missing_aggregate(self):
        gh = FakeGh(rules=[{"type": "required_status_checks",
                            "parameters": {"required_status_checks": [{"context": "CI required"}]}}])
        filtered = CI.replace("on:\n  pull_request:\n", "on:\n  pull_request:\n    paths: ['src/**']\n")
        self.assertEqual(self.run_on({".github/workflows/ci.yml": filtered}, gh)["规范C1"]["status"], "missing")
        no_agg = CI.replace("name: CI required", "name: summary")
        self.assertEqual(self.run_on({".github/workflows/ci.yml": no_agg}, gh)["规范C1"]["status"], "missing")

    def test_c9_dependabot(self):
        self.assertEqual(self.run_on({".github/dependabot.yml": DEPENDABOT})["规范C9"]["status"], "ok")
        self.assertEqual(self.run_on({})["规范C9"]["status"], "missing")
        self.assertEqual(self.run_on({".github/dependabot.yml": DEPENDABOT.replace("default-days: 7", "default-days: 3")})["规范C9"]["status"], "missing")
        extra = DEPENDABOT + '  - package-ecosystem: "pip"\n    directory: "/"\n    schedule:\n      interval: "weekly"\n'
        self.assertEqual(self.run_on({".github/dependabot.yml": extra})["规范C9"]["status"], "missing")

    def test_c10_clean_tree_step(self):
        self.assertEqual(self.run_on({".github/workflows/ci.yml": CI})["规范C10"]["status"], "ok")
        self.assertEqual(self.run_on({".github/workflows/ci.yml": CI.replace("git diff --exit-code", "true")})["规范C10"]["status"], "missing")


if __name__ == "__main__":
    unittest.main()
