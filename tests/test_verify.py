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
    """Reports built by verify/core.py, with evidence laid out like ~/.autopilot/evidence (outside the project,
    paths relative to the report), must stand under spec-guard 0.96.0's own `verification.py check`."""

    def setUp(self):
        # Reuse the official fixture to match the plugin's complete document grammar.
        sys.path.insert(0, str(PLUGIN/'hooks'))
        from test_verification import Repo, run, patch_id
        self.run_sg, self.repo = run, Repo()
        self.addCleanup(self.repo.close)
        self.assertEqual(run(self.repo.root, 'author', '--module', 'a', session='author-test').returncode, 0)
        done = subprocess.run([sys.executable, '-B', str(PLUGIN/'hooks/regression_view.py'), '--json', '--project',
                               str(self.repo.root), '--module', 'a'], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.doc = json.loads(done.stdout)
        self.patch = patch_id(self.repo.root, self.repo.base, self.repo.head)
        self.folder = Path(tempfile.mkdtemp(prefix='autopilot-evidence-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.folder, ignore_errors=True))
        self.report_path = self.folder/(self.patch + '.verification.json')

    def evidence(self, name, text, kind='log', source=None):
        path = self.folder/(self.patch + '.logs')/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
        return dict(type=kind, path=os.path.relpath(path, self.folder),
                    sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(), source=source)

    def check(self, head_exit, base_exit, reply_word, note=True):
        comment = dict(id=5, body='', user={'id': 1}, html_url='https://example.test/c/5', created_at='t', updated_at='t')
        comment['body'] = 'R1 通过\nR2 %s' % reply_word + ('\n意见 R2：列名可以更清楚' if note else '')
        replies = parse_replies([comment], 1, 0)
        cases = []
        for entry in self.doc['cases']:
            cid = entry['id']
            if entry['method'] in ('manual', 'ai-operate'):
                quote = self.evidence(cid + '-quote.txt', comment['body'], 'user-quote', comment['html_url'])
                cases.append(case_result(entry, evidence=[quote], reply=replies['results'][cid]))
                continue
            ev = self.evidence(cid + '.log', 'head run\n')
            base_ev = [self.evidence(cid + '-base.log', 'base run\n')] if head_exit else []
            baseline = {'exit': base_exit, 'timeout': False} if head_exit else None
            cases.append(case_result(entry, {'exit': head_exit, 'timeout': False}, [ev], baseline, base_ev, self.repo.base))
        report = build_report('a', self.doc, self.repo.base, self.repo.head, self.patch, new_executor(), cases,
                              replies['notes'])
        self.report_path.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
        checked = self.run_sg(self.repo.root, 'check', '--module', 'a', '--report', str(self.report_path), '--json')
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        out = json.loads(checked.stdout)
        self.assertEqual((out['established'], out['unverified']), (True, []), out)
        return report

    def test_pass_with_notes_and_baseline_fail_stand(self):
        report = self.check(1, 1, '通过')
        self.assertEqual(report['verdict'], 'pass-with-notes')
        self.assertIn('baseline-fail', [c['result'] for c in report['cases']])

    def test_fail_report_stands(self):
        report = self.check(1, 0, '不通过：页面打不开')
        self.assertEqual(report['verdict'], 'fail')
        self.assertEqual(sorted(c['failCategory'] for c in report['cases'] if c['result'] == 'fail'),
                         ['check-failure', 'user-bug'])

    def test_all_pass_stands(self):
        self.assertEqual(self.check(0, None, '通过')['verdict'], 'pass-with-notes')
        self.assertEqual(self.check(0, None, '通过', note=False)['verdict'], 'pass')

    def test_baseline_fail_without_notes_is_plain_pass(self):
        report = self.check(1, 1, '通过', note=False)
        self.assertEqual(report['verdict'], 'pass')
        self.assertIn('baseline-fail', [c['result'] for c in report['cases']])
