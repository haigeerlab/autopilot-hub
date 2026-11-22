"""Release templates and the repository interface entry point (slice 16)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from pipeline import engine, limits, model
import test_engine

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "pipeline" / "entry.py"


class TemplatesTest(unittest.TestCase):
    def test_templates_load_resolve_and_declare_capabilities(self):
        for name, rollback in (("plugin", False), ("npm", False), ("site", True)):
            with self.subTest(template=name):
                template = model.load_template(name)
                pipeline = model.resolve({"extends": name})
                self.assertEqual(template["name"], name)
                self.assertEqual(pipeline["class"], name)
                self.assertIs(pipeline["rollback"], rollback)
                self.assertEqual(pipeline["steps"], template["steps"])
                ids = [s["id"] for s in pipeline["steps"]]
                self.assertEqual(len(ids), len(set(ids)))

    def test_human_and_ask_steps_wait_at_every_level(self):
        seen = set()
        for name in ("plugin", "npm", "site", "sandbox"):
            for step in model.resolve({"extends": name})["steps"]:
                if step["by"] == "human" or step["run"].startswith("ask:"):
                    seen.add(step["id"])
                    for level in model.LEVELS:
                        with self.subTest(template=name, step=step["id"], level=level):
                            self.assertEqual(engine.decision(step, level)[0], "waiting")
        self.assertTrue({"restart-codex", "reload-claude", "approve-stage", "move-latest", "confirm"} <= seen)

    def test_templates_execute_until_the_first_required_confirmation(self):
        cases = (("plugin", "prepare", "approve", ["impact", "full-test", "prepare"]),
                 ("plugin", "auto-confirm", "restart-codex",
                  ["impact", "full-test", "prepare", "approve", "tag-and-release", "install-local", "verify-install"]),
                 ("npm", "auto-confirm", "approve-stage",
                  ["version-branch", "candidate-check", "pack-smoke", "preflight", "stage-publish"]),
                 ("site", "auto", None,
                  ["build", "ci-green", "rollback-ready", "deploy-preview", "promote", "verify-live"]))
        for name, level, waiting, done in cases:
            with self.subTest(template=name, level=level):
                io = test_engine.FakeIO()
                io.pipeline = model.resolve({"extends": name})
                io.contents[".github/autopr/takeover.yml"] = "release: " + level
                io.command_result["output"] = "ok\nhttps://example.com/release\n"
                result = test_engine.run(io)
                self.assertEqual(result["level"], level)
                self.assertEqual(result["code"], 1 if waiting else 0, result)
                records = io.records()
                self.assertEqual([r["id"] for r in records if r["status"] == "done"], done)
                commands = {s["id"]: s["run"] for s in io.pipeline["steps"]}
                self.assertEqual([r[0] for r in io.ran], [commands[identifier] for identifier in done])
                if waiting:
                    self.assertEqual((records[-1]["id"], records[-1]["status"]), (waiting, "waiting"))

    def test_kernel_cap_keeps_delegate_plugin_approval_human(self):
        pipeline = model.resolve({"extends": "plugin"})
        level, reason = limits.effective_level("haigeerlab/delegate-plugin", "auto-confirm",
                                               pipeline, limits.load_limits())
        self.assertEqual(level, "prepare")
        self.assertIn("流水线自用插件", reason)
        approve = next(s for s in pipeline["steps"] if s["id"] == "approve")
        self.assertEqual(engine.decision(approve, level)[0], "waiting")

    def test_sandbox_impact_runs_even_at_human_level(self):
        impact = model.resolve({"extends": "sandbox"})["steps"][0]
        for level in model.LEVELS:
            self.assertEqual(engine.decision(impact, level)[0], "execute")


class EntryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.interface = self.root / ".github" / "autopr" / "interface.yml"
        self.interface.parent.mkdir(parents=True)

    def run_entry(self, name="check"):
        return subprocess.run([sys.executable, "-B", str(ENTRY), name], cwd=str(self.root),
                              input=b"not passed to the command\n", capture_output=True)

    def test_registered_command_preserves_cwd_and_output_and_closes_stdin(self):
        command = "read value || printf 'stdin closed\\n'; cat marker; printf 'error\\n' >&2; exit 7"
        self.interface.write_text("entries:\n  check:\n    run: " + json.dumps(command) + "\n", encoding="utf-8")
        (self.root / "marker").write_bytes(b"raw output\x00\xff\n")
        result = self.run_entry()
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout, b"stdin closed\nraw output\x00\xff\n")
        self.assertEqual(result.stderr, b"error\n")

    def test_missing_or_unreadable_registration_exits_three(self):
        cases = (None, "entries: {}", "entries: []", "entries:\n  check: {}", "entries:\n  check:\n    run: 3",
                 "entries:\n  check:\n    run: ''", "entries: [", "[]", "", b"\xff")
        message = ("interface.yml 没登记 entries.check.run，这一步无法自动做，"
                   "请在 pipeline.yml 里 replace 成自己的命令或 remove")
        for content in cases:
            with self.subTest(content=content):
                if isinstance(content, bytes):
                    self.interface.write_bytes(content)
                elif content is not None:
                    self.interface.write_text(content, encoding="utf-8")
                result = self.run_entry()
                self.assertEqual(result.returncode, 3)
                self.assertIn(message, (result.stdout + result.stderr).decode("utf-8"))

    def test_invalid_entry_names_exit_two_before_reading_or_executing(self):
        for name in ("release-publish", "CHECK", "../check", "check;touch marker", "", "检查", "check\n"):
            with self.subTest(name=name):
                self.assertEqual(self.run_entry(name).returncode, 2)
        self.assertFalse((self.root / "marker").exists())

    def test_lowercase_digits_and_underscores_are_valid(self):
        self.interface.write_text("entries:\n  release_2:\n    run: exit 23\n", encoding="utf-8")
        self.assertEqual(self.run_entry("release_2").returncode, 23)


if __name__ == "__main__":
    unittest.main()
