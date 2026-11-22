"""Batch integration tests use local files, fake subprocesses and fake GitHub I/O."""
import base64
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from orchestrator import batch, cli, flow
from test_orchestrator import BOT, BOT_USER, T0, report

REPO = "haigeerlab/autopilot-sandbox"
HEAD = "a" * 40


class AcceptancePathsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.common = self.root / "common" / "spec-guard" / "acceptance"
        self.legacy = self.root / "legacy" / "acceptance"
        self.common.mkdir(parents=True)
        self.legacy.mkdir(parents=True)
        self.io = cli.LocalIO("secret", None)
        self.checkout = mock.patch.object(self.io, "checkout_of", return_value=str(self.root))
        self.checkout.start()
        self.addCleanup(self.checkout.stop)
        patch = mock.patch.object(cli.subprocess, "run", side_effect=self.git)
        patch.start()
        self.addCleanup(patch.stop)

    def git(self, args, **kwargs):
        if "--git-common-dir" in args:
            output = "common\n"
        else:
            self.assertEqual(args[-2:], ["--git-path", "spec-guard/acceptance"])
            output = str(self.legacy) + "\n"
        return subprocess.CompletedProcess(args, 0, output, "")

    def write(self, folder, module, sha=HEAD):
        (folder / (module + ".json")).write_text(json.dumps({"module": module, "sha": sha}), encoding="utf-8")

    def test_common_record_wins_and_module_results_are_deduplicated(self):
        self.write(self.common, "m")
        self.write(self.legacy, "m", "b" * 40)
        self.write(self.legacy, "old")
        self.assertEqual(self.io.acceptance(REPO, "m")["record"]["sha"], HEAD)
        self.assertEqual(self.io.find_modules(REPO, HEAD), ["m", "old"])

    def test_new_record_for_other_head_shadows_legacy(self):
        self.write(self.common, "m", "b" * 40)
        self.write(self.legacy, "m")
        self.assertEqual(self.io.find_modules(REPO, HEAD), [])

    def test_malformed_new_record_does_not_resurrect_old_approval(self):
        (self.common / "m.json").write_text("broken", encoding="utf-8")
        self.write(self.legacy, "m")
        self.assertIsNone(self.io.acceptance(REPO, "m"))
        self.assertEqual(self.io.find_modules(REPO, HEAD), [])

    def test_old_location_is_still_readable(self):
        self.write(self.legacy, "m")
        self.assertEqual(self.io.find_modules(REPO, HEAD), ["m"])
        self.assertEqual(self.io.acceptance(REPO, "m")["record"]["sha"], HEAD)


def entry(module="m", head=HEAD, auto=True):
    return {"approval": 123, "module": module, "moduleCommit": "b" * 40,
            "mergeCommit": head, "tree": "c" * 40, "auto": auto, "release": None,
            "check": {"key": "c" * 40 + ".full", "result": "pass", "by": "merge-run",
                      "nonAuthor": True, "command": "make test", "exitCode": 0,
                      "startedAt": T0, "seconds": 812}}


def first_stage(*merges):
    return {"schemaVersion": 1, "repo": REPO, "baseBranch": "main", "integrationBranch": "main",
            "head": HEAD, "pushedUpTo": None, "merges": list(merges or [entry()])}


def final_report():
    value = report()
    value["scope"]["baseline"] = {"to": HEAD}
    return value


def pull(item, number=7, state="open", merged=False):
    return {"number": number, "state": state, "merged_at": T0 if merged else None,
            "body": flow.block("autopilot-batch", {k: item[k] for k in ("module", "mergeCommit", "approval")}),
            "user": BOT_USER, "performed_via_github_app": {"slug": BOT["app_slug"]},
            "created_at": T0, "updated_at": T0,
            "head": {"ref": "autopilot/batch-" + item["module"], "sha": item["mergeCommit"],
                     "repo": {"full_name": REPO}}, "base": {"ref": "main"}}


