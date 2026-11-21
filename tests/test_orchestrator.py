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

    def module_view(self):
        return self.io.view

    def review(self, previous):
        self.io.reviews.append(previous)
        return self.io.reports.pop(0)


class FakeIO:
    def __init__(self, *, merge="auto", files=("docs/a.md",), author=None, fork=False, degrade=(), takeover=True,
                 reports=None, check_exit=0, check_command="make test", stored=None, merged_ok=True, bot=BOT,
                 module=None, acceptance=None, sg_config=None):
        self.calls, self.comments, self.ran, self.reviews, self.events = [], [], [], [], []
        self.evidence_files = []
        self.view = {"modules": [{"id": module, "review": {"spec": "codex-approved", "plan": "approved"},
                                  "reviewApprovedBy": {"spec": "codex", "plan": "user"}}]} if module else None
        self.module, self.acceptance_record = module, acceptance
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
        if module:  # a spec-guard project: capability map + takeover config on the base branch
            self.contents["spec/CAPABILITY-MAP.md"] = "# 能力图\n"
            cfg = sg_config if sg_config is not None else {"takeover": "plan", "moduleTakeover": {module: "merge"}}
            self.contents[".agent/config.json"] = json.dumps(cfg)
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

    def write_check_evidence(self, repo, data):
        self.evidence_files.append(data)

    def find_modules(self, repo, head):
        rec = (self.acceptance_record or {}).get("record") or {}
        extra = getattr(self, "extra_modules", [])
        return ([rec["module"]] if rec.get("sha") == head and rec.get("module") else []) + extra

    def acceptance(self, repo, module):
        return self.acceptance_record

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


def accepted(report_obj=None, sha=HEAD, marker="codex-approved", module="m", tamper=False):
    import hashlib
    text = json.dumps(report_obj if report_obj is not None else report_for(HEAD))
    digest = hashlib.sha256(text.encode()).hexdigest()
    return {"record": {"marker": marker, "sha": sha, "module": module, "report": "/x/report.json",
                       "report_sha256": "0" * 64 if tamper else digest, "at": T0},
            "report_text": text}


def report_for(to):
    r = report()
    r["scope"]["baseline"] = {"mode": "commits", "from": "b" * 40, "to": to}
    return r


class CheckEvidenceFileTest(unittest.TestCase):
    def test_written_after_checks_pass(self):
        io = FakeIO()
        run(io)
        ev = io.evidence_files[0]
        self.assertEqual(ev["schema"], "autopilot.check-evidence/v1")
        self.assertEqual((ev["repo"], ev["head_sha"], ev["runner"], ev["non_author"], ev["clone"]),
                         (REPO, HEAD, "autopilot-orchestrator", True, "throwaway"))
        self.assertEqual(ev["checks"][0]["cmd"], "make test")
        self.assertEqual(ev["checks"][0]["exit"], 0)
        self.assertEqual(len(ev["checks"][0]["output_sha256"]), 64)

    def test_written_in_dry_run_too(self):
        io = FakeIO()
        run(io, dry_run=True)
        self.assertEqual(len(io.evidence_files), 1)

    def test_not_written_when_check_fails(self):
        io = FakeIO(check_exit=1)
        run(io)
        self.assertEqual(io.evidence_files, [])


