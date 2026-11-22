"""Release engine tests: all GitHub, clone and command I/O is fake."""
import base64
import contextlib
import copy
import hashlib
import io as text_io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admission import decide
from orchestrator.flow import block, rest_comment
from pipeline import engine, model

REPO = "haigeerlab/autopilot-sandbox"
COMMIT = "a" * 40
NOW = "2026-11-22T01:00:00Z"
BOT = {"id": 123, "login": "autopilot[bot]", "app_slug": "autopilot"}


def authored(body, **changes):
    item = {"body": body, "user": {"id": BOT["id"], "type": "Bot"},
            "performed_via_github_app": {"slug": BOT["app_slug"]},
            "created_at": NOW, "updated_at": NOW}
    item.update(changes)
    return item


def step(identifier="publish", **changes):
    item = {"id": identifier, "at": "release", "run": "publish", "by": "ai",
            "auto_when": "release: auto-confirm", "stop_if": None, "evidence": ["stdout"]}
    item.update(changes)
    return item


class FakeWork:
    def __init__(self, io):
        self.io = io

    def run_release(self, command, version, repo, issue):
        self.io.ran.append((command, version, repo, issue))
        return copy.deepcopy(self.io.command_result)

    def file_evidence(self, path):
        content = self.io.files.get(path)
        if content is None:
            return None
        return {"path": path, "sha256": hashlib.sha256(content).hexdigest()}


