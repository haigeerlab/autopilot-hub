"""Unit tests for the slice-10 local orchestrator flow (docs/spec/slice-10.md). GitHub, git and delegate are fakes.

Run: python3 -B -m unittest discover -s tests -v
"""
import base64
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from admission import decide as d
from orchestrator import cli, flow
from orchestrator.state import StateCorruptError, StateLockedError, WatchState

if __package__:
    from ._checkup_util import commit_on_branch, git, make_repo, write_files
    from .test_verify import case
else:
    from _checkup_util import commit_on_branch, git, make_repo, write_files
    from test_verify import case

REPO = "haigeerlab/autopilot-sandbox"
HEAD = "a" * 40
BOT = d.load_bot_identity(os.path.join(ROOT, "bot-identity.json"))
BOT_USER = {"login": BOT["login"], "id": BOT["id"], "type": "Bot"}
T0 = "2026-11-22T01:00:00Z"
APP_ID = 123456
VERIFIED = "autopilot/verified"


def b64(text):
    return base64.b64encode(text.encode()).decode()


def report(blocking=0, status="completed", outcome=None, unreviewed=(), fp="f" * 64, recheck=None):
    findings = [{"title": f"问题 {i}", "severity": "blocking"} for i in range(blocking)]
    return {"execution_status": status, "backend": "codex",
            "review_outcome": outcome or ("findings" if blocking else "no_findings_in_scope"),
            "scope": {"fingerprint": {"sha256": fp} if fp else None}, "findings": findings,
            "unreviewed": [{"path": p, "reason": "x"} for p in unreviewed], "recheck": recheck}


def effective(merge="auto"):
    return {"design": "reviewer", "merge": merge, "release": "human", "designForcedBy": None}


class FakeWork:
    def __init__(self, io):
        self.io = io
        self.merge_base = "b" * 40
        self.base_tip = "t" * 40

    def regression(self, module, ref):
        return self.io.regression_head if ref == self.io.pr['head']['sha'] else self.io.regression_base

    def changed_existing_files(self, module):
        return ({'sha': self.io.scope_base or self.merge_base,
                 'source': 'scope_base' if self.io.scope_base else 'merge-base'}, self.io.changed_existing)

    def patch_id(self):
        return 'c' * 40

    def run_case(self, command, sha):
        self.io.case_runs.append((command, sha))
        return {'exit': 0, 'timeout': False, 'output': '成功'}

    def find_check(self):
        return self.io.check_command

    def read_pipeline(self):
        return self.io.contents.get(".github/autopr/pipeline.yml")

    def run_check(self, command):
        self.io.ran.append(command)
        return self.io.check_results.get(command, flow.check_result(self.io.check_exit, "ok\n"))

    def base_view(self):
        self.io.module_reads.append('base')
        return self.io.base

    def module_view(self):
        self.io.module_reads.append('head')
        return self.io.view

    def in_scope(self, module):
        self.io.scope_queries = getattr(self.io, 'scope_queries', []) + [module]
        return module in getattr(self.io, 'in_scope', ())

    def review(self, previous):
        self.io.reviews.append(previous)
        return self.io.reports.pop(0)


class FakeIO:
    def __init__(self, *, merge="auto", files=("docs/a.md",), author=None, fork=False, degrade=(), takeover=True,
                 reports=None, check_exit=0, check_command="make test", stored=None, merged_ok=True, bot=BOT,
                 module="__legacy__", acceptance=None, base_takeover=None):
        if module == "__legacy__":
            module, acceptance = "m", accepted()
        self.calls, self.comments, self.ran, self.reviews, self.events = [], [], [], [], []
        self.statuses, self.module_reads = [], []
        self.automerge_replies = []
        self.check_runs, self.commit_statuses = [], []
        self.base_is_current = True
        self.config = {"mergeMode": "direct"}  # Preserve the existing tests of direct merges and admission.
        self.branch_rules, self.legacy_rules = [], None
        self.evidence_files = []
        self.issue_comments, self.narrow_prs, self.narrow_result = {}, [], "https://github.com/x/pull/9"
        self.view = {"modules": [{"id": module, "review": {"spec": "codex-approved", "plan": "approved"},
                                  "reviewApprovedBy": {"spec": "codex", "plan": "user"}}]} if module else None
        self.module, self.acceptance_record = module, acceptance
        self.bot = bot
        self.check_command, self.check_exit = check_command, check_exit
        self.check_results = {}
        self.scope_base, self.changed_existing = None, []
        self.reports = list(reports if reports is not None else [report()])
        self.stored = stored
        self.merged_ok = merged_ok
        self.merged = False
        self.pr = {"number": 7, "state": "open", "draft": False, "user": author or BOT_USER, "labels": [],
                   "node_id": "PR_node", "auto_merge": None,
                   "head": {"sha": HEAD, "repo": {"full_name": "someone/fork" if fork else REPO}},
                   "base": {"ref": "main", "repo": {"owner": {"login": "haigeerlab"}}}}
        self.files = [{"filename": f, "status": "modified", "additions": 1, "deletions": 0,
                       "patch": "@@ -1 +1 @@\n+x\n"} for f in files]
        self.contents = {".github/autopr/risk-tiers.yml": "extends: plugin\n"}
        if takeover:
            self.contents[".github/autopr/takeover.yml"] = f"merge: {merge}\n"
        self.base = None
        if module:  # a spec-guard project: effective takeover on the base commit
            self.contents["spec/CAPABILITY-MAP.md"] = "# 能力图\n"
            entries = base_takeover if base_takeover is not None else {module: effective()}
            self.base = {"modules": [{"id": key, "takeover": value} for key, value in entries.items()]}
        self.degrade = [{"number": n, "body": '<!-- autopilot-degrade:v1 {"scope": {"repo": "%s"}} -->' % REPO}
                        for n in degrade]

    # --- io interface ---
    def bot_identity(self):
        return self.bot

    def local_config(self):
        return self.config

    def api(self, path, method="GET", body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path.endswith("/comments"):
            cid = len(self.comments) + 1
            comment = {"id": cid, "body": body["body"], "user": BOT_USER,
                       "html_url": f"https://github.com/{REPO}/pull/7#issuecomment-{cid}",
                       "performed_via_github_app": {"slug": BOT["app_slug"]},
                       "created_at": T0, "updated_at": T0}
            self.comments.append(comment)
            return dict(comment)
        if method == "POST" and "/statuses/" in path:
            self.statuses.append((path, body))
            return body
        if path == "graphql":
            self.pr["auto_merge"] = None
            return {"data": {"disablePullRequestAutoMerge": {"pullRequest": {"id": self.pr["node_id"]}}}}
        if method == "PUT" and path.endswith("/merge"):
            self.merged = True
            return {"merged": True}
        if method == "POST" and path.endswith("/labels"):
            labels = {label["name"] for label in self.pr["labels"]} | set(body["labels"])
            self.pr["labels"] = [{"name": name} for name in sorted(labels)]
            return self.pr["labels"]
        if method == "POST":
            return {}
        if path == f"repos/{REPO}/pulls/7":
            if self.merged and self.merged_ok:
                return dict(self.pr, merged=True, merge_commit_sha="m" * 40)
            return self.pr
        if path.startswith(f"repos/{REPO}/issues/7/comments"):
            return list(self.comments)
        for n, cs in self.issue_comments.items():
            if path.startswith(f"repos/{REPO}/issues/{n}/comments"):
                return list(cs)
        if path.startswith(f"repos/{REPO}/pulls/7/files"):
            return self.files
        if path.startswith(f"repos/{REPO}/issues?labels=autopilot:degrade"):
            return self.degrade
        if path == f"repos/{REPO}":
            return {"allow_squash_merge": True, "allow_merge_commit": True}
        raise AssertionError(f"unexpected GET {path}")

    def api_optional(self, path):
        self.calls.append(("GET", path, None))
        if path == f"repos/{REPO}/rules/branches/main":
            return self.branch_rules
        name = path.split("/contents/", 1)[1].split("?", 1)[0]
        text = self.contents.get(name)
        return {"content": b64(text)} if text is not None else None

    def legacy_status_checks(self, repo, branch, *, need_source, need_sync, need_all=False):
        self.calls.append(("GET", f"repos/{repo}/branches/{branch}/protection/required_status_checks", None))
        return self.legacy_rules

    def enable_automerge(self, pr_id, head, method):
        self.calls.append(("POST", "graphql", {"pullRequestId": pr_id, "expectedHeadOid": head, "mergeMethod": method}))
        rejection = (self.automerge_replies.pop(0) if self.automerge_replies else
                     "Auto-merge is already enabled" if self.pr["auto_merge"] else None)
        if rejection is None:
            self.pr["auto_merge"] = {"merge_method": method, "enabled_by": BOT_USER}
        return rejection

    def commit_check_results(self, repo, head):
        self.calls.extend(("GET", f"repos/{repo}/commits/{head}/{endpoint}", None)
                          for endpoint in ("check-runs", "status"))
        return self.check_runs, self.commit_statuses

    def base_is_ancestor(self, repo, base_ref, head):
        self.calls.append(("GET", f"repos/{repo}/compare/{head}...{base_ref}", None))
        return self.base_is_current

    @contextlib.contextmanager
    def checkout(self, repo, pr):
        yield FakeWork(self)

    @contextlib.contextmanager
    def verification_worktree(self, repo, head, base_ref):
        yield 'checkout'

    def verification_paths(self, repo, patch_id):
        return ('evidence/patch.verification.json', 'evidence/patch.logs', 'evidence/patch.cases.json')

    def verification_case_lock(self, path):
        return contextlib.nullcontext()

    def read_bytes(self, path):
        if path not in self.verification_files:
            raise FileNotFoundError(path)
        return self.verification_files[path]

    def write_verification_file(self, path, content):
        self.verification_files[path] = content.encode() if isinstance(content, str) else content

    def verification_check(self, root, module, report):
        self.verify_calls.append('check')
        return self.verify_code, {'problems': [{'code': self.verify_problem}]} if self.verify_code else {}, ''

    def verification_record(self, root, module, report):
        import hashlib
        self.verify_calls.append('record')
        raw = self.verification_files[report]
        data = json.loads(raw)
        self.verification_map = {'modules': [{'id': module, 'verification': {
            'current': self.record_current, 'reportSha256': hashlib.sha256(raw).hexdigest(), 'verdict': data['verdict']}}]}
        return 0, {}, ''

    def verification_view(self, root):
        return self.verification_map

    def ledger(self, event):
        self.events.append(event)

    def write_check_evidence(self, repo, data):
        self.evidence_files.append(data)

    def open_narrow_pr(self, repo, base_ref, module, issue_number, reason):
        self.narrow_prs.append((module, issue_number, reason))
        return self.narrow_result

    def find_modules(self, repo, head):
        self.module_reads.append('find')
        rec = (self.acceptance_record or {}).get("record") or {}
        extra = getattr(self, "extra_modules", [])
        return ([rec["module"]] if rec.get("sha") == head and rec.get("module") else []) + extra

    def acceptance(self, repo, module):
        self.module_reads.append('acceptance')
        return self.acceptance_record

    def now(self):
        return T0

    # --- helpers ---
    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]

    @property
    def graphql_calls(self):
        return [body for method, path, body in self.calls if (method, path) == ("POST", "graphql")]

    def merge_calls(self):
        return [c for c in self.calls if c[0] == "PUT"]


def run(io, **kw):
    if 'state' in kw:
        return flow.run(io, REPO, 7, **kw)
    with tempfile.TemporaryDirectory() as directory:
        return flow.run(io, REPO, 7, state=WatchState(directory), **kw)


def legacy_io(**kw):
    return FakeIO(module="m", acceptance=accepted(), **kw)


def assert_artifact_blocks_merge(test, io, result, reason, step="admission"):
    test.assertEqual((result["result"], result["step"]), ("stopped", step), result)
    test.assertIn(reason, result["reason"])
    risk = flow.evaluate(io, REPO, io.pr, BOT)["risk"]
    test.assertTrue(risk["human_only"], risk)
    test.assertFalse(risk["auto_merge_eligible"], risk)
    test.assertEqual(io.merge_calls(), [])


class VerificationTest(unittest.TestCase):
    def io(self, cases=None, base_cases=None):
        io = FakeIO(module='m', files=('tasks/m/regression.md',))
        io.base['modules'][0]['tier'] = 'normal'
        io.view['modules'][0].update(tier='normal', requirementConfirm={'status': 'confirmed', 'by': 'user'},
                                     designApproval={'status': 'approved', 'approvedBy': 'reviewer'})
        cases = cases or [case()]
        doc = dict(document='tasks/m/regression.md', sha256='d'*64, cases=cases, problems=[])
        io.regression_head = (0, doc)
        io.regression_base = (0, dict(doc, cases=base_cases if base_cases is not None else cases))
        io.verification_files, io.case_runs, io.verify_calls = {}, [], []
        io.verify_code, io.verify_problem, io.record_current = 0, None, True
        io.verification_map = {'modules': [{'id': 'm', 'verification': None}]}
        io.verify_issues, io.verify_comments = [], []
        io.pr['base']['repo']['owner']['id'] = 1
        original = io.api
        def api(path, method='GET', body=None):
            if path.startswith(f'repos/{REPO}/issues?labels=autopilot:verify'):
                io.calls.append((method, path, body))
                return io.verify_issues
            if path == f'repos/{REPO}/issues' and method == 'POST':
                io.calls.append((method, path, body))
                # GitHub leaves performed_via_github_app empty on App-created issues.
                issue = dict(number=10, id=100, body=body['body'], user=BOT_USER,
                             performed_via_github_app=None, created_at=T0, updated_at=T0)
                io.verify_issues.append(issue)
                return issue
            if path.startswith(f'repos/{REPO}/issues/10/comments'):
                io.calls.append((method, path, body))
                if method == 'GET':
                    return list(io.verify_comments)
                comment = dict(id=max([c['id'] for c in io.verify_comments] or [100])+1, body=body['body'],
                               user=BOT_USER, performed_via_github_app={'slug': BOT['app_slug']}, created_at=T0, updated_at=T0)
                io.verify_comments.append(comment)
                return comment
            return original(path, method, body)
        io.api = api
        return io

    def reply(self, io, body, owner=1, edited=False):
        io.verify_comments.append(dict(id=200+len(io.verify_comments), body=body, user={'id': owner},
                                       html_url='https://example.test/comment', created_at=T0,
                                       updated_at='edited' if edited else T0))

    def report(self, io):
        return json.loads(io.verification_files['evidence/patch.verification.json'])

    def evidence(self, io):
        import re
        body = next(c['body'] for c in reversed(io.comments) if 'autopilot-evidence:v2' in c['body'])
        return json.loads(re.search(r'<!-- autopilot-evidence:v2 (.*?) -->', body, re.S).group(1))

    def test_auto_and_reuse(self):
        import hashlib
        io = self.io()
        result = run(io)
        self.assertEqual(result['result'], 'merged', result)
        self.assertEqual(io.verify_calls, ['check', 'record'])
        self.assertEqual(self.report(io)['verdict'], 'pass')
        ev = self.evidence(io)
        self.assertEqual(ev['head_sha'], HEAD)
        self.assertEqual(ev['base_sha'], 'b' * 40)
        self.assertEqual(ev['patch_id'], 'c' * 40)
        self.assertEqual(ev['module'], 'm')
        self.assertEqual(ev['runner'], 'autopilot-orchestrator')
        self.assertEqual(ev['verification'], dict(verdict='pass', regression_sha256='d' * 64,
            report_sha256=hashlib.sha256(io.verification_files['evidence/patch.verification.json']).hexdigest()))
        self.assertEqual(ev['checks'], [{k: io.evidence_files[-1]['checks'][0][k]
                                        for k in ('cmd', 'exit', 'output_sha256')}])
        self.assertEqual(ev['artifacts'], {'module': 'm', 'verified': ['tasks/m/regression.md']})
        self.assertEqual(io.merge_calls()[0][2], {'sha': HEAD, 'merge_method': 'squash'})
        self.assertTrue(any(c[1].endswith('/labels') for c in io.writes()))
        self.assertEqual(result['reason'], 'm' * 40)
        self.assertEqual(len(io.case_runs), 1)
        io.merged = False
        run(io)
        self.assertEqual(len(io.case_runs), 1)

    def test_notes_and_pre_merge_checks_are_in_v2(self):
        io = self.io([case('R3', 'manual')])
        io.contents['.github/autopr/pipeline.yml'] = "extends: plugin\nadd:\n  - id: extra\n    at: pre-merge\n    by: ai\n    run: make extra\n"
        run(io)
        self.reply(io, 'R3 通过\n意见 R3：按钮间距太小')
        result = run(io)
        self.assertEqual(result['result'], 'merged', result)
        self.assertEqual(self.evidence(io)['verification']['verdict'], 'pass-with-notes')
        self.assertEqual([c['cmd'] for c in self.evidence(io)['checks']], ['make test', 'make extra'])
        self.assertIn('按钮间距太小', next(c['body'] for c in io.comments if 'autopilot-evidence:v2' in c['body']))

    def test_new_flow_artifact_approvals(self):
        for approval in (None, {'status': 'not-required'}, {'status': 'approved', 'approvedBy': 'user'},
                         {'status': 'approved', 'approvedBy': 'reviewer'}):
            io = self.io()
            io.files.append({'filename': 'spec/m.md', 'status': 'modified', 'patch': SPEC_PATCH})
            io.view['modules'][0]['designApproval'] = approval
            result = run(io)
            self.assertEqual(result['result'], 'merged', result)
            self.assertEqual(self.evidence(io)['artifacts']['verified'], ['spec/m.md', 'tasks/m/regression.md'])
        for field, bad in (('requirementConfirm', None),
                           ('requirementConfirm', {'status': 'pending', 'by': 'user'}),
                           ('requirementConfirm', {'status': 'revised', 'by': 'user'}),
                           ('requirementConfirm', {'status': 'confirmed', 'by': 'reviewer'}),
                           ('designApproval', {'status': 'pending-user'}),
                           ('designApproval', {'status': 'in-review'}),
                           ('designApproval', {'status': 'approved', 'approvedBy': 'author'})):
            io = self.io()
            io.view['modules'][0][field] = bad
            result = run(io)
            # R4 preserves the slice 12 approval guard as human_only.
            assert_artifact_blocks_merge(self, io, result, 'spec-guard 产物没核对到有效的批准：tasks/m/regression.md')
            self.assertEqual(self.evidence(io)['artifacts']['verified'], [])

    def test_degrade_appearing_after_verification_blocks_merge(self):
        io = self.io()
        original = io.verification_record
        def record(*args):
            value = original(*args)
            io.degrade = [degrade_block(99)]
            return value
        io.verification_record = record
        result = run(io)
        self.assertEqual(result['step'], 'admission', result)
        self.assertEqual(self.evidence(io)['verification']['verdict'], 'pass')
        self.assertEqual(io.merge_calls(), [])

    def test_evidence_verification_must_match_established_report(self):
        original = flow.evidence_v2_body
        for key, changed in (('verdict', 'pass-with-notes'), ('report_sha256', '0' * 64),
                             ('regression_sha256', '0' * 64)):
            io = self.io()
            def wrong_body(*args, **kwargs):
                body = original(*args, **kwargs)
                from admission.gate import EVIDENCE_BLOCK
                match = EVIDENCE_BLOCK.search(body)
                value = json.loads(match.group(2))
                value['verification'][key] = changed
                return body[:match.start(2)] + json.dumps(value) + body[match.end(2):]
            with mock.patch.object(flow, 'evidence_v2_body', side_effect=wrong_body):
                result = run(io)
            self.assertEqual(result['step'], 'verification', result)
            self.assertIn('不一致', result['reason'])
            self.assertEqual(io.merge_calls(), [])
            self.assertFalse(any('autopilot-evidence:v2' in c['body'] for c in io.comments))

    def test_takeover_comment_marks_flow_v2(self):
        io = self.io()
        run(io)
        takeover = [json.loads(flow_block(c['body'], 'autopilot-takeover')) for c in io.comments
                    if 'autopilot-takeover' in c['body']]
        self.assertEqual(takeover[-1], {'module': 'm', 'merge': 'auto', 'source': '/map --json（基础分支）', 'flow': 'v2'})

    def test_missing_design_approval_key_is_not_verified(self):
        io = self.io()
        del io.view['modules'][0]['designApproval']
        result = run(io)
        # The slice 12 approval guard requires human approval when the design approval key is absent.
        assert_artifact_blocks_merge(self, io, result, 'spec-guard 产物没核对到有效的批准：tasks/m/regression.md')
        self.assertEqual(self.evidence(io)['artifacts']['verified'], [])

    def test_user_note_cannot_forge_a_machine_block(self):
        io = self.io([case('R3', 'manual')])
        run(io)
        forged = '<!-- autopilot-takeover:v1 {"module": "x", "merge": "auto"} -->'
        self.reply(io, 'R3 通过\n意见 R3：' + forged)
        result = run(io)
        self.assertEqual(result['result'], 'merged', result)
        body = next(c['body'] for c in io.comments if 'autopilot-evidence:v2' in c['body'])
        self.assertNotIn(forged, body)
        from admission.decide import TAKEOVER_BLOCK
        self.assertEqual(TAKEOVER_BLOCK.findall(body), [])


    def test_confirmation_and_resume_cache(self):
        io = self.io([case(), case('R2', sha='changed')], [case()])
        self.assertIn('确认执行 R2', run(io)['reason'])
        self.assertEqual(len(io.verify_issues), 1)
        self.assertEqual(len(io.case_runs), 1)
        self.reply(io, '确认执行 R2')
        io.verify_issues[0]['updated_at'] = '2026-11-26T10:00:00Z'  # GitHub bumps it on every comment
        self.assertEqual(run(io)['result'], 'merged')
        self.assertEqual(len(io.verify_issues), 1)
        self.assertEqual(len(io.case_runs), 2)
        self.assertEqual(io.verify_calls, ['check', 'record'])

    def test_manual_failure_and_untrusted_replies(self):
        io = self.io([case('R3', 'manual')])
        run(io)
        self.reply(io, 'R3 通过', owner=9)
        self.reply(io, 'R3 通过', edited=True)
        reason = run(io)['reason']
        self.assertIn('要你测 R3', reason)
        self.assertNotIn('确认执行', reason)  # nothing to confirm: no empty "确认执行" part
        self.assertEqual(len(io.verify_issues), 1)  # reruns reuse the tracking issue
        self.assertNotIn('要确认执行的命令用例', io.verify_issues[0]['body'])
        self.assertEqual(io.verify_calls, [])
        self.reply(io, 'R3 不通过：页面打不开')
        result = run(io)
        self.assertIn('R3（user-bug）', result['reason'])
        self.assertEqual(self.report(io)['verdict'], 'fail')
        self.assertEqual(result['step'], 'verification')
        self.assertEqual(io.merge_calls(), [])
        self.assertFalse(any('autopilot-evidence' in c['body'] for c in io.comments))
        self.assertEqual(self.report(io)['cases'][0]['evidence'][0]['type'], 'user-quote')

    def test_checks_and_readback_fail_closed(self):
        for code, problem in [(1, 'patch-mismatch'), (3, 'author-unknown')]:
            io = self.io()
            io.verify_code, io.verify_problem = code, problem
            result = run(io)
            self.assertEqual(result['step'], 'verification')
            self.assertIn(problem, result['reason'])
            self.assertEqual(io.verify_calls, ['check'])
        io = self.io()
        io.record_current = False
        self.assertIn('current', run(io)['reason'])

    def test_bad_documents(self):
        for code, data in [(3, {'error': 'no-document'}), (4, {'error': 'unreadable'}),
                           (0, {'problems': [{'code': 'no-cases'}]})]:
            io = self.io()
            io.regression_head = code, data
            self.assertEqual(run(io)['step'], 'verification')
            self.assertEqual(io.case_runs, [])
        io = self.io()
        io.regression_base = 4, {'error': 'unreadable'}
        self.assertIn('基础提交', run(io)['reason'])

    def test_multiple_modules_and_batch_mismatch(self):
        io = self.io()
        io.base['modules'].append(dict(id='n', tier='normal', takeover=effective()))
        io.files.append({'filename': 'spec/n.md'})
        self.assertIn('多个新流程模块', run(io)['reason'])
        io = self.io()
        io.extra_evidence = {'module': 'other'}
        with mock.patch('orchestrator.batch.batch_payload', return_value={'module': 'other'}):
            io.batch_pr = lambda *a, **k: io.pr
            self.assertEqual(run(io)['step'], 'batch')

    def unticked(self, owners=(), status='in_progress'):
        io = self.io()
        io.base['modules'][0]['status'] = status
        io.files[:] = [{'filename': 'docs/b.md', 'status': 'modified', 'additions': 1, 'deletions': 0, 'patch': '@@'}]
        io.in_scope = set(owners)
        return io

    def test_batch_unticked_implementation_names_its_module(self):
        io = self.unticked(owners=['m'])
        result = run(io, merge_method='merge')
        self.assertEqual(result['step'], 'acceptance', result)
        self.assertIn('模块 m 的范围', result['reason'])
        self.assertIn('tasks/m/todo.md', result['reason'])
        self.assertEqual(io.verify_calls, [])
        self.assertEqual(io.merge_calls(), [])

    def test_batch_unticked_implementation_in_several_scopes(self):
        io = self.unticked(owners=['m', 'n'])
        io.base['modules'].append(dict(id='n', tier='normal', status='in_progress', takeover=effective()))
        reason = run(io, merge_method='merge')['reason']
        self.assertIn('m、n', reason)
        self.assertEqual(io.merge_calls(), [])

    def test_batch_outside_every_scope_explains_how_modules_are_recognised(self):
        io = self.unticked()
        reason = run(io, merge_method='merge')['reason']
        self.assertIn('没有最终验收记录', reason)
        self.assertIn('tasks/<id>/', reason)
        self.assertIn('正在做的新流程模块有 m，', reason)
        self.assertEqual(io.scope_queries, ['m'])

    def test_batch_done_modules_are_not_scope_candidates(self):
        io = self.unticked(owners=['m'], status='done')
        reason = run(io, merge_method='merge')['reason']
        self.assertIn('没有最终验收记录', reason)
        self.assertEqual(getattr(io, 'scope_queries', []), [])

    def test_flow_v2_module_with_acceptance_record_still_verifies(self):
        io = self.io()
        io.files[:] = [{'filename': 'docs/a.md', 'status': 'modified', 'additions': 1, 'deletions': 0, 'patch': '@@'}]
        io.acceptance_record = accepted()
        result = run(io)
        self.assertEqual(result['result'], 'merged', result)
        self.assertEqual(io.verify_calls, ['check', 'record'])
        self.assertEqual(len(io.merge_calls()), 1)

    def test_failures_are_not_cached_and_org_repos_stop(self):
        io = self.io()
        original = io.case_runs
        outputs = iter([{'exit': 1, 'timeout': False, 'output': 'x'}, {'exit': 0, 'timeout': False, 'output': 'ok'},
                        {'exit': 0, 'timeout': False, 'output': 'ok'}])
        work_run = FakeWork.run_case
        def run_case(self_, command, sha):
            original.append((command, sha))
            return next(outputs)
        with mock.patch.object(FakeWork, 'run_case', run_case):
            self.assertIn('不通过', run(io)['reason'])
            self.assertEqual(run(io)['result'], 'merged')
        self.assertEqual(len(original), 3)  # head fail + baseline pass, then a fresh run
        io = self.io([case('R3', 'manual')])
        io.pr['base']['repo']['owner']['type'] = 'Organization'
        self.assertIn('组织', run(io)['reason'])
        self.assertEqual(io.verify_issues, [])

    def test_core_check_stops_cases_but_high_risk_still_verifies(self):
        io = self.io()
        io.check_exit = 1
        result = run(io)
        self.assertEqual(result['step'], 'check', result)
        self.assertEqual((io.case_runs, io.verify_calls), ([], []))
        io = self.io()
        io.config = {}
        io.files.append({'filename': '.github/workflows/ci.yml'})
        result = run(io)
        # High risk now sends the verified PR to the user after the checks and regression cases run.
        self.assertEqual(result['result'], 'handed-to-user', result)
        self.assertEqual((len(io.ran), len(io.case_runs), len(io.verify_calls)), (1, 1, 2))

    def test_dry_run_verifies_no_regression_cases(self):
        io = self.io()
        result = run(io, dry_run=True)
        self.assertEqual((result['result'], result['step']), ('dry-run', 'verification'))
        self.assertEqual(io.writes(), [])
        self.assertEqual(io.case_runs, [])
        self.assertEqual(io.verification_files, {})
        self.assertEqual(io.verify_calls, [])


