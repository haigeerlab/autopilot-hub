"""Unit tests for slice-3 recheck (weekly readiness recheck on the user's Mac -> degrade issues).

Run: python3 -B -m unittest discover -s tests -v
The GitHub API, git and repo-readiness are replaced by fakes; files live in a temp dir.
"""
import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from admission import decide as admission  # noqa: E402
from degrade import run as degrade  # noqa: E402
from recheck import run as r  # noqa: E402

REPO = "haigeerlab/agent-relay"
COMMIT_031 = "c5194789a0cb338436865f29e40f32a6be2833f6"
RULER = b'{"id": "autopr-onboarding", "clauses": []}\n'
RULER_SHA = hashlib.sha256(RULER).hexdigest()
SEED = b'{"schema": "repo-readiness.report/v1", "category": "plugin", "results": []}\n'
SEED_SHA = hashlib.sha256(SEED).hexdigest()


def conclusion(ruler_sha=RULER_SHA, report_sha=SEED_SHA):
    return {"schema": "repo-readiness.conclusion/v1", "repo": REPO, "default_branch": "main",
            "trust_cap": "L0", "basis": {"report_sha256": report_sha,
                                         "rulesets": [{"id": "autopr-onboarding", "kind": "base",
                                                       "version": "2.0.0-trial.1", "sha256": ruler_sha}],
                                         "tool": {"version": "0.2.0", "commit": "c" * 40}}}


class FakeApi:
    def __init__(self, conclusion_doc=None, open_issues=(), fail=False):
        self.conclusion_doc = conclusion_doc
        self.open_issues = list(open_issues)
        self.fail = fail
        self.calls = []

    def __call__(self, path, method="GET", body=None):
        self.calls.append((method, path, body))
        if self.fail:
            raise degrade.ApiError("boom")
        if method != "GET":
            return {"number": 99, "html_url": "https://github.com/haigeerlab/agent-relay/issues/99"}
        if path == f"repos/{REPO}":
            return {"default_branch": "main"}
        if path.startswith(f"repos/{REPO}/contents/.github/autopr/readiness-conclusion.json"):
            if self.conclusion_doc is None:
                raise degrade.NotFound("404")
            raw = json.dumps(self.conclusion_doc).encode()
            return {"content": base64.b64encode(raw).decode(), "encoding": "base64"}
        if path.startswith(f"repos/{REPO}/issues?"):
            return self.open_issues
        raise degrade.ApiError(f"unexpected GET {path}")

    def created(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == f"repos/{REPO}/issues"]


class FakeShell:
    """git clone / checkout / rev-parse / status, and `repo-readiness check` writing a canned report."""

    def __init__(self, report, check_rc=0, head=COMMIT_031, dirty=""):
        self.report = report
        self.check_rc = check_rc
        self.head = head
        self.dirty = dirty
        self.calls = []

    def __call__(self, args, cwd=None):
        self.calls.append(list(args))
        if args[:2] == ["git", "clone"]:
            dest = args[-1]
            os.makedirs(dest, exist_ok=True)
            if "repo-readiness" in args[-2]:
                os.makedirs(os.path.join(dest, "tests", "fixtures", "rulesets"), exist_ok=True)
                with open(os.path.join(dest, "tests", "fixtures", "rulesets", "trial.json"), "wb") as f:
                    f.write(RULER)
            return 0, "", ""
        if args[:3] == ["git", "-C", args[2]] and args[3:4] == ["rev-parse"]:
            return 0, self.head + "\n", ""
        if args[:3] == ["git", "-C", args[2]] and args[3:4] == ["status"]:
            return 0, self.dirty, ""
        if args[:3] == ["git", "-C", args[2]]:
            return 0, "", ""
        if args[1] == "check":
            out = args[args.index("--out") + 1]
            os.makedirs(out, exist_ok=True)
            if self.report is not None:
                with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as f:
                    json.dump(self.report, f)
            return self.check_rc, "", ""
        raise AssertionError(f"unexpected command {args}")

    def check_args(self):
        return next(c for c in self.calls if len(c) > 1 and c[1] == "check")


