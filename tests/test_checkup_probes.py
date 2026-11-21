"""Tests for the ported checkup modules: YAML subset, workflow analysis, lockfiles, gh adapter, check runner."""
import os
import unittest

from _checkup_util import FIXTURES, FakeTransport, good_github, make_repo, write_files  # noqa: F401
from checkup import ecosystem, entry, ghprobe, workflow
from checkup.proc import RunResult
from checkup.yamlsub import YamlSubsetError, load, loads

WF = os.path.join(FIXTURES, "workflows")
SHA = "b" * 40


class YamlSubsetTest(unittest.TestCase):
    def test_interface_example_parses(self):
        d = load(os.path.join(FIXTURES, "yaml", "interface.example.yml"))
        self.assertEqual(d["entries"]["check"]["run"], "/bin/bash scripts/validate.sh")
        self.assertEqual(d["entries"]["verify"]["timeout_minutes"], 20)

    def test_block_scalar_and_flow(self):
        d = loads("run: |\n  a\n  b\nlist: [1, two]\nm: {x: true}\n")
        self.assertEqual(d, {"run": "a\nb\n", "list": [1, "two"], "m": {"x": True}})

    def test_anchors_and_tabs_are_rejected(self):
        for text in ("a: &x 1\n", "a:\n\tb: 1\n", "a: 1\na: 2\n"):
            with self.assertRaises(YamlSubsetError):
                loads(text)

    def test_all_recorded_workflows_load(self):
        for f in os.listdir(WF):
            if f.endswith(".yml"):
                self.assertIsInstance(load(os.path.join(WF, f)), dict, f)


class WorkflowTest(unittest.TestCase):
    def wfs(self, *texts):
        return [("w%d.yml" % i, loads(t)) for i, t in enumerate(texts)]

    def test_triggers_forms(self):
        self.assertEqual(workflow.triggers(loads("on: push\n")), {"push"})
        self.assertEqual(workflow.triggers(loads("on: [push, pull_request]\n")), {"push", "pull_request"})
        self.assertEqual(workflow.triggers(loads("on:\n  pull_request:\n  push:\n")), {"pull_request", "push"})

    def test_inferred_check_prefers_tests_over_lint(self):
        wf = self.wfs("on: pull_request\njobs:\n  j:\n    steps:\n      - run: pnpm install --frozen-lockfile\n"
                      "      - run: pnpm lint\n      - run: pnpm typecheck\n      - run: pnpm test\n")
        self.assertEqual(workflow.inferred_check(wf), ("pnpm test", "w0.yml"))

    def test_inferred_check_ignores_non_pr_workflows_and_setup(self):
        wf = self.wfs("on: push\njobs:\n  j:\n    steps:\n      - run: make test\n",
                      "on: pull_request\njobs:\n  j:\n    steps:\n      - run: pip install pytest\n")
        self.assertEqual(workflow.inferred_check(wf), (None, None))

    def test_multi_line_run_uses_the_first_matching_line(self):
        wf = self.wfs("on: pull_request\njobs:\n  j:\n    steps:\n      - run: |\n          set -e\n"
                      "          bash scripts/validate.sh\n")
        self.assertEqual(workflow.inferred_check(wf)[0], "bash scripts/validate.sh")

    def test_unpinned_actions(self):
        wf = self.wfs("on: push\njobs:\n  j:\n    uses: o/r/.github/workflows/x.yml@main\n"
                      "  k:\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-node@%s\n"
                      "      - uses: ./local\n      - uses: docker://img:1\n" % SHA)
        bad = workflow.unpinned_actions(wf)
        self.assertEqual(sorted(bad), ["w0.yml: actions/checkout@v4", "w0.yml: docker://img:1",
                                       "w0.yml: o/r/.github/workflows/x.yml@main"])

    def test_dangerous_triggers(self):
        wf = self.wfs("on: pull_request_target\njobs:\n  j:\n    steps:\n      - uses: actions/checkout@%s\n"
                      "        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n" % SHA,
                      "on: pull_request_target\njobs:\n  j:\n    steps:\n      - run: echo hi\n",
                      "on: pull_request\njobs:\n  j:\n    steps:\n      - run: echo ${{ github.head_ref }}\n")
        bad = workflow.dangerous_triggers(wf)
        self.assertEqual(len(bad), 1)
        self.assertIn("w0.yml", bad[0])

    def test_recorded_workflows_have_no_dangerous_triggers(self):
        wfs = []
        for f in os.listdir(WF):
            if f.endswith(".yml"):
                wfs.append((f, load(os.path.join(WF, f))))
        self.assertEqual(workflow.dangerous_triggers(wfs), [])