def ruleset(app_id=APP_ID, *, required=True, strict=True):
    checks = [{'context': VERIFIED, 'integration_id': app_id}] if required else []
    return [{'type': 'required_status_checks', 'parameters': {
        'required_status_checks': checks, 'strict_required_status_checks_policy': strict,
    }}]


class RunRouteFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)
        self.state = WatchState(directory.name, clock=lambda: self.now)
        self.state.write_heartbeat()
        self.verify = VerificationTest()

    def new_flow(self):
        io = self.verify.io()
        io.config = {}  # The default mode is automerge.
        io.bot = dict(BOT, app_id=APP_ID)
        io.branch_rules = ruleset()
        return io

    def run_pr(self, io, **kwargs):
        return run(io, state=self.state, **kwargs)

    def cli_run(self, io, *args):
        with mock.patch.object(cli, 'LocalIO', return_value=io), \
                mock.patch.object(cli, 'WatchState', return_value=self.state), \
                mock.patch.object(cli.tokens, 'mint', return_value='token'), \
                mock.patch.object(cli.tokens, 'revoke'), contextlib.redirect_stdout(StringIO()) as stdout:
            code = cli.main(['run', '--repo', REPO, '--pr', '7', *args])
        return code, stdout.getvalue()


class AuthorizeCommandTest(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.env = dict(os.environ, HOME=home.name)
        self.path = Path(home.name, '.autopilot', 'authorizations', 'haigeerlab__autopilot-sandbox.md')

    def authorize(self, *args, repo=REPO):
        return subprocess.run([sys.executable, '-B', os.path.join(ROOT, 'orchestrator/cli.py'),
                               'authorize', '--repo', repo, *args], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def test_records_original_quote_time_session_repo_and_scope_with_private_modes(self):
        quote = '可以打开自动路。\n我同意在回归验证中执行 AI 操作用例。'
        result = self.authorize('--quote', quote, '--session', '确认会话')
        self.assertEqual(result.returncode, 0, result.stderr)
        contents = self.path.read_text(encoding='utf-8')
        for value in (quote, '确认会话', REPO, '合并前验证', 'ai-operate', '一次性克隆', '只跑一轮'):
            self.assertIn(value, contents)
        self.assertRegex(contents, r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}')
        self.assertLess(self.path.stat().st_size, 4096)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.path.is_symlink())

    def test_refuses_overwrite_then_replaces_and_revokes(self):
        first = self.authorize('--quote', '打开自动路', '--session', '原会话')
        self.assertEqual(first.returncode, 0, first.stderr)
        initial = self.path.read_bytes()
        attempted = ('--quote', '再次确认打开自动路', '--session', '新会话')
        rejected = self.authorize(*attempted)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('--replace', rejected.stderr)
        self.assertEqual(self.path.read_bytes(), initial)
        replaced = self.authorize(*attempted, '--replace')
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertIn('再次确认打开自动路', self.path.read_text(encoding='utf-8'))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        revoked = self.authorize('--revoke')
        self.assertEqual(revoked.returncode, 0, revoked.stderr)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_rejects_blank_or_oversized_authorization_and_invalid_repo(self):
        for quote, session, repo in ((' ', '会话', REPO), ('打开自动路', ' ', REPO),
                                     ('大' * 6000, '会话', REPO), ('打开自动路', '会话', '../sandbox')):
            with self.subTest(repo=repo, quote_length=len(quote), session=session):
                result = self.authorize('--quote', quote, '--session', session, repo=repo)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.path.exists())

    def test_replaces_existing_symlink_without_touching_its_target(self):
        self.path.parent.mkdir(parents=True, mode=0o700)
        target = self.path.parent / 'other.md'
        target.write_text('另一份授权', encoding='utf-8')
        self.path.symlink_to(target)
        result = self.authorize('--quote', '打开自动路', '--session', '会话', '--replace')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(target.read_text(encoding='utf-8'), '另一份授权')