class FakeBatchIO:
    def __init__(self, data=None):
        self.data = data or first_stage()
        self.calls, self.pushes, self.comments, self.prs, self.events = [], [], [], [], []
        self.ancestors = set()
        self.post_merge_status = "ahead"
        self.files = {}
        self.route = {"spec": None, "plan": None}
        self.review = final_report()
        self.reviews, self.saved = [], []
        self.mark_result = {"exit": 0, "output": "ok"}
        self.cleaned = False
        self.accepted = False
        self.evidence = {"checks": [{"cmd": "make test", "exit": 0,
                                    "output_sha256": "e" * 64, "tail": [str(i) for i in range(20)]}]}

    def bot_identity(self):
        return BOT

    def first_stage(self, repo):
        return json.dumps(self.data)

    def api(self, path, method="GET", body=None):
        self.calls.append((method, path, body))
        if method == "GET" and "/commits/" in path:
            return {"sha": "f" * 40}
        if method == "GET" and "/compare/" in path:
            if path.endswith("..." + self.data["baseBranch"].replace("/", "%2F")):
                return {"status": self.post_merge_status}
            sha = path.split("/compare/", 1)[1].split("...", 1)[0]
            return {"status": "ahead" if sha in self.ancestors else "diverged"}
        if method == "GET" and "/pulls?" in path:
            return self.prs
        if method == "POST" and path.endswith("/pulls"):
            value = pull(entry())
            value.update(body)
            return value
        if method == "POST" and path.endswith("/comments"):
            self.comments.append(body["body"])
        return {}

    def batch_pr(self, repo, number, pr=None):
        return copy.deepcopy(next(p for p in self.prs if p["number"] == number))

    def push_batch(self, repo, head, branch):
        self.pushes.append((repo, head, branch))

    @contextlib.contextmanager
    def acceptance_worktree(self, repo, head):
        try:
            yield "/worktree"
        finally:
            self.cleaned = True

    def acceptance_map(self, root):
        return {"modules": [{"id": "m", "codexRoute": self.route}]}

    def read_bytes(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def module_start(self, root, module):
        return "d" * 40

    def local_evidence(self, repo, head):
        return self.evidence

    def review_acceptance(self, root, base, head, module, task):
        self.reviews.append((root, base, head, module, task))
        return self.review

    def save_acceptance_review(self, repo, module, head, data):
        self.saved.append(data)
        return "/saved/report.json"

    def mark_acceptance(self, root, module, path):
        self.marked = (root, module, path)
        self.accepted = self.mark_result["exit"] == 0
        return self.mark_result

    def shared_acceptance(self, root, module):
        return {"sha": HEAD} if self.accepted else None

    def acceptance(self, repo, module):
        text = json.dumps(self.review)
        return {"record": {"module": module, "marker": "codex-approved", "sha": HEAD,
                           "report_sha256": hashlib.sha256(text.encode()).hexdigest()}, "report_text": text}

    def ledger(self, event):
        self.events.append(event)

    def now(self):
        return T0


class BatchTest(unittest.TestCase):
    def run_batch(self, io, results=None, dry_run=False):
        results = results or [{"result": "merged", "step": "merge", "reason": "ok", "notes": []}]
        with mock.patch.object(flow, "run", side_effect=results) as run:
            result = batch.run(io, REPO, dry_run=dry_run)
        return result, run

    def test_schema_and_repo_validation(self):
        for change in ({"schemaVersion": 2}, {"schemaVersion": True}, {"repo": "wrong/repo"}, {"merges": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                batch.parse_first_stage(json.dumps(dict(first_stage(), **change)), REPO)
        with self.assertRaises(ValueError):
            batch.parse_first_stage("not json", REPO)

    def test_candidates_skip_manual_remote_ancestors_and_merged_pr(self):
        merges = [entry("manual", auto=False), entry("remote", "1" * 40), entry("done", "2" * 40), entry()]
        io = FakeBatchIO(first_stage(*merges))
        io.ancestors.add("1" * 40)
        io.prs.append(pull(merges[2], state="closed", merged=True))
        result, run = self.run_batch(io)
        self.assertEqual(result["code"], 0)
        self.assertEqual(io.pushes, [(REPO, HEAD, "autopilot/batch-m")])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs, {"merge_method": "merge"})
        created = next(body for method, path, body in io.calls if method == "POST" and path.endswith("/pulls"))
        self.assertEqual(created["title"], "autopilot：合入模块 m（module-crew 合并第一段 C123）")
        self.assertEqual(created["base"], "main")
        self.assertEqual(created["head"], "autopilot/batch-m")
        self.assertEqual(batch.batch_payload(dict(pull(entry()), body=created["body"]), BOT),
                         {"module": "m", "mergeCommit": HEAD, "approval": 123})
        self.assertIn("autopilot:authored", str(io.calls))
        for text in (HEAD, "b" * 40, "123", "指纹"):
            self.assertIn(text, created["body"])

    def test_open_batch_pr_is_resumed_before_new_candidate(self):
        io = FakeBatchIO(first_stage(entry("earlier", "1" * 40), entry()))
        io.prs.append(pull(entry(), number=19))
        result, run = self.run_batch(io)
        self.assertEqual(result["pr"], 19)
        self.assertEqual(run.call_args.args[2], 19)
        self.assertEqual(io.pushes, [])
        self.assertFalse(any(method == "POST" and path.endswith("/pulls") for method, path, body in io.calls))

    def test_no_pending_and_dry_run_have_no_writes(self):
        io = FakeBatchIO(first_stage(entry(auto=False)))
        result, run = self.run_batch(io)
        self.assertEqual((result["code"], result["reason"]), (0, "没有待处理的合并"))
        self.assertFalse(run.called)
        io = FakeBatchIO()
        result, run = self.run_batch(io, dry_run=True)
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["module"], "m")
        self.assertTrue(result["plan"])
        self.assertFalse(run.called)
        self.assertEqual(io.pushes, [])
        self.assertTrue(all(method == "GET" for method, path, body in io.calls))
        self.assertFalse(io.cleaned)

    def acceptance_run(self, io):
        return self.run_batch(io, [{"result": "stopped", "step": "acceptance", "reason": "missing", "notes": []},
                                   {"result": "merged", "step": "merge", "reason": "ok", "notes": []}])

    def test_acceptance_then_runs_flow_again(self):
        io = FakeBatchIO()
        result, run = self.acceptance_run(io)
        self.assertEqual((result["code"], run.call_count), (0, 2))
        self.assertTrue(all(call.kwargs == {"merge_method": "merge"} for call in run.call_args_list))
        self.assertTrue(io.cleaned)
        self.assertEqual(io.marked, ("/worktree", "m", "/saved/report.json"))
        root, base, head, module, task = io.reviews[0]
        self.assertEqual((root, base, head, module), ("/worktree", "d" * 40, HEAD, "m"))
        for text in ("spec/CAPABILITY-MAP.md", "spec/usage/", ".agent/config.json", ".agent/state.json",
                     "make test", "退出码 0", "e" * 64, "10\n11\n12", "19"):
            self.assertIn(text, task)
        self.assertNotIn("\n9\n", task)

    def test_other_flow_stops_return_unchanged(self):
        io = FakeBatchIO()
        result, run = self.run_batch(io, [{"result": "stopped", "step": "risk", "reason": "T4", "notes": ["n"]}])
        self.assertEqual((result["code"], result["step"], result["reason"]), (1, "risk", "T4"))
        self.assertFalse(io.cleaned)

    def test_merged_commit_must_be_remote_base_ancestor(self):
        for status in ("ahead", "identical", "behind", "diverged", "unknown", None):
            with self.subTest(status=status):
                io = FakeBatchIO()
                io.data["baseBranch"] = "release/main"
                io.post_merge_status = status
                merged = {"result": "merged", "step": "merge", "reason": "remote-merge-sha", "notes": ["existing"]}
                result, run = self.run_batch(io, [merged])
                self.assertEqual(result["result"], "merged")
                self.assertEqual(result["code"], 0 if status in ("ahead", "identical") else 1)
                self.assertIn(("GET", f"repos/{REPO}/compare/{HEAD}...release%2Fmain", None), io.calls)
                self.assertEqual(result["notes"][0], "existing")
                if result["code"]:
                    self.assertIn(HEAD, result["notes"][-1])
                    self.assertIn("release/main", result["notes"][-1])
                    self.assertIn("祖先", result["notes"][-1])

    def test_missing_compare_after_merge_reports_failed_ancestry_check(self):
        from degrade.run import NotFound
        io = FakeBatchIO()
        io.prs.append(pull(entry()))
        original = io.api
        def api(path, method="GET", body=None):
            if "/compare/" in path:
                raise NotFound("HTTP 404")
            return original(path, method, body)
        io.api = api
        result, run = self.run_batch(io)
        self.assertEqual((result["code"], result["result"]), (1, "merged"))
        self.assertIn("祖先核对失败", result["notes"][-1])
        self.assertIn("not-found", result["notes"][-1])

    def test_codex_route_hands_off_without_review(self):
        for key in ("spec", "plan"):
            io = FakeBatchIO()
            io.route[key] = "codex-route-takeover"
            result, run = self.acceptance_run(io)
            self.assertEqual(result["code"], 1)
            self.assertIn("codexRoute", result["reason"])
            self.assertIn("要人批", io.comments[-1])
            self.assertEqual(run.call_count, 1)
            self.assertTrue(io.cleaned)
            self.assertEqual(io.reviews, [])

    def test_reuses_release_report_only_with_correct_digest_and_baseline(self):
        for digest_ok, baseline_ok in ((True, True), (False, True), (True, False)):
            with self.subTest(digest=digest_ok, baseline=baseline_ok):
                io = FakeBatchIO()
                value = final_report()
                if not baseline_ok:
                    value["scope"]["baseline"]["to"] = "f" * 40
                raw = json.dumps(value).encode()
                io.files["/module-crew/report.json"] = raw
                io.data["merges"][0]["release"] = {
                    "report": "/module-crew/report.json", "reportSha256": hashlib.sha256(raw).hexdigest() if digest_ok else "0" * 64,
                    "fingerprint": "fp", "item": 124, "rounds": 1, "suggestions": 2}
                result, run = self.acceptance_run(io)
                self.assertEqual(result["code"], 0)
                self.assertEqual(len(io.reviews), 0 if digest_ok and baseline_ok else 1)
                if digest_ok and baseline_ok:
                    self.assertEqual(io.marked[-1], "/module-crew/report.json")

    def test_mark_refusal_includes_reason_and_cleans_worktree(self):
        io = FakeBatchIO()
        io.mark_result = {"exit": 2, "output": "codex-route-important: important paths"}
        result, run = self.acceptance_run(io)
        self.assertEqual((result["code"], run.call_count), (1, 1))
        self.assertIn("codex-route-important", result["reason"])
        self.assertTrue(io.cleaned)
        self.assertIn("spec-guard 拒绝记验收", io.comments[-1])

    def test_inconclusive_and_blocking_reports_stop_and_cleanup(self):
        for value in ({"execution_status": "failed"}, dict(final_report(), findings=[{"severity": "blocking"}])):
            io = FakeBatchIO()
            io.review = value
            result, run = self.acceptance_run(io)
            self.assertEqual((result["code"], run.call_count), (1, 1))
            self.assertTrue(io.cleaned)
            if value["execution_status"] == "failed":
                self.assertEqual(io.events[0]["event"], "codex_unavailable")
            else:
                self.assertIn("1 条 blocking", result["reason"])

    def test_unexpected_exception_still_cleans_worktree(self):
        io = FakeBatchIO()
        io.acceptance_map = mock.Mock(side_effect=RuntimeError("broken"))
        with self.assertRaises(RuntimeError):
            self.acceptance_run(io)
        self.assertTrue(io.cleaned)


class BatchLocalIOTest(unittest.TestCase):
    def setUp(self):
        self.io = cli.LocalIO("hub-secret", "/delegate/delegate-exec.sh")
        self.checkout_patch = mock.patch.object(self.io, "checkout_of", return_value="/checkout")
        self.checkout_patch.start()
        self.addCleanup(self.checkout_patch.stop)

    @staticmethod
    def completed(args, output="", code=0):
        return subprocess.CompletedProcess(args, code, output, "")

    def test_first_stage_command_and_failures(self):
        with mock.patch.dict(os.environ, {"AUTOPILOT_MODULE_CREW": "/plugin/module-crew"}), \
                mock.patch.object(cli.subprocess, "run", return_value=self.completed([], json.dumps(first_stage()))) as process:
            self.assertEqual(batch.parse_first_stage(self.io.first_stage(REPO), REPO), first_stage())
            args, kwargs = process.call_args
            self.assertEqual(args[0], ["/plugin/module-crew", "merge", "first-stage", "--json"])
            self.assertEqual(kwargs["cwd"], "/checkout")
        with mock.patch.object(cli.subprocess, "run", return_value=self.completed([], "not a project", 2)):
            with self.assertRaisesRegex(ValueError, "not a project"):
                self.io.first_stage(REPO)
        with mock.patch.object(cli.subprocess, "run", side_effect=FileNotFoundError("module-crew")):
            with self.assertRaises(OSError):
                self.io.first_stage(REPO)
        with mock.patch.object(self.io, "checkout_of", return_value=None), self.assertRaises(ValueError):
            self.io.first_stage(REPO)

    def test_push_uses_force_and_environment_authentication_only(self):
        with mock.patch.object(cli.subprocess, "run", return_value=self.completed([])) as process:
            self.io.push_batch(REPO, HEAD, "autopilot/batch-m")
        args, kwargs = process.call_args
        self.assertEqual(args[0], ["git", "-C", "/checkout", "push", "--force",
                                   "https://github.com/" + REPO, HEAD + ":refs/heads/autopilot/batch-m"])
        self.assertNotIn("hub-secret", str(args))
        env = kwargs["env"]
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "AUTHORIZATION: basic " +
                         base64.b64encode(b"x-access-token:hub-secret").decode())

    def test_detached_worktree_cleanup_on_success_body_failure_and_add_failure(self):
        for fail in (None, "body", "add"):
            calls = []
            def process(args, **kwargs):
                calls.append(args)
                if fail == "add" and "add" in args:
                    return self.completed(args, "failed add", 1)
                return self.completed(args)
            with self.subTest(fail=fail), mock.patch.object(cli.subprocess, "run", side_effect=process):
                if fail:
                    with self.assertRaises((RuntimeError, flow.Stop)):
                        with self.io.acceptance_worktree(REPO, HEAD):
                            if fail == "body":
                                raise RuntimeError("failure")
                else:
                    with self.io.acceptance_worktree(REPO, HEAD) as path:
                        self.assertIn("autopilot-acceptance-", path)
                self.assertEqual(calls[0][:6], ["git", "-C", "/checkout", "worktree", "add", "--detach"])
                self.assertEqual(calls[0][-1], HEAD)
                self.assertEqual(calls[-1][:6], ["git", "-C", "/checkout", "worktree", "remove", "--force"])
                self.assertEqual(calls[0][-2], calls[-1][-1])

    def test_review_base_is_parent_of_oldest_spec_addition(self):
        with mock.patch.object(cli.subprocess, "run", side_effect=[self.completed([], "1" * 40 + "\n" + "2" * 40 + "\n"),
                                                                  self.completed([], "d" * 40 + "\n")]) as process:
            self.assertEqual(self.io.module_start("/worktree", "m"), "d" * 40)
        self.assertEqual(process.call_args_list[0].args[0], ["git", "-C", "/worktree", "log", "--diff-filter=A",
                                                           "--format=%H", "--", "spec/m.md"])
        self.assertEqual(process.call_args.args[0][-1], "2" * 40 + "^")

    def test_delegate_review_arguments_and_report_storage(self):
        with mock.patch.object(cli.subprocess, "run", return_value=self.completed([], json.dumps(final_report()))) as process:
            self.assertEqual(self.io.review_acceptance("/worktree", "d" * 40, HEAD, "m", "任务"), final_report())
        args, kwargs = process.call_args
        self.assertEqual(args[0], ["bash", "/delegate/delegate-exec.sh", "review", "--repo", "/worktree",
                                   "--base", "d" * 40, "--head", HEAD, "--spec", "spec/m.md", "--", "任务"])
        self.assertNotIn("hub-secret", str(kwargs["env"]))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cli, "STATE", tmp):
            path = self.io.save_acceptance_review(REPO, "m", HEAD, final_report())
            self.assertEqual(path, str(Path(tmp) / "reviews" / "haigeerlab__autopilot-sandbox" /
                                       ("acceptance-m-" + HEAD[:12] + ".json")))
            self.assertEqual(json.loads(Path(path).read_text()), final_report())

    def test_spec_guard_map_and_mark_commands(self):
        with mock.patch.dict(os.environ, {"AUTOPILOT_SPEC_GUARD_ROOT": "/spec-guard"}), \
                mock.patch.object(cli.subprocess, "run", return_value=self.completed([], '{"modules": []}')) as process:
            self.assertEqual(self.io.acceptance_map("/worktree"), {"modules": []})
            self.assertEqual(process.call_args.args[0], ["python3", "-B", "/spec-guard/hooks/map_view.py",
                                                        "--json", "--project", "/worktree"])
            self.io.mark_acceptance("/worktree", "m", "/report.json")
            self.assertEqual(process.call_args.args[0], ["python3", "-B", "/spec-guard/hooks/review_mark.py",
                                                        "--project", "/worktree", "--module", "m", "--target", "acceptance",
                                                        "--state", "codex-approved", "--review", "/report.json", "--confirm"])

    def test_batch_pr_normalizes_body_edit_time_via_graphql(self):
        value = pull(entry())
        value["updated_at"] = "later due to comments"
        with mock.patch.object(self.io, "api", side_effect=[value, {"data": {"repository": {"pullRequest": {"lastEditedAt": None}}}}]):
            self.assertIsNotNone(batch.batch_payload(self.io.batch_pr(REPO, 7), BOT))
        with mock.patch.object(self.io, "api", side_effect=[value, {"data": {"repository": {"pullRequest": {"lastEditedAt": "edited"}}}}]):
            self.assertIsNone(batch.batch_payload(self.io.batch_pr(REPO, 7), BOT))


