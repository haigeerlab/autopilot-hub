"""Tests for slice 13 pipeline loading, merging and display."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from pipeline import model

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "pipeline"


def step(identifier="extra", at="before-release", **fields):
    result = {"id": identifier, "at": at, "run": "echo test", "by": "ai"}
    result.update(fields)
    return result


class PipelineTest(unittest.TestCase):
    def resolve(self, **fields):
        return model.resolve(dict(extends="sandbox", **fields))

    def problems(self, config, templates_dir=model.DEFAULT):
        with self.assertRaises(model.PipelineError) as caught:
            model.resolve(config, templates_dir)
        self.assertIsInstance(caught.exception.problems, list)
        return "\n".join(caught.exception.problems)

    def test_load_template_and_defaults(self):
        template = model.load_template("sandbox")
        self.assertEqual(template["name"], "sandbox")
        self.assertEqual(template["class"], "sandbox")
        self.assertIs(template["rollback"], True)
        result = self.resolve()
        self.assertEqual(result["template"], "sandbox")
        self.assertEqual(result["class"], "sandbox")
        self.assertEqual([s["id"] for s in result["steps"]],
                         ["impact", "prepare", "confirm", "publish", "verify", "rollback-check", "rollback"])
        self.assertEqual(result["steps"][0]["evidence"], ["stdout"])
        self.assertEqual(result["steps"][0]["auto_when"], "release: human")
        self.assertIsNone(result["steps"][0]["stop_if"])
        self.assertEqual(result["steps"][-1], dict(step("rollback", at="after-live", run="echo 回滚演练"),
                                                  auto_when="release: auto", stop_if=None, evidence=["stdout"]))

    def test_template_class_is_required_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "example.yml"
            for category in ("plugin", "npm", "site", "sandbox", None, "unknown", [], True):
                content = {"name": "example", "rollback": True, "steps": []}
                if category is not None:
                    content["class"] = category
                path.write_text("\n".join(key + ": " + json.dumps(value)
                                          for key, value in content.items()), encoding="utf-8")
                with self.subTest(category=category):
                    if isinstance(category, str) and category in ("plugin", "npm", "site", "sandbox"):
                        self.assertEqual(model.resolve({"extends": "example"}, directory)["class"], category)
                    else:
                        self.assertIn("class", self.problems({"extends": "example"}, directory))

    def test_repository_cannot_override_template_class_or_rollback(self):
        for key, value in (("class", "site"), ("rollback", True)):
            self.assertIn(key, self.problems({"extends": "sandbox", key: value}))

    def test_template_validation(self):
        with self.assertRaises(model.PipelineError) as caught:
            model.load_template("invalid", FIXTURES)
        text = "\n".join(caught.exception.problems)
        for field in ("rollback", "pre-merge", "by", "auto_when", "stop_if", "evidence"):
            self.assertIn(field, text)

    def test_unknown_template_and_top_level_keys(self):
        self.assertIn("不存在", self.problems({"extends": "absent"}))
        self.assertIn("typo", self.problems({"extends": "sandbox", "typo": True}))
        self.assertEqual(model.resolve({"extends": "sandbox", "$schema": "unused", "version": 1}),
                         self.resolve())

    def test_add_append_and_insert_in_order(self):
        result = self.resolve(add=[step("last"), step("first", before="prepare"),
                                   step("middle", after="confirm"), step("next", after="middle")])
        self.assertEqual([s["id"] for s in result["steps"] if s["at"] == "before-release"],
                         ["first", "prepare", "confirm", "middle", "next", "last"])
        self.assertNotIn("before", result["steps"][1])

    def test_remove_and_readd(self):
        result = self.resolve(remove=["confirm"], add=[step("confirm")])
        self.assertEqual([s["id"] for s in result["steps"]].count("confirm"), 1)
        self.assertEqual(next(s for s in result["steps"] if s["id"] == "confirm")["by"], "ai")
        self.assertNotIn("publish", [s["id"] for s in self.resolve(remove=["publish"])["steps"]])

    def test_replace_inherits_segment_and_resets_fields(self):
        replacement = {"id": "publish", "run": "echo custom", "by": "external"}
        result = self.resolve(replace=[replacement])
        actual = next(s for s in result["steps"] if s["id"] == "publish")
        self.assertEqual(actual, dict(replacement, at="release", auto_when=None,
                                      stop_if=None, evidence=["stdout"]))
        self.assertNotIn("at", replacement)

    def test_replace_can_change_segment(self):
        result = self.resolve(replace=[step("publish", at="after-live")])
        self.assertEqual([s["id"] for s in result["steps"]][-3:], ["publish", "rollback-check", "rollback"])

    def test_duplicate_ids(self):
        for additions in ([step("impact")], [step("new"), step("new")]):
            with self.subTest(additions=additions):
                self.assertIn("重复", self.problems({"extends": "sandbox", "add": additions}))
        with self.assertRaises(model.PipelineError):
            model.load_template("duplicate", FIXTURES)

    def test_missing_references(self):
        for field, value in (("remove", ["absent"]), ("replace", [step("absent")]),
                             ("add", [step(before="absent")]), ("add", [step(after="absent")])):
            with self.subTest(field=field, value=value):
                self.assertIn("不存在", self.problems({"extends": "sandbox", field: value}))

    def test_duplicate_and_bad_reference_are_both_reported(self):
        text = self.problems({"extends": "sandbox", "add": [step("prepare", before="absent")]})
        self.assertIn("重复", text)
        self.assertIn("不存在", text)

    def test_cross_segment_and_ambiguous_placement(self):
        for placement in ({"before": "impact"}, {"after": "verify"},
                          {"before": "prepare", "after": "confirm"}):
            self.problems({"extends": "sandbox", "add": [step(**placement)]})

    def test_remove_replace_conflict(self):
        self.assertIn("同时", self.problems({"extends": "sandbox", "remove": ["publish"],
                                            "replace": [step("publish")]}))

    def test_pre_merge_rules(self):
        result = self.resolve(add=[step("lint", at="pre-merge")])
        self.assertEqual(result["steps"][0]["id"], "lint")
        self.assertIn("pre-merge", self.problems({"extends": "sandbox",
                                                 "replace": [step("publish", at="pre-merge")]}))
        for field, value in (("remove", ["lint"]), ("replace", [step("lint")])):
            text = self.problems({"extends": "premerge", field: value}, FIXTURES)
            self.assertIn(field, text)
            self.assertIn("pre-merge", text)

    def test_field_validation(self):
        cases = [{"run": "ask: 你"}, {"by": "robot"}, {"by": []}, {"id": "Bad_ID"},
                 {"at": "unknown"}, {"run": ""}, {"auto_when": "release: fast"},
                 {"auto_when": "merge: auto"}, {"auto_when": None},
                 {"stop_if": "exit == 0"}, {"stop_if": "missing: other"},
                 {"evidence": "stdout"}, {"evidence": ["other"]}, {"evidence": [[]]}]
        for fields in cases:
            with self.subTest(fields=fields):
                self.problems({"extends": "sandbox", "add": [step(**fields)]})

    def test_valid_optional_values(self):
        for level in model.LEVELS:
            result = self.resolve(add=[step(auto_when="release: " + level,
                                            stop_if="missing: url", evidence=["url", "file"])])
            self.assertEqual(next(s for s in result["steps"] if s["id"] == "extra")["evidence"],
                             ["url", "file"])
        self.resolve(add=[step(run="ask: 人", by="human")])

    def test_multiple_problems_and_malformed_shapes(self):
        text = self.problems({"extends": "sandbox", "unknown": 1,
                              "add": [step(by="robot", auto_when="bad", evidence=["bad"])]})
        for field in ("unknown", "by", "auto_when", "evidence"):
            self.assertIn(field, text)
        for config in (None, [], {}, {"extends": []}, {"extends": "../sandbox"},
                       {"extends": "sandbox", "add": {}},
                       {"extends": "sandbox", "remove": [[]]},
                       {"extends": "sandbox", "replace": [None]},
                       {"extends": "sandbox", "add": [None]}):
            with self.subTest(config=config):
                self.problems(config)

    def test_template_structure_and_missing_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.yml"
            for content in ("[]", "name: broken\nsteps: []", "name: wrong\nrollback: false\nsteps: []",
                            "name: broken\nrollback: false", "name: broken\nrollback: false\nsteps: {}",
                            "name: broken\nrollback: false\nsteps: [null, {}]"):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(model.PipelineError):
                        model.load_template("broken", directory)

    def test_combines_template_and_repo_problems(self):
        text = self.problems({"extends": "invalid", "wrong": True,
                              "add": [step(by="robot")]}, FIXTURES)
        for field in ("rollback", "wrong", "add[0]", "auto_when"):
            self.assertIn(field, text)

    def test_add_can_target_replaced_segment_but_not_removed_step(self):
        result = self.resolve(replace=[step("publish", at="after-live")],
                              add=[step("before-publish", at="after-live", before="publish")])
        self.assertEqual([s["id"] for s in result["steps"] if s["at"] == "after-live"],
                         ["before-publish", "publish", "rollback-check", "rollback"])
        self.assertIn("不存在", self.problems({"extends": "sandbox", "remove": ["confirm"],
                                               "add": [step(after="confirm")]}))

    def test_resolution_does_not_mutate_input(self):
        config = {"extends": "sandbox", "add": [step()]}
        original = copy.deepcopy(config)
        first = model.resolve(config)
        first["steps"][0]["evidence"].append("url")
        self.assertEqual(config, original)
        self.assertEqual(model.resolve(config)["steps"][0]["evidence"], ["stdout"])

    def test_repo_loading_and_default_category(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(model.read_repo_pipeline(root))
            self.assertIsNone(model.default_template_for(root))
            config_dir = root / ".github" / "autopr"
            config_dir.mkdir(parents=True)
            for category, expected in (("plugin", "plugin"), ("frontend", "site"),
                                       ("content", None)):
                (config_dir / "risk-tiers.yml").write_text("extends: " + category, encoding="utf-8")
                self.assertEqual(model.default_template_for(root), expected)
            (config_dir / "pipeline.yml").write_text("extends: sandbox", encoding="utf-8")
            self.assertEqual(model.read_repo_pipeline(root), {"extends": "sandbox"})


class PipelineCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_dir = self.root / ".github" / "autopr"
        self.config_dir.mkdir(parents=True)
        self.config = self.config_dir / "pipeline.yml"
        self.config.write_text("extends: sandbox\n", encoding="utf-8")

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(ROOT / "pipeline" / "cli.py"),
                               "show", "--repo", str(self.root), *args],
                              text=True, capture_output=True, cwd=self.root)

    def test_text_and_json(self):
        text = self.cli()
        self.assertEqual(text.returncode, 0, text.stderr)
        for value in ("post-merge", "impact", "ask: 你", "[release: prepare]"):
            self.assertIn(value, text.stdout)
        result = self.cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), model.resolve({"extends": "sandbox"}))

    def test_validation_exit_one_prints_all_problems(self):
        self.config.write_text("extends: sandbox\nwrong: true\nremove: [absent]\n", encoding="utf-8")
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("wrong", result.stderr)
        self.assertIn("absent", result.stderr)

    def test_file_and_argument_errors_exit_two(self):
        self.assertEqual(self.cli("--unknown").returncode, 2)
        self.config.unlink()
        self.config.mkdir()
        self.assertEqual(self.cli().returncode, 2)
        self.config.rmdir()
        result = subprocess.run([sys.executable, "-B", str(ROOT / "pipeline" / "cli.py"),
                                 "show", "--repo", str(self.root / "missing")], capture_output=True)
        self.assertEqual(result.returncode, 2)

    def test_yaml_error_and_unknown_category(self):
        self.config.write_text("extends: [\n", encoding="utf-8")
        self.assertEqual(self.cli().returncode, 1)
        self.config.unlink()
        self.assertEqual(self.cli().returncode, 1)
        (self.config_dir / "risk-tiers.yml").write_text("extends: plugin", encoding="utf-8")
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("没有 pipeline.yml，按类别 plugin 套模板", result.stdout)

    def test_empty_or_nonmapping_config_is_not_absence(self):
        for content in ("", "null", "[]"):
            with self.subTest(content=content):
                self.config.write_text(content, encoding="utf-8")
                result = self.cli()
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("没有 pipeline.yml", result.stdout + result.stderr)

    def test_encoding_error_exit_two(self):
        self.config.write_bytes(b"\xff")
        self.assertEqual(self.cli().returncode, 2)

    def test_show_does_not_execute_commands(self):
        marker = self.root / "executed"
        self.config.write_text("extends: sandbox\nadd:\n  - id: never-run\n"
                               "    at: post-merge\n    run: touch " + str(marker) +
                               "\n    by: ai\n", encoding="utf-8")
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("never-run", result.stdout)
        self.assertFalse(marker.exists())

    def test_default_template_notice_and_custom_directory(self):
        self.config.unlink()
        (self.config_dir / "risk-tiers.yml").write_text("extends: frontend", encoding="utf-8")
        result = self.cli("--templates", str(FIXTURES), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["template"], "site")
        self.assertIn("没有 pipeline.yml，按类别 frontend 套模板", result.stderr)


if __name__ == "__main__":
    unittest.main()