class AiOperateFlowTest(RunRouteFixture):
    def setUp(self):
        super().setUp()
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        state = Path(home.name) / '.autopilot'
        self.delegate_tmp = Path(home.name) / 'delegate temp'
        self.delegate_tmp.mkdir()
        home_patch = mock.patch.dict(os.environ, HOME=home.name, TMPDIR=str(self.delegate_tmp))
        home_patch.start()
        self.addCleanup(home_patch.stop)
        state_patch = mock.patch.object(cli, 'STATE', str(state))
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.authorization = state / 'authorizations' / f'{REPO.replace("/", "__")}.md'
        self.authorization.parent.mkdir(parents=True, mode=0o700)
        self.authorization.write_text('我同意打开这个仓库的自动路。\n', encoding='utf-8')
        self.authorization.chmod(0o600)
        source = make_repo(self, {'fixture.txt': 'base'})
        commit_on_branch(source, {'fixture.txt': 'head'})
        self.head = subprocess.check_output(['git', '-C', source, 'rev-parse', 'HEAD'], text=True).strip()
        self.source_object = Path(source, '.git', 'objects', self.head[:2], self.head[2:])
        git(source, 'checkout', '-q', 'main')
        self.work = cli.Work.__new__(cli.Work)
        self.work.root, self.work.delegate = source, None
        self.work.repo = REPO
        plugin = tempfile.TemporaryDirectory()
        self.addCleanup(plugin.cleanup)
        self.plugin = Path(plugin.name)
        self.script = self.plugin / 'scripts' / 'delegate-exec.sh'
        write_files(plugin.name, {
            'scripts/delegate-exec.sh': r'''#!/bin/bash
exec python3 -B "$(dirname "$0")/fake_delegate.py" "$@"
''',
            'scripts/fake_delegate.py': '''import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
options, task = sys.argv[1:-1], sys.argv[-1]
cid = re.search(r'^用例 (R\\d+)', task).group(1)
settings = json.loads((root / 'responses.json').read_text())
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
obj = Path('.git', 'objects', head[:2], head[2:]).stat()
with (root / 'calls.jsonl').open('a', encoding='utf-8') as file:
    print(json.dumps(dict(options=options, task=task, cwd=os.getcwd(), head=head,
        object=[obj.st_dev, obj.st_ino], dot_git=Path('.git').is_dir(),
        fixture=Path('fixture.txt').read_text())), file=file)
Path('fixture.txt').write_text('changed by delegate')
response = settings[cid]
print(response['stdout'])
run_id = f'20261127120000.{os.getpid()}'
result_dir = Path(os.environ['TMPDIR']) / 'delegate'
result_dir.mkdir(exist_ok=True)
result_path = result_dir / f'{run_id}.result.json'
result = dict(runId=run_id, status=response['status'], exitCode=response['exit'])
if response['result'] is None:
    path = Path(options[options.index('--authorized-by') + 1])
    contents = path.read_bytes()
    result['authorization'] = dict(source='file', path=str(path.resolve()),
                                   sha256=hashlib.sha256(contents).hexdigest(), bytes=len(contents),
                                   extra={'keep': ['intact', 7]})
elif isinstance(response['result'], dict):
    result.update(response['result'])
if response['result'] != 'missing':
    contents = response['result'] if isinstance(response['result'], str) else json.dumps(result)
    result_path.write_text(contents, encoding='utf-8')
if response['exit'] and response['status'] != 'needs-input':
    if response['announce']:
        print(f'delegate: result={result_path}', file=sys.stderr)
else:
    if response['announce']:
        print(f'delegate: result={result_path}')
    print(f"delegate: run_id={run_id} status={response['status']} resumable=false")
sys.exit(response['exit'])
''',
        })
        self.script.chmod(0o700)
        self.set_version('0.14.0')
        discover = mock.patch.object(cli, 'find_delegate', return_value=str(self.script))
        self.find_delegate = discover.start()
        self.addCleanup(discover.stop)

        def run_delegate(backend, head, task):
            return self.work.run_delegate(backend, head, task)

        executor = mock.patch.object(FakeWork, 'run_delegate', side_effect=run_delegate, create=True)
        self.delegate = executor.start()
        self.addCleanup(executor.stop)

    def set_version(self, version):
        write_files(str(self.plugin), {'.claude-plugin/plugin.json': json.dumps({'version': version})})

    def io(self, *ids):
        io = self.verify.io([dict(case(cid, 'ai-operate'), command=None, evidence='记录界面反馈') for cid in ids])
        io.pr['head']['sha'] = self.head
        return io

    def authorization_line(self):
        digest = hashlib.sha256(self.authorization.read_bytes()).hexdigest()
        return f'写入授权：文件 {self.authorization.resolve()}（sha256 {digest[:12]}）'

    def reported_authorization(self):
        contents = self.authorization.read_bytes()
        return dict(source='file', path=str(self.authorization.resolve()),
                    sha256=hashlib.sha256(contents).hexdigest(), bytes=len(contents),
                    extra={'keep': ['intact', 7]})

    def responses(self, *, metadata=False, status='done', result=None, announce=True, **outcomes):
        footer = '\n---\n模型：宿主默认\n过程日志（未进入本会话上下文）：日志'
        if metadata:
            footer += '\n' + self.authorization_line()
        (self.plugin / 'responses.json').write_text(json.dumps({
            cid: dict(stdout=stdout + footer, exit=exit_code, status=status, result=result, announce=announce)
            for cid, (stdout, exit_code) in outcomes.items()
        }), encoding='utf-8')

    def calls(self):
        path = self.plugin / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    def assert_clean_clones(self, calls):
        self.assertEqual(len({call['cwd'] for call in calls}), len(calls))
        src = self.source_object.stat()
        for call in calls:
            self.assertTrue(call['dot_git'])
            self.assertEqual((call['head'], call['fixture']), (self.head, 'head'))
            self.assertNotEqual(call['object'], [src.st_dev, src.st_ino])
            self.assertFalse(Path(call['cwd']).exists(), call)
        self.assertEqual(Path(self.work.root, 'fixture.txt').read_text(), 'base')

    def test_delegate_pass_reused_until_case_hash_or_patch_changes(self):
        self.responses(R1=('later output\n{"case":"R1","result":"pass","evidence":"pass"}', 0),
                       R2=('later output\n{"case":"R2","result":"fail","evidence":"fail"}', 0))
        io = self.io('R1', 'R2')
        self.assertEqual(self.cli_run(io, '--author-host', 'claude-code', '--json')[0], 1)
        self.assertEqual(self.cli_run(io, '--author-host', 'claude-code', '--json')[0], 1)
        self.assertEqual(self.delegate.call_count, 3)
        self.assertEqual([entry['result'] for entry in self.verify.report(io)['cases']], ['pass', 'fail'])

        doc = io.regression_head[1]
        doc['cases'][0]['sha256'] = 'f' * 64
        doc['sha256'] = 'e' * 64
        self.assertEqual(self.cli_run(io, '--author-host', 'claude-code', '--json')[0], 1)
        self.assertEqual(self.delegate.call_count, 5)
        self.assertTrue(self.delegate.call_args_list[-2].args[2].startswith('用例 R1'))
        self.assertTrue(self.delegate.call_args.args[2].startswith('用例 R2'))

        with mock.patch.object(FakeWork, 'patch_id', return_value='a' * 40):
            self.assertEqual(self.cli_run(io, '--author-host', 'claude-code', '--json')[0], 1)
        self.assertEqual(self.delegate.call_count, 7)

    def test_failed_delegate_reruns_and_can_pass_on_the_same_patch(self):
        self.responses(R1=('{"case":"R1","result":"fail","evidence":"first attempt"}', 0))
        io = self.io('R1')
        self.assertEqual(self.cli_run(io, '--author-host', 'claude-code', '--json')[0], 1)
        self.assertEqual(self.verify.report(io)['verdict'], 'fail')
        self.assertFalse(any(path.endswith('.ai.json') for path in io.verification_files))

        self.responses(R1=('{"case":"R1","result":"pass","evidence":"second attempt"}', 0))
        code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.verify.report(io)['verdict'], 'pass')
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(sum(path.endswith('.ai.json') for path in io.verification_files), 1)

    def test_legacy_nonpassing_delegate_cache_entries_are_ignored(self):
        self.responses(R1=('{"case":"R1","result":"pass","evidence":"R1 passed"}', 0),
                       R2=('{"case":"R2","result":"pass","evidence":"R2 passed"}', 0))
        io = self.io('R1', 'R2')
        cases = io.regression_head[1]['cases']
        outcomes = [
            dict(result=dict(id='R1', method='ai-operate', result='fail', failCategory='check-failure',
                             baselineCommit=None, evidence=[], executor='delegate:codex', exit=0), reason=None),
            dict(result=None, reason='找不到 delegate-exec.sh'),
        ]
        patch_id = FakeWork(io).patch_id()
        report_path, _, _ = io.verification_paths(REPO, patch_id)
        for entry, outcome in zip(cases, outcomes):
            key = hashlib.sha256(json.dumps([patch_id, entry['id'], entry['sha256']]).encode('utf-8')).hexdigest()
            io.write_verification_file(f'{report_path}.{key}.ai.json', json.dumps(outcome))

        code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.verify.report(io)['verdict'], 'pass')
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(io.verify_issues, [])

    def test_unavailable_delegate_retries_and_recovers_on_the_same_patch(self):
        cases = [
            ('no author', (), '未传 --author-host'),
            ('codex author', ('--author-host', 'codex'), 'claude-code'),
            ('missing', ('--author-host', 'claude-code'), '找不到 delegate'),
            ('old version', ('--author-host', 'claude-code'), '版本低于 0.13'),
            ('timeout', ('--author-host', 'claude-code'), '超时'),
            ('nonzero exit', ('--author-host', 'claude-code'), '退出码 127'),
            ('invalid output', ('--author-host', 'claude-code'), '结尾 JSON'),
        ]
        for fault, args, reason in cases:
            with self.subTest(fault=fault):
                self.find_delegate.return_value = None if fault == 'missing' else str(self.script)
                self.set_version('0.12.6' if fault == 'old version' else '0.14.0')
                self.responses(R1=('not JSON', 127 if fault == 'nonzero exit' else 0))
                io = self.io('R1')
                timeout = (mock.patch.object(self.work, 'run_delegate', return_value=(None, '执行 delegate 超时'))
                           if fault == 'timeout' else contextlib.nullcontext())
                with timeout:
                    code, out = self.cli_run(io, *args, '--json')
                self.assertEqual(code, 1, out)
                self.assertIn(reason, io.verify_issues[0]['body'])
                self.assertFalse(any(path.endswith('.ai.json') for path in io.verification_files))

                self.find_delegate.return_value = str(self.script)
                self.set_version('0.14.0')
                self.responses(R1=('{"case":"R1","result":"pass","evidence":"delegate recovered"}', 0))
                code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
                self.assertEqual(code, 0, out)
                self.assertEqual(self.verify.report(io)['cases'][0]['executor'], 'delegate:codex')

    def test_manual_fallback_keeps_existing_reply_when_delegate_failure_is_unchanged(self):
        self.responses(R1=('not JSON', 0))
        io = self.io('R1')
        code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
        self.assertEqual(code, 1, out)
        self.assertIn('结尾 JSON', io.verify_issues[0]['body'])
        self.verify.reply(io, 'R1 通过')

        code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.delegate.call_count, 2)
        self.assertEqual(len(io.verify_comments), 1)
        self.assertEqual(self.verify.report(io)['cases'][0]['evidence'][0]['type'], 'user-quote')

    def test_manual_fallback_reposts_when_the_reason_changes(self):
        self.responses(R1=('not JSON', 0))
        io = self.io('R1')
        self.assertEqual(self.cli_run(io, '--author-host', 'claude-code', '--json')[0], 1)
        self.verify.reply(io, 'R1 通过')

        self.find_delegate.return_value = None
        code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
        self.assertEqual(code, 1, out)
        self.assertIn('要你测 R1', out)
        self.assertEqual(len(io.verify_comments), 2)
        self.assertIn('找不到 delegate', io.verify_comments[-1]['body'])

    def test_fallback_listing_ignores_case_order_and_unrelated_metadata(self):
        self.responses(R1=('not JSON', 0), R2=('not JSON', 0))
        io = self.io('R1', 'R2')
        self.assertEqual(self.cli_run(io, '--author-host', 'claude-code', '--json')[0], 1)
        issue = io.verify_issues[0]
        raw = flow_block(issue['body'], 'autopilot-verify')
        listing = json.loads(raw)
        listing['manual'].reverse()
        listing['generatedAt'] = T0
        issue['body'] = issue['body'].replace(raw, json.dumps(listing))
        self.verify.reply(io, 'R1 通过\nR2 通过')

        code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
        self.assertEqual(code, 0, out)
        self.assertEqual(len(io.verify_comments), 1)
        self.assertEqual(self.delegate.call_count, 4)

    def test_manual_pass_report_does_not_skip_delegate_on_the_next_run(self):
        self.find_delegate.return_value = None
        io = self.io('R1')
        self.assertEqual(self.cli_run(io, '--author-host', 'claude-code', '--json')[0], 1)
        self.verify.reply(io, 'R1 通过')
        self.assertEqual(self.cli_run(io, '--author-host', 'claude-code', '--json')[0], 0)
        self.assertEqual(self.verify.report(io)['cases'][0]['evidence'][0]['type'], 'user-quote')

        io.merged = False
        self.find_delegate.return_value = str(self.script)
        self.responses(R1=('{"case":"R1","result":"fail","evidence":"delegate reproduced the failure"}', 0))
        code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
        self.assertEqual(code, 1, out)
        result = self.verify.report(io)['cases'][0]
        self.assertEqual((result['executor'], result['result']), ('delegate:codex', 'fail'))

    def test_delegate_only_receives_allowlisted_environment(self):
        self.responses(R1=('delegate output', 0))
        allowed = dict(PATH=os.environ['PATH'], HOME=os.environ['HOME'], USER='runner', LOGNAME='runner',
                       SHELL='/bin/bash', LANG='C', LC_ALL='C', LC_MESSAGES='C', TMPDIR=str(self.delegate_tmp),
                       CODEX_HOME='/tmp/codex-config')
        excluded = ('SSH_AUTH_SOCK', 'AWS_ACCESS_KEY_ID', 'AWS_SESSION_TOKEN', 'GOOGLE_APPLICATION_CREDENTIALS',
                    'AZURE_CLIENT_SECRET', 'GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_PAT',
                    'GIT_CONFIG_COUNT', 'GIT_SSH_COMMAND', 'BASH_ENV', 'TERM', 'AUTOPILOT_OTHER')
        with mock.patch.dict(os.environ, dict(allowed, **dict.fromkeys(excluded, 'fixture')), clear=True), \
                mock.patch.object(subprocess, 'Popen', wraps=subprocess.Popen) as popen:
            output, error = self.work.run_delegate('codex', self.head, '用例 R1')

        self.assertIsNone(error)
        self.assertEqual(output['stdout'].splitlines()[0], 'delegate output')
        delegate = next(call for call in popen.call_args_list if call.args[0][0] == 'bash')
        self.assertEqual(delegate.kwargs['env'], allowed)

    def test_delegate_receives_proxy_environment(self):
        self.responses(R1=('delegate output', 0))
        proxies = {key: f'proxy-fixture-{key}' for key in (
            'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY',
            'http_proxy', 'https_proxy', 'no_proxy', 'all_proxy',
        )}
        with mock.patch.dict(os.environ, proxies), mock.patch.object(subprocess, 'Popen', wraps=subprocess.Popen) as popen:
            output, error = self.work.run_delegate('codex', self.head, '用例 R1')

        self.assertIsNone(error)
        self.assertEqual(output['stdout'].splitlines()[0], 'delegate output')
        delegate = next(call for call in popen.call_args_list if call.args[0][0] == 'bash')
        self.assertEqual({key: delegate.kwargs['env'].get(key) for key in proxies}, proxies)

    def test_delegate_timeout_terminates_group_before_removing_clone(self):
        self.assert_delegate_group_stopped(subprocess.TimeoutExpired('delegate', 1))

    def test_delegate_exit_terminates_remaining_group_before_removing_clone(self):
        self.assert_delegate_group_stopped()

    def test_delegate_group_exit_probe_retries_when_temporarily_denied(self):
        process = mock.Mock(pid=12345)
        with mock.patch.object(os, 'killpg', side_effect=[None, PermissionError, ProcessLookupError]) as killpg, \
                mock.patch.object(cli.time, 'sleep'):
            self.assertTrue(cli._stop_delegate_group(process))
        self.assertEqual(killpg.call_args_list, [
            mock.call(process.pid, signal.SIGTERM), mock.call(process.pid, 0), mock.call(process.pid, 0),
        ])

    def test_interrupted_delegate_stops_group_before_removing_clone(self):
        self.assert_delegate_group_stopped(KeyboardInterrupt())

    def assert_delegate_group_stopped(self, interruption=None):
        real_popen = subprocess.Popen
        process = mock.MagicMock(pid=12345, returncode=0)
        process.__enter__.return_value = process
        process.communicate.side_effect = interruption
        process.communicate.return_value = ('delegate output', None)
        started = []
        signals = []

        def popen(command, **kwargs):
            if command[0] == 'bash':
                started.append(kwargs)
                return process
            return real_popen(command, **kwargs)

        def killpg(pgid, sig):
            self.assertEqual(pgid, process.pid)
            self.assertTrue(Path(started[0]['cwd']).is_dir())
            signals.append(sig)
            if sig == 0 and signal.SIGKILL in signals:
                raise ProcessLookupError

        with mock.patch.object(subprocess, 'Popen', side_effect=popen), \
                mock.patch.object(os, 'killpg', side_effect=killpg), \
                mock.patch.object(cli.time, 'monotonic', side_effect=iter(range(0, 600, 10))):
            if isinstance(interruption, KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.work.run_delegate('codex', self.head, '用例 R1')
            elif interruption is not None:
                output, error = self.work.run_delegate('codex', self.head, '用例 R1')
                self.assertIsNone(output)
                self.assertIn('超时', error)
            else:
                output, error = self.work.run_delegate('codex', self.head, '用例 R1')
                self.assertEqual(output['stdout'], 'delegate output')
                self.assertIn('结果文件路径', error)

        self.assertIs(started[0].get('start_new_session'), True)
        self.assertEqual([sig for sig in signals if sig], [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(signals[-1], 0)
        self.assertFalse(Path(started[0]['cwd']).exists())

    def test_delegate_pass_and_fail_use_independent_clones_and_record_evidence(self):
        self.responses(R1=('progress\n{"case":"R1","result":"pass","evidence":"first evidence"}', 0),
                       R2=('{"case":"R2","result":"fail","evidence":"second evidence"}', 0))
        io = self.io('R1', 'R2')
        code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
        self.assertEqual(code, 1, out)
        self.assertIn('R2（check-failure）', out)
        self.assertEqual(io.verify_issues, [])
        self.assertEqual(io.merge_calls(), [])
        report = self.verify.report(io)
        self.assertEqual(report['verdict'], 'fail')
        auth_contents = self.authorization.read_bytes()
        auth_stat = self.authorization.stat()
        expected_authorization = self.reported_authorization()
        expected_digest = hashlib.sha256(auth_contents).hexdigest()
        for result, expected in zip(report['cases'], [('pass', b'first evidence'), ('fail', b'second evidence')]):
            self.assertEqual((result['result'], result['executor'], result['exit']), (expected[0], 'delegate:codex', 0))
            self.assertEqual(io.verification_files['evidence/' + result['evidence'][0]['path']], expected[1])
            self.assertEqual(result['authorization'], expected_authorization)
            self.assertEqual(result['hubAuthorizationSha256'], expected_digest)
        [cached] = [json.loads(raw)['result'] for path, raw in io.verification_files.items()
                    if path.endswith('.ai.json')]
        self.assertEqual(cached['authorization'], expected_authorization)
        self.assertEqual(cached['hubAuthorizationSha256'], expected_digest)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call['options'], ['run', '--backend', 'codex', '--write',
                                               '--authorized-by', str(self.authorization), '--'])
            for field in ('操作', '成功', '记录界面反馈', 'JSON'):
                self.assertIn(field, call['task'])
            self.assertNotIn(auth_contents.decode().strip(), call['task'])
        self.assertEqual(self.authorization.read_bytes(), auth_contents)
        self.assertEqual(self.authorization.stat().st_mtime_ns, auth_stat.st_mtime_ns)
        self.assert_clean_clones(calls)

    def test_delegate_0132_uses_clone_as_working_directory_and_parses_answer_before_footer(self):
        self.set_version('0.13.2')
        self.responses(R1=('{"case":"R1","result":"pass","evidence":"evidence"}', 0))
        io = self.io('R1')
        code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.verify.report(io)['verdict'], 'pass')
        self.assertEqual(io.verify_issues, [])
        calls = self.calls()
        self.assertEqual([call['options'] for call in calls], [
            ['run', '--backend', 'codex', '--write', '--authorized-by', str(self.authorization), '--'],
        ])
        self.assert_clean_clones(calls)

    def test_missing_or_invalid_authorization_falls_back_without_starting_delegate(self):
        self.authorization.unlink()
        target = self.authorization.parent / 'other.md'
        target.write_text('授权内容', encoding='utf-8')
        self.responses(R1=('delegate must not run', 0))
        for kind in ('missing', 'symlink', 'directory', 'empty', 'too long'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    self.authorization.symlink_to(target)
                elif kind == 'directory':
                    self.authorization.mkdir()
                elif kind in ('empty', 'too long'):
                    self.authorization.write_bytes(b'x' * (65537 if kind == 'too long' else 0))
                try:
                    io = self.io('R1')
                    code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
                    self.assertEqual(code, 1, out)
                    self.assertIn('没有记录打开自动路的授权', io.verify_issues[0]['body'])
                    self.assertEqual(self.calls(), [])
                finally:
                    if kind == 'directory':
                        self.authorization.rmdir()
                    else:
                        self.authorization.unlink(missing_ok=True)

    def test_delegate_0131_falls_back_without_starting(self):
        self.set_version('0.13.1')
        io = self.io('R1')
        self.assertIn('要你测 R1', self.cli_run(io, '--author-host', 'claude-code', '--json')[1])
        self.assertIn('delegate 版本低于 0.13.2（0.13.1）', io.verify_issues[0]['body'])
        self.assertEqual(self.calls(), [])

    def test_invalid_delegate_result_authorization_falls_back_even_with_matching_metadata(self):
        answer = '{"case":"R1","result":"pass","evidence":"evidence"}'
        auth = self.reported_authorization()
        same_prefix = auth['sha256'][:12] + ('0' if auth['sha256'][12] != '0' else '1') + auth['sha256'][13:]
        faults = [
            ('source', {'authorization': dict(auth, source='in-turn')}, 'authorization.source'),
            ('sha256', {'authorization': dict(auth, sha256=same_prefix)}, 'SHA-256 不一致'),
            ('path', {'authorization': dict(auth, path='/different.md')}, '授权文件路径不一致'),
            ('missing object', {}, 'authorization 对象'),
            ('not an object', {'authorization': ['file']}, 'authorization 对象'),
        ]
        for field in ('source', 'path', 'sha256', 'bytes'):
            incomplete = auth.copy()
            del incomplete[field]
            faults.append((f'missing {field}', {'authorization': incomplete}, f'authorization.{field}'))
        for fault, result, reason in faults:
            with self.subTest(fault=fault):
                self.responses(metadata=True, result=result, R1=(answer, 0))
                io = self.io('R1')
                code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
                self.assertEqual(code, 1, out)
                self.assertIn('要你测 R1', out)
                self.assertIn(reason, io.verify_issues[0]['body'])
                self.assertEqual(io.verify_calls, [])
                self.assertFalse(any(path.endswith('.ai.json') for path in io.verification_files))

    def test_missing_or_corrupt_delegate_result_file_falls_back(self):
        answer = '{"case":"R1","result":"pass","evidence":"evidence"}'
        for result, announce, reason in (('missing', True, '无法读取 delegate 结果文件'),
                                         ('invalid JSON', True, 'delegate 结果文件 JSON 无法解析'),
                                         ('{"authorization": NaN}', True, 'delegate 结果文件 JSON 无法解析'),
                                         (None, False, '结果文件路径'),
                                         ({'runId': 'previous'}, True, '运行编号与本次运行不一致'),
                                         ({'status': 'unknown'}, True, '状态不是 done')):
            with self.subTest(result=result, announce=announce):
                self.responses(metadata=True, result=result, announce=announce, R1=(answer, 0))
                io = self.io('R1')
                code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
                self.assertEqual(code, 1, out)
                self.assertIn('要你测 R1', out)
                self.assertIn(reason, io.verify_issues[0]['body'])
                self.assertEqual(io.verify_calls, [])

    def test_need_user_answer_or_status_falls_back_without_continuing(self):
        answer = '{"case":"R1","result":"pass","evidence":"evidence"}'
        for stdout, status, exit_code in (('NEED_USER: 需要选择测试账号\n' + answer, 'done', 0),
                                          (answer, 'needs-input', 1)):
            with self.subTest(status=status):
                self.responses(status=status, R1=(stdout, exit_code))
                io = self.io('R1')
                before = len(self.calls())
                code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
                self.assertEqual(code, 1, out)
                self.assertIn('delegate 要人回答', io.verify_issues[0]['body'])
                self.assertEqual(len(self.calls()), before + 1)
                self.assertEqual(io.verify_calls, [])

    def test_unapproved_module_falls_back_without_delegate(self):
        io = self.io('R1')
        io.view['modules'][0]['requirementConfirm'] = {'status': 'pending'}
        self.responses(R1=('{"case":"R1","result":"pass","evidence":"evidence"}', 0))
        self.assertIn('要你测 R1', self.cli_run(io, '--author-host', 'claude-code', '--json')[1])
        self.assertIn('未确认模块已批准', io.verify_issues[0]['body'])
        self.assertEqual(self.calls(), [])

    def test_old_pass_report_and_cache_are_rerun_without_structured_authorization(self):
        auth = self.reported_authorization()
        legacy = dict(path=auth['path'], sha256Prefix=auth['sha256'][:12], line=self.authorization_line())
        self.responses(R1=('{"case":"R1","result":"pass","evidence":"new delegate run"}', 0))
        for prior in ({}, {'authorization': legacy}):
            with self.subTest(prior=prior):
                io = self.io('R1')
                old_case = dict(id='R1', method='ai-operate', result='pass', failCategory=None,
                                baselineCommit=None, evidence=[], executor='delegate:codex', exit=0, **prior)
                document = io.regression_head[1]
                patch_id = FakeWork(io).patch_id()
                report_path, _, _ = io.verification_paths(REPO, patch_id)
                report = json.dumps(dict(regression={'sha256': document['sha256']}, verdict='pass', cases=[old_case]))
                io.write_verification_file(report_path, report)
                io.verification_map = {'modules': [{'id': 'm', 'verification': {
                    'current': True, 'reportSha256': hashlib.sha256(report.encode()).hexdigest(),
                }}]}
                key = hashlib.sha256(json.dumps([patch_id, 'R1', document['cases'][0]['sha256']]).encode()).hexdigest()
                cache_path = f'{report_path}.{key}.ai.json'
                io.write_verification_file(cache_path, json.dumps(dict(result=old_case, reason=None)))
                before = len(self.calls())
                code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
                self.assertEqual(code, 0, out)
                self.assertEqual(len(self.calls()), before + 1)
                self.assertEqual(self.verify.report(io)['cases'][0]['authorization'], auth)
                self.assertEqual(json.loads(io.verification_files[cache_path])['result']['authorization'], auth)

    def test_pass_report_with_structured_authorization_is_reused(self):
        self.responses(R1=('{"case":"R1","result":"pass","evidence":"evidence"}', 0))
        io = self.io('R1')
        code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
        self.assertEqual(code, 0, out)
        report_path, _, _ = io.verification_paths(REPO, FakeWork(io).patch_id())
        report = io.verification_files[report_path]
        [cache_path] = [path for path in io.verification_files if path.endswith('.ai.json')]
        del io.verification_files[cache_path]

        io.merged = False
        self.find_delegate.return_value = None
        code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
        self.assertEqual(code, 0, out)
        self.assertEqual(io.verification_files[report_path], report)
        self.assertEqual(self.delegate.call_count, 1)

    def test_delegate_authorization_is_checked_against_the_pre_run_digest(self):
        self.responses(R1=('{"case":"R1","result":"pass","evidence":"evidence"}', 0))
        io = self.io('R1')
        real_popen = subprocess.Popen

        def popen(command, **kwargs):
            if command[0] == 'bash':
                self.authorization.write_text('启动时已变更的授权文件', encoding='utf-8')
            return real_popen(command, **kwargs)

        with mock.patch.object(subprocess, 'Popen', side_effect=popen):
            code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
        self.assertEqual(code, 1, out)
        self.assertIn('要你测 R1', out)
        self.assertIn('SHA-256 不一致', io.verify_issues[0]['body'])

    def test_authorization_cannot_be_changed_through_cli_while_delegate_runs(self):
        self.responses(R1=('delegate output', 0))
        original = self.authorization.read_bytes()
        real_popen = subprocess.Popen
        attempted = []

        def popen(command, **kwargs):
            if command[0] == 'bash':
                for flags in (('--revoke',), ('--replace', '--quote', '新授权', '--session', '会话')):
                    with contextlib.redirect_stderr(StringIO()) as stderr:
                        code = cli.main(['authorize', '--repo', REPO, *flags])
                    attempted.append((code, stderr.getvalue()))
            return real_popen(command, **kwargs)

        with mock.patch.object(subprocess, 'Popen', side_effect=popen):
            output, error = self.work.run_delegate('codex', self.head, '用例 R1')
        self.assertIsNone(error)
        self.assertEqual(output['stdout'].splitlines()[0], 'delegate output')
        self.assertEqual(len(attempted), 2)
        for code, message in attempted:
            self.assertNotEqual(code, 0)
            self.assertIn('正在使用授权', message)
        self.assertEqual(self.authorization.read_bytes(), original)

    def test_invalid_answers_and_nonzero_exit_require_manual_and_remove_clones(self):
        self.responses(R1=('{"case":"R1","result":"pass","evidence":"old"}\nnot JSON', 0),
                       R2=('{"case":"R9","result":"pass","evidence":"wrong case"}', 0),
                       R3=('{"case":"R3","result":"pass","evidence":"ignore"}', 127))
        io = self.io('R1', 'R2', 'R3')
        code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
        self.assertEqual(code, 1, out)
        self.assertIn('要你测 R1 R2 R3', out)
        body = io.verify_issues[0]['body']
        self.assertEqual(body.count('AI 执行不可用：'), 3)
        for reason in ('结尾 JSON', '用例编号', '退出码 127'):
            self.assertIn(reason, body)
        self.assertEqual(io.verify_calls, [])
        self.assertEqual(len(self.calls()), 3)
        self.assert_clean_clones(self.calls())

    def test_missing_delegate_waits_for_manual_reply(self):
        self.find_delegate.return_value = None
        io = self.io('R1')
        self.assertIn('要你测 R1', self.cli_run(io, '--author-host', 'claude-code', '--json')[1])
        self.assertIn('AI 执行不可用：找不到 delegate', io.verify_issues[0]['body'])
        self.verify.reply(io, 'R1 通过')
        code, out = self.cli_run(io, '--author-host', 'claude-code', '--json')
        self.assertEqual(code, 0, out)
        result = self.verify.report(io)['cases'][0]
        self.assertEqual((result['method'], result['evidence'][0]['type']), ('ai-operate', 'user-quote'))
        self.assertEqual(len(io.verify_issues), 1)

    def test_old_delegate_waits_for_manual_without_starting(self):
        self.set_version('0.12.6')
        io = self.io('R1')
        self.assertIn('要你测 R1', self.cli_run(io, '--author-host', 'claude-code', '--json')[1])
        self.assertIn('AI 执行不可用：delegate 版本低于 0.13', io.verify_issues[0]['body'])
        self.assertEqual(self.calls(), [])

    def test_no_author_and_codex_author_wait_for_manual_without_delegate(self):
        for args, reason in [((), '未传 --author-host'), (('--author-host', 'codex'), 'claude-code')]:
            with self.subTest(args=args):
                io = self.io('R1')
                self.assertIn('要你测 R1', self.cli_run(io, *args, '--json')[1])
                self.assertIn('AI 执行不可用：', io.verify_issues[0]['body'])
                self.assertIn(reason, io.verify_issues[0]['body'])
        self.delegate.assert_not_called()
        self.find_delegate.assert_not_called()


class RoutePreconditionTest(RunRouteFixture):
    def test_seven_deployment_preconditions(self):
        cases = (
            ('missing status', lambda io: setattr(io, 'branch_rules', ruleset(required=False)),
             '主干未要求 autopilot/verified'),
            ('wrong app', lambda io: setattr(io, 'branch_rules', ruleset(BOT['id'])),
             'autopilot/verified 的来源未限定为 hub App'),
            ('not strict', lambda io: setattr(io, 'branch_rules', ruleset(strict=False)),
             '主干规则未要求分支与主干同步'),
            ('stale heartbeat', lambda io: setattr(self, 'now', self.now + timedelta(minutes=16)),
             'watch 心跳不在 15 分钟内'),
            ('direct', lambda io: io.config.update(mergeMode='direct'), 'mergeMode=direct'),
            ('narrowed', lambda io: self.state.narrow_repo(REPO, 'main-red', 42), '仓库已收紧'),
            ('frozen', lambda io: self.state.set_frozen(True), 'watch 已冻结'),
        )
        for label, arrange, reason in cases:
            with self.subTest(label=label):
                self.state.set_frozen(False)
                self.state.resume(REPO)
                self.state.write_heartbeat()
                io = self.new_flow()
                arrange(io)
                result = self.run_pr(io)
                if label == 'direct':
                    self.assertEqual((result['route'], result['result']), ('direct', 'merged'), result)
                    self.assertEqual(len(io.merge_calls()), 1)
                else:
                    self.assertEqual((result['route'], result['result']), ('manual', 'handed-to-user'), result)
                    self.assertEqual(io.merge_calls(), [])
                self.assertIn(reason, result['routeReasons'])
                evidence = self.verify.evidence(io)
                self.assertEqual(evidence['route'], result['route'])
                self.assertEqual(evidence['routeReasons'], result['routeReasons'])
                self.assertEqual(io.graphql_calls, [])

    def test_collects_all_unmet_conditions(self):
        io = self.new_flow()
        io.bot = BOT  # The bot user ID is not a configuration value for the App ID.
        io.branch_rules = ruleset(strict=False)
        io.base['modules'][0]['takeover'] = effective('human')
        self.now += timedelta(minutes=16)
        result = self.run_pr(io)
        self.assertEqual((result['route'], result['result']), ('manual', 'handed-to-user'))
        reasons = '\n'.join(result['routeReasons'])
        for reason in ('没有任何模块', '模块 m 的接管档位', '准入', 'App ID', '与主干同步', 'watch 心跳'):
            self.assertIn(reason, reasons)
        self.assertEqual(io.verify_calls, ['check', 'record'])

    def test_legacy_branch_protection_and_app_id_from_local_config(self):
        io = self.new_flow()
        io.bot = BOT
        io.config = {'appId': APP_ID}
        io.branch_rules = None
        io.legacy_rules = {'strict': True, 'checks': [{'context': VERIFIED, 'app_id': APP_ID}]}
        self.assertEqual(self.run_pr(io)['result'], 'automerge-on')

    def test_legacy_protection_does_not_accept_an_unpinned_context(self):
        io = self.new_flow()
        io.branch_rules = None
        io.legacy_rules = {'strict': True, 'contexts': [VERIFIED]}
        result = self.run_pr(io)
        self.assertEqual(result['route'], 'manual')
        self.assertIn('autopilot/verified 的来源未限定为 hub App', result['routeReasons'])

    def test_missing_administration_read_selects_manual_with_permission_reason(self):
        io = self.new_flow()
        io.branch_rules = []
        local = cli.LocalIO('run-token', None)
        io.legacy_status_checks = local.legacy_status_checks
        projection = {'protection': {'enabled': True, 'required_status_checks': {'contexts': [VERIFIED]}}}
        with mock.patch.object(local, 'api_optional', return_value=projection), \
                mock.patch.object(cli.tokens, 'mint', side_effect=cli.tokens.TokenError('no permission')) as mint:
            result = self.run_pr(io)
        self.assertEqual((result['route'], result['result']), ('manual', 'handed-to-user'), result)
        self.assertIn('Administration: read', '\n'.join(result['routeReasons']))
        self.assertEqual(self.verify.evidence(io)['routeReasons'], result['routeReasons'])
        self.assertEqual(io.graphql_calls, [])
        mint.assert_called_once_with(REPO, {'administration': 'read'})

    def test_strict_rule_without_any_checks_does_not_prove_synchronization(self):
        io = self.new_flow()
        io.branch_rules = ruleset(required=False)
        io.legacy_rules = {'strict': False, 'checks': [{'context': VERIFIED, 'app_id': APP_ID}]}
        result = self.run_pr(io)
        self.assertEqual(result['route'], 'manual')
        self.assertIn('主干规则未要求分支与主干同步', result['routeReasons'])


class AutoMergeRouteTest(RunRouteFixture):
    def test_records_intent_and_labels_before_enabling_the_verified_head(self):
        io = self.new_flow()
        def spy_state(method):
            def record(*args, **kwargs):
                result = method(*args, **kwargs)
                io.calls.append(("state", method.__name__, (args, kwargs)))
                return result
            return record

        with mock.patch.object(self.state, 'intend', side_effect=spy_state(self.state.intend)), \
                mock.patch.object(self.state, 'set_pr', side_effect=spy_state(self.state.set_pr)), \
                mock.patch.object(self.state, 'lock', wraps=self.state.lock) as lock:
            code, output = self.cli_run(io, '--json', '--notify', 'implementation', '--author-host', 'codex')
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertEqual((result['route'], result['result'], result['routeReasons']),
                         ('automerge', 'automerge-on', []), result)
        evidence = self.verify.evidence(io)
        self.assertEqual(evidence['authorHost'], 'codex')
        self.assertEqual(evidence['validatedHeadOid'], HEAD)
        self.assertEqual(evidence['validatedBaseOid'], FakeWork(io).base_tip)
        [status] = io.statuses
        self.assertEqual(status[1]['state'], 'success')
        intent, = [call for call in io.calls if call[:2] == ('state', 'intend')]
        requested, = [call for call in io.calls if call[:2] == ('state', 'set_pr')
                      and call[2][1]['state'] == 'automerge-requested']
        self.assertEqual(requested[2][1]['notify'], 'implementation')
        label = ('POST', f'repos/{REPO}/issues/7/labels', {'labels': ['autopilot:authored']})
        graphql = ('POST', 'graphql', {'pullRequestId': 'PR_node', 'expectedHeadOid': HEAD, 'mergeMethod': 'squash'})
        ordered = [('POST', *status), intent, requested, label, graphql]
        indices = [io.calls.index(call) for call in ordered]
        self.assertEqual(indices, sorted(indices))
        self.assertEqual(io.merge_calls(), [])
        record = self.state.get_pr(REPO, 7)
        self.assertEqual(record, dict(state='automerge-on', validatedHeadOid=HEAD, validatedBaseOid=FakeWork(io).base_tip,
                                      notify='implementation', since=T0, pause=None))
        self.assertTrue(self.state.is_done(intent[2][0][0]))
        self.assertEqual(self.state.pending_intents(), {})
        self.assertEqual(self.state.pending_notifications(), [])
        self.assertTrue(lock.call_args_list)
        self.assertTrue(all(call == mock.call(wait_seconds=60) for call in lock.call_args_list))

    def test_another_run_does_not_duplicate_the_automerge_comment(self):
        io = self.new_flow()
        self.assertEqual(self.run_pr(io)['result'], 'automerge-on')
        self.assertEqual(self.run_pr(io)['result'], 'automerge-on')
        self.assertEqual(len([call for call in io.calls if call[:2] == ('POST', 'graphql')]), 1)
        self.assertEqual(len(self.state.read()['done']), 1)
        [comment] = [c for c in io.comments if 'autopilot-automerge:v1' in c['body']]
        self.assertIn(f'已打开自动合并，绑定提交 `{HEAD[:12]}`', comment['body'])

    def test_reopens_automerge_after_it_has_been_disabled_on_the_same_head(self):
        io = self.new_flow()
        self.assertEqual(self.run_pr(io)['result'], 'automerge-on')
        io.pr['auto_merge'] = None
        self.assertEqual(self.run_pr(io)['result'], 'automerge-on')
        self.assertEqual(len(io.graphql_calls), 2)
        self.assertEqual(len(self.state.read()['done']), 2)

    def start_automerge_before_crash(self, io):
        with mock.patch.object(flow, 'record_automerge_on', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.run_pr(io)
        self.assertIsNotNone(io.pr['auto_merge'])
        self.assertEqual(self.state.get_pr(REPO, 7)['state'], 'automerge-requested')
        key, = self.state.pending_intents()
        return key

    def test_pending_intent_recovers_a_successful_enable_without_repeating_it(self):
        io = self.new_flow()
        key = self.start_automerge_before_crash(io)
        with mock.patch.object(io, 'enable_automerge', wraps=io.enable_automerge) as enable:
            result = self.run_pr(io, notify='implementation')
        enable.assert_not_called()
        self.assertEqual(result['result'], 'automerge-on', result)
        self.assertEqual(self.state.get_pr(REPO, 7)['state'], 'automerge-on')
        self.assertEqual(self.state.get_pr(REPO, 7)['notify'], 'implementation')
        self.assertTrue(self.state.is_done(key))
        self.assertEqual(self.state.pending_intents(), {})
        self.assertIsNotNone(io.pr['auto_merge'])

    def test_pending_intent_with_a_different_queued_head_disables_and_pauses(self):
        for initial_head, different_head in ((HEAD, 'expectedHeadOid'), ('f' * 40, 'PR head')):
            with self.subTest(different_head=different_head):
                io = self.new_flow()
                io.pr['head']['sha'] = initial_head
                key = self.start_automerge_before_crash(io)
                if different_head == 'expectedHeadOid':
                    io.pr['auto_merge']['expectedHeadOid'] = 'e' * 40
                else:
                    io.pr['head']['sha'] = 'e' * 40
                with mock.patch.object(io, 'enable_automerge', wraps=io.enable_automerge) as enable:
                    result = self.run_pr(io, notify='implementation')
                enable.assert_not_called()
                self.assertEqual((result['result'], result['step']), ('paused', 'merge'), result)
                self.assertIn('head', result['reason'])
                self.assertIsNone(io.pr['auto_merge'])
                self.assertEqual(io.merge_calls(), [])
                self.assertEqual([status['state'] for _, status in io.statuses], ['success'])
                self.assertEqual(self.state.get_pr(REPO, 7)['state'], 'paused')
                self.assertTrue(self.state.is_done(key))

    def test_dry_run_describes_automerge_without_writing_state_or_github(self):
        io = self.new_flow()
        before = self.state.read()
        code, output = self.cli_run(io, '--dry-run', '--json')
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertEqual((result['result'], result['route']), ('dry-run', 'automerge'))
        for step in ('automerge-requested', 'autopilot:authored', 'enablePullRequestAutoMerge', 'expectedHeadOid'):
            self.assertIn(step, '\n'.join(result['notes']))
        self.assertEqual(io.writes(), [])
        self.assertEqual(self.state.read(), before)

    def test_edited_evidence_cannot_claim_admission_passed(self):
        io = self.new_flow()
        self.assertEqual(self.run_pr(io)['result'], 'automerge-on')
        api = io.api

        def edit_evidence(path, method='GET', body=None):
            result = api(path, method, body)
            if method == 'POST' and path.endswith('/comments') and 'autopilot-evidence:v2' in body['body']:
                for comment in io.comments:
                    if 'autopilot-evidence:v2' in comment['body']:
                        comment['updated_at'] = '2026-11-27T01:01:00Z'
            return result

        io.api = edit_evidence
        result = self.run_pr(io)
        self.assertEqual((result['result'], result['step']), ('stopped', 'admission'), result)
        self.assertEqual(io.merge_calls(), [])
        self.assertIsNone(io.pr['auto_merge'])
        self.assertEqual(len(io.graphql_calls), 2)
        disable = ('POST', 'graphql', io.graphql_calls[-1])
        self.assertLess(io.calls.index(disable), io.calls.index(('POST', *io.statuses[-1])))


class AutoMergeFallbackTest(RunRouteFixture):
    mismatch = 'Expected head oid does not match the pull request head oid'
    refused = 'Auto-merge is not allowed'

    def successful_checks(self, io):
        io.branch_rules[0]['parameters']['required_status_checks'].append({'context': 'ci'})
        io.check_runs = [{'name': 'ci', 'status': 'completed', 'conclusion': 'success', 'app': {'id': 42}}]
        io.commit_statuses = [{'context': VERIFIED, 'state': 'success', 'creator': BOT_USER}]

    def assert_paused(self, io, result, reason):
        self.assertEqual((result['route'], result['result']), ('automerge', 'paused'), result)
        self.assertIn(reason, result['reason'])
        self.assertEqual(io.merge_calls(), [])
        record = self.state.get_pr(REPO, 7)
        self.assertEqual(record['state'], 'paused')
        self.assertIn(reason, record['pause']['reason'])
        self.assertEqual(record['pause']['recovery'], '重新 run 通过')
        [notification] = [n for n in self.state.pending_notifications() if reason in n['body']]
        self.assertEqual(notification['target'], 'implementation')
        self.assertIn(reason, notification['body'])

    def test_head_mismatch_retries_four_times_then_reads_head_and_tries_once_more(self):
        io = self.new_flow()
        io.automerge_replies = [self.mismatch] * 5 + [None]
        sleeps = []
        result = self.run_pr(io, sleep=sleeps.append)
        self.assertEqual(result['result'], 'automerge-on', result)
        self.assertEqual(sleeps, [2] * 4)
        graphql = ('POST', 'graphql', {'pullRequestId': 'PR_node', 'expectedHeadOid': HEAD, 'mergeMethod': 'squash'})
        first = io.calls.index(graphql)
        self.assertEqual(io.calls[first:first + 7],
                         [graphql] * 5 + [('GET', f'repos/{REPO}/pulls/7', None), graphql])
        self.assertEqual(io.merge_calls(), [])

    def test_head_mismatch_stops_retrying_as_soon_as_graphql_accepts(self):
        io = self.new_flow()
        io.automerge_replies = [self.mismatch, None]
        sleeps = []
        self.assertEqual(self.run_pr(io, sleep=sleeps.append)['result'], 'automerge-on')
        self.assertEqual(sleeps, [2])

    def test_head_still_changed_after_four_retries_pauses_without_merging(self):
        io = self.new_flow()
        io.automerge_replies = [self.mismatch] * 5
        enable = io.enable_automerge

        def change_head(*args):
            io.pr = dict(io.pr, head=dict(io.pr['head'], sha='e' * 40))
            return enable(*args)

        sleeps = []
        with mock.patch.object(io, 'enable_automerge', side_effect=change_head) as graphql:
            result = self.run_pr(io, notify='implementation', sleep=sleeps.append)
        self.assertEqual(graphql.call_count, 5)
        self.assertEqual(sleeps, [2] * 4)
        self.assert_paused(io, result, 'head 被改了')

    def test_rejection_merges_the_same_head_and_runs_shared_followups(self):
        io = self.new_flow()
        self.successful_checks(io)
        io.automerge_replies = [self.refused]
        io.regression_head[1]['cases'] = [case('R3', 'manual')]
        self.assertEqual(self.run_pr(io)['step'], 'verification')
        self.verify.reply(io, 'R3 通过\n意见 R3：按钮间距太小')
        api = io.api

        def followup_issues(path, method='GET', body=None):
            if path.startswith(f'repos/{REPO}/issues?labels=autopilot:followup'):
                io.calls.append((method, path, body))
                return []
            if path == f'repos/{REPO}/issues' and method == 'POST' and 'autopilot:followup' in body['labels']:
                io.calls.append((method, path, body))
                return {'number': 23}
            return api(path, method, body)

        io.api = followup_issues
        result = self.run_pr(io)
        self.assertEqual((result['route'], result['result'], result['reason']), ('automerge', 'merged', 'm' * 40))
        [merge] = io.merge_calls()
        self.assertEqual(merge, ('PUT', f'repos/{REPO}/pulls/7/merge', {'sha': HEAD, 'merge_method': 'squash'}))
        i = io.calls.index(merge)
        for endpoint in ('check-runs', 'status'):
            self.assertIn(('GET', f'repos/{REPO}/commits/{HEAD}/{endpoint}', None), io.calls[:i])
        self.assertEqual(io.calls[i + 1], ('GET', f'repos/{REPO}/pulls/7', None))
        self.assertIn(('POST', f'repos/{REPO}/issues/7/labels',
                       {'labels': ['autopilot:authored', 'autopilot:auto-merged']}), io.calls[i:])
        self.assertTrue(any('autopilot 已合并' in c['body'] for c in io.comments))
        self.assertIn('已开后续 issue #23', result['notes'])
        self.assertEqual(self.state.get_pr(REPO, 7)['state'], 'post-merge-done')
        self.assertEqual(self.state.pending_intents(), {})
        self.assertEqual(len(self.state.read()['done']), 1)

    def test_required_checks_must_be_successful_and_present(self):
        for outcome in ('failure', None):
            with self.subTest(outcome=outcome):
                io = self.new_flow()
                self.successful_checks(io)
                io.automerge_replies = [self.refused]
                if outcome is None:
                    io.check_runs.clear()
                else:
                    io.check_runs[0]['conclusion'] = outcome
                reason = '缺少必需检查 ci' if outcome is None else '必需检查 ci 未成功'
                self.assert_paused(io, self.run_pr(io, notify='implementation'), reason)

    def test_verified_must_come_from_the_hub_app(self):
        for via in ('check-run', 'commit status'):
            with self.subTest(via=via):
                io = self.new_flow()
                self.successful_checks(io)
                io.automerge_replies = [self.refused]
                io.commit_statuses.clear()
                if via == 'check-run':
                    io.check_runs.append({'name': VERIFIED, 'status': 'completed', 'conclusion': 'success',
                                          'app': {'id': APP_ID + 1}})
                else:
                    io.commit_statuses.append({'context': VERIFIED, 'state': 'success',
                                               'creator': dict(BOT_USER, id=BOT_USER['id'] + 1)})
                self.assert_paused(io, self.run_pr(io, notify='implementation'), '来源不是 hub App')

    def test_fallback_can_verify_the_hub_app_via_check_runs_and_classic_rules(self):
        io = self.new_flow()
        io.branch_rules = []
        io.legacy_rules = {'strict': True, 'contexts': [VERIFIED, 'ci'],
                           'checks': [{'context': VERIFIED, 'app_id': APP_ID}]}
        io.automerge_replies = [self.refused]
        io.check_runs = [{'name': VERIFIED, 'status': 'completed', 'conclusion': 'success', 'app': {'id': APP_ID}}]
        io.commit_statuses = [{'context': 'ci', 'state': 'success', 'creator': {'id': 1}}]
        self.assertEqual(self.run_pr(io)['result'], 'merged')
        self.assertEqual(len(io.merge_calls()), 1)

    def test_fallback_pauses_when_a_scoped_read_token_cannot_be_minted(self):
        for permission in ('Administration', 'Checks'):
            with self.subTest(permission=permission):
                io = self.new_flow()
                self.successful_checks(io)
                io.automerge_replies = [self.refused]
                local = cli.LocalIO('run-token', None)
                if permission == 'Administration':
                    io.legacy_status_checks = local.legacy_status_checks
                else:
                    io.commit_check_results = local.commit_check_results
                projection = {'protection': {'enabled': True, 'required_status_checks': {'contexts': ['classic-ci']}}}
                with mock.patch.object(local, 'api_optional', return_value=projection), \
                        mock.patch.object(cli.tokens, 'mint', side_effect=cli.tokens.TokenError('no permission')) as mint:
                    result = self.run_pr(io, notify='implementation')
                self.assert_paused(io, result, f'{permission}: read')
                mint.assert_called_once_with(REPO, {permission.lower(): 'read'})

    def test_pr_behind_main_pauses_and_cli_exits_one(self):
        io = self.new_flow()
        self.successful_checks(io)
        io.automerge_replies = [self.refused]
        io.pr['mergeable_state'] = 'behind'
        code, output = self.cli_run(io, '--json', '--notify', 'implementation')
        self.assertEqual(code, 1)
        self.assert_paused(io, json.loads(output), 'PR 落后主干')

    def test_blocked_mergeability_does_not_hide_a_base_advance(self):
        io = self.new_flow()
        self.successful_checks(io)
        io.automerge_replies = [self.refused]
        io.pr['mergeable_state'] = 'blocked'
        io.base_is_current = False
        self.assert_paused(io, self.run_pr(io, notify='implementation'), 'PR 落后主干')

    def test_fallback_rechecks_branch_rules_after_the_last_rejection(self):
        io = self.new_flow()
        self.successful_checks(io)
        io.automerge_replies = [self.mismatch] * 5 + [self.refused]
        enable = io.enable_automerge

        def change_rules(*args):
            result = enable(*args)
            if result == self.refused:
                io.branch_rules = ruleset(APP_ID + 1)
            return result

        with mock.patch.object(io, 'enable_automerge', side_effect=change_rules):
            result = self.run_pr(io, notify='implementation', sleep=lambda _: None)
        self.assert_paused(io, result, f'{VERIFIED} 的来源未限定为 hub App')

    def test_non_head_rejection_cannot_merge_a_pr_with_a_new_head(self):
        io = self.new_flow()
        self.successful_checks(io)
        io.automerge_replies = [self.refused]
        enable = io.enable_automerge

        def change_head(*args):
            io.pr = dict(io.pr, head=dict(io.pr['head'], sha='e' * 40))
            return enable(*args)

        with mock.patch.object(io, 'enable_automerge', side_effect=change_head):
            result = self.run_pr(io, notify='implementation')
        self.assert_paused(io, result, 'head 被改了')

    def test_fallback_checks_for_a_freeze_during_the_request(self):
        io = self.new_flow()
        self.successful_checks(io)
        io.automerge_replies = [self.refused]
        enable = io.enable_automerge

        def freeze(*args):
            self.state.set_frozen(True)
            return enable(*args)

        with mock.patch.object(io, 'enable_automerge', side_effect=freeze):
            result = self.run_pr(io, notify='implementation')
        self.assert_paused(io, result, 'watch 已冻结')

    def test_pause_disables_a_request_that_appears_during_enable(self):
        io = self.new_flow()
        self.successful_checks(io)
        io.pr['mergeable_state'] = 'behind'

        def rejected_after_queueing(*args):
            io.pr['auto_merge'] = {'merge_method': 'squash', 'enabled_by': BOT_USER}
            return self.refused

        with mock.patch.object(io, 'enable_automerge', side_effect=rejected_after_queueing):
            result = self.run_pr(io, notify='implementation')
        self.assert_paused(io, result, 'PR 落后主干')
        self.assertIsNone(io.pr['auto_merge'])


class ManualRouteTest(RunRouteFixture):
    def assert_handed_to_user(self, io, *, notify='implementation'):
        result = self.run_pr(io, notify=notify, author_host='claude-code')
        self.assertEqual((result['route'], result['result']), ('manual', 'handed-to-user'), result)
        [(path, status)] = io.statuses
        self.assertEqual(path, f'repos/{REPO}/statuses/{HEAD}')
        self.assertEqual((status['context'], status['state']), (VERIFIED, 'success'))
        self.assertIn(FakeWork(io).patch_id()[:12], status['description'])
        self.assertIn(FakeWork(io).base_tip[:12], status['description'])
        comment = next(c for c in io.comments if c['html_url'] == status['target_url'])
        self.assertIn('autopilot-evidence', comment['body'])
        self.assertIn('"authorHost": "claude-code"', comment['body'])
        self.assertEqual(io.graphql_calls, [])
        self.assertEqual(io.merge_calls(), [])
        handoff = [c for c in io.comments if '由你合并' in c['body']]
        self.assertEqual(len(handoff), 1)
        for reason in result['routeReasons']:
            self.assertIn(reason, handoff[0]['body'])
        notification = self.state.pending_notifications()[-1]
        self.assertEqual(notification['target'], notify)
        self.assertIn('由你合并', notification['body'])
        return result

    def test_t4_runs_validation_and_deduplicates_handoff_for_the_same_head(self):
        io = self.new_flow()
        io.files.append({'filename': '.github/workflows/ci.yml'})
        result = self.assert_handed_to_user(io)
        self.assertIn('T4', '\n'.join(result['routeReasons']))
        self.assertEqual(io.ran, ['make test'])
        self.assertEqual(io.verify_calls, ['check', 'record'])
        self.assertEqual(self.run_pr(io, notify='implementation')['result'], 'handed-to-user')
        self.assertEqual(len([c for c in io.comments if '由你合并' in c['body']]), 1)
        self.assertEqual(len(self.state.pending_notifications()), 1)

    def test_manual_rerun_disables_previously_queued_automerge(self):
        io = self.new_flow()
        self.assertEqual(self.run_pr(io)['result'], 'automerge-on')
        io.branch_rules = ruleset(strict=False)
        result = self.run_pr(io)
        self.assertEqual((result['route'], result['result']), ('manual', 'handed-to-user'), result)
        self.assertIsNone(io.pr['auto_merge'])
        self.assertEqual(io.merge_calls(), [])
        disable = ('POST', 'graphql', io.graphql_calls[-1])
        self.assertIn('disablePullRequestAutoMerge', disable[2]['query'])
        self.assertLess(io.calls.index(disable), io.calls.index(('POST', *io.statuses[-1])))

    def test_early_stop_disables_previously_queued_automerge(self):
        io = self.new_flow()
        self.assertEqual(self.run_pr(io)['result'], 'automerge-on')
        io.pr['draft'] = True
        result = self.run_pr(io)
        self.assertEqual((result['result'], result['step']), ('stopped', 'pr'), result)
        self.assertIsNone(io.pr['auto_merge'])
        self.assertEqual([status['state'] for _, status in io.statuses], ['success'])

    def test_legacy_non_auto_reuses_final_acceptance(self):
        io = legacy_io(base_takeover={'m': effective('human')})
        io.config = {}
        self.assert_handed_to_user(io)
        self.assertIn('acceptance', io.module_reads)
        self.assertEqual(io.ran, ['make test'])

    def test_dependabot_only_checks_and_classifies_risk(self):
        io = self.new_flow()
        io.pr['user'] = {'login': 'dependabot[bot]', 'type': 'Bot'}
        io.config = {'mergeMode': 'direct'}
        with mock.patch.object(flow, 'evaluate', side_effect=AssertionError('module admission')):
            self.assert_handed_to_user(io)
        self.assertEqual(io.module_reads, [])
        self.assertEqual(io.ran, ['make test'])
        self.assertEqual(io.case_runs, [])
        self.assertEqual(io.verify_calls, [])


class UnassignedRouteTest(RunRouteFixture):
    def unassigned_pr(self, spec_guard):
        if spec_guard:
            io = self.verify.unticked(owners=['m'])
        else:
            io = FakeIO(module=None, merge='auto')
        io.config = {'mergeMode': 'direct'}
        io.contents['.github/autopr/pipeline.yml'] = (
            "extends: plugin\nadd:\n  - id: lint\n    at: pre-merge\n    run: make lint\n    by: ai\n"
        )
        return io

    def test_unassigned_passes_checks_and_hands_off(self):
        for spec_guard in (False, True):
            with self.subTest(spec_guard=spec_guard):
                io = self.unassigned_pr(spec_guard)
                with mock.patch.object(flow, 'evaluate', side_effect=AssertionError('module admission')):
                    result = self.run_pr(io, notify='implementation')
                self.assertEqual((result['result'], result.get('route')), ('handed-to-user', 'manual'), result)
                self.assertTrue(any('未归属模块' in reason for reason in result['routeReasons']))
                [(path, status)] = io.statuses
                self.assertEqual(path, f'repos/{REPO}/statuses/{HEAD}')
                self.assertEqual((status['context'], status['state']), (VERIFIED, 'success'))
                self.assertIn('未归属模块', status['description'])
                self.assertIn('交你合并', status['description'])
                comment = next(c['body'] for c in io.comments if c['html_url'] == status['target_url'])
                payload = json.loads(flow_block(comment, 'autopilot-evidence'))
                self.assertEqual(payload['route'], 'manual')
                self.assertEqual(payload['routeReasons'], result['routeReasons'])
                self.assertEqual(payload['validatedHeadOid'], HEAD)
                self.assertEqual(payload['riskTier'], 'T0')
                self.assertEqual([c['cmd'] for c in payload['checks']], ['make test', 'make lint'])
                self.assertEqual(io.ran, ['make test', 'make lint'])
                self.assertEqual(io.module_reads, ['base', 'find'] if spec_guard else [])
                self.assertEqual(getattr(io, 'scope_queries', []), [])
                self.assertEqual(getattr(io, 'verify_calls', []), [])
                self.assertEqual(io.reviews, [])
                self.assertEqual(io.graphql_calls, [])
                self.assertEqual(io.merge_calls(), [])
                self.assertEqual(self.state.get_pr(REPO, 7)['state'], 'handed-to-user')
                self.assertTrue(any('由你合并' in c['body'] for c in io.comments))

    def test_non_spec_guard_hands_off_with_human_missing_or_invalid_takeover(self):
        for options in ({'merge': 'human'}, {'takeover': False}, {'merge': 'yes'}):
            with self.subTest(options=options):
                io = FakeIO(module=None, **options)
                result = self.run_pr(io)
                self.assertEqual((result['result'], result['route']), ('handed-to-user', 'manual'), result)
                self.assertIn('"merge": "human"', io.comments[0]['body'])
                self.assertEqual([status['state'] for _, status in io.statuses], ['success'])
                self.assertEqual(io.merge_calls(), [])

    def test_unassigned_check_failures_do_not_publish_success(self):
        for spec_guard in (False, True):
            for command, step in [('make test', 'check'), ('make lint', 'pre-merge')]:
                with self.subTest(spec_guard=spec_guard, command=command):
                    io = self.unassigned_pr(spec_guard)
                    io.check_results[command] = flow.check_result(1, 'failed')
                    result = self.run_pr(io)
                    self.assertEqual((result['result'], result['step']), ('stopped', step), result)
                    self.assertIn(command, result['reason'])
                    self.assertEqual([status['state'] for _, status in io.statuses], ['failure'])
                    self.assertEqual(io.graphql_calls, [])
                    self.assertEqual(io.merge_calls(), [])

    def test_unassigned_risk_failure_publishes_success_and_hands_off(self):
        cases = [
            ('requirements.txt', '风险档 T3', '改了依赖文件 requirements.txt'),
            ('.github/workflows/ci.yml', '风险档 T4', '.github/workflows/ci.yml'),
            ('spec/other.md', '要你合', 'spec-guard 产物没核对到有效的批准：spec/other.md'),
        ]
        for spec_guard in (False, True):
            for path, risk_summary, risk_detail in cases:
                with self.subTest(spec_guard=spec_guard, path=path):
                    io = self.unassigned_pr(spec_guard)
                    io.files.append({'filename': path, 'additions': 1, 'patch': '@@ -0,0 +1 @@\n+New content\n'})
                    result = self.run_pr(io)
                    self.assertEqual((result['result'], result.get('route')), ('handed-to-user', 'manual'), result)
                    self.assertIn('未归属模块，交你合并', result['routeReasons'])
                    self.assertTrue(any(risk_summary in reason and risk_detail in reason
                                        for reason in result['routeReasons']), result)
                    [(status_path, status)] = io.statuses
                    self.assertEqual(status_path, f'repos/{REPO}/statuses/{HEAD}')
                    # The required status reflects verification of the head, so a human can merge it.
                    self.assertEqual((status['context'], status['state']), (VERIFIED, 'success'))
                    self.assertIn('未归属模块，交你合并', status['description'])
                    self.assertEqual(io.ran, ['make test', 'make lint'])
                    handoff = next(c['body'] for c in io.comments if '由你合并' in c['body'])
                    self.assertIn(risk_detail, handoff)
                    self.assertEqual(io.graphql_calls, [])
                    self.assertEqual(io.merge_calls(), [])

    def test_unassigned_risk_failure_only_disables_queued_automerge_before_success(self):
        io = self.unassigned_pr(spec_guard=True)
        io.files.append({'filename': '.github/workflows/ci.yml'})
        io.pr['auto_merge'] = {'merge_method': 'squash', 'enabled_by': BOT_USER}

        result = self.run_pr(io)

        self.assertEqual(result['result'], 'handed-to-user', result)
        self.assertIsNone(io.pr['auto_merge'])
        [disable] = io.graphql_calls
        self.assertIn('disablePullRequestAutoMerge', disable['query'])
        [(status_path, status)] = io.statuses
        self.assertEqual(status['state'], 'success')
        self.assertLess(io.calls.index(('POST', 'graphql', disable)), io.calls.index(('POST', status_path, status)))
        self.assertEqual(io.merge_calls(), [])

    def test_unassigned_batch_still_stops_without_status(self):
        for spec_guard in (False, True):
            with self.subTest(spec_guard=spec_guard):
                io = self.unassigned_pr(spec_guard)
                result = self.run_pr(io, merge_method='merge')
                step = 'acceptance' if spec_guard else 'takeover'
                self.assertEqual((result['result'], result['step']), ('stopped', step), result)
                self.assertEqual(io.ran, ['make test', 'make lint'] if spec_guard else [])
                if not spec_guard:
                    self.assertIn('一律交你合', result['reason'])
                    self.assertIn('"merge": "human"', io.comments[0]['body'])
                self.assertEqual(io.statuses, [])
                self.assertEqual(io.graphql_calls, [])
                self.assertEqual(io.merge_calls(), [])

    def test_unassigned_dry_run_reports_manual_route_without_github_writes(self):
        for spec_guard in (False, True):
            with self.subTest(spec_guard=spec_guard):
                io = self.unassigned_pr(spec_guard)
                code, output = self.cli_run(io, '--dry-run')
                self.assertEqual(code, 0, output)
                self.assertIn('route=manual', output)
                self.assertIn('未归属模块', output)
                self.assertEqual(io.ran, ['make test', 'make lint'])
                self.assertEqual(io.writes(), [])
                self.assertIsNone(self.state.get_pr(REPO, 7))

    def test_unassigned_rechecks_degrade_before_publishing_success(self):
        for spec_guard in (False, True):
            with self.subTest(spec_guard=spec_guard):
                io = self.unassigned_pr(spec_guard)
                api = io.api

                def post_evidence_and_open_degrade(path, method='GET', body=None):
                    response = api(path, method, body)
                    if method == 'POST' and path.endswith('/comments') and 'autopilot-evidence' in body['body']:
                        io.degrade = [degrade_block(3)]
                    return response

                io.api = post_evidence_and_open_degrade
                result = self.run_pr(io)
                self.assertEqual((result['result'], result['step']), ('stopped', 'admission'), result)
                self.assertIn('#3', result['reason'])
                self.assertEqual([status['state'] for _, status in io.statuses], ['failure'])
                self.assertEqual(io.graphql_calls, [])
                self.assertEqual(io.merge_calls(), [])


class VerificationFailureStatusTest(RunRouteFixture):
    def test_failure_replaces_success_and_disables_only_queued_automerge(self):
        for queued in (False, True):
            with self.subTest(queued=queued):
                io = self.new_flow()
                if queued:
                    self.assertEqual(self.run_pr(io)['result'], 'automerge-on')
                    io.verification_files.clear()  # Force verification of this head to run again.
                case = {'exit': 1, 'timeout': False, 'output': 'regression failed'}
                baseline = {'exit': 0, 'timeout': False, 'output': 'baseline passed'}
                with mock.patch.object(FakeWork, 'run_case', side_effect=[case, baseline]):
                    result = self.run_pr(io)
                self.assertEqual((result['result'], result['step']), ('stopped', 'verification'))
                self.assertEqual(io.statuses[-1][0], f'repos/{REPO}/statuses/{HEAD}')
                self.assertEqual((io.statuses[-1][1]['context'], io.statuses[-1][1]['state']), (VERIFIED, 'failure'))
                self.assertEqual(io.merge_calls(), [])
                if queued:
                    self.assertEqual([s['state'] for _, s in io.statuses], ['success', 'failure'])
                    enable, call = io.graphql_calls
                    self.assertEqual(enable['expectedHeadOid'], HEAD)
                    self.assertIn('disablePullRequestAutoMerge', call['query'])
                    self.assertEqual(call['variables']['pullRequestId'], 'PR_node')
                    self.assertIsNone(io.pr['auto_merge'])
                else:
                    self.assertEqual(io.graphql_calls, [])

    def test_failed_status_write_still_attempts_to_disable_queued_automerge(self):
        io = self.new_flow()
        io.check_exit = 1
        io.pr['auto_merge'] = {'merge_method': 'squash'}
        api = io.api

        def reject_status(path, method='GET', body=None):
            if method == 'POST' and '/statuses/' in path:
                raise cli.ApiError('status write rejected')
            return api(path, method, body)

        io.api = reject_status
        with self.assertRaisesRegex(cli.ApiError, 'status write rejected'):
            self.run_pr(io)
        self.assertIsNone(io.pr['auto_merge'])
        self.assertEqual(len(io.graphql_calls), 1)

    def test_failure_status_and_queue_cleanup_precede_unwritable_repair_state(self):
        for error in (StateLockedError('locked'), StateCorruptError('corrupt')):
            with self.subTest(error=error):
                io = self.new_flow()
                self.assertEqual(self.run_pr(io)['result'], 'automerge-on')
                io.check_exit = 1

                def fail_recording(*args):
                    self.assertEqual(io.statuses[-1][1]['state'], 'failure')
                    self.assertIsNone(io.pr['auto_merge'])
                    raise error

                with mock.patch.object(self.state, 'record_failure', side_effect=fail_recording), \
                        contextlib.redirect_stderr(StringIO()) as stderr:
                    code, _ = self.cli_run(io)
                self.assertEqual(code, 2)
                self.assertIn(str(error), stderr.getvalue())
                self.assertEqual(self.state.get_pr(REPO, 7)['pause']['reason'], 'run-failed')


class RunFailureRecordTest(RunRouteFixture):
    def test_api_error_pauses_and_queues_a_user_notification_before_exit_two(self):
        io = self.new_flow()
        error = cli.ApiError('GitHub status write failed')
        api = io.api

        def reject_status(path, method='GET', body=None):
            if method == 'POST' and '/statuses/' in path:
                raise error
            return api(path, method, body)

        with mock.patch.object(cli, 'LocalIO', return_value=io), \
                mock.patch.object(cli, 'WatchState', return_value=self.state), \
                mock.patch.object(cli.tokens, 'mint', return_value='token') as mint, \
                mock.patch.object(cli.tokens, 'revoke') as revoke, \
                mock.patch.object(io, 'api', side_effect=reject_status), \
                contextlib.redirect_stderr(StringIO()) as stderr:
            exit_code = cli.main(['run', '--repo', REPO, '--pr', '7', '--notify', 'implementation'])
        self.assertEqual(exit_code, 2)
        self.assertIn(str(error), stderr.getvalue())
        mint.assert_called_once_with(REPO, dict(cli.WRITE, statuses='write'))
        revoke.assert_called_once_with('token')
        record = self.state.get_pr(REPO, 7)
        self.assertEqual(record['state'], 'paused')
        self.assertEqual(record['pause']['reason'], 'run-failed')
        self.assertIn(str(error), record['pause']['recovery'])
        [notice] = self.state.pending_notifications()
        self.assertEqual(notice['target'], 'user')
        self.assertIn(str(error), notice['body'])

    def test_local_failure_after_enable_disables_queue_before_reporting_run_failed(self):
        io = self.new_flow()

        def fail_recording(*args):
            self.assertIsNotNone(io.pr['auto_merge'])
            raise StateLockedError('could not record successful enable')

        with mock.patch.object(flow, 'record_automerge_on', side_effect=fail_recording), \
                contextlib.redirect_stderr(StringIO()):
            code, _ = self.cli_run(io)
        self.assertEqual(code, 2)
        self.assertIsNone(io.pr['auto_merge'])
        self.assertIn('disablePullRequestAutoMerge', io.graphql_calls[-1]['query'])
        self.assertEqual(self.state.get_pr(REPO, 7)['pause']['reason'], 'run-failed')


class RepairLimitTest(RunRouteFixture):
    def test_two_failed_repairs_pause_and_notify_once(self):
        io = self.verify.io([case('R9')])
        heads = ['a' * 40, 'e' * 40, 'c' * 40, 'd' * 40]

        def execute(work, command, sha):
            return {'exit': int(sha == io.pr['head']['sha']), 'timeout': False, 'output': 'regression'}

        with mock.patch.object(FakeWork, 'run_case', execute):
            for index, head in enumerate(heads):
                io.pr['head']['sha'] = head
                expected = 'stopped' if index < 2 else 'paused'
                for _ in range(2):
                    with self.subTest(head=head, result=expected):
                        result = self.run_pr(io)
                        self.assertEqual(result['result'], expected, result)
                        self.assertEqual(self.state.repair_count(REPO, 7, 'case:R9'), min(index, 2))
            exit_code, stdout = self.cli_run(io, '--json')
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(stdout)['result'], 'paused')
        self.assertEqual(self.state.read()['repairs'][f'{REPO}#7']['case:R9'], heads[1:3])
        pause = self.state.get_pr(REPO, 7)['pause']
        self.assertEqual(pause['reason'], '同一原因修了两次仍失败：case:R9')
        self.assertEqual(pause['recovery'], '用户决定')
        [notice] = self.state.pending_notifications()
        self.assertEqual(notice['target'], 'user')
        for value in ('case:R9', *heads[1:3], '继续修', '改方案', '交给你手工处理'):
            self.assertIn(value, notice['body'])
        self.assertEqual(io.statuses[-1][1]['state'], 'failure')
        self.assertEqual(io.merge_calls(), [])

    def test_checks_and_review_findings_keep_separate_counts(self):
        io = legacy_io(check_command='R9', check_exit=1)
        first = {'id': 'R9', 'severity': 'blocking', 'title': 'Requires a fix'}
        other = {'file': 'src/a.py', 'title': 'Missing assignment', 'severity': 'blocking', 'line': 7}
        suggestion = {'id': 'advice', 'severity': 'suggestion'}
        self.assertEqual(self.run_pr(io)['step'], 'check')
        io.check_exit = 0
        io.pr['head']['sha'] = 'b' * 40
        review = dict(report_for(io.pr['head']['sha']), findings=[first, other, suggestion], review_outcome='findings')
        io.acceptance_record = accepted(review, sha=io.pr['head']['sha'])
        self.assertEqual(self.run_pr(io)['step'], 'review')
        self.assertEqual(self.state.repair_count(REPO, 7, 'check:R9'), 0)
        self.assertEqual(self.state.repair_count(REPO, 7, 'review:R9'), 0)
        io.pr['head']['sha'] = 'c' * 40
        review = dict(report_for(io.pr['head']['sha']), findings=[first, suggestion], review_outcome='findings',
                      recheck={'items': [{'status': 'unresolved', 'finding': dict(other, line=13)}]})
        io.acceptance_record = accepted(review, sha=io.pr['head']['sha'])
        self.assertEqual(self.run_pr(io)['step'], 'review')
        io.check_exit = 1
        self.assertEqual(self.run_pr(io)['step'], 'check')
        repairs = self.state.read()['repairs'][f'{REPO}#7']
        self.assertEqual(len(repairs), 3, repairs)
        self.assertEqual(repairs['check:R9'], ['c' * 40])
        self.assertEqual(repairs['review:R9'], ['c' * 40])
        [summary] = [key for key in repairs if key not in ('check:R9', 'review:R9')]
        self.assertTrue(summary.startswith('review:'))
        self.assertEqual(repairs[summary], ['c' * 40])

    def test_failed_cases_count_even_if_spec_guard_cannot_record_the_report(self):
        io = self.verify.io([case('R8'), case('R9')])
        io.verify_code, io.verify_problem = 3, 'author-unknown'

        def execute(work, command, sha):
            return {'exit': int(sha == io.pr['head']['sha']), 'timeout': False, 'output': 'regression'}

        with mock.patch.object(FakeWork, 'run_case', execute):
            for head in (HEAD, 'e' * 40):
                io.pr['head']['sha'] = head
                self.assertIn('author-unknown', self.run_pr(io)['reason'])
        self.assertEqual(self.state.read()['repairs'][f'{REPO}#7'], {'case:R8': [head], 'case:R9': [head]})

    def test_existing_test_pauses_before_checks_and_exits_one(self):
        io = self.new_flow()
        io.scope_base, io.changed_existing = 'e' * 40, ['components/widget.spec.js']
        io.pr['auto_merge'] = {'merge_method': 'squash'}
        exit_code, stdout = self.cli_run(io, '--json')
        self.assertEqual(exit_code, 1)
        result = json.loads(stdout)
        self.assertEqual(result['result'], 'paused')
        self.assertEqual(result['repairBaseline'], {'source': 'scope_base', 'sha': io.scope_base})
        reason = '修复时改了已有测试／回归测试文档：components/widget.spec.js'
        self.assertEqual(self.state.get_pr(REPO, 7)['pause']['reason'], reason)
        [notice] = self.state.pending_notifications()
        self.assertEqual(notice['target'], 'user')
        self.assertIn(reason, notice['body'])
        self.assertEqual(io.ran, [])
        self.assertEqual(io.case_runs, [])
        self.assertEqual(io.statuses[-1][1]['state'], 'failure')
        self.assertIsNone(io.pr['auto_merge'])

    def test_existing_regression_document_pauses_before_checks(self):
        io = self.new_flow()
        io.changed_existing = ['tasks/m/regression.md']
        result = self.run_pr(io)
        self.assertEqual(result['result'], 'paused', result)
        self.assertEqual(result['reason'], '修复时改了已有测试／回归测试文档：tasks/m/regression.md')
        self.assertEqual(result['repairBaseline'], {'source': 'merge-base', 'sha': 'b' * 40})
        self.assertEqual(io.ran, [])
        self.assertEqual(io.verify_calls, [])

    def test_added_test_can_pass_and_evidence_names_the_fallback_baseline(self):
        io = self.new_flow()
        io.files.append({'filename': 'tests/test_added.py', 'status': 'added', 'additions': 1,
                         'deletions': 0, 'patch': '+assert True'})
        self.assertEqual(self.run_pr(io)['result'], 'automerge-on')
        self.assertEqual(self.verify.evidence(io)['repairBaseline'], {'source': 'merge-base', 'sha': 'b' * 40})
        self.assertEqual(self.state.pending_notifications(), [])

    def test_unconfirmed_scope_base_is_reported_in_run_evidence(self):
        io = self.new_flow()
        baseline = {'source': 'merge-base', 'sha': 'b' * 40, 'fallbackReason': 'scope-base-not-ancestor'}
        with mock.patch.object(FakeWork, 'changed_existing_files', return_value=(baseline, [])):
            result = self.run_pr(io)
        self.assertEqual(result['repairBaseline'], baseline)
        self.assertIn('祖先', '\n'.join(result['notes']))
        self.assertEqual(self.verify.evidence(io)['repairBaseline'], baseline)

    def test_dry_run_leaves_no_pause_or_notice(self):
        io = self.new_flow()
        io.changed_existing = ['tests/test_existing.py']
        result = self.run_pr(io, dry_run=True)
        self.assertEqual(result['result'], 'stopped', result)
        self.assertIn('tests/test_existing.py', result['reason'])
        self.assertIsNone(self.state.get_pr(REPO, 7))
        self.assertEqual(self.state.pending_notifications(), [])
        self.assertEqual(io.writes(), [])

    def test_work_diffs_committed_files_against_scope_base_then_merge_base(self):
        root = make_repo(self, {'tests/test_old.py': 'old\n', 'tasks/m/regression.md': 'original\n'})
        work = cli.Work.__new__(cli.Work)
        work.root, work.git_env = root, dict(os.environ)
        scope_base = work.git('rev-parse', 'HEAD').strip()
        write_files(root, {'tests/test_existing.py': 'existing\n', 'scope': scope_base})
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'advance main')
        work.base_tip = work.merge_base = work.git('rev-parse', 'HEAD').strip()
        commit_on_branch(root, {'tests/test_old.py': 'changed\n', 'tests/test_existing.py': 'changed\n',
                                'tests/test_added.py': 'new\n', 'tasks/m/regression.md': 'edited\n',
                                'scope': work.merge_base})
        work.head = work.git('rev-parse', 'HEAD').strip()
        git(root, 'checkout', '-q', 'main')
        with tempfile.TemporaryDirectory() as plugin:
            write_files(plugin, {'hooks/module_scope.py': (
                'def scope_base(root, module):\n'
                '    assert module == "m"\n'
                '    return (root / "scope").read_text().strip()\n'
            )})
            with mock.patch.object(cli, 'find_spec_guard_root', return_value=plugin):
                self.assertEqual(work.changed_existing_files('m'), ({'sha': scope_base, 'source': 'scope_base'},
                                 ['tasks/m/regression.md', 'tests/test_old.py']))
        with mock.patch.object(cli, 'find_spec_guard_root', return_value=None):
            self.assertEqual(work.changed_existing_files('m'), ({'sha': work.merge_base, 'source': 'merge-base'},
                             ['scope', 'tasks/m/regression.md', 'tests/test_existing.py', 'tests/test_old.py']))

    def test_main_changes_after_scope_base_are_not_blamed_on_the_pr(self):
        # Review finding: diffing scope_base..head blamed the PR for test edits that reached main later.
        root = make_repo(self, {'tests/test_main_only.py': 'old\n', 'tests/test_pr.py': 'old\n'})
        work = cli.Work.__new__(cli.Work)
        work.root, work.git_env = root, dict(os.environ)
        scope_base = work.git('rev-parse', 'HEAD').strip()
        write_files(root, {'tests/test_main_only.py': 'edited on main\n', 'scope': scope_base})
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'someone else edits a test on main')
        work.base_tip = work.merge_base = work.git('rev-parse', 'HEAD').strip()
        commit_on_branch(root, {'tests/test_pr.py': 'changed by the PR\n'})
        work.head = work.git('rev-parse', 'HEAD').strip()
        git(root, 'checkout', '-q', 'main')
        with tempfile.TemporaryDirectory() as plugin:
            write_files(plugin, {'hooks/module_scope.py': (
                'def scope_base(root, module):\n'
                '    return (root / "scope").read_text().strip()\n'
            )})
            with mock.patch.object(cli, 'find_spec_guard_root', return_value=plugin):
                baseline, paths = work.changed_existing_files('m')
        self.assertEqual(baseline, {'sha': scope_base, 'source': 'scope_base'})
        self.assertEqual(paths, ['tests/test_pr.py'])

    def test_scope_base_from_base_checkout_must_be_an_ancestor_of_the_merge_base(self):
        root = make_repo(self, {'tests/test_old.py': 'old\n'})
        work = cli.Work.__new__(cli.Work)
        work.root, work.git_env = root, dict(os.environ)
        work.merge_base = work.git('rev-parse', 'HEAD').strip()
        commit_on_branch(root, {'tests/test_old.py': 'changed\n'})
        work.head = work.git('rev-parse', 'HEAD').strip()
        git(root, 'checkout', '-q', 'main')
        write_files(root, {'other': 'advanced base\n'})
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'advance main')
        work.base_tip = work.git('rev-parse', 'HEAD').strip()
        with tempfile.TemporaryDirectory() as plugin:
            write_files(plugin, {'hooks/module_scope.py': (
                'import subprocess\n'
                'def scope_base(root, module):\n'
                '    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()\n'
            )})
            with mock.patch.object(cli, 'find_spec_guard_root', return_value=plugin):
                baseline, paths = work.changed_existing_files('m')
        self.assertEqual(baseline, {'source': 'merge-base', 'sha': work.merge_base,
                                    'fallbackReason': 'scope-base-not-ancestor'})
        self.assertEqual(paths, ['tests/test_old.py'])

    def test_batch_skips_repair_guard_and_never_updates_watch_state(self):
        io = self.new_flow()
        io.changed_existing = ['tests/test_old.py', 'tasks/m/regression.md']
        before = self.state.read()
        result = self.run_pr(io, merge_method='merge')
        self.assertEqual(result['result'], 'merged', result)
        self.assertEqual(io.merge_calls()[0][2], {'sha': HEAD, 'merge_method': 'merge'})
        self.assertEqual(self.state.read(), before)
        self.assertEqual(io.statuses, [])

        io = self.new_flow()
        io.check_exit = 1
        for head in (HEAD, 'e' * 40, 'f' * 40):
            io.pr['head']['sha'] = head
            result = self.run_pr(io, merge_method='merge')
            self.assertEqual((result['result'], result['step']), ('stopped', 'check'), result)
        self.assertEqual(self.state.read(), before)
        self.assertEqual(io.statuses, [])


