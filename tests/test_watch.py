"""Watch regression scenarios with local state and fake external interfaces."""

import base64
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io as streams
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from degrade.run import ApiError, NotFound
from orchestrator import cli, flow
from orchestrator.state import StateLockedError, WatchState


REPO = "example/widget"
HEAD = "a" * 40
BASE = "b" * 40
MAIN = "c" * 40
PATCH = "d" * 40
NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
RECENT = "2026-01-01T11:55:00Z"
OLD = "2026-01-01T10:55:00Z"
BOT = {"login": "autopilot[bot]", "id": 123, "app_slug": "autopilot"}
BOT_USER = {"login": BOT["login"], "id": BOT["id"], "type": "Bot"}
LOCAL_IO = cli.LocalIO
WATCH_PERMISSIONS = {"contents": "read", "pull_requests": "write", "issues": "write",
                     "statuses": "read", "actions": "write"}
DISABLE_PERMISSIONS = {"contents": "write", "pull_requests": "write"}


def ci(run_id, event="pull_request", *, head=None, attempt=1, conclusion="failure", started=RECENT):
    return {
        "id": run_id, "run_attempt": attempt, "event": event,
        "head_sha": head or (HEAD if event == "pull_request" else MAIN),
        "head_branch": "feature" if event == "pull_request" else "main",
        "status": "completed" if conclusion else "in_progress", "conclusion": conclusion,
        "name": "CI", "run_started_at": started, "created_at": started,
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}", "pull_requests": [],
    }


def jobs(failed_step):
    return [{"name": "CI", "conclusion": "failure", "steps": [
        {"number": 1, "name": "Setup", "conclusion": "failure" if failed_step == "setup" else "success"},
        {"number": 2, "name": "Run tests", "conclusion": "skipped" if failed_step == "setup" else "failure"},
    ]}]


def reject_disable(method, path):
    if (method, path) == ("POST", "graphql"):
        raise ApiError("Resource not accessible by integration")


class SimulatedCrash(BaseException):
    """Leave intents on disk as an abruptly terminated watch process would."""


class FakeGitHub:
    def __init__(self, state):
        self.state = state
        self.token = "watch-token"
        self.prs, self.runs, self.jobs, self.statuses = {}, {}, {}, {}
        self.issues, self.comments, self.narrow_prs = [], {}, {}
        self.narrow_refs, self.narrow_records, self.unlisted_runs = {}, [], set()
        self.effects, self.requests = [], []
        self.fail = None
        self.after_write = None
        self.report = b""

    def effect(self, *effect):
        self.effects.append(effect)
        if self.after_write:
            self.after_write(effect)

    def add_pr(self, number, *, head=HEAD, queued=True, merged=False):
        self.prs[number] = dict(number=number, node_id=f"node-{number}", head={"sha": head},
                                base={"ref": "main"}, state="closed" if merged else "open", merged=merged,
                                auto_merge={"enabled_at": RECENT} if queued else None, labels=[],
                                merge_commit_sha=MAIN if merged else None,
                                html_url=f"https://github.com/{REPO}/pull/{number}")
        self.statuses.setdefault(head, "success")

    def comment(self, number, body):
        comments = self.comments.setdefault(number, [])
        item = dict(id=len(comments) + 1, body=body, user=BOT_USER, created_at=RECENT, updated_at=RECENT,
                    performed_via_github_app={"slug": BOT["app_slug"]},
                    html_url=f"https://github.com/{REPO}/issues/{number}#issuecomment-{len(comments) + 1}")
        comments.append(item)
        return item

    def merge_evidence(self, number=7):
        self.report = json.dumps({"notes": [{"title": "Follow through on the finding"}]}).encode()
        payload = {"head_sha": HEAD, "module": "feature", "patch_id": PATCH,
                   "verification": {"report_sha256": hashlib.sha256(self.report).hexdigest()}}
        return self.comment(number, "<!-- autopilot-evidence:v2 " + json.dumps(payload) + " -->")["html_url"]

    @staticmethod
    def bot_identity():
        return BOT

    def api_optional(self, path):
        try:
            return self.api(path)
        except NotFound:
            return None

    def verification_paths(self, repo, patch_id):
        assert (repo, patch_id) == (REPO, PATCH)
        return ("report.json",)

    def read_bytes(self, path):
        assert path == "report.json"
        return self.report

    def open_narrow_pr(self, repo, base, module, issue, reason, **kwargs):
        if issue not in self.narrow_prs:
            self.narrow_prs[issue] = f"https://github.com/{repo}/pull/900"
            self.effect("narrow-pr", issue)
        return self.narrow_prs[issue]

    def api(self, path, method="GET", body=None, **kwargs):
        self.requests.append((method, path))
        if self.fail:
            self.fail(method, path)
        return deepcopy(self._api(path, method, body))

    def _api(self, path, method, body):
        if path == "graphql" and method == "POST":
            assert "disablePullRequestAutoMerge" in body["query"], body
            node_id = body["variables"]["pullRequestId"]
            number = int(node_id.split("-")[1])
            assert self.prs[number]["auto_merge"] is not None
            self.prs[number]["auto_merge"] = None
            self.effect("close", number, bool(self.state.get_narrowed(REPO)))
            return {"data": {"disablePullRequestAutoMerge": {"pullRequest": {"id": node_id}}}}

        url = urlsplit(path)
        prefix = f"repos/{REPO}"
        assert url.path.startswith(prefix), (method, path)
        endpoint = url.path[len(prefix):].strip("/")
        parts = endpoint.split("/")
        query = parse_qs(url.query)
        if endpoint == "" and method == "GET":
            return {"default_branch": "main"}
        if endpoint == "pulls" and method == "GET":
            if "head" in query:
                return [pr for pr in self.narrow_records if pr["head"]["ref"] == query["head"][0].split(":")[1]]
            return [pr for pr in self.prs.values() if pr["state"] == "open"]
        if endpoint == "pulls" and method == "POST":
            assert body["head"] in self.narrow_refs
            pr = dict(number=900, html_url=f"https://github.com/{REPO}/pull/900", user=BOT_USER,
                      head={"ref": body["head"], "repo": {"full_name": REPO}}, base={"ref": body["base"]})
            self.narrow_records.append(pr)
            self.effect("narrow-pr", pr["number"])
            return pr
        if endpoint.startswith("git/ref/heads/") and method == "GET":
            branch = endpoint.removeprefix("git/ref/heads/")
            if branch not in self.narrow_refs:
                raise NotFound(path)
            return {"object": {"sha": self.narrow_refs[branch]}}
        if parts[0] == "pulls" and len(parts) == 2 and method == "GET":
            return self.prs[int(parts[1])]
        if parts[0] == "commits" and parts[2] in ("status", "statuses"):
            status = self.statuses.get(parts[1], [])
            rows = status if isinstance(status, list) else [{
                "context": flow.VERIFIED, "state": status, "creator": BOT_USER,
                "target_url": f"https://github.com/{REPO}/issues/7#issuecomment-1",
            }]
            return {"statuses": [{k: v for k, v in row.items() if k != "creator"} for row in rows]} \
                if parts[2] == "status" else rows
        if parts[0] == "commits" and parts[2] == "pulls":
            return [pr for pr in self.prs.values() if pr["merge_commit_sha"] == parts[1]]
        if endpoint == "actions/runs" and method == "GET":
            matches = [run for run in self.runs.values() if run["id"] not in self.unlisted_runs
                       and all(run[field] in query[param] for param, field in
                              (("event", "event"), ("branch", "head_branch"), ("head_sha", "head_sha"))
                              if param in query)]
            if "page" in query:
                size, page = int(query["per_page"][0]), int(query["page"][0])
                ordered = sorted(matches, key=lambda run: run["id"], reverse=True)
                return {"total_count": len(matches), "workflow_runs": ordered[(page - 1) * size:page * size]}
            return [{"workflow_runs": list(reversed(matches[1:]))}, {"workflow_runs": matches[:1]}]
        if parts[:2] == ["actions", "runs"]:
            run_id = int(parts[2])
            run = self.runs[run_id]
            if len(parts) == 3 and method == "GET":
                return run
            if parts[-1] == "jobs" and method == "GET":
                return {"jobs": self.jobs.get(run_id, jobs("test"))}
            if parts[-1] == "rerun-failed-jobs" and method == "POST":
                run.update(run_attempt=run["run_attempt"] + 1, status="queued", conclusion=None)
                self.effect("rerun", run_id)
                return None
        if endpoint.startswith("contents/") and method == "GET":
            if endpoint == "contents/spec/CAPABILITY-MAP.md":
                return {"content": base64.b64encode(b"capability map").decode()}
            raise NotFound(path)
        if endpoint == "issues" and method == "GET":
            label = query["labels"][0]
            return [issue for issue in self.issues if label in issue["labels"]
                    and (query.get("state") == ["all"] or issue["state"] == "open")]
        if endpoint == "issues" and method == "POST":
            number = 100 + len(self.issues)
            created = dict(number=number, state="open", user=BOT_USER, **body,
                           html_url=f"https://github.com/{REPO}/issues/{number}")
            self.issues.append(created)
            self.effect("issue", body["labels"][0], number)
            return created
        if parts[0] == "issues" and len(parts) >= 2:
            number = int(parts[1])
            if len(parts) == 2 and method == "GET":
                return next(issue for issue in self.issues if issue["number"] == number)
            if parts[2] == "comments":
                if method == "GET":
                    return self.comments.get(number, [])
                if method == "POST":
                    item = self.comment(number, body["body"])
                    self.effect("comment", number)
                    return item
            if parts[2] == "labels" and method == "POST":
                self.prs[number]["labels"].extend({"name": label} for label in body["labels"])
                self.effect("label", number)
                return self.prs[number]["labels"]
        raise AssertionError((method, path, body))


class WatchTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp = temporary.name
        self.directory = Path(self.temp)
        self.state = WatchState(self.directory, clock=lambda: NOW)
        self.github = FakeGitHub(self.state)
        self.config = {"repos": {REPO: {"watchMainRed": False}}}
        self.fail_desktop = False
        self.fail_relay = False
        self.relay_keys = set()
        self.output = streams.StringIO()
        self.errors = streams.StringIO()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(cli, "STATE", self.temp))
        stack.enter_context(mock.patch.object(cli, "WatchState", side_effect=lambda *_: self.state))
        self.mint = stack.enter_context(mock.patch.object(cli.tokens, "mint", return_value="watch-token"))
        self.revoke = stack.enter_context(mock.patch.object(cli.tokens, "revoke"))
        stack.enter_context(mock.patch.object(cli.LocalIO, "local_config", side_effect=lambda: self.config))
        self.local_io = stack.enter_context(mock.patch.object(cli, "LocalIO", wraps=cli.LocalIO, return_value=self.github))
        stack.enter_context(mock.patch.object(subprocess, "run", side_effect=self.fake_process))

    def trace_disable_credentials(self):
        trace = mock.Mock()
        write_io = mock.Mock(token="disable-token")
        write_io.api.side_effect = self.github.api
        self.mint.side_effect = lambda repo, permissions: (
            write_io.token if permissions == DISABLE_PERMISSIONS else self.github.token)
        self.local_io.side_effect = lambda token, _: write_io if token == write_io.token else self.github
        trace.attach_mock(self.mint, "mint")
        trace.attach_mock(write_io.api, "write_api")
        trace.attach_mock(self.revoke, "revoke")
        return trace

    @staticmethod
    def disable_credential_calls():
        return [mock.call.mint(REPO, DISABLE_PERMISSIONS), mock.call.write_api("graphql", "POST", mock.ANY),
                mock.call.revoke("disable-token")]

    def fake_process(self, args, **kwargs):
        if args[0] == "osascript":
            if self.fail_desktop:
                return subprocess.CompletedProcess(args, 1)
            self.github.effect("desktop", args[-1])
        elif Path(args[0]).name == "agent-relay":
            self.assertEqual(args[1:5], ["send", "--from", "autopilot-watch", "--to"])
            self.assertTrue(kwargs["input"])
            key = args[args.index("--idempotency-key") + 1]
            if self.fail_relay:
                return subprocess.CompletedProcess(args, 1)
            if key not in self.relay_keys:
                self.relay_keys.add(key)
                self.github.effect("relay", args[5], kwargs["input"])
        else:
            self.fail(f"Unexpected subprocess: {args[0]}")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def command(self, *args):
        with redirect_stdout(self.output), redirect_stderr(self.errors):
            return cli.main(list(args))

    def once(self):
        return self.command("watch", "--once")

    def tracked_pr(self, number=7, *, since=RECENT, **fields):
        self.github.add_pr(number)
        self.state.set_pr(REPO, number, **dict(dict(
            state="automerge-on", validatedHeadOid=HEAD, validatedBaseOid=BASE,
            since=since, notify="author"), **fields))

    def effects(self, name):
        return [effect for effect in self.github.effects if effect[0] == name]

    def assert_paused(self, record):
        self.assertEqual(record["state"], "paused", record)
        self.assertTrue(record["pause"]["reason"])
        self.assertTrue(record["pause"]["at"])
        self.assertTrue(record["pause"]["recovery"])


