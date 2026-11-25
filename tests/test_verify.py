"""Pure verification report and reply tests."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verify.core import select_cases, parse_replies, case_result, build_report, new_executor


def case(cid='R1', method='command', sha='same'):
    return dict(id=cid, method=method, sha256=sha, title='用例', command='true', steps='操作', expected='成功')


class VerifyTest(unittest.TestCase):
    def test_selection(self):
        base = {'cases': [case(), case('R2')]}
        head = {'cases': [case(), case('R2', sha='new'), case('R3', 'manual'), case('R4', 'ai-operate')]}
        groups = select_cases(base, head)
        self.assertEqual([c['id'] for c in groups['auto']], ['R1'])
        self.assertEqual([c['id'] for c in groups['confirm']], ['R2'])
        self.assertEqual([c['id'] for c in groups['manual']], ['R3', 'R4'])
        self.assertEqual(len(select_cases(None, head)['confirm']), 2)

    def test_replies(self):
        def comment(cid, body, owner=1, edited=False):
            return dict(id=cid, body=body, user={'id': owner}, html_url='https://example.test/'+str(cid),
                        created_at='a', updated_at='b' if edited else 'a')
        comments = [comment(1, 'R1 通过'), comment(3, 'R1 通过', owner=2),
                    comment(4, 'R1 通过', edited=True), comment(5, '确认执行 R5、R6,R7\nR2 通过\nR3 不通过：说明\n意见：文字\n意见 R2：建议'),
                    comment(6, 'R2 不通过:最新')]
        replies = parse_replies(comments, 1, 2)
        self.assertNotIn('R1', replies['results'])
        self.assertEqual(replies['confirmed'], {'R5', 'R6', 'R7'})
        self.assertEqual(replies['results']['R2']['detail'], '最新')
        self.assertEqual(replies['results']['R2']['comment']['id'], 6)
        self.assertEqual(replies['results']['R3']['result'], 'fail')
        self.assertEqual([n['location'] for n in replies['notes']], [None, 'R2'])

    def test_results(self):
        evidence = dict(type='log', path='R1.log', sha256='a'*64, source=None)
        ok = {'exit': 0, 'timeout': False}
        bad = {'exit': 1, 'timeout': False}
        self.assertEqual(case_result(case(), ok, [evidence])['result'], 'pass')
        result = case_result(case(), bad, [evidence], bad, [evidence], 'base')
        self.assertEqual((result['result'], result['baselineCommit'], len(result['evidence'])), ('baseline-fail', 'base', 2))
        self.assertEqual(case_result(case(), bad, [evidence], ok)['failCategory'], 'check-failure')
        self.assertEqual(case_result(case(), {'exit': None, 'timeout': True}, [evidence])['failCategory'], 'run-failure')
        quote = dict(evidence, type='user-quote', source='https://example.test/comment')
        result = case_result(case(method='manual'), evidence=[quote], reply={'result': 'fail', 'detail': '错误'})
        self.assertEqual(result['failCategory'], 'user-bug')
        self.assertEqual(result['evidence'][0]['source'], quote['source'])

    def test_report(self):
        doc = dict(document='tasks/m/regression.md', sha256='d'*64, cases=[case()])
        passed = case_result(case(), {'exit': 0}, [])
        for results, notes, verdict in [([passed], [], 'pass'), ([passed], [{'title': '意见'}], 'pass-with-notes'),
                                        ([dict(passed, result='fail')], [], 'fail')]:
            report = build_report('m', doc, 'base', 'head', 'patch', new_executor(), results, notes)
            self.assertEqual(report['verdict'], verdict)
            self.assertEqual(set(report), {'schema', 'module', 'usage', 'regression', 'base', 'head', 'patchId', 'executor', 'verdict', 'cases', 'notes'})
        self.assertNotEqual(new_executor()['sessionId'], new_executor()['sessionId'])


PLUGIN = Path.home()/'.claude/plugins/cache/spec-guard-marketplace/spec-guard/0.96.0'


@unittest.skipUnless((PLUGIN/'hooks/verification.py').is_file(), '本机没有 spec-guard 0.96.0 插件缓存（CI 不安装插件）')
class RealContractTest(unittest.TestCase):
    def test_generated_report_stands(self):
        # Reuse the official fixture to match the plugin's complete document grammar.
        sys.path.insert(0, str(PLUGIN/'hooks'))
        from test_verification import Repo, run, patch_id
        repo = Repo()
        self.addCleanup(repo.close)
        self.assertEqual(run(repo.root, 'author', '--module', 'a', session='author-test').returncode, 0)
        done = subprocess.run([sys.executable, '-B', str(PLUGIN/'hooks/regression_view.py'), '--json', '--project', str(repo.root), '--module', 'a'], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        doc = json.loads(done.stdout)
        cases = []
        for entry in doc['cases']:
            ev = repo.evidence(entry['id']+'.txt')
            if entry['method'] == 'manual':
                ev.update(type='user-quote', source='https://example.test/comment')
            cases.append(case_result(entry, {'exit': 0}, [ev], reply={'result': 'pass'}))
        report = build_report('a', doc, repo.base, repo.head, patch_id(repo.root, repo.base, repo.head), new_executor(), cases, [])
        path = Path(repo.tmp.name+'-evidence')/'generated.json'
        path.write_text(json.dumps(report), encoding='utf-8')
        checked = run(repo.root, 'check', '--module', 'a', '--report', str(path), '--json')
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