class DirectModeTest(RunRouteFixture):
    def test_dry_run_prints_direct_and_makes_no_github_or_state_writes(self):
        io = self.new_flow()
        io.local_config = cli.LocalIO.local_config
        directory = self.state.path.parent
        (directory / 'config.json').write_text(json.dumps({'mergeMode': 'direct'}), encoding='utf-8')
        self.state.set_frozen(True)
        before = self.state.read()
        with mock.patch.object(cli, 'LocalIO', return_value=io), \
                mock.patch.object(cli, 'STATE', str(directory)), \
                mock.patch.object(cli, 'WatchState', return_value=self.state), \
                mock.patch.object(cli.tokens, 'mint', return_value='token') as mint, \
                mock.patch.object(cli.tokens, 'revoke'), contextlib.redirect_stdout(StringIO()) as stdout:
            exit_code = cli.main(['run', '--repo', REPO, '--pr', '7', '--dry-run', '--author-host', 'codex'])
        self.assertEqual(exit_code, 0)
        self.assertIn('route=direct', stdout.getvalue())
        self.assertEqual(io.writes(), [])
        self.assertEqual(self.state.read(), before)
        mint.assert_called_once_with(REPO, cli.READ)

    def test_run_exit_codes(self):
        outcomes = {'merged': 0, 'automerge-on': 0, 'handed-to-user': 0, 'dry-run': 0,
                    'stopped': 1, 'paused': 1}
        with mock.patch.object(cli.tokens, 'mint', return_value='token'), \
                mock.patch.object(cli.tokens, 'revoke'), contextlib.redirect_stdout(StringIO()):
            for result, expected in outcomes.items():
                with self.subTest(result=result), mock.patch.object(flow, 'run', return_value={'result': result}):
                    self.assertEqual(cli.main(['run', '--repo', REPO, '--pr', '7', '--json']), expected)