def report(signals=None, requests=()):
    return {"schema": "repo-readiness.report/v1", "category": "plugin", "results": [],
            "drop_signals": list(signals or []), "judgment_requests": list(requests)}


SIGNAL = {"clause": "G2", "target": "repo", "rule": "immediate", "streak": 1,
          "from_tier": "basic", "to_tier": None, "to_trust_cap": "L0"}


class RecheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state")
        self.seed = os.path.join(self.tmp, "seed-report.json")
        with open(self.seed, "wb") as f:
            f.write(SEED)
        self.allowed = {"tools": [{"name": "repo-readiness", "version": "0.3.1", "commit": COMMIT_031}]}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def entry(self, **kw):
        e = {"repo": REPO, "seed_report": self.seed}
        e.update(kw)
        return e

    def go(self, api, sh, entry=None, tool_version="0.3.1"):
        return r.recheck_repo(entry or self.entry(), api=api, sh=sh, state_dir=self.state,
                              allowed_tools=self.allowed, tool_version=tool_version)

    def baseline_path(self):
        return os.path.join(self.state, "haigeerlab__agent-relay", "report.json")

    def write_baseline(self, data=b'{"category": "plugin"}'):
        os.makedirs(os.path.dirname(self.baseline_path()), exist_ok=True)
        with open(self.baseline_path(), "wb") as f:
            f.write(data)

    # --- happy paths ---
    def test_drop_signal_opens_readiness_drop_issue(self):
        self.write_baseline()
        api, sh = FakeApi(conclusion()), FakeShell(report([SIGNAL]))
        out = self.go(api, sh)
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(len(api.created()), 1)
        body = api.created()[0][2]["body"]
        payload = json.loads(admission.DEGRADE_BLOCK.findall(body)[0])
        self.assertEqual((payload["reason"], payload["level"]), ("readiness-drop", "L0"))
        self.assertEqual(len(payload["ref"]), 64)  # sha256 of the new report file
        self.assertIn("G2", body)
        self.assertEqual(admission.degrade_levels([{"body": body}], REPO), ["L0"])
        self.assertIn("--baseline", sh.check_args())

    def test_no_signal_opens_nothing_and_updates_baseline(self):
        self.write_baseline()
        api, sh = FakeApi(conclusion()), FakeShell(report())
        out = self.go(api, sh)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(api.created(), [])
        self.assertIn("drop_signals", open(self.baseline_path(), encoding="utf-8").read())

    def test_lowest_to_trust_cap_wins_and_null_is_L0(self):
        self.write_baseline()
        s1 = dict(SIGNAL, clause="G7", to_trust_cap="L2")
        s2 = dict(SIGNAL, clause="G5", to_trust_cap=None)
        api = FakeApi(conclusion())
        self.go(api, FakeShell(report([s1, s2])))
        payload = json.loads(admission.DEGRADE_BLOCK.findall(api.created()[0][2]["body"])[0])
        self.assertEqual(payload["level"], "L0")

    def test_invalid_to_trust_cap_is_L0(self):
        self.write_baseline()
        api = FakeApi(conclusion())
        self.go(api, FakeShell(report([dict(SIGNAL, to_trust_cap="L9")])))
        payload = json.loads(admission.DEGRADE_BLOCK.findall(api.created()[0][2]["body"])[0])
        self.assertEqual(payload["level"], "L0")

    def test_first_run_uses_seed_report_matching_conclusion(self):
        api, sh = FakeApi(conclusion()), FakeShell(report([SIGNAL]))
        out = self.go(api, sh)
        self.assertEqual(out["status"], "ok")
        args = sh.check_args()
        self.assertEqual(args[args.index("--baseline") + 1], self.seed)
        self.assertEqual(args[args.index("--category") + 1], "plugin")
        self.assertEqual(len(api.created()), 1)

    def test_first_run_without_matching_seed_only_creates_baseline(self):
        api, sh = FakeApi(conclusion(report_sha="f" * 64)), FakeShell(report([SIGNAL]))
        out = self.go(api, sh)
        self.assertEqual(out["status"], "baseline-created")
        self.assertNotIn("--baseline", sh.check_args())
        self.assertEqual(api.created(), [])
        self.assertTrue(os.path.exists(self.baseline_path()))

    def test_judgment_requests_are_listed_not_resolved(self):
        self.write_baseline()
        out = self.go(FakeApi(conclusion()), FakeShell(report(requests=[{"clause": "D1"}])))
        self.assertTrue(any("D1" in n for n in out["notes"]), out["notes"])

    # --- skips ---
    def test_no_conclusion_is_skipped(self):
        api, sh = FakeApi(None), FakeShell(report([SIGNAL]))
        out = self.go(api, sh)
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(sh.calls, [])

    def test_tool_not_in_allowlist_is_skipped(self):
        out = self.go(FakeApi(conclusion()), FakeShell(report([SIGNAL])), tool_version="0.3.0")
        self.assertEqual(out["status"], "skipped")
        self.assertTrue(any("允许列表" in n for n in out["notes"]))

    def test_ruler_changed_is_skipped(self):
        api = FakeApi(conclusion(ruler_sha="e" * 64))
        out = self.go(api, FakeShell(report([SIGNAL])))
        self.assertEqual(out["status"], "skipped")
        self.assertTrue(any("尺子" in n for n in out["notes"]))
        self.assertEqual(api.created(), [])

    # --- idempotency and failures ---
    def test_same_report_is_not_opened_twice(self):
        self.write_baseline()
        rep = report([SIGNAL])
        sha = hashlib.sha256(json.dumps(rep).encode()).hexdigest()
        block = json.dumps({"scope": {"repo": REPO, "tier": "*"}, "level": "L0", "reason": "readiness-drop", "ref": sha})
        api = FakeApi(conclusion(), open_issues=[{"number": 42, "body": f"<!-- autopilot-degrade:v1 {block} -->"}])
        self.go(api, FakeShell(rep))
        self.assertEqual(api.created(), [])

    def test_dirty_tool_checkout_fails(self):
        self.write_baseline(b'{"keep": 1}')
        out = self.go(FakeApi(conclusion()), FakeShell(report([SIGNAL]), dirty=" M x\n"))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(open(self.baseline_path(), "rb").read(), b'{"keep": 1}')

    def test_wrong_tool_commit_fails(self):
        out = self.go(FakeApi(conclusion()), FakeShell(report([SIGNAL]), head="9" * 40))
        self.assertEqual(out["status"], "failed")

    def test_check_usage_error_fails_and_keeps_baseline(self):
        self.write_baseline(b'{"keep": 1}')
        api = FakeApi(conclusion())
        out = self.go(api, FakeShell(report([SIGNAL]), check_rc=3))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(api.created(), [])
        self.assertEqual(open(self.baseline_path(), "rb").read(), b'{"keep": 1}')

    def test_check_exit_1_is_normal(self):
        self.write_baseline()
        out = self.go(FakeApi(conclusion()), FakeShell(report([SIGNAL]), check_rc=1))
        self.assertEqual(out["status"], "ok")

    def test_api_failure_fails_without_writing(self):
        self.write_baseline(b'{"keep": 1}')
        api = FakeApi(conclusion(), fail=True)
        out = self.go(api, FakeShell(report([SIGNAL])))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(open(self.baseline_path(), "rb").read(), b'{"keep": 1}')


class NotFoundTest(unittest.TestCase):
    def test_not_found_is_an_api_error(self):
        self.assertTrue(issubclass(degrade.NotFound, degrade.ApiError))


if __name__ == "__main__":
    unittest.main()