class LockfileTest(unittest.TestCase):
    def repo(self, files):
        root = make_repo(self, files)
        return root, set(files)

    def test_node_with_lock_and_ci_ci(self):
        root, tracked = self.repo({"package.json": '{"dependencies": {"a": "1"}}', "package-lock.json": "{}",
                                   ".github/workflows/ci.yml": "on: pull_request\njobs:\n  j:\n    steps:\n      - run: npm ci\n"})
        self.assertEqual(ecosystem.lockfile_installed(root, ["package.json"], tracked)[0], "ok")

    def test_node_without_lock(self):
        root, tracked = self.repo({"package.json": '{"dependencies": {"a": "1"}}'})
        st, detail = ecosystem.lockfile_installed(root, ["package.json"], tracked)
        self.assertEqual(st, "missing")
        self.assertIn("没有锁文件", detail)

    def test_ci_installs_without_freezing(self):
        root, tracked = self.repo({"package.json": '{"dependencies": {"a": "1"}}', "package-lock.json": "{}",
                                   ".github/workflows/ci.yml": "on: pull_request\njobs:\n  j:\n    steps:\n      - run: npm install\n"})
        st, detail = ecosystem.lockfile_installed(root, ["package.json"], tracked)
        self.assertEqual(st, "missing")
        self.assertIn("没有按锁文件", detail)

    def test_no_dependencies_needs_no_lock(self):
        root, tracked = self.repo({"package.json": '{"name": "x"}'})
        self.assertEqual(ecosystem.lockfile_installed(root, ["package.json"], tracked)[0], "ok")

    def test_python_requirements_must_be_pinned(self):
        root, tracked = self.repo({"requirements.txt": "flask\n"})
        self.assertEqual(ecosystem.lockfile_installed(root, ["requirements.txt"], tracked)[0], "missing")
        root, tracked = self.repo({"requirements.txt": "flask==3.0.0\n"})
        self.assertEqual(ecosystem.lockfile_installed(root, ["requirements.txt"], tracked)[0], "ok")

    def test_broken_package_json_is_unknown(self):
        root, tracked = self.repo({"package.json": "{nope"})
        self.assertEqual(ecosystem.lockfile_installed(root, ["package.json"], tracked)[0], "unknown")


def fake_runner(code=0, out="", err="", timed_out=False):
    return lambda argv, t: RunResult(code, out, err, timed_out)


def raw(status_line, body="{}", headers=()):
    return "\n".join([status_line] + list(headers)) + "\n\n" + body