class HappyPathTest(unittest.TestCase):
    def test_explicit_merge_preserves_commit_history(self):
        io = legacy_io()
        res = run(io, merge_method="merge")
        self.assertEqual(res["result"], "merged", res)
        self.assertEqual(io.merge_calls(), [("PUT", f"repos/{REPO}/pulls/7/merge",
                                            {"sha": HEAD, "merge_method": "merge"})])

    def test_merges_the_reviewed_head(self):
        io = legacy_io()
        res = run(io)
        self.assertEqual(res["result"], "merged", res)
        self.assertEqual(io.merge_calls(), [("PUT", f"repos/{REPO}/pulls/7/merge", {"sha": HEAD, "merge_method": "squash"})])
        labels = [c for c in io.calls if c[1].endswith("/labels")]
        self.assertEqual(labels[0][2], {"labels": ["autopilot:authored", "autopilot:auto-merged"]})
        self.assertEqual(io.ran, ["make test"])

    def test_posts_takeover_then_evidence_bound_to_head(self):
        io = legacy_io()
        run(io)
        bodies = [c["body"] for c in io.comments]
        self.assertIn("autopilot-takeover:v1", bodies[0])
        ev = json.loads(flow_block(bodies[1], "autopilot-evidence"))
        self.assertEqual(ev["head_sha"], HEAD)
        self.assertEqual(ev["checks"][0]["exit"], 0)
        self.assertEqual(ev["review"]["blocking"], 0)
        self.assertIn("已合并", bodies[-1])

    def test_owner_authored_pr_is_allowed(self):
        self.assertEqual(run(legacy_io(author={"login": "haigeerlab", "id": 1, "type": "User"}))["result"], "merged")

    def test_dry_run_writes_nothing(self):
        io = legacy_io()
        res = run(io, dry_run=True)
        self.assertEqual(res["result"], "dry-run")
        self.assertEqual(io.writes(), [])
        self.assertEqual(len(io.reviews), 0)


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

    def test_explicit_merge_stops_when_repository_disallows_merge_commits(self):
        io = legacy_io()
        original = io.api
        def api(path, method="GET", body=None):
            if path == f"repos/{REPO}":
                return {"allow_squash_merge": True, "allow_merge_commit": False}
            return original(path, method, body)
        io.api = api
        res = self.assertStops(io, "merge", merge_method="merge")
        self.assertEqual(res["reason"], "仓库不允许 merge commit，module-crew 的合并是叠起来的，不能压平或变基，交你在仓库设置里打开 Allow merge commits")

    def test_no_bot_identity(self):
        io = legacy_io(bot=None)
        self.assertStops(io, "identity")
        self.assertEqual(io.calls, [])

    def test_fork(self):
        self.assertStops(legacy_io(fork=True), "pr", "fork")

    def test_stranger_author(self):
        self.assertStops(legacy_io(author={"login": "stranger", "id": 9, "type": "User"}), "pr", "stranger")

    def test_open_degrade_issue(self):
        self.assertStops(legacy_io(degrade=[3]), "degrade", "#3")

    def test_high_risk_in_direct_mode_stops_at_admission(self):
        io = legacy_io(files=(".github/workflows/ci.yml",))
        # Direct mode now reaches live admission after checking and reusing final acceptance.
        self.assertStops(io, "admission", "T4")
        self.assertEqual(io.reviews, [])
        self.assertEqual(io.ran, ["make test"])

    def test_no_check_command(self):
        self.assertStops(legacy_io(check_command=None), "check", "找不到检查命令")

    def test_failing_check(self):
        io = legacy_io(check_exit=1)
        self.assertStops(io, "check", "退出码 1")
        self.assertEqual(io.reviews, [])

    def test_readback_not_merged(self):
        io = legacy_io(merged_ok=False)
        res = run(io)
        self.assertEqual((res["result"], res["step"]), ("stopped", "readback"))
        self.assertEqual(len(io.merge_calls()), 1)  # merge was requested; reading back is what failed
        self.assertFalse([c for c in io.calls if c[1].endswith("/labels")])

    def test_every_stop_leaves_a_bot_comment(self):
        io = legacy_io(check_exit=3)
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
        io = legacy_io()
        run(io)
        ev = io.evidence_files[0]
        self.assertEqual(ev["schema"], "autopilot.check-evidence/v1")
        self.assertEqual((ev["repo"], ev["head_sha"], ev["runner"], ev["non_author"], ev["clone"]),
                         (REPO, HEAD, "autopilot-orchestrator", True, "throwaway"))
        self.assertEqual(ev["checks"][0]["cmd"], "make test")
        self.assertEqual(ev["checks"][0]["exit"], 0)
        self.assertEqual(len(ev["checks"][0]["output_sha256"]), 64)

    def test_written_in_dry_run_too(self):
        io = legacy_io()
        run(io, dry_run=True)
        self.assertEqual(len(io.evidence_files), 1)

    def test_not_written_when_check_fails(self):
        io = legacy_io(check_exit=1)
        run(io)
        self.assertEqual(io.evidence_files, [])