class ExtraEvidenceTest(unittest.TestCase):
    def test_only_trusted_unedited_batch_pr_gets_readable_extra_check(self):
        from test_orchestrator import FakeIO
        for trusted, edited, matching in ((True, False, True), (False, False, True),
                                          (True, True, True), (True, False, False)):
            with self.subTest(trusted=trusted, edited=edited, matching=matching):
                io = FakeIO()
                io.pr.update(pull(entry()))
                io.pr["base"]["repo"] = {"owner": {"login": "haigeerlab"}}
                io.extra_evidence = entry()
                if not trusted:
                    io.pr["user"] = {"login": "haigeerlab", "type": "User", "id": 123}
                if edited:
                    io.pr["updated_at"] = "edited"
                if not matching:
                    io.extra_evidence["mergeCommit"] = "f" * 40
                io.batch_pr = lambda repo, number, pr=None: io.pr
                value = flow.run(io, REPO, 7)
                bodies = "\n".join(c["body"] for c in io.comments)
                if trusted and not edited and matching:
                    self.assertEqual(value["result"], "merged")
                    self.assertIn("module-crew 合并队列检查：make test，pass，耗时 812 秒（额外参考，不替代本机重跑）", bodies)
                    from pipeline.engine import payload
                    evidence = next(payload(c["body"], "autopilot-evidence") for c in io.comments
                                    if "<!-- autopilot-evidence:v1" in c["body"])
                    self.assertEqual(len(evidence["checks"]), 1)
                    self.assertEqual(io.ran, ["make test"])
                else:
                    self.assertNotIn("module-crew 合并队列检查", bodies)
                    self.assertNotEqual(value["result"], "merged")

    def test_extra_check_cannot_replace_a_failed_local_check(self):
        from test_orchestrator import FakeIO
        io = FakeIO(check_exit=1)
        io.pr.update(pull(entry()))
        io.pr["base"]["repo"] = {"owner": {"login": "haigeerlab"}}
        io.extra_evidence = entry()
        io.batch_pr = lambda repo, number, pr=None: io.pr
        self.assertEqual(flow.run(io, REPO, 7)["step"], "check")


