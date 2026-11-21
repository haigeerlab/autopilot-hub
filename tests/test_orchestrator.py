"""Unit tests for the slice-10 local orchestrator flow (docs/spec/slice-10.md). GitHub, git and delegate are fakes.

Run: python3 -B -m unittest discover -s tests -v
"""
import base64
import contextlib
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from admission import decide as d
from orchestrator import flow

REPO = "haigeerlab/autopilot-sandbox"
HEAD = "a" * 40
BOT = d.load_bot_identity(os.path.join(ROOT, "bot-identity.json"))
BOT_USER = {"login": BOT["login"], "id": BOT["id"], "type": "Bot"}
T0 = "2026-11-22T01:00:00Z"


def b64(text):
    return base64.b64encode(text.encode()).decode()


def report(blocking=0, status="completed", outcome=None, unreviewed=(), fp="f" * 64, recheck=None):
    findings = [{"title": f"问题 {i}", "severity": "blocking"} for i in range(blocking)]
    return {"execution_status": status, "backend": "codex",
            "review_outcome": outcome or ("findings" if blocking else "no_findings_in_scope"),
            "scope": {"fingerprint": {"sha256": fp} if fp else None}, "findings": findings,
            "unreviewed": [{"path": p, "reason": "x"} for p in unreviewed], "recheck": recheck}


class FakeWork:
    def __init__(self, io):
        self.io = io

    def find_check(self):
        return self.io.check_command

    def run_check(self, command):
        self.io.ran.append(command)
        return flow.check_result(self.io.check_exit, "ok\n")

    def review(self, previous):
        self.io.reviews.append(previous)
        return self.io.reports.pop(0)


class FakeIO:
    def __init__(self, *, merge="auto", files=("docs/a.md",), author=None, fork=False, degrade=(), takeover=True,
                 reports=None, check_exit=0, check_command="make test", stored=None, merged_ok=True, bot=BOT):
        self.calls, self.comments, self.ran, self.reviews, self.events = [], [], [], [], []
        self.bot = bot
        self.check_command, self.check_exit = check_command, check_exit
        self.reports = list(reports if reports is not None else [report()])
        self.stored = stored
        self.merged_ok = merged_ok
        self.merged = False
        self.pr = {"number": 7, "state": "open", "draft": False, "user": author or BOT_USER,
                   "head": {"sha": HEAD, "repo": {"full_name": "someone/fork" if fork else REPO}},
                   "base": {"ref": "main", "repo": {"owner": {"login": "haigeerlab"}}}}
        self.files = [{"filename": f, "status": "modified", "additions": 1, "deletions": 0,
                       "patch": "@@ -1 +1 @@\n+x\n"} for f in files]
        self.contents = {".github/autopr/risk-tiers.yml": "extends: plugin\n"}
        if takeover:
            self.contents[".github/autopr/takeover.yml"] = f"merge: {merge}\n"
        self.degrade = [{"number": n, "body": '<!-- autopilot-degrade:v1 {"scope": {"repo": "%s"}} -->' % REPO}
                        for n in degrade]

    # --- io interface ---
    def bot_identity(self):
        return self.bot

    def api(self, path, method="GET", body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path.endswith("/comments"):
            self.comments.append({"id": len(self.comments) + 1, "body": body["body"], "user": BOT_USER,
                                  "performed_via_github_app": {"slug": BOT["app_slug"]},
                                  "created_at": T0, "updated_at": T0})
            return {}
        if method == "PUT" and path.endswith("/merge"):
            self.merged = True
            return {"merged": True}
        if method == "POST":
            return {}
        if path == f"repos/{REPO}/pulls/7":
            if self.merged and self.merged_ok:
                return dict(self.pr, merged=True, merge_commit_sha="m" * 40)
            return self.pr
        if path.startswith(f"repos/{REPO}/issues/7/comments"):
            return list(self.comments)
        if path.startswith(f"repos/{REPO}/pulls/7/files"):
            return self.files
        if path.startswith(f"repos/{REPO}/issues?labels=autopilot:degrade"):
            return self.degrade
        if path == f"repos/{REPO}":
            return {"allow_squash_merge": True}
        raise AssertionError(f"unexpected GET {path}")

    def api_optional(self, path):
        self.calls.append(("GET", path, None))
        name = path.split("/contents/", 1)[1].split("?", 1)[0]
        text = self.contents.get(name)
        return {"content": b64(text)} if text is not None else None

    @contextlib.contextmanager
    def checkout(self, repo, pr):
        yield FakeWork(self)

    def load_review(self, repo, number):
        return self.stored

    def save_review(self, repo, number, data):
        self.stored = data

    def ledger(self, event):
        self.events.append(event)

    def now(self):
        return T0

    # --- helpers ---
    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]

    def merge_calls(self):
        return [c for c in self.calls if c[0] == "PUT"]