class PreMergeTest(unittest.TestCase):
    def pipeline(self, io, *steps):
        lines = ["extends: plugin", "add:"]
        for identifier, command, by in steps:
            lines.extend(["  - id: " + identifier, "    at: pre-merge",
                          "    run: " + json.dumps(command), "    by: " + by])
        io.contents[".github/autopr/pipeline.yml"] = "\n".join(lines)

    def test_ai_steps_run_in_order_before_review_and_join_both_evidence_records(self):
        io = legacy_io()
        self.pipeline(io, ("extra-one", "echo first", "ai"), ("extra-two", "echo second", "ai"))
        io.check_results = {"echo first": flow.check_result(0, "first output\n"),
                            "echo second": flow.check_result(0, "second output\n")}
        commands = ["make test", "echo first", "echo second"]

        original = flow.accepted_review
        def accepted_after_checks(*args):
            self.assertEqual(io.ran, commands)
            return original(*args)

        with mock.patch.object(flow, "accepted_review", side_effect=accepted_after_checks):
            result = run(io)
        self.assertEqual(result["result"], "merged", result)
        evidence = next(c["body"] for c in io.comments if "autopilot-evidence:v1" in c["body"])
        checks = json.loads(flow_block(evidence, "autopilot-evidence"))["checks"]
        self.assertEqual([c["cmd"] for c in checks], commands)
        local = io.evidence_files[-1]
        self.assertEqual([{k: c[k] for k in ("cmd", "exit", "output_sha256")} for c in local["checks"]], checks)
        self.assertEqual(checks[1]["output_sha256"], io.check_results["echo first"]["output_sha256"])
        self.assertEqual(local["checks"][2]["tail"], ["second output"])
        self.assertIn("echo first", evidence)
        self.assertIn("second output", evidence)

    def test_nonzero_stops_before_later_steps_and_review_and_saves_local_failure(self):
        io = legacy_io()
        self.pipeline(io, ("extra-one", "echo first", "ai"), ("extra-two", "echo second", "ai"))
        io.check_results["echo first"] = flow.check_result(9, "failed output\n")
        result = run(io)
        self.assertEqual((result["result"], result["step"]), ("stopped", "pre-merge"), result)
        self.assertIn("extra-one", result["reason"])
        self.assertIn("退出码 9", result["reason"])
        self.assertEqual(io.ran, ["make test", "echo first"])
        self.assertEqual(io.reviews, [])
        self.assertEqual(io.merge_calls(), [])
        self.assertEqual(io.evidence_files[-1]["checks"][-1]["exit"], 9)
        self.assertEqual(io.evidence_files[-1]["checks"][-1]["tail"], ["failed output"])

    def test_human_external_and_ask_steps_stop_without_running_the_command(self):
        for by, command in (("human", "人工核对"), ("external", "等部署"), ("human", "ask: 你")):
            with self.subTest(by=by, command=command):
                io = legacy_io()
                self.pipeline(io, ("signoff", command, by))
                result = run(io)
                self.assertEqual((result["result"], result["step"]), ("stopped", "pre-merge"), result)
                self.assertIn("合并前追加步骤 signoff 要人做：" + command, result["reason"])
                self.assertEqual(io.ran, ["make test"])
                self.assertEqual(io.reviews, [])
                self.assertEqual(io.merge_calls(), [])

    def test_invalid_pipeline_stops_before_review(self):
        for content in ("extends: [", "extends: absent", "extends: plugin\nwrong: true", "", "[]"):
            with self.subTest(content=content):
                io = legacy_io()
                io.contents[".github/autopr/pipeline.yml"] = content
                result = run(io)
                self.assertEqual((result["result"], result["step"]), ("stopped", "pre-merge"), result)
                self.assertIn("pipeline.yml", result["reason"])
                self.assertEqual(io.reviews, [])
                self.assertEqual(io.ran, ["make test"])

    def test_absent_or_release_only_pipeline_adds_no_checks(self):
        for content in (None, "extends: plugin"):
            io = legacy_io()
            if content is not None:
                io.contents[".github/autopr/pipeline.yml"] = content
            result = run(io)
            self.assertEqual(result["result"], "merged", result)
            self.assertEqual(io.ran, ["make test"])

    def test_core_check_cannot_be_replaced_or_skipped(self):
        io = legacy_io(check_exit=1)
        self.pipeline(io, ("extra", "true", "ai"))
        with mock.patch.object(FakeWork, "read_pipeline", side_effect=AssertionError("must check first")):
            result = run(io)
        self.assertEqual(result["step"], "check")
        self.assertEqual(io.ran, ["make test"])

    def test_additional_check_timeout_is_reported_as_pre_merge(self):
        io = legacy_io()
        self.pipeline(io, ("extra", "slow command", "ai"))
        with mock.patch.object(FakeWork, "run_check",
                               side_effect=[flow.check_result(0, "ok"), flow.Stop("check", "检查超过 15 分钟")]):
            result = run(io)
        self.assertEqual(result["step"], "pre-merge", result)
        self.assertIn("extra", result["reason"])
        self.assertIn("15 分钟", result["reason"])
        self.assertEqual(io.reviews, [])


