"""Unit tests for the slice-10 local orchestrator flow (docs/spec/slice-10.md). GitHub, git and delegate are fakes.

Run: python3 -B -m unittest discover -s tests -v
"""
import base64
import contextlib
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from admission import decide as d
from orchestrator import cli, flow
from orchestrator.state import WatchState

if __package__:
    from .test_verify import case
else:
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
        return self.io.regression_head if ref == HEAD else self.io.regression_base

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
        self.statuses, self.graphql_calls, self.module_reads = [], [], []
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
        self.reports = list(reports if reports is not None else [report()])
        self.stored = stored
        self.merged_ok = merged_ok
        self.merged = False
        self.pr = {"number": 7, "state": "open", "draft": False, "user": author or BOT_USER,
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
            self.graphql_calls.append(body)
            self.pr["auto_merge"] = None
            return {"data": {"disablePullRequestAutoMerge": {"pullRequest": {"id": self.pr["node_id"]}}}}
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

    def legacy_status_checks(self, repo, branch, *, need_source, need_sync):
        self.calls.append(("GET", f"repos/{repo}/branches/{branch}/protection/required_status_checks", None))
        return self.legacy_rules

    @contextlib.contextmanager
    def checkout(self, repo, pr):
        yield FakeWork(self)

    @contextlib.contextmanager
    def verification_worktree(self, repo, head, base_ref):
        yield 'checkout'

    def verification_paths(self, repo, patch_id):
        return ('evidence/patch.verification.json', 'evidence/patch.logs', 'evidence/patch.cases.json')

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

    def test_unticked_implementation_names_its_module(self):
        io = self.unticked(owners=['m'])
        result = run(io)
        self.assertEqual(result['step'], 'acceptance', result)
        self.assertIn('模块 m 的范围', result['reason'])
        self.assertIn('tasks/m/todo.md', result['reason'])
        self.assertEqual(io.verify_calls, [])
        self.assertEqual(io.merge_calls(), [])

    def test_unticked_implementation_in_several_scopes(self):
        io = self.unticked(owners=['m', 'n'])
        io.base['modules'].append(dict(id='n', tier='normal', status='in_progress', takeover=effective()))
        reason = run(io)['reason']
        self.assertIn('m、n', reason)
        self.assertEqual(io.merge_calls(), [])

    def test_outside_every_scope_explains_how_modules_are_recognised(self):
        io = self.unticked()
        reason = run(io)['reason']
        self.assertIn('没有最终验收记录', reason)
        self.assertIn('tasks/<id>/', reason)
        self.assertIn('正在做的新流程模块有 m，', reason)
        self.assertEqual(io.scope_queries, ['m'])

    def test_done_modules_are_not_scope_candidates(self):
        io = self.unticked(owners=['m'], status='done')
        reason = run(io)['reason']
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

    def test_dry_run_and_non_spec_guard(self):
        io = self.io()
        result = run(io, dry_run=True)
        self.assertEqual((result['result'], result['step']), ('dry-run', 'verification'))
        self.assertEqual(io.writes(), [])
        self.assertEqual(io.case_runs, [])
        self.assertEqual(io.verification_files, {})
        self.assertEqual(io.verify_calls, [])
        io = FakeIO(module=None, merge='auto')
        self.assertIn('一律交你合', run(io)['reason'])
        self.assertEqual(io.ran, [])


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
        self.assertEqual(self.run_pr(io)['result'], 'automerge-pending')

    def test_legacy_protection_does_not_accept_an_unpinned_context(self):
        io = self.new_flow()
        io.branch_rules = None
        io.legacy_rules = {'strict': True, 'contexts': [VERIFIED]}
        result = self.run_pr(io)
        self.assertEqual(result['route'], 'manual')
        self.assertIn('autopilot/verified 的来源未限定为 hub App', result['routeReasons'])

    def test_strict_rule_without_any_checks_does_not_prove_synchronization(self):
        io = self.new_flow()
        io.branch_rules = ruleset(required=False)
        io.legacy_rules = {'strict': False, 'checks': [{'context': VERIFIED, 'app_id': APP_ID}]}
        result = self.run_pr(io)
        self.assertEqual(result['route'], 'manual')
        self.assertIn('主干规则未要求分支与主干同步', result['routeReasons'])


class AutomergePendingTest(RunRouteFixture):
    def test_all_conditions_pass_with_only_a_placeholder(self):
        io = self.new_flow()
        result = self.run_pr(io, notify='implementation', author_host='codex')
        self.assertEqual((result['route'], result['result'], result['routeReasons']),
                         ('automerge', 'automerge-pending', []), result)
        self.assertIn('Task 4 实现', result['reason'])
        evidence = self.verify.evidence(io)
        self.assertEqual(evidence['authorHost'], 'codex')
        self.assertEqual(evidence['validatedHeadOid'], HEAD)
        self.assertEqual(evidence['validatedBaseOid'], FakeWork(io).base_tip)
        self.assertEqual(io.statuses[0][1]['state'], 'success')
        self.assertEqual(io.graphql_calls, [])
        self.assertEqual(io.merge_calls(), [])
        self.assertFalse(any(path.endswith('/labels') for _, path, _ in io.writes()))
        self.assertEqual(self.state.pending_notifications(), [])

    def test_edited_evidence_cannot_claim_admission_passed(self):
        io = self.new_flow()
        api = io.api

        def edit_evidence(path, method='GET', body=None):
            result = api(path, method, body)
            if method == 'POST' and path.endswith('/comments') and 'autopilot-evidence:v2' in body['body']:
                io.comments[-1]['updated_at'] = '2026-11-27T01:01:00Z'
            return result

        io.api = edit_evidence
        result = self.run_pr(io)
        self.assertEqual((result['result'], result['step']), ('stopped', 'admission'), result)
        self.assertEqual(io.merge_calls(), [])
        self.assertEqual(io.graphql_calls, [])


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


class VerificationFailureStatusTest(RunRouteFixture):
    def test_failure_replaces_success_and_disables_only_queued_automerge(self):
        for queued in (False, True):
            with self.subTest(queued=queued):
                io = self.new_flow()
                if queued:
                    self.assertEqual(self.run_pr(io)['result'], 'automerge-pending')
                    io.pr['auto_merge'] = {'merge_method': 'squash'}
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
                    [call] = io.graphql_calls
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
                    'stopped': 1, 'paused': 1, 'automerge-pending': 1}
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

    def test_takeover_human_or_missing(self):
        io = FakeIO(module=None, merge="human")
        self.assertStops(io, "takeover")
        self.assertIn('"merge": "human"', io.comments[0]["body"])
        self.assertStops(FakeIO(module=None, takeover=False), "takeover")
        self.assertStops(FakeIO(module=None, merge="yes"), "takeover")

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

    def test_non_spec_guard_auto_takeover_still_goes_to_human(self):
        """Q-A1: without a regression document nothing merges itself, whatever takeover.yml says."""
        io = FakeIO(module=None, merge="auto")
        self.assertStops(io, "takeover", "一律交你合")
        self.assertIn('"merge": "human"', io.comments[0]["body"])
        self.assertEqual(io.ran, [])

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
                mock.patch.object(cli.tokens, "mint", side_effect=cli.tokens.TokenError("no permission")), \
                mock.patch.object(cli.tokens, "revoke") as revoke:
            result = self.io.legacy_status_checks(REPO, "main", need_source=True, need_sync=False)
        self.assertEqual(result, {"contexts": ["ci", VERIFIED]})
        self.assertFalse(any("app_id" in c for c in result.get("checks") or []))
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