class HeadChangedTest(WatchTestCase):
    def test_changed_head_closes_queue_and_notifies_session(self):
        self.tracked_pr()
        self.github.prs[7]["head"]["sha"] = MAIN
        self.fail_relay = True
        trace = self.trace_disable_credentials()

        self.once()

        self.assertIsNone(self.github.prs[7]["auto_merge"])
        self.assertEqual(self.effects("close"), [("close", 7, False)])
        self.assertEqual(trace.mock_calls, [mock.call.mint(REPO, WATCH_PERMISSIONS),
                                           *self.disable_credential_calls(), mock.call.revoke("watch-token")])
        record = self.state.get_pr(REPO, 7)
        self.assert_paused(record)
        self.assertIn("run", record["pause"]["recovery"])
        notices = self.state.pending_notifications()
        self.assertEqual([item["target"] for item in notices], ["author"])
        self.assertEqual(len(self.effects("desktop")), 1)
        for body in (notices[0]["body"], self.github.comments[7][0]["body"]):
            for detail in (HEAD, MAIN, "已关闭自动合并", "重新运行 autopilot run"):
                self.assertIn(detail, body)

    def test_disable_error_keeps_head_change_pending_and_retries_next_round(self):
        self.tracked_pr()
        self.github.prs[7]["head"]["sha"] = MAIN
        self.github.fail = reject_disable
        trace = self.trace_disable_credentials()

        self.assertEqual(self.once(), 1)

        record = self.state.get_pr(REPO, 7)
        self.assert_paused(record)
        self.assertEqual(record["resumeState"], "automerge-on")
        self.assertIn("Resource not accessible by integration", record["pause"]["reason"])
        self.assertTrue(any(event["event"] == "watch-error" for event in LOCAL_IO.ledger_events()))
        self.assertTrue(self.github.prs[7]["auto_merge"])
        self.assertFalse(self.effects("relay"))
        self.assertIn("Resource not accessible by integration", self.github.comments[7][0]["body"])
        self.assertEqual(trace.mock_calls, [mock.call.mint(REPO, WATCH_PERMISSIONS),
                                           *self.disable_credential_calls(), mock.call.revoke("watch-token")])

        self.github.fail = None
        self.assertEqual(self.once(), 0)
        self.assertEqual(self.once(), 0)

        record = self.state.get_pr(REPO, 7)
        self.assert_paused(record)
        self.assertIsNone(record["resumeState"])
        self.assertIn("head 已变化", record["pause"]["reason"])
        self.assertIsNone(self.github.prs[7]["auto_merge"])
        self.assertEqual(self.effects("close"), [("close", 7, False)])
        self.assertEqual(len(self.effects("relay")), 1)
        self.assertEqual(self.effects("relay")[0][1], "author")
        self.assertIn(MAIN, self.effects("relay")[0][2])
        self.assertEqual(len(self.effects("desktop")), 2)
        self.assertEqual(self.state.pending_intents(), {})

    def test_already_disabled_queue_does_not_need_a_write_token(self):
        self.tracked_pr()
        self.github.add_pr(7, head=MAIN, queued=False)
        trace = self.trace_disable_credentials()

        self.assertEqual(self.once(), 0)

        self.assertEqual(trace.mock_calls, [mock.call.mint(REPO, WATCH_PERMISSIONS), mock.call.revoke("watch-token")])
        self.assert_paused(self.state.get_pr(REPO, 7))
        self.assertFalse(self.effects("close"))

    def test_verified_must_be_success_on_the_validated_head(self):
        self.tracked_pr()
        self.github.statuses[HEAD] = "failure"

        self.once()

        self.assert_paused(self.state.get_pr(REPO, 7))
        self.assertIsNone(self.github.prs[7]["auto_merge"])
        self.assertEqual(len(self.effects("relay")), 1)
        self.assertEqual(len(self.effects("desktop")), 1)

    def test_verified_requires_hub_creator_on_the_latest_status(self):
        trusted = {"context": flow.VERIFIED, "state": "success", "creator": BOT_USER}
        other_context = {"context": "CI", "state": "success", "creator": BOT_USER}
        for number, (creator, bot) in enumerate([
                (None, BOT), (dict(BOT_USER, id=456), BOT), (BOT_USER, None),
                (dict(BOT_USER, type="User"), BOT)], start=7):
            with self.subTest(creator=creator, bot=bot):
                self.tracked_pr(number)
                self.github.statuses[HEAD] = [other_context, dict(trusted, creator=creator), trusted]
                with mock.patch.object(self.github, "bot_identity", return_value=bot):
                    self.assertEqual(self.once(), 0)
                self.assert_paused(self.state.get_pr(REPO, number))
                self.assertIsNone(self.github.prs[number]["auto_merge"])

        self.tracked_pr(12)
        self.github.statuses[HEAD] = [other_context, trusted]
        self.assertEqual(self.once(), 0)
        self.assertEqual(self.state.get_pr(REPO, 12)["state"], "automerge-on")
        paths = [urlsplit(path).path for method, path in self.github.requests if method == "GET"]
        self.assertIn(f"repos/{REPO}/commits/{HEAD}/statuses", paths)
        self.assertNotIn(f"repos/{REPO}/commits/{HEAD}/status", paths)