class LiveTransportTest(unittest.TestCase):
    def fetch(self, **kw):
        seen = []

        def runner(argv, t):
            seen.append(argv)
            return RunResult(**kw)
        return ghprobe.LiveTransport(runner=runner).fetch("repos/o/n"), seen

    def test_only_get_is_ever_requested(self):
        r, seen = self.fetch(code=0, stdout=raw("HTTP/2.0 200 OK", '{"a": 1}'), stderr="")
        self.assertTrue(r.ok)
        self.assertEqual(r.data, {"a": 1})
        self.assertEqual(seen, [["gh", "api", "-i", "--method", "GET", "repos/o/n"]])

    def test_classification(self):
        cases = [(dict(code=1, stdout=raw("HTTP/2.0 404 Not Found", '{"message": "Not Found"}'), stderr=""), "not_found"),
                 (dict(code=1, stdout=raw("HTTP/2.0 403 Forbidden", '{"message": "Resource not accessible"}'), stderr=""), "forbidden"),
                 (dict(code=1, stdout=raw("HTTP/2.0 403 Forbidden", "{}", ["X-Ratelimit-Remaining: 0"]), stderr=""), "rate_limited"),
                 (dict(code=1, stdout=raw("HTTP/2.0 502 Bad Gateway"), stderr=""), "server_error"),
                 (dict(code=4, stdout="", stderr="gh auth login"), "unauthenticated"),
                 (dict(code=1, stdout="", stderr="dial tcp: no such host"), "network"),
                 (dict(code=3, stdout="", stderr="executable not found: gh"), "gh_missing"),
                 (dict(code=3, stdout="", stderr="timeout after 30s", timed_out=True), "network")]
        for kw, want in cases:
            self.assertEqual(self.fetch(**kw)[0].status, want, kw)

    def test_env_errors_raise_and_paths_are_validated(self):
        gh = ghprobe.GitHub(FakeTransport({"repos/o/n": ghprobe.GhResponse("unauthenticated", message="no login")}))
        with self.assertRaises(ghprobe.EnvNotReady):
            gh.get("repos/o/n")
        for bad in ("repos/o/../x", "--method", "https://evil/x", "repos/o n"):
            with self.assertRaises(ValueError):
                gh.get(bad)


class RecordedGitHubTest(unittest.TestCase):
    """Real GitHub responses recorded with repo-readiness (read-only)."""

    def gh(self, name):
        return ghprobe.GitHub(ghprobe.ReplayTransport(os.path.join(FIXTURES, "github", name))), "haigeerlab/" + name

    def test_agent_relay_legacy_protection_has_no_pr_requirement(self):
        gh, name = self.gh("agent-relay")
        st, detail, ev = ghprobe.effective_branch_rules(gh, name, "main")
        self.assertEqual(st, "missing")
        self.assertIn("只能经 PR 合入", detail)
        self.assertIn("旧版分支保护：禁止删除分支、禁止强推", ev)

    def test_pwa_platform_ruleset_is_enough(self):
        gh, name = self.gh("pwa-platform")
        self.assertEqual(ghprobe.effective_branch_rules(gh, name, "main")[0], "ok")

    def test_recorded_ci_secret_and_permissions(self):
        for repo in ("agent-relay", "pwa-platform"):
            gh, name = self.gh(repo)
            self.assertEqual(ghprobe.main_ci_green(gh, name, "main")[0], "ok", repo)
            self.assertEqual(ghprobe.secret_scanning(gh, name, True)[0], "ok", repo)
            self.assertEqual(ghprobe.workflow_permissions(gh, name)[0], "ok", repo)


class EntryRunTest(unittest.TestCase):
    def test_pass_fail_and_tail(self):
        root = make_repo(self, {"a.txt": "x"})
        st, detail, lines = entry.run_check(root, "echo hello")
        self.assertEqual((st, lines), ("ok", ["hello"]))
        st, detail, lines = entry.run_check(root, "echo bad; exit 1")
        self.assertEqual(st, "missing")
        self.assertIn("退出码 1", detail)
        st, _, lines = entry.run_check(root, "i=0; while [ $i -lt 100 ]; do echo l$i; i=$((i+1)); done")
        self.assertEqual((st, len(lines), lines[-1]), ("ok", 40, "l99"))

    def test_timeout_is_unknown(self):
        root = make_repo(self, {"a.txt": "x"})
        st, detail, _ = entry.run_check(root, "sleep 5", timeout_s=1)
        self.assertEqual(st, "unknown")

    def test_runs_committed_head_in_a_clone_and_hides_tokens(self):
        root = make_repo(self, {"a.txt": "x"})
        write_files(root, {"b.txt": "uncommitted"})
        os.environ["GH_TOKEN"] = "secret"
        self.addCleanup(os.environ.pop, "GH_TOKEN", None)
        st, _, lines = entry.run_check(root, 'ls; echo "token=[$GH_TOKEN]"')
        self.assertEqual(st, "ok")
        self.assertIn("a.txt", lines)
        self.assertNotIn("b.txt", lines)
        self.assertIn("token=[]", lines)
        self.assertTrue(os.path.exists(os.path.join(root, "b.txt")))


if __name__ == "__main__":
    unittest.main()
