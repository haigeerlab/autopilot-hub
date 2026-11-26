"""Slice 18c: audit command, weekly report issue and de-duplicated violation issues (research audit-spec M5/M6).

Run: python3 -B -m unittest discover -s tests -v
"""
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from admission import decide as d
from audit import run as r

BOT = d.load_bot_identity(os.path.join(ROOT, "bot-identity.json"))
BOT_USER = {"login": BOT["login"], "id": BOT["id"], "type": "Bot"}
PIN = "7" * 40
REPO = "haigeerlab/agent-relay"
T0 = "2026-11-23T01:00:00Z"
CLEAN_PINS = {".github/workflows/autopilot-admission.yml": [PIN, PIN],
              ".github/workflows/autopilot-degrade.yml": [PIN, PIN]}


def baseline(**over):
    data = {"since": "2026-11-23", "approval": "x#1", "hub_pin": PIN,
            "takeover": {"project": "plan", "modules": {}}, "checkup": {"C1": "ok"}}
    data.update(over)
    return data


def snapshot(**over):
    data = {"takeover": {"project": "plan", "modules": {}}, "pins": dict(CLEAN_PINS),
            "checkup": {"C1": "ok"}, "spec_guard": True,
            "view": {"modules": [{"id": "m", "status": "done", "progress": {"done": 2},
                                  "review": {"spec": "approved", "plan": "approved"},
                                  "reviewApprovedBy": {"spec": "user", "plan": "user"},
                                  "codexRoute": {"spec": None, "plan": None}, "important": {"reasons": []}}]}}
    data.update(over)
    return data


class FakeIO:
    def __init__(self, baselines=None, snapshots=None, issues=(), degrade=(), records=None):
        self.bases = baselines if baselines is not None else {REPO: baseline()}
        self.snaps = snapshots or {REPO: snapshot()}
        self.issues = [dict(i) for i in issues]
        self.degrade = list(degrade)
        self.calls = []
        self.edited = {}
        self.records = records
        self.verification_calls = []

    def verification_records(self, repo, module_ids):
        self.verification_calls.append((repo, module_ids))
        return self.records

    def edited_at(self, number):
        return self.edited.get(number)

    def bot_identity(self):
        return BOT

    def baselines(self):
        return self.bases

    def snapshot(self, repo, ref):
        snap = self.snaps.get(repo)
        if isinstance(snap, Exception):
            raise snap
        return snap

    def degrade_issues(self, repo):
        return self.degrade

    def today(self):
        return "2026-11-23"

    def hub_api(self, path, method="GET", body=None):
        self.calls.append((method, path, body))
        if method == "GET" and "issues?labels=" in path:
            label = re.search(r"labels=([^&]+)", path).group(1)
            return [i for i in self.issues if label in i["labels"] and i["state"] == "open"]
        if method == "GET" and path.endswith("/comments?per_page=100"):
            n = int(path.split("/")[-2])
            return next(i for i in self.issues if i["number"] == n).get("comments", [])
        if method == "POST" and path.endswith("/issues"):
            n = 100 + len(self.issues)
            self.issues.append({"number": n, "title": body["title"], "body": body["body"], "labels": body["labels"],
                                "state": "open", "user": BOT_USER, "performed_via_github_app": None,
                                "created_at": T0, "updated_at": T0, "comments": []})
            return {"number": n, "html_url": "https://github.com/x/issues/%d" % n}
        if method == "POST" and path.endswith("/comments"):
            n = int(path.split("/")[-2])
            issue = next(i for i in self.issues if i["number"] == n)
            issue["updated_at"] = "2026-11-23T09:00:00Z"  # GitHub bumps updated_at on every comment
            issue["comments"].append({"body": body["body"], "user": BOT_USER,
                                      "performed_via_github_app": {"slug": BOT["app_slug"]},
                                      "created_at": T0, "updated_at": T0})
            return {}
        return {}

    def created(self, label):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/issues") and label in c[2]["labels"]]