def run(io, **kw):
    return flow.run(io, REPO, 7, **kw)


class HappyPathTest(unittest.TestCase):
    def test_merges_the_reviewed_head(self):
        io = FakeIO()
        res = run(io)
        self.assertEqual(res["result"], "merged", res)
        self.assertEqual(io.merge_calls(), [("PUT", f"repos/{REPO}/pulls/7/merge", {"sha": HEAD, "merge_method": "squash"})])
        labels = [c for c in io.calls if c[1].endswith("/labels")]
        self.assertEqual(labels[0][2], {"labels": ["autopilot:authored", "autopilot:auto-merged"]})
        self.assertEqual(io.ran, ["make test"])

    def test_posts_takeover_then_evidence_bound_to_head(self):
        io = FakeIO()
        run(io)
        bodies = [c["body"] for c in io.comments]
        self.assertIn("autopilot-takeover:v1", bodies[0])
        ev = json.loads(flow_block(bodies[1], "autopilot-evidence"))
        self.assertEqual(ev["head_sha"], HEAD)
        self.assertEqual(ev["checks"][0]["exit"], 0)
        self.assertEqual(ev["review"]["blocking"], 0)
        self.assertIn("已合并", bodies[-1])

    def test_owner_authored_pr_is_allowed(self):
        self.assertEqual(run(FakeIO(author={"login": "haigeerlab", "id": 1, "type": "User"}))["result"], "merged")

    def test_dry_run_writes_nothing(self):
        io = FakeIO()
        res = run(io, dry_run=True)
        self.assertEqual(res["result"], "dry-run")
        self.assertEqual(io.writes(), [])
        self.assertEqual(len(io.reviews), 1)


def flow_block(body, name):
    import re
    return re.search(r"<!--\s*%s:v1\s+(.*?)\s*-->" % name, body, re.S).group(1)