class BatchCLITest(unittest.TestCase):
    def test_permissions_result_and_revocation(self):
        for dry_run in (False, True):
            args = ["batch", "--repo", REPO] + (["--dry-run"] if dry_run else [])
            with self.subTest(dry_run=dry_run), \
                    mock.patch.object(cli.tokens, "mint", return_value="token") as mint, \
                    mock.patch.object(cli.tokens, "revoke") as revoke, \
                    mock.patch.object(cli.batch, "run", return_value={"code": 0, "reason": "没有待处理的合并"}) as run, \
                    mock.patch("builtins.print"):
                self.assertEqual(cli.main(args), 0)
                mint.assert_called_once_with(REPO, cli.READ if dry_run else cli.WRITE)
                revoke.assert_called_once_with("token")
                self.assertEqual(run.call_args.kwargs["dry_run"], dry_run)

    def test_interface_failure_is_exit_two_and_token_is_revoked(self):
        for error in (ValueError("schema mismatch"), OSError("not installed"), subprocess.TimeoutExpired("module-crew", 120)):
            with self.subTest(error=error), \
                    mock.patch.object(cli.tokens, "mint", return_value="token"), \
                    mock.patch.object(cli.tokens, "revoke") as revoke, \
                    mock.patch.object(cli.batch, "run", side_effect=error), mock.patch("builtins.print"):
                self.assertEqual(cli.main(["batch", "--repo", REPO]), 2)
                revoke.assert_called_once_with("token")


