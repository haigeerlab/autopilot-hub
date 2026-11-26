"""Local validation entry: conservative quick selection and Bash compatibility."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from checkup import standard


class ValidateSelectionTest(unittest.TestCase):
    def select(self, changed, *args):
        env = dict(os.environ, PYTHON=sys.executable, VALIDATE_CHANGED=changed)
        result = subprocess.run(
            ["bash", "scripts/validate.sh", "--quick", "--list", *args],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = {line for line in result.stdout.splitlines() if line.startswith("tests/")}
        return selected, result.stdout + result.stderr

    def all_tests(self):
        def modules(suite):
            for test in suite:
                if isinstance(test, unittest.TestSuite):
                    yield from modules(test)
                else:
                    yield "tests/" + test.__class__.__module__.replace(".", "/") + ".py"
        return set(modules(unittest.defaultTestLoader.discover(str(ROOT / "tests"))))

    def test_python_source_selects_referencing_tests_without_full_fallback(self):
        selected, output = self.select("rules/local_paths.py")
        self.assertIn("tests/test_rules_standard_a.py", selected)
        self.assertLess(len(selected), len(self.all_tests()))
        self.assertIn("rules/local_paths.py", output)

    def test_from_import_with_alias_selects_test(self):
        selected, _ = self.select("audit/plan_check.py")
        self.assertIn("tests/test_audit_plan.py", selected)
        self.assertLess(len(selected), len(self.all_tests()))

    def test_changed_test_selects_itself(self):
        selected, _ = self.select("tests/test_rules.py")
        self.assertEqual(selected, {"tests/test_rules.py"})

    def test_documents_select_repository_text_scanners(self):
        scanners = {test for test in self.all_tests()
                    if "ls-files" in (ROOT / test).read_text(encoding="utf-8")}
        for changed in ("docs/README.md\nAGENTS.md", "docs/example.txt", "README.md"):
            with self.subTest(changed=changed):
                selected, output = self.select(changed)
                self.assertEqual(selected, scanners)
                self.assertIn("tests/test_no_local_paths.py", selected)
                self.assertIn("文档改动，加跑全仓文本检查", output)

    def test_documents_also_select_scanners_alongside_code(self):
        selected, _ = self.select("tests/test_rules.py\nREADME.md")
        self.assertIn("tests/test_rules.py", selected)
        self.assertIn("tests/test_no_local_paths.py", selected)
        self.assertLess(len(selected), len(self.all_tests()))

    def test_workflow_falls_back_to_full(self):
        selected, output = self.select(".github/workflows/ci.yml")
        self.assertEqual(selected, self.all_tests())
        self.assertIn("对应不上，退回全量：.github/workflows/ci.yml", output)

    def test_unmatched_non_document_falls_back_even_with_matching_test(self):
        for unmatched in ("missing_" + "source.py", "settings.json", "tests/test_deleted.py"):
            with self.subTest(unmatched=unmatched):
                selected, output = self.select("tests/test_rules.py\n" + unmatched)
                self.assertEqual(selected, self.all_tests())
                self.assertIn("对应不上，退回全量：" + unmatched, output)

    def test_empty_override_falls_back_to_full(self):
        selected, _ = self.select("")
        self.assertEqual(selected, self.all_tests())

    def test_plain_list_ignores_changed_override(self):
        result = subprocess.run(
            ["bash", "scripts/validate.sh", "--list"], cwd=ROOT,
            env=dict(os.environ, PYTHON=sys.executable, VALIDATE_CHANGED="docs/README.md"),
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(result.stdout.splitlines()), self.all_tests())

    def test_script_is_executable_and_passes_c5(self):
        self.assertTrue(os.access(ROOT / "scripts/validate.sh", os.X_OK))
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory) / "scripts"
            scripts.mkdir()
            (scripts / "validate.sh").write_bytes((ROOT / "scripts/validate.sh").read_bytes())
            self.assertEqual(standard.c5(directory)["status"], "ok")


class ValidateRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "scripts").mkdir()
        (self.root / "scripts/validate.sh").write_bytes((ROOT / "scripts/validate.sh").read_bytes())
        (self.root / "tests").mkdir()
        self.env = dict(os.environ, PYTHON=sys.executable)
        self.env.pop("VALIDATE_CHANGED", None)

    def run_script(self, *args):
        return subprocess.run(["bash", "scripts/validate.sh", *args], cwd=self.root,
                              env=self.env, capture_output=True, text=True, timeout=15)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write_test(self, path, imports=""):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(imports + "\nimport unittest\nclass Example(unittest.TestCase):\n"
                          "    def test_success(self):\n        pass\n", encoding="utf-8")

    def test_quick_follows_transitive_imports_to_fixed_point(self):
        package = self.root / "sample"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
        (package / "b.py").write_text("from . import a as first\n", encoding="utf-8")
        (package / "c.py").write_text("from sample.b import first\n", encoding="utf-8")
        (package / "d.py").write_text("import sample.c as third\n", encoding="utf-8")
        self.write_test("tests/test_direct.py", "import sample.a")
        self.write_test("tests/test_indirect.py", "from sample import b")
        self.write_test("tests/test_transitive.py", "import sample.d")
        self.write_test("tests/test_unrelated.py")
        self.env["VALIDATE_CHANGED"] = "sample/a.py"
        result = self.run_script("--quick", "--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(result.stdout.splitlines()), {
            "tests/test_direct.py", "tests/test_indirect.py", "tests/test_transitive.py"})
        self.assertNotIn("退回全量", result.stderr)

    def test_quick_selects_tests_that_import_by_name_from_a_subdirectory(self):
        tool = self.root / "tool"
        tool.mkdir()
        (tool / "engine.py").write_text("VALUE = 1\n", encoding="utf-8")
        (tool / "cli.py").write_text("import engine\n", encoding="utf-8")
        self.write_test("tests/test_package.py", "import tool.cli")
        self.write_test("tests/test_by_name.py",
                        "import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tool'))\n"
                        "import engine")
        self.write_test("tests/test_unrelated.py")
        self.env["VALIDATE_CHANGED"] = "tool/engine.py"
        result = self.run_script("--quick", "--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(result.stdout.splitlines()), {"tests/test_package.py", "tests/test_by_name.py"})
        self.assertNotIn("退回全量", result.stderr)

    def test_full_discovery_includes_test_prefix_and_package_tests(self):
        self.write_test("tests/testlegacy.py")
        self.write_test("tests/test_shared.py")
        self.write_test("tests/nested/test_shared.py")
        (self.root / "tests/nested/__init__.py").write_text("", encoding="utf-8")
        self.write_test("tests/not_a_package/test_ignored.py")
        result = self.run_script("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = {"tests/testlegacy.py", "tests/test_shared.py", "tests/nested/test_shared.py"}
        self.assertEqual(set(result.stdout.splitlines()), expected)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nested.test_shared.Example", result.stderr)
        self.assertIn("test_shared.Example", result.stderr)
        self.assertEqual(result.stderr.count("test_success ("), 3)

    def test_all_files_run_after_failure_and_timings_are_reported(self):
        (self.root / "tests/test_a.py").write_text(
            "import unittest\nclass Failure(unittest.TestCase):\n"
            "    def test_failure(self):\n        self.fail('expected failure')\n", encoding="utf-8")
        (self.root / "tests/test_z.py").write_text(
            "import unittest\nclass Success(unittest.TestCase):\n"
            "    def test_success(self):\n        print('last test ran')\n", encoding="utf-8")
        result = self.run_script()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("last test ran", result.stdout)
        self.assertIn("test_failure (", result.stderr)
        self.assertIn("test_success (", result.stderr)
        self.assertIn("用时最长的 5 个测试文件", result.stdout)
        self.assertRegex(result.stdout, r"tests/test_a.py \d+\.\d+ 秒")
        self.assertFalse(list(self.root.rglob("__pycache__")))

    def test_git_combines_branch_staged_unstaged_and_untracked_changes(self):
        self.git("init", "-b", "main")
        self.git("config", "maintenance.auto", "false")
        self.git("config", "gc.auto", "0")
        self.git("config", "user.name", "Validation Test")
        self.git("config", "user.email", "validation@example.invalid")
        names = ("branch", "staged", "unstaged", "untracked", "unchanged")
        for name in names[:-2] + ("unchanged",):
            self.write_test("tests/test_%s.py" % name)
        self.git("add", ".")
        self.git("commit", "-m", "test: initial")
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-b", "feature")
        with (self.root / "tests/test_branch.py").open("a", encoding="utf-8") as stream:
            stream.write("# branch change\n")
        self.git("add", ".")
        self.git("commit", "-m", "test: branch change")
        with (self.root / "tests/test_staged.py").open("a", encoding="utf-8") as stream:
            stream.write("# staged change\n")
        self.git("add", ".")
        with (self.root / "tests/test_unstaged.py").open("a", encoding="utf-8") as stream:
            stream.write("# unstaged change\n")
        self.write_test("tests/test_untracked.py")
        expected = {"tests/test_%s.py" % name for name in names[:-1]}
        # First exercise the local main fallback, then prefer origin/main even if main has moved.
        for remote in (False, True):
            with self.subTest(remote=remote):
                if remote:
                    self.git("update-ref", "refs/remotes/origin/main", base)
                    self.git("branch", "-f", "main", "HEAD")
                result = self.run_script("--quick", "--list")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(set(result.stdout.splitlines()), expected)


if __name__ == "__main__":
    unittest.main()