class AcceptanceTest(unittest.TestCase):
    """spec-guard modules: final acceptance recorded outside the commit; its review report replaces ours."""

    def test_module_without_acceptance_stops_after_writing_evidence(self):
        io = FakeIO(module="m")
        res = run(io)
        self.assertEqual((res["result"], res["step"]), ("stopped", "acceptance"))
        self.assertEqual(len(io.evidence_files), 1)
        self.assertEqual(io.reviews, [])
        self.assertEqual(io.merge_calls(), [])

    def test_valid_acceptance_reuses_its_report_and_merges(self):
        io = FakeIO(module="m", acceptance=accepted(), reports=[])
        res = run(io)
        self.assertEqual(res["result"], "merged", res)
        self.assertEqual(io.reviews, [])
        ev = json.loads(flow_block([c["body"] for c in io.comments if "autopilot-evidence" in c["body"]][0],
                                   "autopilot-evidence"))
        self.assertEqual(ev["acceptance"], {"marker": "codex-approved", "sha": HEAD})

    def test_takeover_comment_names_the_module(self):
        io = FakeIO(module="m", acceptance=accepted(), reports=[])
        run(io)
        self.assertIn('"module": "m"', io.comments[0]["body"])

    def test_spec_guard_project_without_any_auto_stops_before_checks(self):
        io = FakeIO(module="m", acceptance=accepted(), reports=[], sg_config={"takeover": "plan"})
        res = run(io)
        self.assertEqual((res["result"], res["step"]), ("stopped", "takeover"))
        self.assertEqual(io.ran, [])

    def test_module_merge_from_base_config(self):
        io = FakeIO(module="m", acceptance=accepted(), reports=[],
                    sg_config={"takeover": "merge", "moduleTakeover": {"m": "plan", "other": "merge"}})
        res = run(io)
        self.assertEqual((res["result"], res["step"]), ("stopped", "takeover"))
        self.assertIn("模块 m", res["reason"])

    def test_project_takeover_applies_to_module_without_entry(self):
        io = FakeIO(module="m", acceptance=accepted(), reports=[], sg_config={"takeover": {"merge": "auto"}})
        self.assertEqual(run(io)["result"], "merged")

    def test_several_records_for_head_stop(self):
        io = FakeIO(module="m", acceptance=accepted(), reports=[])
        io.extra_modules = ["n"]
        res = run(io)
        self.assertEqual((res["result"], res["step"]), ("stopped", "acceptance"))
        self.assertIn("多条", res["reason"])

    def test_stale_record_means_no_module_and_stops(self):
        io = FakeIO(module="m", acceptance=accepted(sha="b" * 40), reports=[])
        res = run(io)
        self.assertEqual((res["result"], res["step"]), ("stopped", "acceptance"))
        self.assertEqual(io.reviews, [])

    def test_bad_acceptance_records(self):
        cases = {
            "标记": accepted(marker="pending"),
            "模块": accepted(module="other"),
            "sha256": accepted(tamper=True),
            "报告提交": accepted(report_obj=report_for("c" * 40)),
            "blocking": accepted(report_obj=dict(report_for(HEAD), findings=[{"severity": "blocking"}],
                                                review_outcome="findings")),
        }
        for name, acc in cases.items():
            io = FakeIO(module="m", acceptance=acc, reports=[])
            res = run(io)
            self.assertEqual(res["result"], "stopped", name)
            self.assertIn(res["step"], ("acceptance", "review", "takeover"), name)
            self.assertEqual(io.merge_calls(), [], name)

    def test_human_approved_acceptance_counts(self):
        self.assertEqual(run(FakeIO(module="m", acceptance=accepted(marker="approved"), reports=[]))["result"], "merged")


SPEC_PATCH = "@@ -9 +9 @@\n-- must do A\n+- must do B\n"


class ModuleArtifactsTest(unittest.TestCase):
    """Slice 12: a module PR that also changes its own approved spec/tasks can still merge automatically."""

    def io(self, files=("spec/m.md", "tasks/m/plan.md", "docs/a.md"), **kw):
        io = FakeIO(module="m", acceptance=accepted(), reports=[], **kw)
        io.files = [{"filename": f, "status": "modified", "additions": 1, "deletions": 1,
                     "patch": SPEC_PATCH if f.startswith(("spec/", "tasks/")) else "@@ -1 +1 @@\n+x\n"} for f in files]
        return io

    def evidence(self, io):
        return json.loads(flow_block([c["body"] for c in io.comments if "autopilot-evidence" in c["body"]][0],
                                     "autopilot-evidence"))

    def test_own_approved_artifacts_are_verified_and_merge(self):
        io = self.io()
        res = run(io)
        self.assertEqual(res["result"], "merged", res)
        self.assertEqual(self.evidence(io)["artifacts"], {"module": "m", "verified": ["spec/m.md", "tasks/m/plan.md"]})

    def test_revised_spec_is_not_verified_and_stops_at_admission(self):
        io = self.io()
        io.view["modules"][0]["review"]["spec"] = "revised"
        res = run(io)
        self.assertEqual((res["result"], res["step"]), ("stopped", "admission"))
        self.assertIn("T4", res["reason"])
        self.assertEqual(self.evidence(io)["artifacts"]["verified"], [])

    def test_codex_approval_not_counted_by_takeover_is_not_verified(self):
        io = self.io()
        io.view["modules"][0]["reviewApprovedBy"]["spec"] = None
        self.assertEqual(run(io)["step"], "admission")

    def test_other_modules_artifacts_and_capability_map_stay_T4(self):
        for f in ("spec/other.md", "spec/CAPABILITY-MAP.md", "tasks/other/plan.md"):
            io = self.io(files=("spec/m.md", f))
            res = run(io)
            self.assertEqual(res["result"], "stopped", f)
            self.assertIn(res["step"], ("risk", "admission"), f)  # the capability map never gets past the pre-check
            self.assertEqual(io.merge_calls(), [], f)

    def test_no_map_view_verifies_nothing(self):
        io = self.io()
        io.view = None
        self.assertEqual(run(io)["step"], "admission")

    def test_verify_artifacts_unit(self):
        view = {"modules": [{"id": "m", "review": {"spec": "approved", "plan": "codex-approved"},
                             "reviewApprovedBy": {"spec": "user", "plan": "codex"}}]}
        self.assertEqual(flow.verify_artifacts(view, "m", ["spec/m.md", "tasks/m/todo.md", "spec/mm.md", "src/x.py"]),
                         ["spec/m.md", "tasks/m/todo.md"])
        self.assertEqual(flow.verify_artifacts(view, "n", ["spec/n.md"]), [])