class FakeIO:
    def __init__(self, steps=None):
        self.pipeline = {"template": "sandbox", "rollback": True,
                         "steps": steps if steps is not None else [step()]}
        self.contents = {".github/autopr/takeover.yml": "release: auto\n"}
        self.calls, self.comments, self.ran, self.clones, self.events = [], [], [], [], []
        self.issue = None
        self.command_result = {"exit": 0, "output": "ok\n"}
        self.files = {}
        self.commit = COMMIT
        self.label_exists = True

    def bot_identity(self):
        return BOT

    def api_optional(self, path):
        self.calls.append(("GET", path, None))
        if "/labels/" in path:
            return {"name": "autopilot:release"} if self.label_exists else None
        name = path.split("/contents/", 1)[1].split("?", 1)[0]
        text = self.contents.get(name)
        return {"content": base64.b64encode(text.encode()).decode()} if text is not None else None

    def api(self, path, method="GET", body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            if path == "repos/" + REPO:
                return {"default_branch": "main"}
            if "/commits/" in path:
                return {"sha": self.commit}
            if "/comments" in path:
                return copy.deepcopy(self.comments)
        if method == "POST" and path.endswith("/labels"):
            self.label_exists = True
            return body
        if method == "POST" and path.endswith("/issues"):
            self.issue = authored(body["body"], number=9, state="open",
                                  html_url="https://github.com/" + REPO + "/issues/9")
            return copy.deepcopy(self.issue)
        if method == "POST" and path.endswith("/comments"):
            self.comments.append(authored(body["body"], id=len(self.comments) + 1))
            return self.comments[-1]
        if method == "PATCH":
            self.issue.update(body)
            return copy.deepcopy(self.issue)
        raise AssertionError((method, path, body))

    def release_issue(self, repo, number):
        return copy.deepcopy(self.issue)

    def resolve_pipeline(self, repo, ref, notes):
        self.calls.append(("GET", "pipeline@" + ref, None))
        return copy.deepcopy(self.pipeline)

    @contextlib.contextmanager
    def release_checkout(self, repo, base_ref, commit):
        self.clones.append((repo, base_ref, commit))
        yield FakeWork(self)

    def now(self):
        return NOW

    def ledger(self, event):
        self.events.append(event)

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def records(self):
        return [engine.payload(c["body"], "autopilot-release-step") for c in self.comments
                if engine.payload(c["body"], "autopilot-release-step") is not None]


def run(io, **kwargs):
    if "resume" not in kwargs:
        kwargs.setdefault("version", "1.2.3")
    return engine.run(io, REPO, **kwargs)


class ReleaseLevelTest(unittest.TestCase):
    def level(self, files):
        io = FakeIO()
        io.contents = files
        notes = []
        return engine.read_release_level(io, REPO, COMMIT, notes), notes

    def test_spec_guard_uses_project_release_only(self):
        for level in model.LEVELS:
            config = {"takeover": {"release": level}, "moduleTakeover": {"m": {"release": "auto"}}}
            actual, notes = self.level({"spec/CAPABILITY-MAP.md": "", ".agent/config.json": json.dumps(config),
                                        ".github/autopr/takeover.yml": "release: auto"})
            self.assertEqual(actual, level)
            self.assertEqual(notes, [])

    def test_presets_and_defaults(self):
        for value in ("none", "plan", "merge", {}, {"release": "invalid"}, "invalid", "human", "prepare", "auto-confirm", "auto", None, []):
            with self.subTest(value=value):
                actual, notes = self.level({"spec/CAPABILITY-MAP.md": "", ".agent/config.json":
                                            json.dumps({"takeover": value})})
                self.assertEqual(actual, "human")
        self.assertEqual(self.level({})[0], "human")
        self.assertEqual(self.level({"spec/CAPABILITY-MAP.md": ""})[0], "human")

    def test_invalid_spec_guard_root_has_a_chinese_note(self):
        for config in ("[]", "null", "42"):
            actual, notes = self.level({"spec/CAPABILITY-MAP.md": "", ".agent/config.json": config})
            self.assertEqual(actual, "human")
            self.assertTrue(notes)

    def test_yaml_levels_and_unreadable_notes(self):
        for level in model.LEVELS:
            self.assertEqual(self.level({".github/autopr/takeover.yml": "release: " + level})[0], level)
        for text in ("release: invalid", "[]", "release: [", "release: null"):
            actual, notes = self.level({".github/autopr/takeover.yml": text})
            self.assertEqual(actual, "human")
            self.assertTrue(notes)
        actual, notes = self.level({"spec/CAPABILITY-MAP.md": "", ".agent/config.json": "{"})
        self.assertEqual(actual, "human")
        self.assertTrue(notes)
        self.assertNotIn("合并", "".join(notes))


class DecisionTest(unittest.TestCase):
    def test_waiting_matrix(self):
        for changes, reason in (({"run": "ask: 测试人员", "by": "human"}, "等 测试人员 确认"),
                                ({"by": "human"}, "这一步要你本人做：publish"),
                                ({"by": "external"}, "这一步由外部系统做：publish，做完告诉我"),
                                ({"auto_when": None}, "不写就永远等人")):
            state, text = engine.decision(step(**changes), "auto")
            self.assertEqual(state, "waiting")
            self.assertIn(reason, text)

    def test_ai_level_matrix(self):
        for current in model.LEVELS:
            for needed in model.LEVELS:
                state, reason = engine.decision(step(auto_when="release: " + needed), current)
                enough = model.LEVELS.index(current) >= model.LEVELS.index(needed)
                self.assertEqual(state, "execute" if enough else "waiting")
                if not enough:
                    self.assertEqual(reason, f"当前发布档位 {current} 低于这一步需要的 {needed}，要你确认后才做")


class ExecutionTest(unittest.TestCase):
    def test_success_evidence_blocks_summary_close_and_ledger(self):
        io = FakeIO([step("before", at="pre-merge"), step(evidence=["stdout", "url", "file"])])
        output = "https://example.com/first\nhttps://example.com/second\nFILE: dist/a.zip\n" + "\n".join(map(str, range(50)))
        io.command_result["output"] = output
        io.files["dist/a.zip"] = b"artifact"
        result = run(io)
        self.assertEqual(result["code"], 0, result)
        record = io.records()[0]
        self.assertEqual(record["id"], "publish")
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["exit"], 0)
        self.assertIsNone(record["confirmed_words"])
        self.assertIsNone(record["reason"])
        self.assertEqual(record["evidence"]["stdout"], {"sha256": hashlib.sha256(output.encode()).hexdigest(),
                                                       "tail": list(map(str, range(10, 50)))})
        self.assertEqual(record["evidence"]["url"], "https://example.com/first")
        self.assertEqual(record["evidence"]["file"], {"path": "dist/a.zip", "sha256": hashlib.sha256(b"artifact").hexdigest()})
        self.assertIn("<details>", io.comments[0]["body"])
        start = engine.payload(io.issue["body"], "autopilot-release")
        self.assertEqual(start, {"version": "1.2.3", "commit": COMMIT, "template": "sandbox", "level": "auto",
                                 "pipeline_sha256": hashlib.sha256(json.dumps(io.pipeline, sort_keys=True).encode()).hexdigest(),
                                 "steps": ["publish"]})
        opened = next(c[2] for c in io.writes() if c[1].endswith("/issues"))
        self.assertEqual(opened["title"], "autopilot 发布 " + REPO + " v1.2.3")
        self.assertEqual(opened["labels"], ["autopilot:release"])
        self.assertIn("publish：done", io.comments[-1]["body"])
        self.assertEqual(io.issue["state"], "closed")
        self.assertEqual(io.events, [{"event": "release_done", "repo": REPO, "version": "1.2.3", "issue": 9, "at": NOW}])
        self.assertEqual(io.clones, [(REPO, "main", COMMIT)])
        self.assertEqual(io.ran, [("publish", "1.2.3", REPO, 9)])

    def test_missing_release_label_is_created_before_opening_issue(self):
        io = FakeIO()
        io.label_exists = False
        result = run(io)
        self.assertEqual(result["code"], 0)
        self.assertEqual(io.writes()[0][1], "repos/" + REPO + "/labels")
        self.assertEqual(io.writes()[0][2]["name"], "autopilot:release")
        self.assertTrue(io.label_exists)

    def test_resolved_segment_order_and_pre_merge_skip(self):
        config = {"extends": "sandbox", "remove": [s["id"] for s in model.load_template("sandbox")["steps"]],
                  "add": [dict(step(segment, at=segment, run=segment), stop_if="exit != 0")
                          for segment in reversed(model.SEGMENTS)]}
        io = FakeIO()
        io.pipeline = model.resolve(config)
        self.assertEqual(run(io)["code"], 0)
        self.assertEqual([command[0] for command in io.ran], list(model.SEGMENTS[1:]))
        self.assertEqual(len(io.clones), 1)

    def test_nonzero_and_missing_evidence_stop(self):
        for exit_code, evidence, stop_if, missing in ((7, [], None, "退出码 7"),
                (0, ["url"], None, "url"), (0, ["file"], None, "file"),
                (0, [], "missing: url", "url"), (0, [], "missing: file", "file"),
                (124, ["stdout"], None, "退出码 124")):
            with self.subTest(exit_code=exit_code, evidence=evidence, stop_if=stop_if):
                io = FakeIO([step(evidence=evidence, stop_if=stop_if), step("later")])
                io.command_result["exit"] = exit_code
                result = run(io)
                self.assertEqual((result["code"], result["result"]), (1, "stopped"))
                self.assertIn(missing, result["reason"])
                self.assertEqual(io.records()[0]["status"], "stopped")
                self.assertEqual(len(io.ran), 1)
                self.assertEqual(io.events, [])
                self.assertEqual(io.issue["state"], "open")

    def test_file_uses_first_marker_only(self):
        io = FakeIO([step(evidence=["file"])])
        io.command_result["output"] = "FILE: missing\nFILE: valid\n"
        io.files["valid"] = b"ok"
        self.assertEqual(run(io)["result"], "stopped")

    def test_waiting_does_not_clone_or_run(self):
        io = FakeIO([step(run="ask: 你", by="human")])
        result = run(io)
        self.assertEqual((result["code"], result["result"]), (1, "waiting"))
        self.assertEqual(io.ran, [])
        self.assertEqual(io.clones, [])
        self.assertEqual(io.records()[0]["reason"], "等 你 确认")
        self.assertIsNone(io.records()[0]["exit"])
        self.assertIn("--resume 9", result["resume_command"])
        self.assertIn("issues/9", result["url"])

    def test_dry_run_is_read_only_and_lists_every_step(self):
        io = FakeIO([step(), step("confirm", run="ask: 你", by="human"), step("later")])
        result = run(io, dry_run=True)
        self.assertEqual((result["code"], result["result"]), (0, "dry-run"))
        self.assertEqual([p["status"] for p in result["plan"]], ["execute", "waiting", "execute"])
        self.assertEqual(result["level"], "auto")
        self.assertEqual((io.writes(), io.ran, io.clones, io.events), ([], [], [], []))


