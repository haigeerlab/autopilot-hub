"""Unit tests for slice-2 auto-degrade (open `autopilot:degrade` issues on GitHub-side incidents).

Run: python3 -B -m unittest discover -s tests -v
Stdlib only; the GitHub API is replaced by a fake.
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

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
            if "issues?state=all" in path:  # slice 6: revert also records a correction event
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
        self.assertFalse(admission.degrade_item([{"number": 1, "body": body}], REPO)[0]["ok"])
        self.assertIn("这个仓库不会自动合并", body)
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


def closed_event(sender_login="haigeerlab", sender_type="User", labels=("autopilot:degrade",)):
    return {"action": "closed", "repository": {"full_name": REPO, "default_branch": "main"},
            "sender": {"login": sender_login, "type": sender_type},
            "issue": {"number": 42, "html_url": "https://github.com/haigeerlab/agent-relay/issues/42",
                      "labels": [{"name": n} for n in labels]}}


def permission_of(login, perm):
    return {f"repos/{REPO}/collaborators/{login}/permission": {"permission": perm}}


class CloseCheckTest(unittest.TestCase):
    """Slice 11 (B11): only a repo admin closing a degrade issue lifts it; anyone else's close is undone."""

    def reopened(self, api):
        return [c for c in api.calls if c[0] == "PATCH" and c[1] == f"repos/{REPO}/issues/42"]

    def test_admin_close_stands(self):
        api = FakeApi(permission_of("haigeerlab", "admin"))
        out = r.run("issues", closed_event(), api)
        self.assertEqual(self.reopened(api), [])
        self.assertEqual(out["reopened"], 0)

    def test_writer_close_is_reopened_with_comment(self):
        api = FakeApi(permission_of("helper", "write"))
        out = r.run("issues", closed_event("helper"), api)
        self.assertEqual(self.reopened(api)[0][2], {"state": "open"})
        comments = [c for c in api.calls if c[0] == "POST" and c[1] == f"repos/{REPO}/issues/42/comments"]
        self.assertIn("管理员", comments[0][2]["body"])
        self.assertEqual(out["reopened"], 1)

    def test_bot_close_is_reopened_without_permission_lookup(self):
        api = FakeApi()
        r.run("issues", closed_event("haigeerlab-autopilot[bot]", "Bot"), api)
        self.assertEqual(len(self.reopened(api)), 1)
        self.assertFalse([c for c in api.calls if "collaborators" in c[1]])

    def test_non_degrade_issue_close_is_ignored(self):
        api = FakeApi()
        r.run("issues", closed_event("helper", labels=("bug",)), api)
        self.assertEqual(api.writes(), [])


class SkipMainRedTest(unittest.TestCase):
    """R18: opting in hands main CI failures to watch and keeps the other triggers."""

    def run_event(self, skip_main_red, event_name, event, api):
        log = io.StringIO()
        with TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            summary_path = Path(tmp) / "summary.md"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            with patch.dict(os.environ, {"SKIP_MAIN_RED": skip_main_red}), patch.object(r, "gh_api", api):
                with redirect_stdout(log):
                    self.assertEqual(r.main(["--event-name", event_name, "--event-path", str(event_path),
                                             "--out-md", str(summary_path)]), 0)
            return log.getvalue(), summary_path.read_text(encoding="utf-8")

    def test_main_ci_failure_is_skipped_only_when_enabled(self):
        for skip_main_red in ("false", "true"):
            with self.subTest(skip_main_red=skip_main_red):
                api = FakeApi(pulls_of(MERGE, [AUTHORED]))
                log, summary = self.run_event(skip_main_red, "workflow_run", workflow_run_event(), api)
                if skip_main_red == "true":
                    self.assertEqual(api.calls, [])
                    self.assertIn("skip_main_red=true", log)
                    self.assertIn("watch", log)
                    self.assertIn("skip_main_red=true", summary)
                else:
                    self.assertEqual(len(api.created()), 1)
                    _, payload = created_payload(api)
                    self.assertEqual((payload["reason"], payload["level"], payload["ref"]),
                                     ("main-red", "L2", MERGE))

    def test_revert_regression_and_freeze_open_degrade_in_both_modes(self):
        cases = (
            ("push", push_event(revert_message()), pulls_of(REVERTED, [AUTHORED]), "revert", "L2"),
            ("issues", label_event("autopilot:regression"), {}, "regression", "L2"),
            ("issues", label_event("autopilot:freeze"), {}, "freeze", "L0"),
        )
        for skip_main_red in ("false", "true"):
            for event_name, event, responses, reason, level in cases:
                with self.subTest(skip_main_red=skip_main_red, reason=reason):
                    api = FakeApi(responses)
                    self.run_event(skip_main_red, event_name, event, api)
                    self.assertEqual(len(api.created()), 1)
                    _, payload = created_payload(api)
                    self.assertEqual((payload["reason"], payload["level"]), (reason, level))
                    if reason == "revert":
                        self.assertEqual(payload["ref"], REVERT_COMMIT)
                        self.assertTrue(any(method == "POST" and "correction:revert" in body.get("labels", [])
                                            for method, _, body in api.writes()))
                    else:
                        self.assertEqual(payload["ref"], event["issue"]["html_url"])

    def test_non_admin_close_is_reopened_with_comment_in_both_modes(self):
        for skip_main_red in ("false", "true"):
            with self.subTest(skip_main_red=skip_main_red):
                api = FakeApi(permission_of("helper", "write"))
                self.run_event(skip_main_red, "issues", closed_event("helper"), api)
                issue_path = f"repos/{REPO}/issues/42"
                reopen, comment = api.writes()
                self.assertEqual(reopen, ("PATCH", issue_path, {"state": "open"}))
                self.assertEqual(comment[:2], ("POST", f"{issue_path}/comments"))
                self.assertIn("管理员", comment[2]["body"])


class RevertLabelTest(unittest.TestCase):
    def test_revert_does_not_call_issue_labels(self):
        api = FakeApi(pulls_of(REVERTED, [AUTHORED]))
        r.run("push", push_event(revert_message()), api)
        labels = [c for c in api.calls if "/issues/" in c[1] and c[1].endswith("/labels")]
        self.assertEqual(labels, [])


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