class TakeoverConfigTest(unittest.TestCase):
    def test_presets_and_objects(self):
        self.assertEqual(flow.module_merge({}, "m"), "human")
        self.assertEqual(flow.module_merge({"takeover": "merge"}, "m"), "auto")
        self.assertEqual(flow.module_merge({"takeover": "plan"}, "m"), "human")
        self.assertEqual(flow.module_merge({"takeover": {"plan": "codex"}}, "m"), "human")
        self.assertEqual(flow.module_merge({"takeover": {"merge": "auto", "release": "prepare"}}, "m"), "auto")

    def test_module_entry_wins_and_bad_entries_fall_back(self):
        cfg = {"takeover": "none", "moduleTakeover": {"m": "merge"}}
        self.assertEqual(flow.module_merge(cfg, "m"), "auto")
        self.assertEqual(flow.module_merge(cfg, "n"), "human")
        bad = {"takeover": "none", "moduleTakeover": {"m": "merge", "x": {"merge": "yes"}}}
        self.assertEqual(flow.module_merge(bad, "m"), "human")
        self.assertEqual(flow.module_merge({"takeover": "bogus"}, "m"), "human")
        self.assertEqual(flow.module_merge({"takeover": {"merge": "auto", "colour": "red"}}, "m"), "human")


class LocalIOTest(unittest.TestCase):
    """The file side of LocalIO: evidence file and acceptance record, in a temp HOME-like state dir."""

    def setUp(self):
        import tempfile
        from orchestrator import cli
        self.cli = cli
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_state = cli.STATE
        cli.STATE = os.path.join(self.tmp.name, "state")
        self.addCleanup(setattr, cli, "STATE", self.old_state)
        self.io = cli.LocalIO("t", "/nonexistent")

    def test_evidence_file_path_and_content(self):
        data = flow.check_evidence(REPO, HEAD, "make test", flow.check_result(0, "ok\n"), T0)
        self.io.write_check_evidence(REPO, data)
        path = os.path.join(self.cli.STATE, "evidence", "haigeerlab__autopilot-sandbox", HEAD + ".json")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), data)

    def test_acceptance_from_git_path(self):
        import subprocess
        root = os.path.join(self.tmp.name, "checkout")
        os.makedirs(root)
        subprocess.run(["git", "-C", root, "init", "-q"], check=True)
        os.makedirs(self.cli.STATE)
        with open(os.path.join(self.cli.STATE, "config.json"), "w") as fh:
            json.dump({"checkouts": {REPO: root}}, fh)
        self.assertIsNone(self.io.acceptance(REPO, "m"))
        report_path = os.path.join(self.tmp.name, "report.json")
        with open(report_path, "w") as fh:
            fh.write("{}")
        acc_dir = os.path.join(root, ".git", "spec-guard", "acceptance")
        os.makedirs(acc_dir)
        with open(os.path.join(acc_dir, "m.json"), "w") as fh:
            json.dump({"marker": "codex-approved", "sha": HEAD, "module": "m", "report": report_path}, fh)
        found = self.io.acceptance(REPO, "m")
        self.assertEqual(found["record"]["sha"], HEAD)
        self.assertEqual(found["report_text"], "{}")
        self.assertIsNone(self.io.acceptance(REPO, "../m"))
        self.assertIsNone(self.io.acceptance("other/repo", "m"))


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