class ResumeTest(unittest.TestCase):
    def waiting(self):
        io = FakeIO([step("first"), step("confirm", run="ask: 你", by="human"), step("last")])
        self.assertEqual(run(io)["result"], "waiting")
        return io

    def test_waiting_without_confirm_reads_only(self):
        io = self.waiting()
        io.calls.clear()
        result = run(io, resume=9)
        self.assertEqual((result["code"], result["result"]), (1, "waiting"))
        self.assertIn("还在等：等 你 确认", result["reason"])
        self.assertEqual(io.writes(), [])
        self.assertEqual(len(io.ran), 1)

    def test_confirm_preserves_words_and_skips_done_on_original_commit(self):
        io = self.waiting()
        io.commit = "b" * 40
        words = ' 已核验，批准发布 "1.2.3"\n原样记录 --> '
        result = run(io, resume=9, confirm=words)
        self.assertEqual(result["code"], 0, result)
        self.assertEqual([r["status"] for r in io.records()], ["done", "waiting", "done", "done"])
        self.assertEqual(io.records()[2]["confirmed_words"], words)
        self.assertEqual([r[0] for r in io.ran], ["publish", "publish"])
        self.assertEqual(io.clones[-1][-1], COMMIT)

    def test_confirm_ai_executes_then_continues_in_one_original_checkout(self):
        for condition in ("release: auto-confirm", None):
            with self.subTest(auto_when=condition):
                io = FakeIO([step(auto_when=condition),
                             step("later", run="verify", auto_when="release: prepare")])
                io.contents[".github/autopr/takeover.yml"] = "release: prepare"
                self.assertEqual(run(io)["result"], "waiting")
                self.assertEqual((io.ran, io.clones), ([], []))
                io.commit = "b" * 40
                words = ' 可以执行发布 "1.2.3"\n原样记录 --> '
                result = run(io, resume=9, confirm=words)
                self.assertEqual((result["code"], result["result"]), (0, "done"))
                self.assertEqual(io.ran, [("publish", "1.2.3", REPO, 9),
                                          ("verify", "1.2.3", REPO, 9)])
                self.assertEqual(io.clones, [(REPO, "main", COMMIT)])
                records = io.records()
                self.assertEqual([r["status"] for r in records], ["waiting", "done", "done"])
                self.assertEqual(records[1]["confirmed_words"], words)
                self.assertEqual(records[1]["exit"], 0)
                self.assertIn("stdout", records[1]["evidence"])
                self.assertIsNone(records[2]["confirmed_words"])

    def test_confirm_ai_execution_failure_stops_before_next_step(self):
        io = FakeIO([step(), step("later", run="verify", auto_when="release: prepare")])
        io.contents[".github/autopr/takeover.yml"] = "release: prepare"
        self.assertEqual(run(io)["result"], "waiting")
        io.command_result = {"exit": 2, "output": "publish failed\n"}
        words = " 批准执行\n发布 "
        result = run(io, resume=9, confirm=words)
        self.assertEqual((result["code"], result["result"]), (1, "stopped"))
        self.assertEqual(io.ran, [("publish", "1.2.3", REPO, 9)])
        self.assertEqual(io.clones, [(REPO, "main", COMMIT)])
        record = io.records()[-1]
        self.assertEqual((record["id"], record["status"], record["exit"]), ("publish", "stopped", 2))
        self.assertEqual(record["confirmed_words"], words)
        self.assertEqual(record["evidence"]["stdout"]["tail"], ["publish failed"])
        self.assertEqual(io.issue["state"], "open")
        self.assertEqual(io.events, [])

    def test_confirm_ask_does_not_execute_any_command(self):
        for by in ("ai", "human", "external"):
            with self.subTest(by=by):
                io = FakeIO([step(run="ask: 你", by=by)])
                self.assertEqual(run(io)["result"], "waiting")
                result = run(io, resume=9, confirm="已经核验")
                self.assertEqual((result["code"], result["result"]), (0, "done"))
                self.assertEqual((io.ran, io.clones), ([], []))
                record = io.records()[-1]
                self.assertEqual(record["status"], "done")
                self.assertEqual(record["confirmed_words"], "已经核验")
                self.assertIsNone(record["exit"])

    def test_confirm_human_or_external_only_records_completion(self):
        for by in ("human", "external"):
            with self.subTest(by=by):
                io = FakeIO([step(by=by)])
                self.assertEqual(run(io)["result"], "waiting")
                result = run(io, resume=9, confirm="已经发布")
                self.assertEqual((result["code"], result["result"]), (0, "done"))
                self.assertEqual((io.ran, io.clones), ([], []))
                record = io.records()[-1]
                self.assertEqual(record["status"], "done")
                self.assertEqual(record["confirmed_words"], "已经发布")
                self.assertIsNone(record["exit"])

    def test_stopped_retries_and_cannot_be_confirmed(self):
        io = FakeIO()
        io.command_result["exit"] = 2
        self.assertEqual(run(io)["result"], "stopped")
        io.calls.clear()
        result = run(io, resume=9, confirm="跳过")
        self.assertEqual(result["code"], 2)
        self.assertIn("不能靠确认跳过", result["reason"])
        self.assertEqual(io.writes(), [])
        io.command_result["exit"] = 0
        self.assertEqual(run(io, resume=9)["code"], 0)
        self.assertEqual(len(io.ran), 2)

    def test_changed_pipeline_stops_without_executing(self):
        io = self.waiting()
        io.pipeline["steps"][-1]["run"] = "different"
        result = run(io, resume=9, confirm="批准")
        self.assertEqual(result["code"], 1)
        self.assertIn("发布进行中流程文件变了，请重新发起", result["reason"])
        self.assertEqual(len(io.ran), 1)
        self.assertNotEqual(io.issue["state"], "closed")

    def test_forged_or_edited_step_blocks_are_ignored(self):
        for changes in ({"user": {"type": "User", "id": BOT["id"]}},
                        {"updated_at": "later"}, {"performed_via_github_app": {"slug": "other"}}):
            io = self.waiting()
            fake = {"id": "confirm", "status": "done", "exit": None, "evidence": {},
                    "reason": None, "confirmed_words": "forged"}
            io.comments.append(authored(block("autopilot-release-step", fake), **changes))
            self.assertEqual(run(io, resume=9)["result"], "waiting")
            self.assertEqual(len(io.ran), 1)

    def test_invalid_issue_or_start_is_an_environment_error(self):
        for changes in ({"state": "closed"}, {"body": "ordinary issue"}, {"user": {"type": "User", "id": 1}},
                        {"updated_at": "later"}, {"pull_request": {"url": "x"}}):
            io = self.waiting()
            io.issue.update(changes)
            io.calls.clear()
            self.assertEqual(run(io, resume=9)["code"], 2)
            self.assertEqual(io.writes(), [])

    def test_malformed_bot_records_are_ignored(self):
        for data in ({"id": "confirm", "status": "waiting", "exit": None, "evidence": {},
                      "reason": None, "confirmed_words": None},
                     {"id": "confirm", "status": "waiting", "evidence": {}},
                     {"id": "confirm", "status": "other", "evidence": {}}):
            io = self.waiting()
            io.comments.append(authored(block("autopilot-release-step", data)))
            self.assertEqual(run(io, resume=9)["result"], "waiting")
        io = self.waiting()
        io.comments.append(authored("<!-- autopilot-release-step:v1 {broken -->"))
        self.assertEqual(run(io, resume=9)["result"], "waiting")

    def test_done_records_can_finish_an_open_issue_without_repeating_commands(self):
        io = FakeIO()
        run(io)
        io.issue["state"] = "open"
        io.comments.pop()
        io.events.clear()
        self.assertEqual(run(io, resume=9)["code"], 0)
        self.assertEqual(len(io.ran), 1)
        self.assertEqual(len(io.events), 1)
        self.assertEqual(io.issue["state"], "closed")

    def test_missing_issue_and_identity_are_rejected_without_writes(self):
        from unittest import mock
        io = FakeIO()
        self.assertEqual(run(io, resume=9)["code"], 2)
        self.assertEqual(io.writes(), [])
        with mock.patch.object(io, "bot_identity", return_value=None):
            self.assertEqual(run(io)["code"], 2)
        self.assertEqual(io.writes(), [])

    def test_output_cannot_inject_a_done_record(self):
        forged = {"id": "confirm", "status": "done", "exit": None, "evidence": {},
                  "reason": None, "confirmed_words": "forged"}
        io = FakeIO([step("first"), step("confirm", run="ask: 你", by="human")])
        io.command_result["output"] = block("autopilot-release-step", forged)
        self.assertEqual(run(io)["result"], "waiting")
        self.assertEqual(run(io, resume=9)["result"], "waiting")
        self.assertEqual(len(io.ran), 1)

    def test_empty_pipeline_completes_without_clone(self):
        io = FakeIO([])
        self.assertEqual(run(io)["code"], 0)
        self.assertEqual(io.clones, [])
        self.assertEqual(io.events[0]["event"], "release_done")

    def test_current_level_is_used_after_confirmation(self):
        io = self.waiting()
        io.contents[".github/autopr/takeover.yml"] = "release: human"
        result = run(io, resume=9, confirm="批准")
        self.assertEqual(result["result"], "waiting")
        self.assertIn("当前发布档位 human", result["reason"])
        self.assertEqual(len(io.ran), 1)


