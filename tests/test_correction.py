"""Unit tests for slice-6 correction capture (`correction/cli.py`) and the revert hook in degrade.

Run: python3 -B -m unittest discover -s tests -v
Stdlib only; the GitHub API is replaced by a fake.
"""
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from correction import cli as c
from degrade import run as degrade

REPO = "haigeerlab/agent-relay"


class FakeGitHub:
    """A tiny in-memory issue tracker behind the `api(path, method, body)` signature."""

    def __init__(self, fail_on=None):
        self.issues, self.labels, self.calls = [], set(), []
        self.fail_on = fail_on  # (method, path substring) -> ApiError

    def __call__(self, path, method="GET", body=None):
        self.calls.append((method, path, body))
        if self.fail_on and method == self.fail_on[0] and self.fail_on[1] in path:
            raise c.ApiError("boom")
        if method == "GET" and "/issues?" in path:
            wanted = path.split("labels=")[1].split("&")[0].split(",")
            state = path.split("state=")[1].split("&")[0]
            return [i for i in self.issues if set(wanted) <= {lb["name"] for lb in i["labels"]}
                    and (state == "all" or i["state"] == state)]
        if method == "POST" and path.endswith("/labels"):
            if body["name"] in self.labels:
                raise c.ApiError("422 already_exists")
            self.labels.add(body["name"])
            return {}
        if method == "POST" and path.endswith("/issues"):
            n = len(self.issues) + 1
            issue = {"number": n, "html_url": f"https://github.com/{REPO}/issues/{n}", "state": "open",
                     "title": body["title"], "body": body["body"], "labels": [{"name": x} for x in body["labels"]]}
            self.issues.append(issue)
            return issue
        if method == "PATCH":
            n = int(path.rsplit("/", 1)[1])
            self.issues[n - 1].update(body)
            return self.issues[n - 1]
        raise c.ApiError(f"unexpected {method} {path}")

    def writes(self):
        return [x for x in self.calls if x[0] != "GET"]


def candidates(api):
    return [i for i in api.issues if "correction:candidate" in {lb["name"] for lb in i["labels"]}]


