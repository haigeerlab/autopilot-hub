"""Unit tests for slice-2 auto-degrade (open `autopilot:degrade` issues on GitHub-side incidents).

Run: python3 -B -m unittest discover -s tests -v
Stdlib only; the GitHub API is replaced by a fake.
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from admission import decide as admission  # noqa: E402
from degrade import run as r  # noqa: E402

REPO = "haigeerlab/agent-relay"
REVERTED = "a" * 40
REVERT_COMMIT = "b" * 40
MERGE = "c" * 40
AUTHORED = {"number": 7, "html_url": "https://github.com/haigeerlab/agent-relay/pull/7",
            "labels": [{"name": "autopilot:authored"}]}
HUMAN = {"number": 8, "html_url": "https://github.com/haigeerlab/agent-relay/pull/8", "labels": []}


class FakeApi:
    """Records calls; `responses` maps GET paths to JSON values; missing GET paths raise."""

    def __init__(self, responses=None, fail=False):
        self.responses = dict(responses or {})
        self.responses.setdefault(f"repos/{REPO}/issues?labels=autopilot:degrade&state=open&per_page=100", [])
        self.calls = []
        self.fail = fail

    def __call__(self, path, method="GET", body=None):
        self.calls.append((method, path, body))
        if self.fail:
            raise r.ApiError("boom")
        if method == "GET":
            if "labels=correction" in path:  # slice 6: revert also records a correction event
                return self.responses.get(path, [])
            if path not in self.responses:
                raise r.ApiError(f"unexpected GET {path}")
            return self.responses[path]
        return {"number": 99, "html_url": "https://github.com/x/y/issues/99"}

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]

    def created(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == f"repos/{REPO}/issues"
                and "autopilot:degrade" in (c[2].get("labels") or [])]


def push_event(message, sha=REVERT_COMMIT, ref="refs/heads/main"):
    return {"ref": ref, "repository": {"full_name": REPO, "default_branch": "main"},
            "commits": [{"id": sha, "message": message}]}


def revert_message(target=REVERTED):
    return f'Revert "fix: something (#7)"\n\nThis reverts commit {target}.\n'


def workflow_run_event(conclusion="failure", branch="main", event="push", sha=MERGE):
    return {"repository": {"full_name": REPO, "default_branch": "main"},
            "workflow_run": {"conclusion": conclusion, "head_branch": branch, "event": event, "head_sha": sha,
                             "html_url": "https://github.com/haigeerlab/agent-relay/actions/runs/1", "name": "CI"}}


def label_event(label, issue_labels=()):
    return {"action": "labeled", "label": {"name": label},
            "repository": {"full_name": REPO, "default_branch": "main"},
            "issue": {"number": 5, "html_url": "https://github.com/haigeerlab/agent-relay/issues/5",
                      "labels": [{"name": n} for n in issue_labels] + [{"name": label}]}}


def pulls_of(sha, prs):
    return {f"repos/{REPO}/commits/{sha}/pulls": prs}


def created_payload(api):
    body = api.created()[0][2]["body"]
    return body, json.loads(admission.DEGRADE_BLOCK.findall(body)[0])


class ParseRevertTest(unittest.TestCase):
    def test_parses_reverted_sha(self):
        self.assertEqual(r.parse_revert(revert_message()), REVERTED)

    def test_requires_revert_title(self):
        self.assertIsNone(r.parse_revert(f"fix: undo\n\nThis reverts commit {REVERTED}.\n"))

    def test_requires_full_sha(self):
        self.assertIsNone(r.parse_revert('Revert "x"\n\nThis reverts commit abc123.\n'))


class RevertTest(unittest.TestCase):
    def test_revert_of_authored_pr_opens_L2_issue(self):
        api = FakeApi(pulls_of(REVERTED, [AUTHORED]))
        out = r.run("push", push_event(revert_message()), api)
        self.assertEqual(len(api.created()), 1)
        body, payload = created_payload(api)
        self.assertEqual(payload["level"], "L2")
        self.assertEqual(payload["reason"], "revert")
        self.assertEqual(payload["ref"], REVERT_COMMIT)
        self.assertEqual(payload["scope"], {"repo": REPO, "tier": "*"})
        self.assertIn("关闭本 issue", body)
        self.assertEqual(admission.degrade_levels([{"body": body}], REPO), ["L2"])
        self.assertEqual(out["opened"], 1)

    def test_revert_of_human_pr_does_nothing(self):
        api = FakeApi(pulls_of(REVERTED, [HUMAN]))
        out = r.run("push", push_event(revert_message()), api)
        self.assertEqual(api.writes(), [])
        self.assertTrue(any("autopilot:authored" in n for n in out["notes"]), out["notes"])

    def test_revert_of_commit_without_pr_does_nothing(self):
        api = FakeApi(pulls_of(REVERTED, []))
        r.run("push", push_event(revert_message()), api)
        self.assertEqual(api.writes(), [])

    def test_ordinary_commit_does_nothing(self):
        api = FakeApi()
        r.run("push", push_event("feat: add thing"), api)
        self.assertEqual(api.writes(), [])

    def test_push_to_other_branch_does_nothing(self):
        api = FakeApi(pulls_of(REVERTED, [AUTHORED]))
        r.run("push", push_event(revert_message(), ref="refs/heads/feature"), api)
        self.assertEqual(api.writes(), [])


class MainRedTest(unittest.TestCase):
    def test_failed_main_ci_on_authored_merge_opens_L2(self):
        api = FakeApi(pulls_of(MERGE, [AUTHORED]))
        r.run("workflow_run", workflow_run_event(), api)
        _, payload = created_payload(api)
        self.assertEqual((payload["reason"], payload["level"], payload["ref"]), ("main-red", "L2", MERGE))

    def test_success_does_nothing(self):
        api = FakeApi(pulls_of(MERGE, [AUTHORED]))
        r.run("workflow_run", workflow_run_event(conclusion="success"), api)
        self.assertEqual(api.writes(), [])

    def test_pr_run_does_nothing(self):
        api = FakeApi(pulls_of(MERGE, [AUTHORED]))
        r.run("workflow_run", workflow_run_event(event="pull_request"), api)
        self.assertEqual(api.writes(), [])

    def test_other_branch_does_nothing(self):
        api = FakeApi(pulls_of(MERGE, [AUTHORED]))
        r.run("workflow_run", workflow_run_event(branch="feature"), api)
        self.assertEqual(api.writes(), [])

    def test_human_merge_does_nothing(self):
        api = FakeApi(pulls_of(MERGE, [HUMAN]))
        r.run("workflow_run", workflow_run_event(), api)
        self.assertEqual(api.writes(), [])


class LabelTest(unittest.TestCase):
    def test_freeze_label_opens_L0(self):
        api = FakeApi()
        r.run("issues", label_event("autopilot:freeze"), api)
        _, payload = created_payload(api)
        self.assertEqual((payload["reason"], payload["level"]), ("freeze", "L0"))
        self.assertEqual(payload["ref"], "https://github.com/haigeerlab/agent-relay/issues/5")

    def test_regression_label_opens_L2(self):
        api = FakeApi()
        r.run("issues", label_event("autopilot:regression"), api)
        _, payload = created_payload(api)
        self.assertEqual((payload["reason"], payload["level"]), ("regression", "L2"))

    def test_other_label_does_nothing(self):
        api = FakeApi()
        r.run("issues", label_event("bug"), api)
        self.assertEqual(api.writes(), [])

    def test_label_on_a_degrade_issue_is_ignored(self):
        api = FakeApi()
        r.run("issues", label_event("autopilot:freeze", issue_labels=["autopilot:degrade"]), api)
        self.assertEqual(api.writes(), [])

    def test_unlabeled_action_does_nothing(self):
        api = FakeApi()
        ev = label_event("autopilot:freeze")
        ev["action"] = "unlabeled"
        r.run("issues", ev, api)
        self.assertEqual(api.writes(), [])


class IdempotencyAndErrorsTest(unittest.TestCase):
    def existing(self, reason, ref):
        block = json.dumps({"scope": {"repo": REPO, "tier": "*"}, "level": "L2", "reason": reason, "ref": ref})
        return {"number": 42, "body": f"<!-- autopilot-degrade:v1 {block} -->"}

    def test_same_reason_and_ref_comments_instead_of_reopening(self):
        responses = pulls_of(REVERTED, [AUTHORED])
        responses[f"repos/{REPO}/issues?labels=autopilot:degrade&state=open&per_page=100"] = [
            self.existing("revert", REVERT_COMMIT)]
        api = FakeApi(responses)
        out = r.run("push", push_event(revert_message()), api)
        self.assertEqual(api.created(), [])
        self.assertEqual([c[1] for c in api.writes()], [f"repos/{REPO}/issues/42/comments"])
        self.assertEqual(out["commented"], 1)

    def test_different_ref_opens_a_new_issue(self):
        responses = pulls_of(REVERTED, [AUTHORED])
        responses[f"repos/{REPO}/issues?labels=autopilot:degrade&state=open&per_page=100"] = [
            self.existing("revert", "d" * 40)]
        api = FakeApi(responses)
        r.run("push", push_event(revert_message()), api)
        self.assertEqual(len(api.created()), 1)

    def test_api_failure_raises_and_writes_nothing(self):
        api = FakeApi(fail=True)
        with self.assertRaises(r.ApiError):
            r.run("push", push_event(revert_message()), api)
        self.assertEqual(api.writes(), [])

    def test_lookup_failure_does_not_open_an_issue(self):
        api = FakeApi()  # commits/{sha}/pulls not stubbed -> ApiError
        with self.assertRaises(r.ApiError):
            r.run("push", push_event(revert_message()), api)
        self.assertEqual(api.writes(), [])

    def test_unknown_event_does_nothing(self):
        api = FakeApi()
        out = r.run("schedule", {"repository": {"full_name": REPO, "default_branch": "main"}}, api)
        self.assertEqual(api.writes(), [])
        self.assertEqual(out["opened"], 0)


class ParsePagesTest(unittest.TestCase):
    def test_concatenated_pages(self):
        self.assertEqual(r.parse_pages('[1,2]\n[3]'), [1, 2, 3])

    def test_single_object(self):
        self.assertEqual(r.parse_pages('{"a": 1}'), {"a": 1})


if __name__ == "__main__":
    unittest.main()