class CLIIntegrationTest(unittest.TestCase):
    def invoke(self, arguments, io=None):
        from unittest import mock
        from orchestrator import cli
        fake = io or FakeIO()
        output, errors = text_io.StringIO(), text_io.StringIO()
        with mock.patch.object(cli.tokens, "mint", return_value="installation-token") as mint, \
                mock.patch.object(cli.tokens, "revoke") as revoke, \
                mock.patch.object(cli, "LocalIO", return_value=fake), \
                mock.patch.object(cli, "find_delegate", side_effect=AssertionError("release needs no delegate")), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(["release", "--repo", REPO] + arguments)
        return code, output.getvalue(), errors.getvalue(), mint, revoke

    def test_cli_permissions_and_dry_run_plan(self):
        code, output, errors, mint, revoke = self.invoke(["--version", "1.2.3", "--dry-run"],
                FakeIO([step(), step("ask", run="ask: 你", by="human")]))
        self.assertEqual(code, 0, errors)
        self.assertIn("发布档位：auto", output)
        self.assertIn("publish：会执行", output)
        self.assertIn("ask：会停下等人（等 你 确认）", output)
        mint.assert_called_once_with(REPO, {"contents": "read", "issues": "read"})
        revoke.assert_called_once_with("installation-token")

    def test_cli_waiting_prints_link_and_resume_example(self):
        code, output, errors, mint, revoke = self.invoke(["--version", "1.2.3"],
                FakeIO([step(run="ask: 你", by="human")]))
        self.assertEqual(code, 1, errors)
        self.assertIn("等 你 确认", output)
        self.assertIn("https://github.com/" + REPO + "/issues/9", output)
        self.assertIn("--resume 9", output)
        self.assertIn('--confirm "用户原话"', output)
        mint.assert_called_once_with(REPO, {"contents": "read", "issues": "write"})
        revoke.assert_called_once()

    def test_cli_resume_and_confirm(self):
        fake = FakeIO([step(run="ask: 你", by="human")])
        run(fake)
        code, output, errors, mint, revoke = self.invoke(["--resume", "9", "--confirm", "核验完成"], fake)
        self.assertEqual(code, 0, errors)
        self.assertIn("发布全部完成", output)
        self.assertEqual(fake.records()[-1]["confirmed_words"], "核验完成")
        revoke.assert_called_once()

    def test_cli_errors_revoke_token_and_return_two(self):
        from unittest import mock
        from orchestrator import cli
        from degrade.run import ApiError
        for error in (ApiError("失败"), model.PipelineError(["流程错误"]), OSError("missing"), ValueError("invalid")):
            fake = FakeIO()
            with mock.patch.object(fake, "resolve_pipeline", side_effect=error):
                code, output, errors, mint, revoke = self.invoke(["--version", "1.2.3"], fake)
            self.assertEqual(code, 2, errors)
            revoke.assert_called_once()

    def test_cli_bad_combinations_do_not_mint(self):
        for arguments in (["--version", "1", "--confirm", "批准"], ["--resume", "0"],
                          ["--resume", "1", "--dry-run"], ["--version", " "],
                          ["--resume", "1", "--confirm", " "]):
            code, output, errors, mint, revoke = self.invoke(arguments)
            self.assertEqual(code, 2)
            mint.assert_not_called()
            revoke.assert_not_called()

    def test_cli_token_failure(self):
        from unittest import mock
        from orchestrator import cli
        with mock.patch.object(cli.tokens, "mint", side_effect=cli.tokens.TokenError("钥匙串不可用")), \
                mock.patch.object(cli.tokens, "revoke") as revoke, contextlib.redirect_stderr(text_io.StringIO()):
            self.assertEqual(cli.main(["release", "--repo", REPO, "--version", "1"]), 2)
        revoke.assert_not_called()