class RecordTest(unittest.TestCase):
    def test_issue_title_labels_and_machine_block(self):
        api = FakeGitHub()
        res = c.record(api, REPO, "weakened-assertion", "删了断言让测试变绿，应该保留原断言" * 5, "https://x/pr/1")
        self.assertEqual(res["status"], "created")
        issue = api.issues[0]
        self.assertEqual({lb["name"] for lb in issue["labels"]}, {"correction", "correction:weakened-assertion"})
        self.assertTrue(issue["title"].startswith("纠正：weakened-assertion — 删了断言"))
        self.assertLessEqual(len(issue["title"]), len("纠正：weakened-assertion — ") + 40)
        payload = c.issue_payload(issue)
        self.assertEqual(payload["class"], "weakened-assertion")
        self.assertEqual(payload["ref"], "https://x/pr/1")
        self.assertIn("<!-- autopilot-correction:v1 ", issue["body"])
        self.assertIn("https://x/pr/1", issue["body"])
        self.assertEqual({"correction", "correction:weakened-assertion"}, api.labels)

    def test_bad_slug_is_rejected_before_any_call(self):
        for bad in ("Bad", "a_b", "-a", "a--b", "a-", "", "中文"):
            api = FakeGitHub()
            with self.assertRaises(c.BadInput):
                c.record(api, REPO, bad, "x")
            self.assertEqual(api.calls, [])

    def test_empty_summary_and_bad_repo_rejected(self):
        with self.assertRaises(c.BadInput):
            c.record(FakeGitHub(), REPO, "ok", "  ")
        with self.assertRaises(c.BadInput):
            c.record(FakeGitHub(), "noslash", "ok", "x")

    def test_same_ref_same_class_is_not_recorded_twice_even_if_closed(self):
        api = FakeGitHub()
        c.record(api, REPO, "revert", "a", "sha1")
        api.issues[0]["state"] = "closed"
        res = c.record(api, REPO, "revert", "a again", "sha1")
        self.assertEqual(res["status"], "duplicate")
        self.assertEqual(res["number"], 1)
        self.assertEqual(len(api.issues), 1)

    def test_same_ref_other_class_is_recorded(self):
        api = FakeGitHub()
        c.record(api, REPO, "a-class", "a", "sha1")
        self.assertEqual(c.record(api, REPO, "b-class", "b", "sha1")["status"], "created")

    def test_first_event_opens_no_candidate(self):
        api = FakeGitHub()
        res = c.record(api, REPO, "x-class", "one", "r1")
        self.assertIsNone(res["candidate"])
        self.assertIsNone(res["ask"])
        self.assertEqual(candidates(api), [])

    def test_second_event_opens_one_candidate_and_asks(self):
        api = FakeGitHub()
        c.record(api, REPO, "x-class", "one", "r1")
        res = c.record(api, REPO, "x-class", "two", "r2")
        cands = candidates(api)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["title"], "晋升候选：x-class")
        self.assertIn("correction:x-class", {lb["name"] for lb in cands[0]["labels"]})
        self.assertIn("#1", cands[0]["body"])
        self.assertIn("#2", cands[0]["body"])
        self.assertIn("要不要沉淀成规则？", res["ask"])
        self.assertEqual(res["count"], 2)

    def test_third_event_updates_the_same_candidate(self):
        api = FakeGitHub()
        for ref in ("r1", "r2", "r3"):
            c.record(api, REPO, "x-class", ref, ref)
        self.assertEqual(len(candidates(api)), 1)
        self.assertIn("3 次", candidates(api)[0]["body"])
        self.assertIn("#4", candidates(api)[0]["body"])  # #3 is the candidate itself

    def test_candidate_is_not_counted_as_an_event(self):
        api = FakeGitHub()
        c.record(api, REPO, "x-class", "one", "r1")
        c.record(api, REPO, "x-class", "two", "r2")
        self.assertEqual(c.record(api, REPO, "x-class", "three", "r3")["count"], 3)

    def test_read_failure_writes_nothing(self):
        api = FakeGitHub(fail_on=("GET", "labels=correction:x-class"))
        with self.assertRaises(c.ApiError):
            c.record(api, REPO, "x-class", "one", "r1")
        self.assertEqual(api.writes(), [])

    def test_existing_label_error_is_ignored(self):
        api = FakeGitHub()
        api.labels.update({"correction", "correction:x-class"})
        self.assertEqual(c.record(api, REPO, "x-class", "one")["status"], "created")


class ClassesAndMainTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        for cls, ref in (("a-class", "1"), ("b-class", "2"), ("a-class", "3")):
            c.record(self.api, REPO, cls, "s", ref)

    def test_classes_counts_exclude_candidates(self):
        self.assertEqual(c.classes(self.api, REPO), [{"class": "a-class", "count": 2}, {"class": "b-class", "count": 1}])

    def run_main(self, argv, api=None):
        out = io.StringIO()
        code = c.main(argv, api=api or self.api, out=out)
        return code, out.getvalue()

    def test_classes_json_and_text(self):
        code, out = self.run_main(["classes", "--repo", REPO, "--json"])
        self.assertEqual((code, json.loads(out)[0]), (0, {"class": "a-class", "count": 2}))
        code, out = self.run_main(["classes", "--repo", REPO])
        self.assertIn("a-class\t2", out)

    def test_record_exit_codes(self):
        self.assertEqual(self.run_main(["record", "--repo", REPO, "--class", "Bad Slug", "--summary", "s"])[0], 2)
        self.assertEqual(self.run_main(["record", "--repo", REPO, "--summary", "s"])[0], 2)
        code, out = self.run_main(["record", "--repo", REPO, "--class", "c-new", "--summary", "s", "--ref", "z"])
        self.assertEqual(code, 0)
        self.assertIn("已记录", out)
        code, out = self.run_main(["record", "--repo", REPO, "--class", "c-new", "--summary", "s", "--ref", "z"])
        self.assertIn("已记录过", out)

    def test_api_failure_exit_1(self):
        code, _ = self.run_main(["classes", "--repo", REPO], api=FakeGitHub(fail_on=("GET", "issues")))
        self.assertEqual(code, 1)

    def test_main_asks_at_second_event(self):
        code, out = self.run_main(["record", "--repo", REPO, "--class", "b-class", "--summary", "s", "--ref", "new"])
        self.assertEqual(code, 0)
        self.assertIn("要不要沉淀成规则？", out)