class PrCiFailureTest(WatchTestCase):
    def test_test_failure_without_session_notifies_user_once(self):
        self.tracked_pr(notify=None)
        self.github.runs[10] = ci(10)
        self.fail_desktop = True

        self.once()

        notices = self.state.pending_notifications()
        self.assertEqual([item["target"] for item in notices], ["user"])
        self.assertIn("CI 测试失败", notices[0]["body"])
        self.assertEqual(self.effects("relay"), [])
        self.fail_desktop = False
        self.once()
        self.once()
        self.assertEqual(self.state.pending_notifications(), [])
        self.assertEqual(len(self.effects("desktop")), 1)
        self.assertEqual(self.effects("comment"), [("comment", 7)])

    def test_environment_reruns_only_twice(self):
        self.tracked_pr()
        self.github.runs[10] = ci(10)
        self.github.jobs[10] = jobs("setup")

        for attempt in (1, 2, 3):
            self.github.runs[10].update(conclusion="failure", status="completed", run_attempt=attempt)
            self.once()
            self.assertEqual(self.state.get_run(REPO, 10, attempt)["classification"], "env")

        self.once()
        self.assertEqual(self.effects("rerun"), [("rerun", 10), ("rerun", 10)])
        self.assert_paused(self.state.get_run(REPO, 10, 3))
        self.assert_paused(self.state.get_pr(REPO, 7))
        self.assertEqual(len(self.effects("desktop")), 1)

    def test_config_pauses_and_notifies_user_and_session_without_narrowing(self):
        self.tracked_pr()
        self.github.runs[10] = ci(10, conclusion="startup_failure")

        self.once()

        self.assert_paused(self.state.get_run(REPO, 10, 1))
        self.assert_paused(self.state.get_pr(REPO, 7))
        self.assertEqual(self.state.get_run(REPO, 10, 1)["classification"], "config")
        self.assertIsNone(self.state.get_narrowed(REPO))
        self.assertEqual(len(self.effects("desktop")), 1)
        self.assertEqual(len(self.effects("relay")), 1)
        self.assertEqual(self.effects("issue"), [])

    def test_test_failure_notifies_session_once_and_ignores_other_heads(self):
        self.tracked_pr()
        self.github.runs.update({10: ci(10), 11: ci(11, head=MAIN)})
        self.once()
        self.once()

        self.assertEqual(self.state.get_run(REPO, 10, 1)["classification"], "test")
        self.assertIsNone(self.state.get_run(REPO, 11, 1))
        self.assertEqual(len(self.effects("relay")), 1)
        self.assertEqual(self.effects("desktop"), [])


class MainRedTest(WatchTestCase):
    def test_first_main_watch_sets_cursor_without_processing_history(self):
        self.config["repos"][REPO]["watchMainRed"] = True
        self.github.add_pr(7)
        self.github.runs = {
            18: ci(18, "push"),
            19: ci(19, "push", conclusion="startup_failure"),
            20: ci(20, "push"),
            21: ci(21, "push") | {"head_branch": "other"},
            22: ci(22),
        }
        self.github.jobs[20] = jobs("setup")

        self.assertEqual(self.once(), 0)
        self.assertEqual(self.state.get_cursor(REPO), 20)
        self.assertEqual(self.state.read()["runs"], {})
        self.assertIsNone(self.state.get_narrowed(REPO))
        self.assertEqual(self.github.effects, [])
        self.assertFalse(any(path.startswith(f"repos/{REPO}/actions/runs/") for _, path in self.github.requests))
        events = [event for event in LOCAL_IO.ledger_events() if event.get("repo") == REPO]
        self.assertEqual(len(events), 1)
        self.assertIn("首次巡检，从此处开始", events[0]["detail"])

        self.github.runs[23] = ci(23, "push")
        self.assertEqual(self.once(), 0)
        self.assertEqual(self.state.get_cursor(REPO), 23)
        self.assertEqual(set(self.state.read()["runs"]), {f"{REPO}:23:1"})
        self.assertIsNotNone(self.state.get_narrowed(REPO))
        self.assertEqual(len(self.effects("issue")), 1)

    def test_first_main_watch_with_no_runs_processes_the_next_push(self):
        self.config["repos"][REPO]["watchMainRed"] = True
        self.assertEqual(self.once(), 0)
        self.assertEqual(self.state.get_cursor(REPO), 0)

        self.github.runs[20] = ci(20, "push")
        self.assertEqual(self.once(), 0)
        self.assertEqual(self.state.get_cursor(REPO), 20)
        self.assertIsNotNone(self.state.get_narrowed(REPO))

    def test_narrows_in_order_and_only_repository_resume_releases_hold(self):
        self.config["repos"][REPO]["watchMainRed"] = True
        self.state.set_cursor(REPO, 19)
        self.github.add_pr(7)
        self.github.add_pr(8)
        self.github.runs[20] = ci(20, "push")

        self.once()

        self.assertEqual(self.state.get_narrowed(REPO)["reason"], "main-red")
        self.assertEqual(self.github.effects[:4], [
            ("close", 7, True), ("close", 8, True), ("issue", "autopilot:degrade", 100), ("narrow-pr", 100),
        ])
        self.assertEqual(len(self.effects("desktop")), 1)
        self.assertGreater(self.github.effects.index(self.effects("desktop")[0]), 3)
        self.assertEqual(flow.degrade_payload(self.github.issues[0])["reason"], "main-red")
        self.assertEqual(self.state.get_cursor(REPO), 20)
        route, _ = flow.choose_route(self.github, REPO, "main", self.state, {}, BOT,
                                     direct=False, dependabot=False, flow_v2=True, decision=None, reasons=[])
        self.assertEqual(route, "manual")

        self.command("watch", "--resume", f"{REPO}#7")
        self.assertIsNotNone(self.state.get_narrowed(REPO))
        self.command("watch", "--resume", REPO)
        self.assertIsNone(self.state.get_narrowed(REPO))
        self.once()
        self.assertIsNone(self.state.get_narrowed(REPO))
        self.assertEqual(len(self.effects("issue")), 1)

    def test_default_off_leaves_main_red_to_degrade_workflow(self):
        self.github.runs[20] = ci(20, "push")

        self.once()

        self.assertIsNone(self.state.get_run(REPO, 20, 1))
        self.assertIsNone(self.state.get_narrowed(REPO))
        self.assertEqual(self.github.requests, [])

    def test_narrowing_and_queue_close_survive_evidence_api_failure(self):
        self.config["repos"][REPO]["watchMainRed"] = True
        self.state.set_cursor(REPO, 19)
        self.github.add_pr(7)
        self.github.runs[20] = ci(20, "push")

        def unavailable(method, path):
            if path.startswith(f"repos/{REPO}/commits/{MAIN}/pulls"):
                raise ApiError("cannot read associated PR")

        self.github.fail = unavailable
        self.assertEqual(self.once(), 1)

        self.assertTrue(self.state.get_narrowed(REPO))
        self.assertEqual(self.effects("close"), [("close", 7, True)])
        self.assert_paused(self.state.get_run(REPO, 20, 1))
        self.assertEqual(self.state.get_cursor(REPO), 19)
        self.github.fail = None
        self.once()
        self.assertEqual(self.state.get_cursor(REPO), 20)
        self.assertEqual(len(self.effects("issue")), 1)

    def test_main_config_pause_notifies_without_closing_the_repo_queue(self):
        self.config["repos"][REPO].update(watchMainRed=True, notify="repo-owner")
        self.state.set_cursor(REPO, 19)
        self.github.add_pr(7)
        self.github.runs[20] = ci(20, "push", conclusion="startup_failure")

        self.once()

        self.assert_paused(self.state.get_run(REPO, 20, 1))
        self.assertIsNone(self.state.get_narrowed(REPO))
        self.assertEqual(self.effects("close"), [])
        self.assertEqual(self.effects("issue"), [])
        self.assertEqual(len(self.effects("desktop")), 1)
        self.assertEqual([notice[1] for notice in self.effects("relay")], ["repo-owner"])