class BatchGuardTest(unittest.TestCase):
    def test_open_pr_with_wrong_source_stops_and_comments_without_push(self):
        for mutation in ("edited", "author", "head", "base"):
            io = FakeBatchIO()
            value = pull(entry())
            if mutation == "edited":
                value["updated_at"] = "edited"
            elif mutation == "author":
                value["user"] = {"id": 4, "type": "User"}
            elif mutation == "head":
                value["head"]["sha"] = "f" * 40
            else:
                value["base"]["ref"] = "other"
            io.prs.append(value)
            with self.subTest(mutation=mutation), mock.patch.object(flow, "run") as run:
                result = batch.run(io, REPO)
                self.assertEqual(result["code"], 1)
                self.assertTrue(io.comments)
                self.assertEqual(io.pushes, [])
                self.assertFalse(run.called)

    def test_missing_or_invalid_codex_route_fails_closed(self):
        for route in (None, {}, {"spec": None}):
            io = FakeBatchIO()
            io.route = route
            with mock.patch.object(flow, "run", return_value={"result": "stopped", "step": "acceptance"}):
                result = batch.run(io, REPO)
            self.assertEqual(result["code"], 1)
            self.assertEqual(io.reviews, [])
            self.assertTrue(io.cleaned)

    def test_mark_success_must_be_read_back_from_shared_location(self):
        io = FakeBatchIO()
        io.shared_acceptance = lambda root, module: {"sha": "f" * 40}
        with mock.patch.object(flow, "run", return_value={"result": "stopped", "step": "acceptance"}) as run:
            result = batch.run(io, REPO)
        self.assertEqual(result["code"], 1)
        self.assertIn("新位置", result["reason"])
        self.assertTrue(io.cleaned)
        self.assertEqual(run.call_count, 1)


