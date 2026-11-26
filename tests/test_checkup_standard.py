"""Plugin-standard cards and reminders in the checkup report (checkup/standard.py).

Run: python3 -B -m unittest discover -s tests -v
"""
import os
import json
import sys
import tempfile
import unittest
import subprocess
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from checkup import standard as st
from checkup.ghprobe import GhResponse

MIT = open(os.path.join(ROOT, "LICENSE"), encoding="utf-8").read()
CI = open(os.path.join(ROOT, "examples", "caller-ci-standard.yml"), encoding="utf-8").read()
DEPENDABOT = open(os.path.join(ROOT, ".github", "dependabot.yml"), encoding="utf-8").read()
IDS = ["规范" + n for n in ("A3", "B2", "B3", "B4", "C1", "C2", "C3", "C4", "C5", "C6",
                            "C9", "C10", "D1", "D2", "D3", "E1", "F2", "G1", "G2", "H1", "H2", "H4")]
TARGET = "b" * 40
PLUGIN = '{"name": "x", "version": "1.2.3"}'


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(text, bytes):
        with open(path, "wb") as fh:
            fh.write(text)
        return
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
    def run_on(self, files, gh=None, repo="o/r", target=None):
        with tempfile.TemporaryDirectory() as root:
            for rel, text in files.items():
                write(root, rel, text)
            write(root, "standard-target.json", json.dumps(target if target is not None else {
                "schema": "autopilot.standard-target/v1", "hub_target": TARGET}))
            return by_id(st.findings(root, gh, repo, "main", target_path=os.path.join(root, "standard-target.json")))

    def test_ids_do_not_clash_with_checkup_numbers(self):
        out = self.run_on({})
        self.assertEqual(list(out), IDS)
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

    def test_b2_release_entry(self):
        files = {"scripts/release.py": "", ".github/autopr/interface.yml": "entries:\n  release_prepare:\n    command: python3 scripts/release.py prepare\n"}
        self.assertEqual(self.run_on(files)["规范B2"]["status"], "ok")
        for absent in files:
            self.assertEqual(self.run_on({k: v for k, v in files.items() if k != absent})["规范B2"]["status"], "missing")
        files[".github/autopr/interface.yml"] = "entries: &unsupported {}"
        self.assertEqual(self.run_on(files)["规范B2"]["status"], "unknown")

    def test_b3_release_evidence(self):
        files = {"plugins/x/.claude-plugin/plugin.json": PLUGIN}
        evidence = {"docs/releases/v1.2.3-%s.json" % kind: "{}" for kind in ("source", "package", "claude", "codex")}
        self.assertEqual(self.run_on(dict(files, **evidence))["规范B3"]["status"], "ok")
        out = self.run_on(files)["规范B3"]
        self.assertEqual(out["status"], "missing")
        for path in evidence:
            self.assertIn(path, out["detail"])
        files["plugins/y/.claude-plugin/plugin.json"] = PLUGIN.replace('"x"', '"y"')
        self.assertEqual(self.run_on(files)["规范B3"]["status"], "unknown")

    def test_b3_non_plugin_repository(self):
        out = self.run_on({"README.md": "# Project"})["规范B3"]
        self.assertEqual(out["status"], "ok")
        self.assertIn("不是插件仓库", out["detail"])

    def test_b3_inconsistent_plugin_versions_are_unknown(self):
        files = {"plugins/x/.claude-plugin/plugin.json": PLUGIN,
                 "plugins/x/.codex-plugin/plugin.json": PLUGIN.replace("1.2.3", "1.2.4")}
        self.assertEqual(self.run_on(files)["规范B3"]["status"], "unknown")

    def test_b4_changelog(self):
        self.assertEqual(self.run_on({"CHANGELOG.md": "# Changelog\n## [Unreleased]\n"})["规范B4"]["status"], "ok")
        for text in ("", " ## [Unreleased]", "### [Unreleased]"):
            self.assertEqual(self.run_on({"CHANGELOG.md": text})["规范B4"]["status"], "missing")

    def test_c2_validate_quick(self):
        self.assertEqual(self.run_on({"scripts/validate.sh": 'case "$1" in --quick) ;; esac'})["规范C2"]["status"], "ok")
        self.assertEqual(self.run_on({"scripts/validate.sh": "true"})["规范C2"]["status"], "missing")

    def test_c3_git_maintenance(self):
        files = {"tests/test_git.py": 'run(["git", "init"])\n', "tests/helpers.py": '"maintenance.auto"\n"gc.auto"\n'}
        self.assertEqual(self.run_on(files)["规范C3"]["status"], "ok")
        del files["tests/helpers.py"]
        self.assertEqual(self.run_on(files)["规范C3"]["status"], "missing")
        self.assertEqual(self.run_on({"test_git.py": "git init"})["规范C3"]["status"], "missing")
        out = self.run_on({"src/app.py": "git init"})["规范C3"]
        self.assertEqual(out["status"], "ok")
        self.assertIn("测试没有创建 git 仓库", out["detail"])

    def test_c3_skips_binary_test_fixtures(self):
        files = {"tests/test_git.py": 'run(["git", "init"])\n"maintenance.auto"\n"gc.auto"\n',
                 "tests/fixtures/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe"}
        self.assertEqual(self.run_on(files)["规范C3"]["status"], "ok")
        files["tests/fixtures/large.bin"] = b"\x00" + b"x" * (st.MAX_FILE_BYTES + 10)
        self.assertEqual(self.run_on(files)["规范C3"]["status"], "ok")
        for name, data in (("latin1.txt", b"caf\xe9\n"), ("utf16.py", "git init\n".encode("utf-16"))):
            with self.subTest(name=name):
                self.assertEqual(self.run_on(dict(files, **{"tests/fixtures/" + name: data}))["规范C3"]["status"], "unknown")

    def test_c4_python_matrix(self):
        workflow = ('name: CI\njobs:\n  test:\n    runs-on: ubuntu-latest\n'
                    '    strategy:\n      matrix:\n        python: ["3.9", "3.14"]\n'
                    '    steps:\n      - uses: actions/setup-python@v5\n'
                    '        with:\n          python-version: ${{ matrix.python }}\n')
        self.assertEqual(self.run_on({"app.py": "", ".github/workflows/ci.yml": workflow})["规范C4"]["status"], "ok")
        self.assertEqual(self.run_on({"app.py": ""})["规范C4"]["status"], "unknown")
        self.assertEqual(self.run_on({})["规范C4"]["status"], "ok")

    def test_c4_hub_ci_system_python(self):
        with open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8") as fh:
            workflow = fh.read()
        out = self.run_on({"app.py": "", ".github/workflows/ci.yml": workflow})["规范C4"]
        self.assertEqual(out["status"], "ok", out["detail"])

    def test_c4_caller_ci_system_python(self):
        for workflow in (CI, "\n".join(line for line in CI.splitlines() if not line.lstrip().startswith("#"))):
            with self.subTest(with_comments=workflow == CI):
                out = self.run_on({"app.py": "", ".github/workflows/ci.yml": workflow})["规范C4"]
                self.assertEqual(out["status"], "ok", out["detail"])

    def test_c5_bash_compatibility(self):
        self.assertEqual(self.run_on({"scripts/check.sh": "while read -r line; do echo yes; done"})["规范C5"]["status"], "ok")
        for syntax in ("mapfile -t rows", "readarray rows", "declare -A lookup", "echo ${name,,}", "echo ${name^^}"):
            out = self.run_on({"scripts/check.sh": "#!/bin/bash\n" + syntax})["规范C5"]
            self.assertEqual(out["status"], "missing")
            self.assertIn("scripts/check.sh:2", out["detail"])
        self.assertEqual(self.run_on({".github/workflows/ci.yml": "name: CI\nrun: mapfile -t rows\n"})["规范C5"]["status"], "ok")

    def test_c6_action_pins(self):
        files = {".github/workflows/ci.yml": 'jobs:\n  test:\n    steps:\n      - uses: actions/checkout@' + TARGET + '\n      - uses: ./local\n      - uses: docker://alpine:3\n'}
        self.assertEqual(self.run_on(files)["规范C6"]["status"], "ok")
        for ref in ("v4", "abc123", "B" * 40, ""):
            files[".github/workflows/ci.yml"] = 'jobs:\n  test:\n    steps:\n      - uses: "actions/checkout@%s" # version\n' % ref
            out = self.run_on(files)["规范C6"]
            self.assertEqual(out["status"], "missing")
            self.assertIn("actions/checkout@" + ref, out["detail"])
            self.assertIn("ci.yml", out["detail"])

    def test_d1_plugin_layout(self):
        self.assertEqual(self.run_on({"plugins/x/.claude-plugin/plugin.json": PLUGIN})["规范D1"]["status"], "ok")
        files = {".claude-plugin/plugin.json": PLUGIN}
        self.assertEqual(self.run_on(files)["规范D1"]["status"], "missing")
        out = self.run_on(files, repo="haigeerlab/module-crew")["规范D1"]
        self.assertEqual(out["status"], "ok")
        self.assertIn("规范例外", out["detail"])
        self.assertIn("不是插件仓库", self.run_on({})["规范D1"]["detail"])

    def test_d2_both_manifests(self):
        files = {"plugins/x/.claude-plugin/plugin.json": PLUGIN, "plugins/x/.codex-plugin/plugin.json": PLUGIN}
        self.assertEqual(self.run_on(files)["规范D2"]["status"], "ok")
        del files["plugins/x/.codex-plugin/plugin.json"]
        self.assertEqual(self.run_on(files)["规范D2"]["status"], "missing")
        self.assertEqual(self.run_on({})["规范D2"]["status"], "ok")

    def test_d3_test_names(self):
        files = {"tests/" + name: "" for name in ("test_good.py", "__init__.py", "conftest.py", "_helper.py")}
        self.assertEqual(self.run_on(files)["规范D3"]["status"], "ok")
        files["tests/nested/bad_test.py"] = "def test_example():\n    pass\n"
        out = self.run_on(files)["规范D3"]
        self.assertEqual(out["status"], "missing")
        self.assertIn("tests/nested/bad_test.py", out["detail"])

    def test_d3_support_module_without_tests(self):
        files = {"tests/vault_support.py": 'import unittest\n\ndef make_vault():\n    return {}\n'}
        self.assertEqual(self.run_on(files)["规范D3"]["status"], "ok")

    def test_d3_testcase_support_modules_without_test_methods(self):
        for name, class_name in (("vault_support.py", "VaultTestCase"), ("helpers.py", "RepoCase")):
            with self.subTest(name=name):
                files = {"tests/" + name: "import unittest\n\nclass %s(unittest.TestCase):\n    pass\n" % class_name}
                self.assertEqual(self.run_on(files)["规范D3"]["status"], "ok")

    def test_d3_misnamed_class_with_test_method(self):
        for bases in ("", "(unittest.TestCase)"):
            with self.subTest(bases=bases):
                files = {"tests/vault_support.py": "import unittest\n\nclass TestVault%s:\n    def test_open(self):\n        pass\n" % bases}
                out = self.run_on(files)["规范D3"]
                self.assertEqual(out["status"], "missing")
                self.assertIn("tests/vault_support.py", out["detail"])

    def test_e1_interface_version(self):
        for key in ("interface", "version"):
            files = {"plugins/x/.claude-plugin/plugin.json": PLUGIN, "plugins/x/interface.json": json.dumps({key: "2.10"})}
            self.assertEqual(self.run_on(files)["规范E1"]["status"], "ok")
            for bad in ('{"version":"1.2.3"}', '{"interface":1.2}', "[]", "{", "{}"):
                files["plugins/x/interface.json"] = bad
                self.assertEqual(self.run_on(files)["规范E1"]["status"], "missing")
        self.assertIn("没有对外接口文件", self.run_on({})["规范E1"]["detail"])

    def test_e1_interface_takes_precedence_over_plugin_version(self):
        files = {"plugins/module-crew/.claude-plugin/plugin.json": PLUGIN,
                 "plugins/module-crew/interface.json": '{"interface": "1.1", "plugin": "module-crew", "version": "0.24.2"}'}
        self.assertEqual(self.run_on(files)["规范E1"]["status"], "ok")
        for interface in (None, "1.1.0"):
            files["plugins/module-crew/interface.json"] = json.dumps({"interface": interface, "version": "1.1"})
            self.assertEqual(self.run_on(files)["规范E1"]["status"], "missing")

    def test_f2_pr_template(self):
        for path in (".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE.md", ".github/PULL_REQUEST_TEMPLATE/bug.md"):
            self.assertEqual(self.run_on({path: "template"})["规范F2"]["status"], "ok")
        self.assertEqual(self.run_on({})["规范F2"]["status"], "missing")

    def test_g1_required_docs(self):
        files = {name: "" for name in ("README.md", "README.en.md", "CHANGELOG.md", "SECURITY.md", "LICENSE", "CLAUDE.md", "AGENTS.md")}
        self.assertEqual(self.run_on(files)["规范G1"]["status"], "ok")
        del files["README.en.md"]
        out = self.run_on(files)["规范G1"]
        self.assertEqual(out["status"], "missing")
        self.assertIn("README.en.md", out["detail"])

    def test_g2_shared_sections(self):
        shared = "<!-- shared:begin -->\nidentical\n<!-- shared:end -->"
        files = {"CLAUDE.md": "Claude\n" + shared, "AGENTS.md": shared + "\nCodex"}
        self.assertEqual(self.run_on(files)["规范G2"]["status"], "ok")
        for bad in (shared.replace("identical", "different"), "no markers", "<!-- shared:begin -->only"):
            files["AGENTS.md"] = bad
            self.assertEqual(self.run_on(files)["规范G2"]["status"], "missing")
        self.assertEqual(self.run_on({})["规范G2"]["status"], "unknown")
        files = {"CLAUDE.md": shared, "AGENTS.md": shared.replace("\n", "\r\n")}
        self.assertEqual(self.run_on(files)["规范G2"]["status"], "missing")

    def test_h1_hub_baseline(self):
        content = "jobs:\n  hub:\n    uses: haigeerlab/autopilot-hub/.github/workflows/rules.yml@%s\n    with:\n      hub_ref: '%s'\n" % (TARGET, TARGET)
        files = {".github/workflows/ci.yml": content}
        self.assertEqual(self.run_on(files)["规范H1"]["status"], "ok")
        files[".github/workflows/ci.yml"] = content.replace(TARGET, "a" * 40)
        out = self.run_on(files)["规范H1"]
        self.assertEqual(out["status"], "missing")
        self.assertIn("hub_ref", out["detail"])
        self.assertIn("autopilot-hub", out["detail"])
        self.assertIn("没有接入 hub workflow", self.run_on({})["规范H1"]["detail"])
        self.assertEqual(self.run_on(files, target={})["规范H1"]["status"], "unknown")
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(st.h1(root, target_path=os.path.join(root, "absent.json"))["status"], "unknown")

    def test_h1_ignores_hub_ref_input_definition(self):
        content = ("on:\n  workflow_call:\n    inputs:\n      hub_ref:\n"
                   "        description: Hub baseline\n        required: true\n        type: string\n"
                   "jobs:\n  hub:\n    uses: haigeerlab/autopilot-hub/.github/workflows/rules.yml@%s\n"
                   "    with:\n      hub_ref: '%s'\n") % (TARGET, TARGET)
        out = self.run_on({".github/workflows/reusable.yml": content})["规范H1"]
        self.assertEqual(out["status"], "ok")

    def test_h1_checks_only_string_hub_ref_under_with(self):
        content = ("env:\n  hub_ref: main\njobs:\n  hub:\n    with:\n"
                   "      hub_ref:\n        description: Not a reference\n        type: string\n")
        out = self.run_on({".github/workflows/reusable.yml": content})["规范H1"]
        self.assertEqual(out["status"], "ok")
        self.assertIn("没有接入 hub workflow", out["detail"])

    def test_h1_hub_repository_is_not_applicable(self):
        files = {".github/workflows/ci.yml": "jobs:\n  hub:\n    uses: haigeerlab/autopilot-hub/.github/workflows/rules.yml@main\n"}
        out = self.run_on(files, repo="haigeerlab/autopilot-hub", target={})["规范H1"]
        self.assertEqual(out["status"], "ok")
        self.assertIn("hub 本身，不适用", out["detail"])

    def test_h2_risk_config(self):
        self.assertEqual(self.run_on({".github/autopr/risk-tiers.yml": ""})["规范H2"]["status"], "ok")
        self.assertEqual(self.run_on({".github/autopr/risk-tiers.json": "{}"})["规范H2"]["status"], "missing")
        self.assertIn("没有风险配置", self.run_on({})["规范H2"]["detail"])

    def test_h4_rules_object(self):
        self.assertEqual(self.run_on({".github/autopr/rules.json": "{}"})["规范H4"]["status"], "ok")
        self.assertEqual(self.run_on({})["规范H4"]["status"], "ok")
        for bad in ("[]", "null", "{broken", '"text"'):
            self.assertEqual(self.run_on({".github/autopr/rules.json": bad})["规范H4"]["status"], "missing")

    def test_skip_generated_and_local_directories(self):
        files = {}
        for directory in st.SKIP_DIRS:
            files[directory + "/bad.sh"] = "mapfile rows"
            files[directory + "/test_bad.py"] = "git init"
            files[directory + "/.claude-plugin/plugin.json"] = PLUGIN
        out = self.run_on(files)
        for fid in ("规范C3", "规范C4", "规范C5", "规范D1", "规范D2"):
            self.assertEqual(out[fid]["status"], "ok")

    def test_c3_settings_in_ci_or_validate_and_shell_tests(self):
        for path, settings in (
            (".github/workflows/ci.yml", "env:\n  GIT_CONFIG_KEY_0: maintenance.auto\n  GIT_CONFIG_KEY_1: gc.auto\n"),
            ("scripts/validate.sh", "git -c maintenance.auto=false -c gc.auto=0 init"),
            ("scripts/validate.sh", "git config maintenance.auto false\ngit config gc.auto 0"),
        ):
            with self.subTest(path=path, settings=settings):
                files = {"tests/smoke.sh": "git init", path: settings}
                self.assertEqual(self.run_on(files)["规范C3"]["status"], "ok")
        self.assertEqual(self.run_on({"tests/smoke.sh": "git init"})["规范C3"]["status"], "missing")

    def test_c4_rejects_incidental_versions_and_follows_local_workflows(self):
        incidental = 'name: CI\n# Python 3.9 macos /usr/bin/python3 3.14\nversions: ["3.9", "3.14"]\n'
        out = self.run_on({"app.py": "", ".github/workflows/ci.yml": incidental})["规范C4"]
        self.assertEqual(out["status"], "unknown")
        files = {"app.py": "", ".github/workflows/ci.yml": 'name: CI\njobs:\n  call:\n    uses: ./.github/workflows/python.yml\n',
                 ".github/workflows/python.yml": ('jobs:\n  test:\n    runs-on: macos-latest\n'
                     '    steps:\n      - run: /usr/bin/python3 -m unittest\n'
                     '      - uses: actions/setup-python@v5\n        with:\n          python-version: "3.14"\n')}
        self.assertEqual(self.run_on(files)["规范C4"]["status"], "ok")
        files[".github/workflows/python.yml"] = files[".github/workflows/python.yml"].replace("macos-latest", "ubuntu-latest")
        self.assertNotEqual(self.run_on(files)["规范C4"]["status"], "ok")
        missing = self.run_on({"app.py": ""})["规范C4"]
        self.assertEqual(missing["detail"], "没有名为 CI 的 workflow（见规范C1）")

    def test_d3_nested_async_tests_and_help(self):
        out = self.run_on({"plugins/x/tests/helpers.py": "async def test_open():\n    pass\n"})["规范D3"]
        self.assertEqual(out["status"], "missing")
        self.assertIn("plugins/x/tests/helpers.py", out["detail"])
        self.assertNotIn("辅助文件以 _ 开头", out["fix"]["how"])

    def test_f2_additional_template_locations(self):
        for path in ("pull_request_template.md", "Pull_Request_Template.MD", "docs/PULL_REQUEST_TEMPLATE.md"):
            with self.subTest(path=path):
                self.assertEqual(self.run_on({path: "template"})["规范F2"]["status"], "ok")

    def test_g2_all_shared_sections(self):
        shared = "<!-- shared:begin -->one<!-- shared:end -->"
        second = "<!-- shared:begin -->two<!-- shared:end -->"
        for other in (shared, shared + second.replace("two", "different")):
            files = {"CLAUDE.md": shared + second, "AGENTS.md": other}
            self.assertEqual(self.run_on(files)["规范G2"]["status"], "missing")
        self.assertEqual(self.run_on({"CLAUDE.md": shared + second, "AGENTS.md": shared + second})["规范G2"]["status"], "ok")

    def test_h1_checkout_repository_refs_and_expressions(self):
        checkout = ('jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v4\n'
                    '        with:\n          repository: haigeerlab/autopilot-hub\n          ref: %s\n')
        for ref, expected in ((TARGET, "ok"), ("a" * 40, "missing"), ("${{ inputs.hub_ref }}", "ok")):
            self.assertEqual(self.run_on({".github/workflows/ci.yml": checkout % ref})["规范H1"]["status"], expected)
        caller = 'jobs:\n  call:\n    with:\n      hub_ref: ${{ inputs.hub_ref }}\n'
        self.assertEqual(self.run_on({".github/workflows/ci.yml": caller})["规范H1"]["status"], "ok")
        unrelated = (checkout % ("a" * 40)).replace("repository: haigeerlab/autopilot-hub", "repository: other/repo")
        self.assertEqual(self.run_on({".github/workflows/ci.yml": unrelated})["规范H1"]["status"], "ok")

    def test_h4_accepts_utf8_bom(self):
        self.assertEqual(self.run_on({".github/autopr/rules.json": '\ufeff{"enabled": true}'})["规范H4"]["status"], "ok")

    def test_deep_json_and_yaml_do_not_abort_findings(self):
        deep_json = '[' * 2000 + '0' + ']' * 2000
        files = {"LICENSE": MIT, "plugins/x/.claude-plugin/plugin.json": deep_json,
                 "plugins/x/interface.json": deep_json, ".github/autopr/rules.json": deep_json,
                 ".github/dependabot.yml": 'updates: ' + deep_json,
                 ".github/autopr/interface.yml": 'entries: ' + deep_json,
                 ".github/workflows/ci.yml": 'name: CI\njobs: ' + deep_json}
        out = self.run_on(files)
        self.assertEqual(list(out), IDS)
        self.assertEqual(out["规范B3"]["status"], "unknown")
        self.assertNotEqual(out["规范C6"]["status"], "ok")
        with tempfile.TemporaryDirectory() as root:
            write(root, "target.json", deep_json)
            self.assertEqual(st.h1(root, os.path.join(root, "target.json"))["status"], "unknown")

    def test_findings_isolates_unexpected_item_errors(self):
        with patch.object(st, "b2", side_effect=RuntimeError("unexpected")):
            out = self.run_on({})
        self.assertEqual(list(out), IDS)
        self.assertEqual(out["规范B2"]["status"], "unknown")
        self.assertEqual(out["规范B2"]["detail"], "体检项出错：RuntimeError")
        self.assertEqual(out["规范H4"]["status"], "ok")

    def test_special_and_large_files_are_not_opened(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, "outside.py", 'git init\nmaintenance.auto\ngc.auto')
            os.makedirs(os.path.join(root, "tests"))
            os.symlink(os.path.join(root, "outside.py"), os.path.join(root, "tests", "linked.py"))
            os.mkfifo(os.path.join(root, "tests", "pipe.sh"))
            write(root, "tests/large.py", 'x' * (1024 * 1024 + 1))
            write(root, "target.json", json.dumps({"hub_target": TARGET}))
            real_open = os.open
            def checked_open(path, *args, **kwargs):
                self.assertNotIn(os.path.basename(path), ("linked.py", "pipe.sh", "large.py"))
                return real_open(path, *args, **kwargs)
            with patch.object(st.os, "open", side_effect=checked_open):
                out = by_id(st.findings(root, None, "o/r", "main", os.path.join(root, "target.json")))
            self.assertEqual(out["规范C3"]["status"], "unknown")
            self.assertIsNone(st._read(root, "tests/linked.py"))
            self.assertIsNone(st._read(root, "tests/large.py"))
            os.symlink(os.path.join(root, "tests"), os.path.join(root, "linked-dir"))
            self.assertIsNone(st._read(root, "linked-dir/large.py"))

    def test_git_scan_ignores_untracked_and_ignored_content(self):
        with tempfile.TemporaryDirectory() as root:
            subprocess.run(["git", "init", "-q", root], check=True)
            write(root, ".gitignore", ".agent/\n")
            write(root, "README.md", "tracked")
            subprocess.run(["git", "-C", root, "add", ".gitignore", "README.md"], check=True)
            for prefix in (".agent/", "untracked/"):
                write(root, prefix + ".claude-plugin/plugin.json", PLUGIN)
                write(root, prefix + "tests/bad.py", "def test_x(): pass\ngit init")
                write(root, prefix + "bad.sh", "mapfile rows")
            out = by_id(st.findings(root, None, "o/r", "main"))
            for fid in ("规范D1", "规范D2", "规范B3", "规范D3", "规范C3", "规范C4", "规范C5"):
                self.assertEqual(out[fid]["status"], "ok", fid)
            self.assertEqual(set(st._files(root)), {".gitignore", "README.md"})

    def test_git_scan_failure_and_non_utf8_fall_back(self):
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(os.path.join(root, ".git"))
            write(root, "tests/test_repo.py", "git init")
            for response in (subprocess.CompletedProcess([], 1, b'', b'failed'),
                             subprocess.CompletedProcess([], 0, b'bad\xff.py\0', b'')):
                with patch.object(st.subprocess, "run", return_value=response):
                    self.assertEqual(st.c3(root)["status"], "missing")

    def test_d1_remote_non_utf8_and_explicit_repo_precedence(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, ".claude-plugin/plugin.json", PLUGIN)
            real_run = subprocess.run
            def run_remote(args, **kwargs):
                if "remote" in args:
                    return real_run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'bad\\xff github.com:haigeerlab/module-crew.git (fetch)')"], **kwargs)
                return real_run(args, **kwargs)
            with patch.object(st.subprocess, "run", side_effect=run_remote):
                self.assertEqual(st.d1(root)["status"], "ok")
                self.assertEqual(st.d1(root, "other/repo")["status"], "missing")

    def test_c4_explicit_versions_missing_calls_and_cycles(self):
        ci = ('name: CI\njobs:\n  old:\n    steps:\n      - with:\n          python-version: "3.9"\n'
              '  new:\n    steps:\n      - with:\n          python-version: "3.14"\n')
        self.assertEqual(self.run_on({"app.py": "", ".github/workflows/ci.yml": ci})["规范C4"]["status"], "ok")
        ci = 'name: CI\njobs:\n  call:\n    uses: ./.github/workflows/other.yml\n'
        files = {"app.py": "", ".github/workflows/ci.yml": ci}
        self.assertEqual(self.run_on(files)["规范C4"]["status"], "unknown")
        files[".github/workflows/other.yml"] = 'jobs:\n  call:\n    uses: ./.github/workflows/ci.yml\n'
        self.assertEqual(self.run_on(files)["规范C4"]["status"], "unknown")

    def test_b3_unsafe_manifest_does_not_open_or_block(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "plugins/x/.claude-plugin"))
            path = os.path.join(root, "plugins/x/.claude-plugin/plugin.json")
            os.mkfifo(path)
            self.assertEqual(st.b3(root)["status"], "unknown")
            os.unlink(path)
            write(root, "real.json", PLUGIN)
            os.symlink(os.path.join(root, "real.json"), path)
            self.assertEqual(st.b3(root)["status"], "unknown")

    def test_git_tracked_plugin_ignores_other_version_declarations(self):
        with tempfile.TemporaryDirectory() as root:
            subprocess.run(["git", "init", "-q", root], check=True)
            write(root, ".gitignore", ".agent/\n")
            for manifest in st.MANIFESTS:
                write(root, "plugins/x/" + manifest, PLUGIN)
            for kind in ("source", "package", "claude", "codex"):
                write(root, "docs/releases/v1.2.3-%s.json" % kind, "{}")
            subprocess.run(["git", "-C", root, "add", "."], check=True)
            write(root, ".agent/.claude-plugin/plugin.json", PLUGIN.replace("1.2.3", "9.9.9"))
            self.assertEqual(st.b3(root)["status"], "ok")
            self.assertEqual(st.d2(root)["status"], "ok")

    def test_f2_empty_template_directory(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".github/PULL_REQUEST_TEMPLATE"))
            self.assertEqual(st.f2(root)["status"], "ok")

    def test_unknown_inputs_and_read_only_scan(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, "tests/test_git.py", 'run(["git", "init"])')
            with open(os.path.join(root, "tests", "helper.py"), "wb") as fh:
                fh.write(b"\xff")
            self.assertEqual(st.c3(root)["status"], "unknown")
            write(root, ".github/workflows/ci.yml", "name: CI\nunsupported: &anchor {}\n")
            self.assertEqual(st.c6(root)["status"], "unknown")
            write(root, "standard-target.json", json.dumps({"hub_target": TARGET}))
            self.assertEqual(st.h1(root, os.path.join(root, "standard-target.json"))["status"], "unknown")
            before = {}
            for directory, _, names in os.walk(root):
                for name in names:
                    path = os.path.join(directory, name)
                    with open(path, "rb") as fh:
                        before[path] = fh.read()
            st.findings(root, None, "o/r", "main", target_path=os.path.join(root, "standard-target.json"))
            after = {}
            for directory, _, names in os.walk(root):
                for name in names:
                    path = os.path.join(directory, name)
                    with open(path, "rb") as fh:
                        after[path] = fh.read()
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