class TimeoutAndFailureTest(WatchTestCase):
    def test_report_failure_lock_timeout_reports_unsaved_failure(self):
        with WatchState(self.directory).lock(), mock.patch("orchestrator.state.time.monotonic", side_effect=[0, 31]):
            result = self.command("report-failure", "--repo", REPO, "--pr", "7",
                                  "--stage", "open-pr", "--detail", "GitHub refused")

        self.assertNotEqual(result, 0)
        self.assertIn("失败未记录", self.errors.getvalue())
        self.assertIn("重试", self.errors.getvalue())
        self.assertIsNone(self.state.get_pr(REPO, 7))
        self.assertEqual(self.state.pending_notifications(), [])

    def test_report_failure_waits_for_lock_and_records_after_release(self):
        other = ExitStack()
        with other:
            other.enter_context(WatchState(self.directory).lock())
            with mock.patch("orchestrator.state.time.sleep", side_effect=lambda _: other.close()) as sleep:
                result = self.command("report-failure", "--repo", REPO, "--pr", "7",
                                      "--stage", "open-pr", "--detail", "GitHub refused")

        sleep.assert_called_once()
        self.assertEqual(result, 0)
        self.assert_paused(self.state.get_pr(REPO, 7))
        self.assertEqual(len(self.state.pending_notifications()), 1)

    def test_automerge_over_one_hour_is_closed_and_paused(self):
        self.tracked_pr(since=OLD)
        trace = self.trace_disable_credentials()

        self.once()

        self.assert_paused(self.state.get_pr(REPO, 7))
        self.assertEqual(len(self.effects("close")), 1)
        self.assertEqual(len(self.effects("desktop")), 1)
        self.assertEqual(len(self.effects("relay")), 1)
        self.assertEqual(trace.mock_calls, [mock.call.mint(REPO, WATCH_PERMISSIONS),
                                           *self.disable_credential_calls(), mock.call.revoke("watch-token")])

    def test_pr_running_ci_timeout_pauses_pr_and_attempt(self):
        self.tracked_pr()
        self.github.runs[10] = ci(10, conclusion=None, started=OLD)

        self.once()

        self.assert_paused(self.state.get_run(REPO, 10, 1))
        self.assert_paused(self.state.get_pr(REPO, 7))
        self.assertEqual(len(self.effects("desktop")), 1)
        self.assertIsNone(self.github.prs[7]["auto_merge"])
        self.assertEqual(len(self.effects("relay")), 1)

    def test_main_running_ci_timeout_pauses_attempt_without_narrowing(self):
        self.config["repos"][REPO]["watchMainRed"] = True
        self.state.set_cursor(REPO, 9)
        self.github.runs[10] = ci(10, "push", conclusion=None, started=OLD)

        self.once()

        self.assert_paused(self.state.get_run(REPO, 10, 1))
        self.assertIsNone(self.state.get_narrowed(REPO))
        self.assertEqual(len(self.effects("desktop")), 1)

    def test_report_failure_records_pause_and_queues_delivery(self):
        self.github.add_pr(7)
        self.assertEqual(self.command("report-failure", "--repo", REPO, "--pr", "7",
                                      "--stage", "open-pr", "--detail", "GitHub refused"), 0)
        self.assert_paused(self.state.get_pr(REPO, 7))
        self.assertEqual(len(self.state.pending_notifications()), 1)
        self.mint.assert_not_called()

        self.once()

        self.assertEqual(len(self.effects("desktop")), 1)
        self.assertIn("GitHub refused", self.github.comments[7][0]["body"])

    def test_api_error_records_pause_and_does_not_escape(self):
        self.tracked_pr()
        self.github.fail = lambda method, path: self.fail_status_read(path)
        self.fail_desktop = True

        self.once()

        self.assert_paused(self.state.get_pr(REPO, 7))
        self.assertEqual(len(self.state.pending_notifications()), 1)

    @staticmethod
    def fail_status_read(path):
        if path.split("?")[0].endswith(("/status", "/statuses")):
            raise ApiError("cannot read GitHub status")

    def test_comment_failure_retries_only_comment(self):
        self.tracked_pr(since=OLD)

        def fail_comment(method, path):
            if method == "POST" and path.endswith("/comments"):
                raise ApiError("temporarily unavailable")

        self.github.fail = fail_comment
        self.once()
        self.assertEqual(len(self.state.pending_notifications()), 1)
        self.assertEqual(len(self.effects("desktop")), 1)
        self.github.fail = None
        self.once()
        self.assertEqual(len(self.effects("desktop")), 1)
        self.assertEqual(len(self.effects("comment")), 1)
        self.assertEqual(self.state.pending_notifications(), [])

    def test_token_error_defers_queue_close_until_github_is_reachable(self):
        self.tracked_pr()
        self.mint.side_effect = cli.tokens.TokenError("could not mint")

        self.once()

        self.assert_paused(self.state.get_pr(REPO, 7))
        self.assertTrue(self.github.prs[7]["auto_merge"])
        self.assertFalse(self.effects("close"))
        self.mint.side_effect = None
        self.once()
        self.assertEqual(self.effects("close"), [("close", 7, False)])

    def test_open_pr_failure_without_a_pr_still_sends_a_desktop_notice(self):
        self.command("report-failure", "--repo", REPO, "--pr", "0",
                     "--stage", "open-pr", "--detail", "no PR was created")
        self.once()

        self.assert_paused(self.state.get_pr(REPO, 0))
        self.assertEqual(len(self.effects("desktop")), 1)
        self.assertEqual(self.github.requests, [])

    def test_report_failure_closes_a_tracked_queue(self):
        self.tracked_pr()
        self.command("report-failure", "--repo", REPO, "--pr", "7",
                     "--stage", "push", "--detail", "the session failed")
        self.assert_paused(self.state.get_pr(REPO, 7))
        self.assertEqual(self.github.requests, [])

        self.once()

        self.assertIsNone(self.github.prs[7]["auto_merge"])
        self.assertEqual(self.state.get_pr(REPO, 7)["pause"]["reason"], "push")
        self.assertEqual(len(self.effects("desktop")), 1)