class BatchFlowIntegrationTest(unittest.TestCase):
    def make_io(self, allow_merge=True):
        from test_orchestrator import FakeIO
        io = FakeBatchIO()
        runner = FakeIO(module="m", reports=[])
        runner.pr.update(pull(entry()))
        runner.pr["base"]["repo"] = {"owner": {"login": "haigeerlab"}}
        batch_api = io.api
        def api(path, method="GET", body=None):
            if path == f"repos/{REPO}":
                return {"allow_squash_merge": True, "allow_merge_commit": allow_merge}
            if "/commits/" in path or "/compare/" in path or "/pulls?" in path or (method == "POST" and path.endswith("/pulls")):
                return batch_api(path, method, body)
            return runner.api(path, method, body)
        io.api = api
        io.batch_pr = lambda repo, number, pr=None: runner.pr
        io.api_optional = runner.api_optional
        io.checkout = runner.checkout
        io.find_modules = lambda repo, head: ["m"] if io.accepted else []
        io.write_check_evidence = runner.write_check_evidence
        return io, runner

    def test_real_flow_checks_then_accepts_then_rechecks_and_merges(self):
        io, runner = self.make_io()
        result = batch.run(io, REPO)
        self.assertEqual((result["code"], result["result"]), (0, "merged"))
        self.assertTrue(runner.merged)
        self.assertEqual(runner.merge_calls(), [("PUT", f"repos/{REPO}/pulls/7/merge",
                                                {"sha": HEAD, "merge_method": "merge"})])
        self.assertEqual(runner.ran, ["make test", "make test"])
        self.assertEqual(runner.reviews, [])
        self.assertEqual(len(io.reviews), 1)
        self.assertTrue(io.cleaned)
        self.assertTrue(any("module-crew 合并队列检查" in c["body"] for c in runner.comments))

    def test_batch_stops_without_merge_request_when_merge_commits_are_disabled(self):
        io, runner = self.make_io(allow_merge=False)
        result = batch.run(io, REPO)
        self.assertEqual((result["code"], result["result"], result["step"]), (1, "stopped", "merge"))
        self.assertIn("Allow merge commits", result["reason"])
        self.assertEqual(runner.merge_calls(), [])
        self.assertFalse(runner.merged)
        self.assertFalse(any(path.endswith("...main") for method, path, body in io.calls))

    def test_real_flow_merged_result_fails_when_original_head_is_not_an_ancestor(self):
        io, runner = self.make_io()
        io.post_merge_status = "diverged"
        result = batch.run(io, REPO)
        self.assertEqual((result["code"], result["result"]), (1, "merged"))
        self.assertTrue(runner.merged)
        self.assertIn("祖先核对失败", result["notes"][-1])