class LocalReleaseIOTest(unittest.TestCase):
    def test_run_command_environment_stdin_timeout_and_combined_output(self):
        import subprocess
        from unittest import mock
        from orchestrator import cli
        work = cli.Work.__new__(cli.Work)
        work.root = "/fake/clone"
        environment = {"PATH": "/bin", "HOME": "/home/test", "GH_TOKEN": "secret", "GITHUB_TOKEN": "secret",
                       "GH_ENTERPRISE_TOKEN": "secret", "CUSTOM": "kept"}
        completed = subprocess.CompletedProcess([], 0, "output\n", None)
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(cli.subprocess, "run", return_value=completed) as process:
            result = work.run_release("publish", "1.2.3", REPO, 9)
        self.assertEqual(result, {"exit": 0, "output": "output\n"})
        args, options = process.call_args
        self.assertEqual(args[0], ["/bin/sh", "-c", "publish"])
        self.assertEqual(options["cwd"], "/fake/clone")
        self.assertEqual(options["env"], {"PATH": "/bin", "HOME": "/home/test", "CUSTOM": "kept",
                                          "AUTOPILOT_VERSION": "1.2.3", "AUTOPILOT_REPO": REPO,
                                          "AUTOPILOT_RELEASE_ISSUE": "9"})
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.STDOUT)
        self.assertEqual(options["timeout"], 1800)
        self.assertNotIn("git_env", options)

    def test_timeout_is_failure_with_partial_output(self):
        import subprocess
        from unittest import mock
        from orchestrator import cli
        work = cli.Work.__new__(cli.Work)
        work.root = "/fake/clone"
        with mock.patch.object(cli.subprocess, "run", side_effect=subprocess.TimeoutExpired("publish", 1800, output=b"partial\n")):
            result = work.run_release("publish", "1", REPO, 9)
        self.assertEqual(result["exit"], 124)
        self.assertIn("partial", result["output"])
        self.assertIn("30 分钟", result["output"])

    def test_artifact_evidence_rejects_escape_and_accepts_relative_and_absolute_inside(self):
        import tempfile
        from pathlib import Path
        from orchestrator import cli
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "clone"
            root.mkdir()
            artifact = root / "dist.zip"
            artifact.write_bytes(b"zip")
            outside = Path(directory) / "secret"
            outside.write_bytes(b"secret")
            (root / "link").symlink_to(outside)
            work = cli.Work.__new__(cli.Work)
            work.root = str(root)
            expected = {"path": "dist.zip", "sha256": hashlib.sha256(b"zip").hexdigest()}
            self.assertEqual(work.file_evidence("dist.zip"), expected)
            self.assertEqual(work.file_evidence(str(artifact)), expected)
            for path in ("../secret", str(outside), "link", "missing", "."):
                self.assertIsNone(work.file_evidence(path))

    def test_checkout_detaches_at_original_commit(self):
        from unittest import mock
        from orchestrator import cli
        io = cli.LocalIO("token", None)
        with mock.patch.object(cli, "Work") as work_class:
            with io.release_checkout(REPO, "main", COMMIT) as work:
                self.assertIs(work, work_class.return_value)
        self.assertEqual(work_class.call_args.args[2], {"base": {"ref": "main"}, "head": {"sha": COMMIT}})
        work_class.return_value.git.assert_called_once_with("checkout", "-q", "--detach", COMMIT)

    def test_resolve_reads_base_pipeline_and_category_fallback(self):
        from unittest import mock
        from orchestrator import cli
        io = cli.LocalIO("token", None)
        fake = FakeIO()
        fake.contents[".github/autopr/pipeline.yml"] = "extends: sandbox"
        notes = []
        with mock.patch.object(io, "api_optional", side_effect=fake.api_optional):
            pipeline = io.resolve_pipeline(REPO, COMMIT, notes)
        self.assertEqual(pipeline, model.resolve({"extends": "sandbox"}))
        self.assertTrue(all("ref=" + COMMIT in c[1] for c in fake.calls))
        fake.contents.pop(".github/autopr/pipeline.yml")
        fake.contents[".github/autopr/risk-tiers.yml"] = "extends: plugin"
        with mock.patch.object(io, "api_optional", side_effect=fake.api_optional), \
                mock.patch.object(model, "resolve", return_value={"steps": []}) as resolve:
            io.resolve_pipeline(REPO, COMMIT, notes)
        resolve.assert_called_once_with({"extends": "plugin"})
        self.assertIn("按类别 plugin 套模板", notes[-1])

    def test_issue_metadata_uses_body_edit_time_for_trust(self):
        from unittest import mock
        from orchestrator import cli
        io = cli.LocalIO("token", None)
        issue = authored("body", number=9, state="open", updated_at="later")
        for edited_at, expected in ((None, True), ("later", False)):
            result = {"data": {"repository": {"issue": {"lastEditedAt": edited_at}}}}
            with mock.patch.object(io, "api", side_effect=[issue, result]):
                loaded = io.release_issue(REPO, 9)
            self.assertEqual(decide.trusted(rest_comment(loaded), BOT), expected)


if __name__ == "__main__":
    unittest.main()