class FreezeTest(WatchTestCase):
    def test_watch_controls_report_lock_timeout_without_changing_state(self):
        self.state.set_frozen(True)
        self.state.narrow_repo(REPO, "main-red", "20:1")
        for flags in [("--freeze",), ("--unfreeze",), ("--resume", REPO)]:
            with self.subTest(flags=flags), WatchState(self.directory).lock(), \
                    mock.patch("orchestrator.state.time.monotonic", side_effect=[0, 31]):
                self.errors.seek(0)
                self.errors.truncate()
                self.assertNotEqual(self.command("watch", *flags), 0)
                self.assertIn("未生效", self.errors.getvalue())
                self.assertIn(flags[0], self.errors.getvalue())
        self.assertTrue(self.state.is_frozen())
        self.assertIsNotNone(self.state.get_narrowed(REPO))
        self.mint.assert_not_called()

    def test_freeze_waits_for_lock_before_closing_queue(self):
        self.tracked_pr()
        other = ExitStack()
        with other:
            other.enter_context(WatchState(self.directory).lock())
            with mock.patch("orchestrator.state.time.sleep", side_effect=lambda _: other.close()) as sleep:
                result = self.command("watch", "--freeze")

        sleep.assert_called_once()
        self.assertEqual(result, 0)
        self.assertTrue(self.state.is_frozen())
        self.assertIsNone(self.github.prs[7]["auto_merge"])

    def test_interrupted_freeze_wait_reports_failure(self):
        with WatchState(self.directory).lock(), mock.patch("orchestrator.state.time.sleep", side_effect=KeyboardInterrupt):
            result = self.command("watch", "--freeze")

        self.assertNotEqual(result, 0)
        self.assertIn("中断", self.errors.getvalue())
        self.assertFalse(self.state.is_frozen())

    def test_frozen_watch_only_closes_queued_prs(self):
        self.tracked_pr()
        self.github.add_pr(8)
        self.config["repos"][REPO]["watchMainRed"] = True
        self.github.runs[20] = ci(20, "push")
        self.state.enqueue_notification("author", "pending notice", "notice-key")
        trace = self.trace_disable_credentials()

        self.command("watch", "--freeze")
        self.once()
        self.once()

        self.assertTrue(self.state.is_frozen())
        self.assertEqual(self.github.effects, [("close", 7, False), ("close", 8, False)])
        read = mock.call.mint(REPO, WATCH_PERMISSIONS)
        revoke = mock.call.revoke("watch-token")
        self.assertEqual(trace.mock_calls, [read, *self.disable_credential_calls(),
                                           *self.disable_credential_calls(), revoke, read, revoke, read, revoke])
        self.assertIsNone(self.state.get_cursor(REPO))
        self.assertEqual(len(self.state.pending_notifications()), 1)
        self.assertTrue(self.state.heartbeat_is_fresh(15))
        self.command("watch", "--unfreeze")
        self.assertFalse(self.state.is_frozen())

    def test_held_lock_exits_without_calls_or_state_changes(self):
        other = WatchState(self.directory)
        with other.lock():
            self.assertEqual(self.command("watch", "--once"), 0)

        self.assertEqual(self.state.read()["heartbeat"], None)
        self.mint.assert_not_called()

    def test_daemon_skips_locked_round_and_retries_after_poll_interval(self):
        with ExitStack() as other:
            other.enter_context(WatchState(self.directory).lock())

            def sleep(seconds):
                self.assertEqual(seconds, 300)
                if self.state.read()["heartbeat"] is not None:
                    raise KeyboardInterrupt
                self.mint.assert_not_called()
                other.close()

            with mock.patch.object(cli.time, "sleep", side_effect=sleep) as wait:
                self.assertEqual(self.command("watch"), 0)

        self.assertEqual(wait.call_args_list, [mock.call(300), mock.call(300)])
        self.assertTrue(self.state.heartbeat_is_fresh(15))

    def test_unfreeze_and_repository_resume_can_be_requested_together(self):
        self.state.set_frozen(True)
        self.state.narrow_repo(REPO, "main-red", "20:1")

        self.assertEqual(self.command("watch", "--unfreeze", "--resume", REPO), 0)

        self.assertFalse(self.state.is_frozen())
        self.assertIsNone(self.state.get_narrowed(REPO))

    def test_enabling_checks_hold_under_the_same_lock_as_watch(self):
        self.tracked_pr(state="automerge-requested")
        io = LOCAL_IO("watch-token", None, automerge_target=(self.state, REPO, 7))

        def enable(path, method, body, **kwargs):
            with self.assertRaises(StateLockedError), WatchState(self.directory).lock():
                self.fail("watch acquired the lock during the GitHub mutation")
            return {"data": {"enablePullRequestAutoMerge": {"pullRequest": {"id": "node-7"}}}}

        with mock.patch.object(io, "api", side_effect=enable) as api:
            self.assertIsNone(io.enable_automerge("node-7", HEAD, "squash"))
            self.state.set_frozen(True)
            with self.assertRaises(flow.Stop):
                io.enable_automerge("node-7", HEAD, "squash")
        api.assert_called_once()