class DegradeRevertCorrectionTest(unittest.TestCase):
    """The revert degrade also records a `revert` correction through the same code path."""

    REVERTED, REVERT_COMMIT = "a" * 40, "b" * 40
    PR = {"number": 7, "html_url": f"https://github.com/{REPO}/pull/7", "labels": [{"name": "autopilot:authored"}]}

    def fake(self):
        api = FakeGitHub()
        inner = api.__call__

        def call(path, method="GET", body=None):
            if method == "GET" and path.endswith("/pulls"):
                return [self.PR]
            if method == "GET" and "labels=autopilot:degrade" in path:
                return []
            return inner(path, method, body)
        api.call = call
        return api, call

    def event(self, sha=None):
        msg = f'Revert "fix: x (#7)"\n\nThis reverts commit {self.REVERTED}.\n'
        return {"ref": "refs/heads/main", "repository": {"full_name": REPO, "default_branch": "main"},
                "commits": [{"id": sha or self.REVERT_COMMIT, "message": msg}]}

    def test_revert_records_correction_with_commit_ref(self):
        api, call = self.fake()
        out = degrade.run("push", self.event(), call)
        self.assertEqual(out["opened"], 1)
        titles = [i["title"] for i in api.issues]
        self.assertTrue(any(t.startswith("纠正：revert — ") for t in titles))
        event = next(i for i in api.issues if "correction:revert" in {lb["name"] for lb in i["labels"]})
        payload = c.issue_payload(event)
        self.assertEqual(payload["ref"], self.REVERT_COMMIT)
        self.assertIn("pull/7", payload["summary"])

    def test_second_revert_opens_candidate_and_notes_the_question(self):
        api, call = self.fake()
        degrade.run("push", self.event(), call)
        out = degrade.run("push", self.event("c" * 40), call)
        self.assertEqual(len(candidates(api)), 1)
        self.assertTrue(any("要不要沉淀成规则？" in n for n in out["notes"]))

    def test_same_revert_commit_is_not_recorded_twice(self):
        api, call = self.fake()
        degrade.run("push", self.event(), call)
        degrade.run("push", self.event(), call)
        events = [i for i in api.issues if "correction:revert" in {lb["name"] for lb in i["labels"]}]
        self.assertEqual(len(events), 1)

    def test_correction_failure_does_not_hide_the_degrade(self):
        api, call = self.fake()
        api.fail_on = ("GET", "labels=correction:revert")
        out = degrade.run("push", self.event(), call)
        self.assertEqual(out["opened"], 1)
        self.assertTrue(any("纠错记录没写成" in n for n in out["notes"]))

    def test_non_revert_reasons_record_nothing(self):
        api, call = self.fake()
        ev = {"action": "labeled", "label": {"name": "autopilot:freeze"},
              "repository": {"full_name": REPO, "default_branch": "main"},
              "issue": {"number": 5, "html_url": f"https://github.com/{REPO}/issues/5", "labels": [{"name": "autopilot:freeze"}]}}
        degrade.run("issues", ev, call)
        self.assertFalse([i for i in api.issues if "correction" in {lb["name"] for lb in i["labels"]}])


if __name__ == "__main__":
    unittest.main()