class AcceptanceTest(unittest.TestCase):
    """spec-guard modules: final acceptance recorded outside the commit; its review report replaces ours."""

    def test_no_module_acceptance_hands_off_after_writing_evidence(self):
        io = FakeIO(module="m")
        res = run(io)
        self.assertEqual((res["result"], res["route"]), ("handed-to-user", "manual"))
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
        self.assertIn("/map --json（基础分支）", io.comments[0]["body"])
        self.assertFalse(any("contents/.agent/config.json" in path for _, path, _ in io.calls))

    def test_spec_guard_project_without_any_auto_checks_then_hands_off(self):
        io = FakeIO(module="m", acceptance=accepted(), reports=[], base_takeover={"m": effective("human")})
        io.config = {}
        res = run(io)
        # A project-wide human setting now permits checks before the manual handoff.
        self.assertEqual((res["result"], res["route"]), ("handed-to-user", "manual"))
        self.assertEqual(io.ran, ["make test"])

    def test_module_human_from_base_view(self):
        io = FakeIO(module="m", acceptance=accepted(), reports=[],
                    base_takeover={"m": effective("human"), "other": effective()})
        io.config = {}
        res = run(io)
        # The module's human setting is now a handoff reason.
        self.assertEqual((res["result"], res["route"]), ("handed-to-user", "manual"))
        self.assertIn("模块 m", " ".join(res["routeReasons"]))

    def test_effective_auto_from_base_view(self):
        io = FakeIO(module="m", acceptance=accepted(), reports=[], base_takeover={"m": effective()})
        self.assertEqual(run(io)["result"], "merged")

    def test_unavailable_base_view_checks_then_hands_off(self):
        io = FakeIO(module="m", acceptance=accepted(), reports=[])
        io.base = None
        io.config = {}
        res = run(io)
        # An unavailable base view now permits checks while keeping the route manual.
        self.assertEqual((res["result"], res["route"]), ("handed-to-user", "manual"))
        self.assertEqual(io.ran, ["make test"])
        self.assertTrue(res["notes"])

    def test_head_cannot_widen_base_takeover(self):
        io = FakeIO(module="m", acceptance=accepted(), reports=[],
                    base_takeover={"m": effective("human"), "other": effective()})
        io.view["modules"][0]["takeover"] = effective()
        io.config = {}
        res = run(io)
        # The base setting still controls the route, so the head's setting only leads to handoff.
        self.assertEqual((res["result"], res["route"]), ("handed-to-user", "manual"))
        self.assertIn("模块 m", " ".join(res["routeReasons"]))
        self.assertEqual(io.merge_calls(), [])

    def test_module_added_in_head_is_human(self):
        io = FakeIO(module="m", acceptance=accepted(), reports=[], base_takeover={"other": effective()})
        io.config = {}
        res = run(io)
        # A module missing on the base remains manual after checks and final acceptance.
        self.assertEqual((res["result"], res["route"]), ("handed-to-user", "manual"))
        self.assertIn("基础分支上还没有模块 m", " ".join(res["notes"]))

    def test_several_records_for_head_stop(self):
        io = FakeIO(module="m", acceptance=accepted(), reports=[])
        io.extra_modules = ["n"]
        res = run(io)
        self.assertEqual((res["result"], res["step"]), ("stopped", "acceptance"))
        self.assertIn("多条", res["reason"])
        self.assertEqual([status["state"] for _, status in io.statuses], ["failure"])
        self.assertEqual(io.merge_calls(), [])

    def test_stale_record_means_no_module_and_hands_off(self):
        io = FakeIO(module="m", acceptance=accepted(sha="b" * 40), reports=[])
        res = run(io)
        self.assertEqual((res["result"], res["route"]), ("handed-to-user", "manual"))
        self.assertIn("未归属模块", " ".join(res["routeReasons"]))
        self.assertEqual([status["state"] for _, status in io.statuses], ["success"])
        self.assertNotIn("acceptance", io.module_reads)
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
            # Admission follows acceptance now, so exercise a bad module ID in the file selected for m.
            io.find_modules = lambda repo, head: ["m"]
            res = run(io)
            self.assertEqual(res["result"], "stopped", name)
            self.assertIn(res["step"], ("acceptance", "review"), name)
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
        # R4 preserves the slice 12 approval guard for revised specs as human_only.
        assert_artifact_blocks_merge(self, io, res, "spec-guard 产物没核对到有效的批准：spec/m.md")
        self.assertEqual(self.evidence(io)["artifacts"]["verified"], [])

    def test_codex_approval_not_counted_by_takeover_is_not_verified(self):
        io = self.io()
        io.view["modules"][0]["reviewApprovedBy"]["spec"] = None
        # The slice 12 approval guard keeps an approval without a recognized approver human_only.
        assert_artifact_blocks_merge(self, io, run(io), "spec-guard 产物没核对到有效的批准：spec/m.md")
        self.assertEqual(self.evidence(io)["artifacts"]["verified"], [])

    def test_other_modules_artifacts_and_capability_map_require_human_approval(self):
        cases = (
            ("spec/other.md", "admission", "spec-guard 产物没核对到有效的批准：spec/other.md"),
            ("spec/CAPABILITY-MAP.md", "admission", "能力图改动要用户批"),
            ("tasks/other/plan.md", "admission", "spec-guard 产物没核对到有效的批准：tasks/other/plan.md"),
        )
        for path, step, reason in cases:
            with self.subTest(path=path):
                io = self.io(files=("spec/m.md", path))
                # Direct admission now blocks a capability-map change after verification.
                assert_artifact_blocks_merge(self, io, run(io), reason, step)

    def test_no_map_view_verifies_nothing(self):
        io = self.io()
        io.view = None
        # The slice 12 approval guard leaves artifacts human_only when the map view is unavailable.
        assert_artifact_blocks_merge(self, io, run(io), "spec-guard 产物没核对到有效的批准：spec/m.md")
        self.assertEqual(self.evidence(io)["artifacts"]["verified"], [])

    def test_verify_artifacts_unit(self):
        view = {"modules": [{"id": "m", "review": {"spec": "approved", "plan": "codex-approved"},
                             "reviewApprovedBy": {"spec": "user", "plan": "codex"}}]}
        self.assertEqual(flow.verify_artifacts(view, "m", ["spec/m.md", "tasks/m/todo.md", "spec/mm.md", "src/x.py"]),
                         ["spec/m.md", "tasks/m/todo.md"])
        self.assertEqual(flow.verify_artifacts(view, "n", ["spec/n.md"]), [])


def degrade_block(number, reason="revert", pr_url="https://github.com/haigeerlab/autopilot-sandbox/pull/5"):
    payload = {"scope": {"repo": REPO, "tier": "*"}, "level": "L2", "reason": reason, "ref": "c" * 40,
               "evidence": [pr_url] if pr_url else []}
    return {"number": number, "body": "降级\n" + flow.block("autopilot-degrade", payload).replace("autopilot-degrade:v1", "autopilot-degrade:v1")}


class NarrowTest(unittest.TestCase):
    """Slice 11 (B10): an open degrade issue from a revert / red main / regression narrows the takeover once."""

    def io(self, **kw):
        io = FakeIO(module="m", acceptance=accepted(), reports=[], **kw)
        io.degrade = [degrade_block(3)]
        io.issue_comments[5] = [{"id": 1, "body": flow.block("autopilot-takeover", {"module": "m", "merge": "auto"}),
                                 "user": BOT_USER, "performed_via_github_app": {"slug": BOT["app_slug"]},
                                 "created_at": T0, "updated_at": T0}]
        io.issue_comments[3] = []
        return io

    def issue_comment_posts(self, io, n):
        return [c for c in io.calls if c[0] == "POST" and c[1] == f"repos/{REPO}/issues/{n}/comments"]

    def test_narrows_the_pr_module_then_stops_on_degrade(self):
        io = self.io()
        res = run(io)
        self.assertEqual((res["result"], res["step"]), ("stopped", "degrade"))
        self.assertEqual(io.narrow_prs[0][:2], ("m", 3))
        posted = self.issue_comment_posts(io, 3)
        self.assertIn("autopilot-narrowed:v1", posted[0][2]["body"])
        self.assertIn("pull/9", posted[0][2]["body"])

    def test_already_narrowed_issue_is_skipped(self):
        io = self.io()
        io.issue_comments[3] = [{"id": 2, "body": flow.block("autopilot-narrowed", {"pr": "x"}), "user": BOT_USER,
                                 "performed_via_github_app": {"slug": BOT["app_slug"]}, "created_at": T0, "updated_at": T0}]
        run(io)
        self.assertEqual(io.narrow_prs, [])

    def test_forged_narrowed_marker_does_not_skip(self):
        io = self.io()
        io.issue_comments[3] = [{"id": 2, "body": flow.block("autopilot-narrowed", {"pr": "x"}),
                                 "user": {"login": "x", "id": 5, "type": "User"}, "created_at": T0, "updated_at": T0}]
        run(io)
        self.assertEqual(len(io.narrow_prs), 1)

    def test_regression_without_pr_narrows_the_project(self):
        io = self.io()
        io.degrade = [degrade_block(3, reason="regression", pr_url=None)]
        run(io)
        self.assertEqual(io.narrow_prs[0][:2], (None, 3))

    def test_freeze_does_not_narrow(self):
        io = self.io()
        io.degrade = [degrade_block(3, reason="freeze", pr_url=None)]
        run(io)
        self.assertEqual(io.narrow_prs, [])

    def test_already_strictest_is_recorded_without_pr(self):
        io = self.io()
        io.narrow_result = None
        run(io)
        self.assertIn("已是最严", self.issue_comment_posts(io, 3)[0][2]["body"])

    def test_non_spec_guard_repo_only_comments(self):
        io = FakeIO(module=None)
        io.degrade = [degrade_block(3)]
        io.issue_comments[3] = []
        run(io)
        self.assertEqual(io.narrow_prs, [])
        self.assertIn("takeover.yml", self.issue_comment_posts(io, 3)[0][2]["body"])

    def test_dry_run_does_not_narrow(self):
        io = self.io()
        run(io, dry_run=True)
        self.assertEqual(io.narrow_prs, [])
        self.assertEqual(io.writes(), [])


class StatsTest(unittest.TestCase):
    def test_three_counts(self):
        class StatsIO:
            def api(self, path, method="GET", body=None):
                if "labels=autopilot:auto-merged" in path:
                    return [{"number": 2, "pull_request": {}, "closed_at": "2026-11-20T00:00:00Z", "labels": []},
                            {"number": 4, "pull_request": {}, "closed_at": "2026-11-20T00:00:00Z", "labels": []},
                            {"number": 6, "pull_request": {}, "closed_at": "2026-11-01T00:00:00Z",
                             "labels": [{"name": "autopilot:reverted"}]},  # legacy labels do not count
                            {"number": 9, "labels": []}]  # an issue, not a PR
                if path == f"repos/{REPO}/issues?labels=autopilot:degrade&state=all&per_page=100":
                    payload = {"scope": {"repo": REPO}, "reason": "revert",
                               "evidence": [f"https://github.com/{REPO}/commit/abc",
                                            f"https://github.com/{REPO}/pull/4",
                                            f"https://github.com/{REPO}/pull/6"]}
                    wrong_scope = dict(payload, scope={"repo": "other/repo"},
                                       evidence=[f"https://github.com/{REPO}/pull/6"])
                    return [{"number": 20, "state": "closed", "body": flow.block("autopilot-degrade", payload)},
                            degrade_block(21, pr_url=f"https://github.com/{REPO}/pull/4"),  # duplicate
                            degrade_block(22, reason="main-red", pr_url=f"https://github.com/{REPO}/pull/6"),
                            {"number": 23, "body": flow.block("autopilot-degrade", wrong_scope)},
                            dict(degrade_block(24, pr_url=f"https://github.com/{REPO}/pull/6"), pull_request={}),
                            {"number": 25, "body": "<!-- autopilot-degrade:v1 not-json -->"}]
                if "labels=autopilot:regression" in path:
                    return [{"number": 10, "body": "回归，见 #2", "created_at": "2026-11-21T00:00:00Z"},
                            {"number": 11, "body": "见 #6", "created_at": "2026-11-21T00:00:00Z"},  # > 14 days
                            {"number": 12, "body": "见 #4", "created_at": "2026-11-21T00:00:00Z"},  # already reverted
                            {"number": 13, "body": "见 #22", "created_at": "2026-11-21T00:00:00Z"}]
                raise AssertionError(path)

            def ledger_events(self):
                return [{"event": "codex_unavailable", "repo": REPO, "started_at": "2026-11-21T01:00:00Z",
                         "stopped_at": "2026-11-21T01:30:00Z"},
                        {"event": "codex_unavailable", "repo": "other/repo", "started_at": T0, "stopped_at": T0}]
        out = flow.stats(StatsIO(), REPO)
        self.assertEqual((out["auto_merged"], out["reverted"], out["reworked"], out["reverted_or_reworked"]), (3, 1, 1, 2))
        self.assertEqual((out["codex_unavailable_stops"], out["codex_unavailable_minutes"]), (1, 30.0))

    def test_revert_of_pr_not_auto_merged_is_not_counted(self):
        class StatsIO:
            def api(self, path, method="GET", body=None):
                if "labels=autopilot:auto-merged" in path:
                    return [{"number": 2, "pull_request": {}}]
                if path == f"repos/{REPO}/issues?labels=autopilot:degrade&state=all&per_page=100":
                    payload = {"scope": {"repo": REPO}, "reason": "revert",
                               "evidence": [f"https://github.com/{REPO}/pull/99",
                                            f"https://github.com/{REPO}/pull/2"]}
                    return [{"number": 20, "body": flow.block("autopilot-degrade", payload)}]
                if "labels=autopilot:regression" in path:
                    return []
                raise AssertionError(path)

            def ledger_events(self):
                return []

        out = flow.stats(StatsIO(), REPO)
        self.assertEqual((out["auto_merged"], out["reverted"], out["reverted_or_reworked"]), (1, 0, 0))