class ResumeTest(WatchTestCase):
    def test_main_actions_paging_stops_at_cursor_and_processes_new_runs_in_order(self):
        self.config["repos"][REPO]["watchMainRed"] = True
        self.state.set_cursor(REPO, 120)
        self.github.runs = {number: ci(number, "push", conclusion="success") for number in range(1, 232)}
        api = self.github.api

        def one_page_at_a_time(path, method="GET", body=None, **kwargs):
            if urlsplit(path).path == f"repos/{REPO}/actions/runs":
                self.assertIs(kwargs.get("paginate"), False)
            return api(path, method, body, **kwargs)

        with mock.patch.object(self.github, "api", side_effect=one_page_at_a_time):
            self.assertEqual(self.once(), 0)

        queries = [parse_qs(urlsplit(path).query) for method, path in self.github.requests
                   if method == "GET" and urlsplit(path).path == f"repos/{REPO}/actions/runs"]
        self.assertEqual([query["page"] for query in queries], [["1"], ["2"]])
        detail = [int(path.rsplit("/", 1)[1]) for method, path in self.github.requests
                  if method == "GET" and path.startswith(f"repos/{REPO}/actions/runs/")]
        self.assertEqual(detail, list(range(121, 232)))
        self.assertEqual(self.state.get_cursor(REPO), 231)

    def test_processes_all_runs_beyond_cursor_and_finishes_merge_once(self):
        self.tracked_pr()
        self.github.add_pr(7, queued=False, merged=True)
        evidence_url = self.github.merge_evidence()
        self.config["repos"][REPO]["watchMainRed"] = True
        self.state.set_cursor(REPO, 20)
        self.github.runs.update({
            19: ci(19, "push"),
            21: ci(21, "push", conclusion="success"),
            22: ci(22, "push", attempt=2, conclusion="success"),
        })

        self.once()
        self.once()

        self.assertIsNone(self.state.get_run(REPO, 19, 1))
        self.assertEqual(self.state.get_cursor(REPO), 22)
        self.assertEqual(self.state.get_run(REPO, 21, 1)["validatedHeadOid"], HEAD)
        self.assertEqual(self.state.get_run(REPO, 21, 1)["evidenceUrl"], evidence_url)
        self.assertEqual(self.state.get_run(REPO, 22, 2)["classification"], "env")
        self.assertEqual(self.state.get_pr(REPO, 7)["state"], "post-merge-done")
        self.assertEqual([e[1] for e in self.effects("issue")], ["autopilot:followup"])
        self.assertEqual(len(self.effects("label")), 1)

    def test_reconciles_enable_intent_after_remote_queue_was_opened(self):
        self.tracked_pr(state="automerge-requested")
        key = f"{REPO}:7:{HEAD}:automerge"
        self.state.intend(key, dict(action="enable-automerge", repo=REPO, pr=7,
                                    validatedHeadOid=HEAD, validatedBaseOid=BASE, since=RECENT, notify="author"))

        self.once()

        self.assertTrue(self.state.is_done(key))
        self.assertEqual(self.state.get_pr(REPO, 7)["state"], "automerge-on")
        self.assertEqual(self.github.effects, [])
        self.assertFalse(any(path == "graphql" and method == "POST" for method, path in self.github.requests))

    def test_crash_after_issue_creation_resumes_narrowing_with_same_issue(self):
        self.config["repos"][REPO]["watchMainRed"] = True
        self.state.set_cursor(REPO, 19)
        self.github.add_pr(7)
        self.github.runs[20] = ci(20, "push")

        def crash(effect):
            if effect[0] == "issue":
                raise SimulatedCrash

        self.github.after_write = crash
        with self.assertRaises(SimulatedCrash):
            self.once()
        self.assertEqual(self.state.get_cursor(REPO), 19)
        self.assertTrue(self.state.get_narrowed(REPO))
        self.github.after_write = None
        self.once()
        self.once()

        self.assertEqual(self.state.get_cursor(REPO), 20)
        self.assertEqual(len(self.effects("close")), 1)
        self.assertEqual(len(self.effects("issue")), 1)
        self.assertEqual(len(self.effects("narrow-pr")), 1)
        self.assertEqual(len(self.effects("desktop")), 1)

    def test_rerun_intent_is_reconciled_when_attempt_advances_after_a_crash(self):
        self.tracked_pr()
        self.github.runs[10] = ci(10)
        self.github.jobs[10] = jobs("setup")

        def crash(effect):
            if effect[0] == "rerun":
                raise SimulatedCrash

        self.github.after_write = crash
        with self.assertRaises(SimulatedCrash):
            self.once()
        self.github.after_write = None
        self.github.runs[10].update(status="completed", conclusion="success")
        self.once()

        self.assertEqual(len(self.effects("rerun")), 1)
        self.assertEqual(self.state.get_run(REPO, 10, 1)["state"], "rerun")
        self.assertEqual(self.state.get_run(REPO, 10, 2)["state"], "green")
        self.assertEqual(self.state.get_run(REPO, 10, 2)["classification"], "env")

    def test_resume_pr_clears_pause_without_restoring_an_automerge_request(self):
        self.tracked_pr(since=OLD)
        self.once()
        self.assert_paused(self.state.get_pr(REPO, 7))

        self.command("watch", "--resume", f"{REPO}#7")

        self.assertIsNone(self.state.get_pr(REPO, 7)["pause"])
        self.assertIsNone(self.github.prs[7]["auto_merge"])

    def test_resume_pr_restores_pending_inspection_state_after_disable_error(self):
        self.tracked_pr(state="automerge-requested", since=OLD)
        self.github.fail = reject_disable
        self.assertEqual(self.once(), 1)
        self.assert_paused(self.state.get_pr(REPO, 7))

        self.assertEqual(self.command("watch", "--resume", f"{REPO}#7"), 0)

        record = self.state.get_pr(REPO, 7)
        self.assertEqual(record["state"], "automerge-requested")
        self.assertIsNone(record["pause"])
        self.assertIsNone(record["resumeState"])
        self.assertTrue(self.github.prs[7]["auto_merge"])

        self.github.fail = None
        self.assertEqual(self.once(), 0)
        self.assert_paused(self.state.get_pr(REPO, 7))
        self.assertIsNone(self.github.prs[7]["auto_merge"])

    def test_successful_direct_run_clears_an_earlier_pause(self):
        self.state.pause_pr(REPO, 7, "previous failure", "rerun")
        with mock.patch.object(flow, "run", return_value={"result": "merged", "route": "direct"}):
            self.assertEqual(self.command("run", "--repo", REPO, "--pr", "7", "--json"), 0)

        record = self.state.get_pr(REPO, 7)
        self.assertEqual(record["state"], "post-merge-done")
        self.assertIsNone(record["pause"])

    def test_interrupted_enable_supersedes_an_old_pause_only_with_a_new_validation(self):
        self.github.add_pr(7, queued=False)
        self.state.set_pr(REPO, 7, validatedHeadOid=BASE, since=OLD)
        self.state.pause_pr(REPO, 7, "previous failure", "rerun")
        key = f"{REPO}:7:{HEAD}:automerge"
        self.state.intend(key, dict(action="enable-automerge", repo=REPO, pr=7, validatedHeadOid=HEAD,
                                    validatedBaseOid=BASE, since=RECENT, notify="author"))

        self.once()

        self.assertIn(key, self.state.pending_intents())
        record = self.state.get_pr(REPO, 7)
        self.assertEqual(record["state"], "automerge-requested")
        self.assertEqual(record["validatedHeadOid"], HEAD)
        self.assertEqual(self.effects("close"), [])

    def test_followup_issue_written_before_crash_is_reconciled(self):
        self.tracked_pr()
        self.github.add_pr(7, queued=False, merged=True)
        self.github.merge_evidence()

        def crash(effect):
            if effect[0] == "issue":
                raise SimulatedCrash

        self.github.after_write = crash
        with self.assertRaises(SimulatedCrash):
            self.once()
        self.github.after_write = None
        self.once()

        self.assertEqual(self.state.get_pr(REPO, 7)["state"], "post-merge-done")
        self.assertEqual([e[1] for e in self.effects("issue")], ["autopilot:followup"])
        self.assertEqual(self.state.pending_intents(), {})

    def test_in_progress_run_revisited_after_cursor_passes_it(self):
        self.config["repos"][REPO]["watchMainRed"] = True
        self.state.set_cursor(REPO, 19)
        self.github.runs = {20: ci(20, "push", conclusion=None), 21: ci(21, "push", conclusion="success")}
        self.once()
        self.assertEqual(self.state.get_cursor(REPO), 21)
        self.assertEqual(self.state.get_run(REPO, 20, 1)["state"], "pending")

        self.github.runs[20].update(conclusion="failure", status="completed")
        self.github.unlisted_runs.add(20)
        self.once()

        self.assertEqual(self.state.get_cursor(REPO), 21)
        self.assertIsNotNone(self.state.get_narrowed(REPO))
        self.assertEqual(self.state.get_run(REPO, 20, 1)["state"], "red-classified")

    def test_crash_after_notifying_in_github_does_not_repeat_either_channel(self):
        self.tracked_pr(since=OLD)

        def crash(effect):
            if effect[0] == "comment":
                raise SimulatedCrash

        self.github.after_write = crash
        with self.assertRaises(SimulatedCrash):
            self.once()
        self.github.after_write = None
        self.once()

        self.assertEqual(len(self.effects("desktop")), 1)
        self.assertEqual(len(self.effects("comment")), 1)
        self.assertEqual(self.state.pending_notifications(), [])

    def test_narrow_pr_recovers_both_branch_push_and_pr_creation(self):
        io = LOCAL_IO("watch-token", None)
        branch = "autopilot/narrow-100"

        def git(*args):
            if args[0] == "status":
                return " M .agent/config.json"
            if args[0] == "rev-parse":
                return PATCH
            if args[0] == "push":
                self.assertNotIn("--force", args)
                self.github.narrow_refs[branch] = PATCH
                self.github.effect("narrow-push", branch)
            return ""

        def process(args, **kwargs):
            if args[0] == "python3" and "narrow" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return self.fake_process(args, **kwargs)

        def narrow():
            return io.open_narrow_pr(REPO, "main", None, 100, "reason", watch_state=self.state)

        with mock.patch.object(io, "api", side_effect=self.github.api), \
                mock.patch.object(io, "bot_identity", return_value=BOT), \
                mock.patch.object(cli, "find_project_config", return_value=__file__), \
                mock.patch.object(cli.Work, "git", side_effect=git), \
                mock.patch.object(subprocess, "run", side_effect=process):
            for effect in ("narrow-push", "narrow-pr"):
                def crash(event):
                    if event[0] == effect:
                        raise SimulatedCrash
                self.github.after_write = crash
                with self.state.lock(), self.assertRaises(SimulatedCrash):
                    narrow()
            self.github.after_write = None
            with self.state.lock():
                self.assertEqual(narrow(), f"https://github.com/{REPO}/pull/900")

        self.assertEqual(len(self.effects("narrow-push")), 1)
        self.assertEqual(len(self.effects("narrow-pr")), 1)
        self.assertEqual(self.state.pending_intents(), {})


if __name__ == "__main__":
    unittest.main()