class StopTest(unittest.TestCase):
    def assertStops(self, io, step, text=None, **kw):
        res = run(io, **kw)
        self.assertEqual(res["result"], "stopped", res)
        self.assertEqual(res["step"], step, res)
        if text:
            self.assertIn(text, res["reason"])
        self.assertEqual(io.merge_calls(), [])
        return res

    def test_no_bot_identity(self):
        io = FakeIO(bot=None)
        self.assertStops(io, "identity")
        self.assertEqual(io.calls, [])

    def test_fork(self):
        self.assertStops(FakeIO(fork=True), "pr", "fork")

    def test_stranger_author(self):
        self.assertStops(FakeIO(author={"login": "stranger", "id": 9, "type": "User"}), "pr", "stranger")

    def test_open_degrade_issue(self):
        self.assertStops(FakeIO(degrade=[3]), "degrade", "#3")

    def test_takeover_human_or_missing(self):
        io = FakeIO(merge="human")
        self.assertStops(io, "takeover")
        self.assertIn('"merge": "human"', io.comments[0]["body"])
        self.assertStops(FakeIO(takeover=False), "takeover")
        self.assertStops(FakeIO(merge="yes"), "takeover")

    def test_high_risk_stops_before_any_review(self):
        io = FakeIO(files=(".github/workflows/ci.yml",))
        self.assertStops(io, "risk", "T4")
        self.assertEqual(io.reviews, [])
        self.assertEqual(io.ran, [])

    def test_no_check_command(self):
        self.assertStops(FakeIO(check_command=None), "check", "找不到检查命令")

    def test_failing_check(self):
        io = FakeIO(check_exit=1)
        self.assertStops(io, "check", "退出码 1")
        self.assertEqual(io.reviews, [])

    def test_codex_unavailable_stops_and_is_recorded(self):
        io = FakeIO(reports=[report(status="conditions_missing")])
        self.assertStops(io, "review", "Codex 不可用")
        self.assertEqual(io.events[0]["event"], "codex_unavailable")
        self.assertIsNone(io.stored)

    def test_inconclusive_is_unavailable_too(self):
        self.assertStops(FakeIO(reports=[report(outcome="inconclusive")]), "review", "没有结论")

    def test_blocking_round_one_asks_for_rework_and_saves_report(self):
        io = FakeIO(reports=[report(blocking=2)])
        self.assertStops(io, "review", "2 条 blocking（第 1 轮）")
        self.assertEqual(io.stored["rounds"], 1)

    def test_round_two_rechecks_against_round_one(self):
        first = report(blocking=1)
        io = FakeIO(stored={"head_sha": "b" * 40, "rounds": 1, "report": first}, reports=[report()])
        self.assertEqual(run(io)["result"], "merged")
        self.assertEqual(io.reviews, [first])

    def test_round_two_still_blocking_goes_to_human(self):
        io = FakeIO(stored={"head_sha": "b" * 40, "rounds": 1, "report": report(blocking=1)},
                    reports=[report(recheck={"items": [{"status": "unresolved"}]})])
        self.assertStops(io, "review", "复审 2 轮后仍有 1 条 blocking")

    def test_same_head_reuses_conclusive_review(self):
        io = FakeIO(stored={"head_sha": HEAD, "rounds": 1, "report": report()}, reports=[])
        self.assertEqual(run(io)["result"], "merged")
        self.assertEqual(io.reviews, [])

    def test_suggestions_do_not_block(self):
        r = report()
        r["findings"] = [{"title": "x", "severity": "suggestion"}]
        self.assertEqual(run(FakeIO(reports=[r]))["result"], "merged")

    def test_missing_severity_counts_as_blocking(self):
        r = report()
        r["findings"] = [{"title": "x"}]
        self.assertStops(FakeIO(reports=[r]), "review", "blocking")

    def test_unreviewed_files(self):
        self.assertStops(FakeIO(reports=[report(unreviewed=["big.bin"])]), "review", "big.bin")

    def test_missing_fingerprint(self):
        self.assertStops(FakeIO(reports=[report(fp=None)]), "review", "指纹")

    def test_readback_not_merged(self):
        io = FakeIO(merged_ok=False)
        res = run(io)
        self.assertEqual((res["result"], res["step"]), ("stopped", "readback"))
        self.assertEqual(len(io.merge_calls()), 1)  # merge was requested; reading back is what failed
        self.assertFalse([c for c in io.calls if c[1].endswith("/labels")])

    def test_every_stop_leaves_a_bot_comment(self):
        io = FakeIO(check_exit=3)
        run(io)
        self.assertTrue(io.comments[-1]["body"].startswith(flow.RUN_MARKER))
        self.assertIn("「check」", io.comments[-1]["body"])


class AdmissionAgreesTest(unittest.TestCase):
    def test_evaluate_matches_workflow_inputs(self):
        io = FakeIO()
        io.comments = [{"id": 1, "body": flow.block("autopilot-takeover", {"merge": "auto", "module": None}),
                        "user": BOT_USER, "performed_via_github_app": {"slug": BOT["app_slug"]},
                        "created_at": T0, "updated_at": T0}]
        res = flow.evaluate(io, REPO, io.pr, BOT)
        self.assertTrue(next(i for i in res["items"] if i["id"] == "takeover")["ok"])
        self.assertFalse(res["auto_merge"])  # no evidence yet

    def test_edited_evidence_blocks_merge(self):
        class EditingIO(FakeIO):
            def api(self, path, method="GET", body=None):
                out = super().api(path, method, body)
                if method == "POST" and path.endswith("/comments") and "autopilot-evidence" in body["body"]:
                    self.comments[-1]["updated_at"] = "2026-11-22T02:00:00Z"
                return out
        io = EditingIO()
        res = run(io)
        self.assertEqual((res["result"], res["step"]), ("stopped", "admission"))
        self.assertEqual(io.merge_calls(), [])


if __name__ == "__main__":
    unittest.main()