class TakeoverViewTest(unittest.TestCase):
    def test_effective_values(self):
        for merge in ("auto", "human"):
            for forced in (None, "important"):
                with self.subTest(merge=merge, forced=forced):
                    takeover = dict(effective(merge), designForcedBy=forced)
                    view = {"modules": [{"id": "m", "takeover": takeover}]}
                    self.assertEqual(flow.view_merge(view, "m"), (merge, None))

    def test_unavailable_or_missing_module_is_human_with_reason(self):
        for view in (None, {}, {"modules": []}):
            with self.subTest(view=view):
                merge, reason = flow.view_merge(view, "m")
                self.assertEqual(merge, "human")
                self.assertTrue(reason)
        self.assertIn("基础分支上还没有模块 m", flow.view_merge({"modules": []}, "m")[1])

    def test_invalid_or_legacy_takeover_is_human_with_reason(self):
        partial = [{k: v for k, v in effective().items() if k != drop} for drop in ("design", "release")]
        for takeover in [None, "merge", {}, {"merge": "auto"}, dict(effective(), merge="yes")] + partial:
            with self.subTest(takeover=takeover):
                view = {"modules": [{"id": "m", "takeover": takeover}]}
                merge, reason = flow.view_merge(view, "m")
                self.assertEqual(merge, "human")
                self.assertTrue(reason)
        self.assertIn("1.22", flow.view_merge({"modules": [{"id": "m", "takeover": {"merge": "auto"}}]}, "m")[1])


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

    def test_enable_automerge_sends_the_guarded_graphql_mutation(self):
        response = {"data": {"enablePullRequestAutoMerge": {"pullRequest": {"id": "PR_node"}}}}
        with mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(
                returncode=0, stdout=json.dumps(response), stderr="")) as request:
            self.assertIsNone(self.io.enable_automerge("PR_node", HEAD, "squash"))
        args, kwargs = request.call_args
        self.assertEqual(args[0], ["gh", "api", "graphql", "--method", "POST", "--input", "-"])
        payload = json.loads(kwargs["input"])
        self.assertIn("enablePullRequestAutoMerge(input:{pullRequestId:$pullRequestId,"
                      "expectedHeadOid:$expectedHeadOid,mergeMethod:$mergeMethod})", payload["query"])
        self.assertEqual(payload["variables"], dict(pullRequestId="PR_node", expectedHeadOid=HEAD, mergeMethod="SQUASH"))

    def test_enable_automerge_preserves_a_graphql_rejection_but_raises_on_transport_errors(self):
        message = "Expected head oid does not match"
        response = {"data": {"enablePullRequestAutoMerge": None}, "errors": [{"message": message}]}
        with mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(
                returncode=1, stdout=json.dumps(response), stderr=f"gh: {message}")):
            self.assertEqual(self.io.enable_automerge("PR_node", HEAD, "merge"), message)
        with mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(
                returncode=1, stdout="", stderr="gh: network failure")):
            with self.assertRaisesRegex(cli.ApiError, "network failure"):
                self.io.enable_automerge("PR_node", HEAD, "merge")

    def test_fallback_reads_all_pages_of_check_runs_and_statuses_with_scoped_tokens(self):
        runs = [{"name": "a"}, {"name": "b"}]
        statuses = [{"context": "c"}, {"context": VERIFIED}]
        responses = [
            "\n".join(json.dumps({"check_runs": [run]}) for run in runs),
            "\n".join(json.dumps({"statuses": [status]}) for status in statuses),
        ]
        with mock.patch.object(cli.tokens, "mint", return_value="checks-reader") as mint, \
                mock.patch.object(cli.tokens, "revoke") as revoke, \
                mock.patch.object(cli.subprocess, "run", side_effect=[
                    mock.Mock(returncode=0, stdout=response, stderr="") for response in responses]) as request:
            self.assertEqual(self.io.commit_check_results(REPO, HEAD), (runs, statuses))
        mint.assert_called_once_with(REPO, {"checks": "read"})
        revoke.assert_called_once_with("checks-reader")
        for call, endpoint, token in zip(request.call_args_list, ("check-runs", "status"), ("checks-reader", "t")):
            self.assertEqual(call.args[0], ["gh", "api", f"repos/{REPO}/commits/{HEAD}/{endpoint}",
                                            "--method", "GET", "--paginate"])
            self.assertEqual(call.kwargs["env"]["GH_TOKEN"], token)

    def test_combined_commit_statuses_get_creators_from_their_exact_status_ids(self):
        latest = {"id": 20, "context": VERIFIED, "state": "success"}
        foreign = {"id": 2}
        history = [dict(latest, creator=foreign), dict(latest, id=19, creator=BOT_USER)]
        path = f"repos/{REPO}/commits/{HEAD}"
        with mock.patch.object(cli.tokens, "mint", return_value="checks-reader"), \
                mock.patch.object(cli.tokens, "revoke"), \
                mock.patch.object(cli.LocalIO, "api", side_effect=[
                    {"check_runs": []}, {"statuses": [latest]}, history]) as get:
            result = self.io.commit_check_results(REPO, HEAD)
        self.assertEqual(result, ([], [dict(latest, creator=foreign)]))
        self.assertEqual(get.call_args_list, [mock.call(path + "/check-runs"), mock.call(path + "/status"),
                                             mock.call(path + "/statuses")])

    def test_compare_checks_ancestry_using_the_live_base_branch(self):
        for status, expected in (("ahead", True), ("identical", True), ("behind", False), ("diverged", False)):
            with self.subTest(status=status), mock.patch.object(self.io, "api", return_value={"status": status}) as get:
                self.assertEqual(self.io.base_is_ancestor(REPO, "release/main", HEAD), expected)
                get.assert_called_once_with(f"repos/{REPO}/compare/release%2Fmain...{HEAD}?per_page=1", paginate=False)

    def test_classic_status_checks_use_a_separate_read_only_token_when_relevant(self):
        branch_path = f"repos/{REPO}/branches/main"
        for contexts, need_source in ((["ci"], True), (["ci", VERIFIED], False), (["ci", VERIFIED], True)):
            with self.subTest(contexts=contexts, need_source=need_source):
                projection = {"protection": {"enabled": True, "required_status_checks": {"contexts": contexts}}}
                checks = {"strict": True, "checks": [{"context": VERIFIED, "app_id": APP_ID}]}
                with mock.patch.object(cli.LocalIO, "api_optional", side_effect=[projection, checks]) as get, \
                        mock.patch.object(cli.tokens, "mint", return_value="classic-reader") as mint, \
                        mock.patch.object(cli.tokens, "revoke") as revoke:
                    result = self.io.legacy_status_checks(REPO, "main", need_source=need_source, need_sync=False)
                if VERIFIED in contexts and need_source:
                    self.assertEqual(result, checks)
                    self.assertEqual(get.call_args_list, [mock.call(branch_path), mock.call(
                        branch_path + "/protection/required_status_checks")])
                    mint.assert_called_once_with(REPO, {"administration": "read"})
                    revoke.assert_called_once_with("classic-reader")
                else:
                    self.assertEqual(result, {"contexts": contexts})
                    get.assert_called_once_with(branch_path)
                    mint.assert_not_called()
                    revoke.assert_not_called()

    def test_classic_status_checks_without_administration_read_fail_closed(self):
        # The App may lack Administration: read; the run must not fail, and the source stays unconfirmed.
        projection = {"protection": {"enabled": True, "required_status_checks": {"contexts": ["ci", VERIFIED]}}}
        with mock.patch.object(cli.LocalIO, "api_optional", return_value=projection), \
                mock.patch.object(cli.tokens, "mint", side_effect=cli.tokens.TokenError("no permission")) as mint, \
                mock.patch.object(cli.tokens, "revoke") as revoke:
            with self.assertRaisesRegex(flow.ChecksUnconfirmed, "Administration: read"):
                self.io.legacy_status_checks(REPO, "main", need_source=True, need_sync=False)
        mint.assert_called_once_with(REPO, {"administration": "read"})
        revoke.assert_not_called()

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


class LocalBaseViewTest(unittest.TestCase):
    def test_map_runs_on_base_and_check_returns_to_head(self):
        import subprocess
        import tempfile
        from _checkup_util import commit_on_branch, make_repo
        from orchestrator import cli
        root = make_repo(self, {"source": "base"})
        base = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
        commit_on_branch(root, {"source": "head"})
        head = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
        work = cli.Work.__new__(cli.Work)
        work.root, work.base_tip, work.head, work.git_env = root, base, head, dict(os.environ)
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "map_view.py")
            with open(script, "w") as stream:
                stream.write("import json, pathlib\nprint(json.dumps({'source': pathlib.Path('source').read_text()}))\n")
            with mock.patch.object(cli, "find_map_view", return_value=script):
                self.assertEqual(work.base_view(), {"source": "base"})
        self.assertEqual(work.git("rev-parse", "HEAD").strip(), base)
        self.assertEqual(work.run_check("test $(cat source) = head")["exit"], 0)
        self.assertEqual(work.git("rev-parse", "HEAD").strip(), head)

    def test_map_failures_return_none(self):
        import subprocess
        from orchestrator import cli
        work = cli.Work.__new__(cli.Work)
        work.root, work.base_tip = "/fake/clone", "b" * 40
        failures = [subprocess.CompletedProcess([], 1, "{}", "failed"),
                    subprocess.CompletedProcess([], 0, "not json", ""),
                    subprocess.TimeoutExpired("map", 120), OSError("unavailable")]
        for result in failures:
            with self.subTest(result=result), mock.patch.object(work, "git") as git_call, \
                    mock.patch.object(cli, "find_map_view", return_value="/fake/map_view.py"), \
                    mock.patch.object(cli.os.path, "exists", return_value=True), \
                    mock.patch.object(cli.subprocess, "run") as process:
                if isinstance(result, Exception):
                    process.side_effect = result
                else:
                    process.return_value = result
                self.assertIsNone(work.base_view())
                git_call.assert_called_once_with("checkout", "-q", "--detach", work.base_tip)
                arguments, options = process.call_args
                self.assertEqual(arguments[0], ["python3", "-B", "/fake/map_view.py", "--json", "--project", work.root])
                self.assertEqual(options["timeout"], 120)
                self.assertEqual(options["stdin"], subprocess.DEVNULL)

    def test_missing_map_returns_none(self):
        from orchestrator import cli
        work = cli.Work.__new__(cli.Work)
        work.base_tip = "b" * 40
        for script, exists in ((None, False), ("/fake/missing.py", False)):
            with self.subTest(script=script), mock.patch.object(work, "git"), \
                    mock.patch.object(cli, "find_map_view", return_value=script), \
                    mock.patch.object(cli.os.path, "exists", return_value=exists):
                self.assertIsNone(work.base_view())


class LocalWorkPipelineTest(unittest.TestCase):
    def test_pipeline_is_read_from_base_while_commands_share_the_head_checkout(self):
        import subprocess
        from _checkup_util import commit_on_branch, make_repo
        from orchestrator import cli
        base_pipeline = "extends: plugin\nadd:\n  - id: extra\n    at: pre-merge\n    run: test -f artifact\n    by: ai\n"
        root = make_repo(self, {".github/autopr/pipeline.yml": base_pipeline, "source": "base"})
        base = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
        commit_on_branch(root, {".github/autopr/pipeline.yml": "extends: npm\n", "source": "head"})
        head = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
        work = cli.Work.__new__(cli.Work)
        work.root, work.base_tip, work.head, work.git_env = root, base, head, dict(os.environ)
        self.assertEqual(work.run_check("test $(cat source) = head && echo checked > artifact")["exit"], 0)
        self.assertEqual(work.read_pipeline(), base_pipeline)
        self.assertEqual(work.git("rev-parse", "HEAD").strip(), head)
        self.assertEqual(work.run_check("test $(cat source) = head && test $(cat artifact) = checked")["exit"], 0)

    def test_pr_cannot_add_its_own_pre_merge_pipeline(self):
        import subprocess
        from _checkup_util import commit_on_branch, make_repo
        from orchestrator import cli
        root = make_repo(self, {"source": "base"})
        base = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
        commit_on_branch(root, {".github/autopr/pipeline.yml": "extends: plugin\n"})
        work = cli.Work.__new__(cli.Work)
        work.root, work.base_tip, work.git_env = root, base, dict(os.environ)
        self.assertIsNone(work.read_pipeline())

    def test_pre_merge_uses_the_core_check_timeout_environment_and_closed_stdin(self):
        import subprocess
        from orchestrator import cli
        work = cli.Work.__new__(cli.Work)
        work.root, work.head = "/fake/clone", HEAD
        environment = {"PATH": "/bin", "HOME": "/home/test", "GH_TOKEN": "secret", "UNSAFE": "secret"}
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(work, "git") as git_call, \
                mock.patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as process:
            result = work.run_check("extra command")
        self.assertEqual(result["exit"], 0)
        git_call.assert_called_once_with("checkout", "-q", "--detach", HEAD)
        args, options = process.call_args
        self.assertEqual(args[0], ["/bin/sh", "-c", "extra command"])
        self.assertEqual(options["cwd"], work.root)
        self.assertEqual(options["env"], {"PATH": "/bin", "HOME": "/home/test"})
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["timeout"], cli.CHECK_TIMEOUT)


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
        io = EditingIO(module="m", acceptance=accepted())
        res = run(io)
        self.assertEqual((res["result"], res["step"]), ("stopped", "admission"))
        self.assertEqual(io.merge_calls(), [])


class VerificationLocalIOTest(unittest.TestCase):
    def test_run_case_cleans_checkout_and_removes_tokens(self):
        from orchestrator import cli
        import subprocess
        work = cli.Work.__new__(cli.Work)
        work.root = 'clone'
        completed = subprocess.CompletedProcess([], 0, 'ok', '')
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'secret', 'GIT_CONFIG_COUNT': '1'}), \
                mock.patch.object(cli.subprocess, 'run', return_value=completed) as process:
            self.assertEqual(work.run_case('true', HEAD), {'exit': 0, 'timeout': False, 'output': 'ok'})
        calls = process.call_args_list
        self.assertEqual(calls[0].args[0][-5:], ['checkout', '-q', '-f', '--detach', HEAD])
        self.assertEqual(calls[1].args[0][-4:], ['clone', 'clean', '-q', '-ffdx'])
        for call in calls:
            self.assertNotIn('GH_TOKEN', call.kwargs['env'])
            self.assertNotIn('GIT_CONFIG_COUNT', call.kwargs['env'])
            self.assertEqual(call.kwargs['stdin'], subprocess.DEVNULL)

    def test_timeout_is_a_result(self):
        from orchestrator import cli
        import subprocess
        work = cli.Work.__new__(cli.Work)
        work.root = 'clone'
        done = subprocess.CompletedProcess([], 0, '', '')
        with mock.patch.object(cli.subprocess, 'run', side_effect=[done, done, subprocess.TimeoutExpired('true', 1, output=b'partial')]):
            result = work.run_case('true', HEAD)
        self.assertTrue(result['timeout'])
        self.assertIsNone(result['exit'])
        self.assertIn('partial', result['output'])

    def test_plugin_commands_and_atomic_files(self):
        from orchestrator import cli
        import subprocess
        import tempfile
        from pathlib import Path
        io = cli.LocalIO('secret', None)
        done = subprocess.CompletedProcess([], 0, '{}', '')
        with mock.patch.object(cli, 'verification_plugin', return_value='plugin'), \
                mock.patch.object(cli.subprocess, 'run', return_value=done) as process:
            io.verification_check('checkout', 'm', 'report')
            self.assertIn('--json', process.call_args.args[0])
            io.verification_record('checkout', 'm', 'report')
            self.assertIn('--confirm', process.call_args.args[0])
            self.assertEqual(process.call_args.kwargs['cwd'], 'checkout')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'nested'/'report.json'
            io.write_verification_file(path, '内容')
            self.assertEqual(path.read_text(), '内容')
            self.assertFalse(Path(str(path)+'.tmp').exists())

    def test_in_scope_reads_spec_guard_scope_check_on_head(self):
        from orchestrator import cli
        import subprocess
        work = cli.Work.__new__(cli.Work)
        work.root, work.head, work.merge_base = 'clone', HEAD, 'b' * 40
        work.git = mock.Mock()
        inside = subprocess.CompletedProcess([], 0, '{"outside": 0, "files": [{"path": "src/a.py"}]}', '')
        outside = subprocess.CompletedProcess([], 0, '{"outside": 1, "files": []}', '')
        refused = subprocess.CompletedProcess([], 4, 'flow-scope-unknown', '')
        broken = subprocess.CompletedProcess([], 0, 'not json', '')
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'secret'}), \
                mock.patch.object(cli, 'find_spec_guard_root', return_value='plugin'), \
                mock.patch.object(cli.os.path, 'isfile', return_value=True), \
                mock.patch.object(cli.subprocess, 'run', side_effect=[inside, outside, refused, broken]) as process:
            self.assertEqual([work.in_scope('m') for _ in range(4)], [True, False, False, False])
        args = process.call_args_list[0].args[0]
        self.assertIn('module_scope.py', args[2])
        self.assertEqual(args[args.index('--base') + 1], 'b' * 40)
        self.assertEqual(args[args.index('--module') + 1], 'm')
        self.assertNotIn('GH_TOKEN', process.call_args_list[0].kwargs['env'])
        work.git.assert_any_call('checkout', '-q', '-f', '--detach', HEAD)
        work.git.assert_called_with('clean', '-q', '-ffdx')
        work.git.side_effect = flow.Stop('clone', 'git checkout 失败')
        with mock.patch.object(cli, 'find_spec_guard_root', return_value='plugin'), \
                mock.patch.object(cli.os.path, 'isfile', return_value=True):
            self.assertFalse(work.in_scope('m'))
        work.git.side_effect = None
        with mock.patch.object(cli, 'find_spec_guard_root', return_value=None):
            self.assertFalse(work.in_scope('m'))

    def test_worktree_rejects_wrong_default_branch(self):
        from orchestrator import cli
        import subprocess
        io = cli.LocalIO('secret', None)
        done = subprocess.CompletedProcess([], 0, '', '')
        wrong = subprocess.CompletedProcess([], 0, 'refs/remotes/origin/other\n', '')
        with mock.patch.object(io, 'checkout_of', return_value='checkout'), \
                mock.patch.object(cli.os.path, 'isdir', return_value=True), \
                mock.patch.object(io, '_batch_process', side_effect=[done, done, wrong]):
            with self.assertRaisesRegex(flow.Stop, '默认分支不是'):
                with io.verification_worktree(REPO, HEAD, 'main'):
                    self.fail('wrong default must not be used')



class VerifyArtifactsFlowTest(unittest.TestCase):
    def test_rule_comes_from_the_caller_not_the_head_view(self):
        unapproved_old = {'id': 'm', 'tier': 'normal', 'review': {'spec': 'pending', 'plan': 'pending'},
                          'reviewApprovedBy': {}, 'requirementConfirm': {'status': 'confirmed', 'by': 'user'},
                          'designApproval': None}
        view = {'modules': [unapproved_old]}
        self.assertEqual(flow.verify_artifacts(view, 'm', ['spec/m.md']), [])
        self.assertEqual(flow.verify_artifacts(view, 'm', ['spec/m.md'], flow_v2=True), ['spec/m.md'])

    def test_defang(self):
        self.assertEqual(flow.defang('a <!-- b --> c').count('<!--'), 0)
        self.assertEqual(flow.defang('a <!-- b --> c').count('-->'), 0)


if __name__ == "__main__":
    unittest.main()