class BatchBoundaryTest(unittest.TestCase):
    def test_local_merge_not_known_to_github_is_still_a_candidate(self):
        from degrade.run import NotFound
        io = FakeBatchIO()
        original = io.api
        def api(path, method="GET", body=None):
            if "/compare/" in path and not path.endswith("...main"):
                raise NotFound("HTTP 404: local commit has not been pushed")
            if "/commits/" in path:
                return {"sha": "f" * 40}
            return original(path, method, body)
        io.api = api
        with mock.patch.object(flow, "run", return_value={"result": "merged", "step": "merge", "reason": "ok"}):
            result = batch.run(io, REPO)
        self.assertEqual(result["code"], 0)
        self.assertEqual(io.pushes[0][1], HEAD)

    def test_dry_run_with_conflicting_pr_still_never_comments(self):
        io = FakeBatchIO()
        value = pull(entry())
        value["updated_at"] = "edited"
        io.prs.append(value)
        result = batch.run(io, REPO, dry_run=True)
        self.assertNotEqual(result["code"], 0)
        self.assertEqual(io.comments, [])
        self.assertTrue(all(method == "GET" for method, path, body in io.calls))

    def test_release_with_malformed_baseline_falls_back_to_review(self):
        io = FakeBatchIO()
        raw = json.dumps(dict(final_report(), scope=["invalid"])).encode()
        io.files["/report"] = raw
        item = entry()
        item["release"] = {"report": "/report", "reportSha256": hashlib.sha256(raw).hexdigest()}
        self.assertIsNone(batch.release_review(io, item))

    def test_map_view_uses_configured_spec_guard_root(self):
        with mock.patch.dict(os.environ, {"AUTOPILOT_SPEC_GUARD_ROOT": "/sg"}):
            self.assertEqual(cli.find_map_view(), "/sg/hooks/map_view.py")