class RunTest(unittest.TestCase):
    def flow_io(self, status="in_progress", confirmation="confirmed", **kw):
        view = {"modules": [{"id": "flow", "tier": "normal", "status": status, "progress": {"done": 1},
                             "requirementConfirm": {"status": confirmation, "by": "user"}, "reviewRounds": {}}]}
        return FakeIO(snapshots={REPO: snapshot(view=view)}, **kw)

    def test_unconfirmed_flow_opens_deduplicated_violation(self):
        io = self.flow_io(confirmation="pending")
        res = r.run(io)
        self.assertEqual(res["repos"][0]["status"], "违规")
        self.assertEqual(res["code"], 1)
        created = io.created(r.VIOLATION_LABEL)
        self.assertEqual(len(created), 1)
        self.assertEqual(r.payload(created[0][2]["body"], r.VIOLATION),
                         {"repo": REPO, "kind": "requirement-not-confirmed"})
        r.run(io)
        self.assertEqual(len(io.created(r.VIOLATION_LABEL)), 1)
        self.assertEqual(io.verification_calls, [])

    def test_done_flow_with_local_record_is_clean(self):
        io = self.flow_io(status="done", records={"flow": {"verdict": "pass"}})
        res = r.run(io)
        self.assertEqual(res["repos"][0]["status"], "合规")
        self.assertEqual(res["code"], 0)
        self.assertEqual(io.created(r.VIOLATION_LABEL), [])
        self.assertEqual(io.verification_calls, [(REPO, ["flow"])])

    def test_verification_only_reads_done_flow_modules(self):
        for io in (FakeIO(), self.flow_io(), FakeIO(snapshots={REPO: snapshot(spec_guard=False)})):
            with self.subTest(io=io):
                r.run(io)
                self.assertEqual(io.verification_calls, [])

    def test_unavailable_map_is_reported_once(self):
        for view in (None, {}, {"modules": None}):
            with self.subTest(view=view):
                io = FakeIO(snapshots={REPO: snapshot(view=view)})
                res = r.run(io)
                self.assertEqual([p["kind"] for p in res["repos"][0]["problems"]], ["map-unavailable"])
                self.assertEqual(io.verification_calls, [])

    def test_clean_repo_only_comments_on_report(self):
        io = FakeIO()
        res = r.run(io)
        self.assertEqual(res["code"], 0)
        self.assertEqual(len(io.created(r.REPORT_LABEL)), 1)
        self.assertEqual(io.created(r.VIOLATION_LABEL), [])
        report = io.issues[0]
        self.assertIn("✅", report["comments"][0]["body"])

    def test_report_issue_is_reused(self):
        io = FakeIO()
        r.run(io)
        r.run(io)
        self.assertEqual(len(io.created(r.REPORT_LABEL)), 1)
        self.assertEqual(len(io.issues[0]["comments"]), 2)

    def test_widened_takeover_opens_severe_violation_once(self):
        io = FakeIO(snapshots={REPO: snapshot(takeover={"project": "merge", "modules": {}})})
        res = r.run(io)
        self.assertEqual(res["code"], 1)
        created = io.created(r.VIOLATION_LABEL)
        self.assertEqual(len(created), 1)
        self.assertIn("takeover-wider", created[0][2]["body"])
        self.assertIn("严重", created[0][2]["title"])
        r.run(io)  # same violation again: comment, no new issue
        self.assertEqual(len(io.created(r.VIOLATION_LABEL)), 1)
        violation = next(i for i in io.issues if r.VIOLATION_LABEL in i["labels"])
        self.assertEqual(len(violation["comments"]), 1)
        self.assertIn("仍存在", violation["comments"][0]["body"])

    def test_resolved_violation_gets_one_note_and_stays_open(self):
        io = FakeIO(snapshots={REPO: snapshot(takeover={"project": "merge", "modules": {}})})
        r.run(io)
        io.snaps[REPO] = snapshot()
        r.run(io)
        r.run(io)
        violation = next(i for i in io.issues if r.VIOLATION_LABEL in i["labels"])
        notes = [c for c in violation["comments"] if "未再发现" in c["body"]]
        self.assertEqual(len(notes), 1)
        self.assertEqual(violation["state"], "open")
        self.assertFalse(any(c[0] == "PATCH" for c in io.calls))

    def test_forged_violation_issue_is_ignored(self):
        forged = {"number": 5, "title": "x", "labels": [r.VIOLATION_LABEL], "state": "open",
                  "body": r.block(r.VIOLATION, {"repo": REPO, "kind": "takeover-wider"}),
                  "user": {"login": "someone", "id": 1, "type": "User"}, "performed_via_github_app": None,
                  "created_at": T0, "updated_at": T0, "comments": []}
        io = FakeIO(snapshots={REPO: snapshot(takeover={"project": "merge", "modules": {}})}, issues=[forged])
        r.run(io)
        self.assertEqual(len(io.created(r.VIOLATION_LABEL)), 1)

    def test_edited_report_issue_is_not_reused(self):
        io = FakeIO()
        r.run(io)
        io.edited[io.issues[0]["number"]] = "2026-11-23T09:30:00Z"
        r.run(io)
        self.assertEqual(len(io.created(r.REPORT_LABEL)), 2)

    def test_plan_and_pin_and_degrade_problems_all_reported(self):
        view = {"modules": [{"id": "x", "status": "in_progress", "progress": {"done": 1},
                             "review": {"spec": "approved", "plan": "pending"},
                             "reviewApprovedBy": {"spec": "user", "plan": None},
                             "codexRoute": {"spec": None, "plan": None}, "important": {"reasons": []}}]}
        pins = dict(CLEAN_PINS, **{".github/workflows/autopilot-degrade.yml": ["8" * 40, "8" * 40]})
        block = '<!-- autopilot-degrade:v1 {"scope": {"repo": "%s"}} -->' % REPO
        io = FakeIO(snapshots={REPO: snapshot(view=view, pins=pins)}, degrade=[{"number": 3, "body": block}])
        res = r.run(io)
        kinds = sorted(p["kind"] for p in res["repos"][0]["problems"])
        self.assertEqual(kinds, ["degrade-open", "pin-changed", "plan-not-approved"])

    def test_not_spec_guard_skips_plan_with_note(self):
        io = FakeIO(snapshots={REPO: snapshot(spec_guard=False, view=None)})
        res = r.run(io)
        self.assertEqual(res["repos"][0]["problems"], [])
        self.assertTrue(any("不是 spec-guard 项目" in n for n in res["repos"][0]["notes"]))

    def test_unregistered_repo(self):
        io = FakeIO()
        res = r.run(io, repos=["haigeerlab/other"])
        self.assertEqual(res["repos"][0]["status"], "未登记")
        self.assertEqual(res["code"], 0)

    def test_snapshot_failure_is_cannot_judge(self):
        io = FakeIO(snapshots={REPO: OSError("clone failed")})
        res = r.run(io)
        self.assertEqual(res["repos"][0]["status"], "判不了")
        self.assertEqual(res["code"], 2)

    def test_no_post_writes_nothing(self):
        io = FakeIO(snapshots={REPO: snapshot(takeover={"project": "merge", "modules": {}})})
        res = r.run(io, post=False)
        self.assertEqual(res["code"], 1)
        self.assertEqual([c for c in io.calls if c[0] != "GET"], [])

    def test_severe_first_in_report(self):
        text = r.render([{"repo": REPO, "status": "违规", "notes": [], "problems": [
            {"kind": "pin-changed", "severity": "提醒", "detail": "钉变了"},
            {"kind": "takeover-wider", "severity": "严重", "detail": "放宽"}]}], "2026-11-23")
        self.assertLess(text.index("放宽"), text.index("钉变了"))


if __name__ == "__main__":
    unittest.main()