class AcceptanceBoundaryTest(unittest.TestCase):
    def test_missing_release_report_runs_review_and_includes_all_check_evidence(self):
        io = FakeBatchIO()
        io.data["merges"][0]["release"] = {"report": "/missing.json", "reportSha256": "0" * 64}
        io.evidence["checks"].append({"cmd": "make smoke", "exit": 0, "output_sha256": "f" * 64, "tail": ["smoke ok"]})
        batch.record_acceptance(io, REPO, io.data["merges"][0], 7)
        task = io.reviews[0][-1]
        self.assertIn("make test", task)
        self.assertIn("make smoke", task)
        self.assertIn("f" * 64, task)
        self.assertIn("smoke ok", task)
        self.assertTrue(io.cleaned)

    def test_route_refusal_code_is_preserved_beyond_output_excerpt(self):
        io = FakeBatchIO()
        io.mark_result = {"exit": 2, "output": "x" * 400 + "\ncodex-route-pre-declared"}
        with self.assertRaises(flow.Stop) as raised:
            batch.record_acceptance(io, REPO, entry(), 7)
        self.assertEqual(raised.exception.step, "acceptance")
        self.assertIn("codex-route-pre-declared", raised.exception.reason)
        self.assertLess(len(raised.exception.reason), 380)
        self.assertTrue(io.cleaned)

    def test_an_edit_in_creation_second_is_still_an_edit(self):
        io = cli.LocalIO("secret", None)
        value = pull(entry())
        response = {"data": {"repository": {"pullRequest": {"lastEditedAt": T0}}}}
        with mock.patch.object(io, "api", side_effect=[value, response]):
            self.assertIsNone(batch.batch_payload(io.batch_pr(REPO, 7), BOT))

    def test_cleanup_failure_is_reported(self):
        io = cli.LocalIO("secret", None)
        with mock.patch.object(io, "checkout_of", return_value="/checkout"), \
                mock.patch.object(cli.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, "", ""),
                                                                      subprocess.CompletedProcess([], 1, "", "remove failed")]):
            with self.assertRaisesRegex(flow.Stop, "清理验收 worktree 失败"):
                with io.acceptance_worktree(REPO, HEAD):
                    pass
