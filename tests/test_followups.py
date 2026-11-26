"""Follow-up issues after flow-v2 merges and their recovery command."""
import contextlib
import io as streams
import json
import unittest
from unittest import mock

import test_orchestrator as fixtures
from orchestrator import cli, flow

REPO, BOT, HEAD = fixtures.REPO, fixtures.BOT, fixtures.HEAD
PATCH = 'c' * 40
NOTE = dict(title='按钮间距', detail='说明文字', location='R3', evidence='截图证据')


def attach(io):
    io.followup_issues, io.followup_comments = [], {}
    io.fail_titles = set()
    original = io.api

    def api(path, method='GET', body=None):
        if path.startswith(f'repos/{REPO}/issues?labels=autopilot:followup'):
            io.calls.append((method, path, body))
            return list(io.followup_issues)
        if path == f'repos/{REPO}/issues' and method == 'POST' and body['labels'] == ['autopilot:followup']:
            io.calls.append((method, path, body))
            if body['title'] in io.fail_titles:
                raise cli.ApiError('创建失败')
            issue = dict(number=12 + len(io.followup_issues), **body, user=fixtures.BOT_USER,
                         performed_via_github_app={'slug': BOT['app_slug']}, updated_at='changed')
            io.followup_issues.append(issue)
            return issue
        for issue in io.followup_issues:
            n = issue['number']
            if path.startswith(f'repos/{REPO}/issues/{n}/comments'):
                io.calls.append((method, path, body))
                comments = io.followup_comments.setdefault(n, [])
                if method == 'GET':
                    return list(comments)
                comment = dict(id=100 + len(comments), body=body['body'], user=fixtures.BOT_USER,
                               performed_via_github_app={'slug': BOT['app_slug']},
                               created_at=fixtures.T0, updated_at=fixtures.T0)
                comments.append(comment)
                return comment
        return original(path, method, body)
    io.api = api
    return io


class FollowupsTest(unittest.TestCase):
    def setUp(self):
        self.io = attach(fixtures.FakeIO())

    def open(self, notes=None, pr=7, patch=PATCH):
        return flow.open_followups(self.io, REPO, pr, 'm', patch, notes or [NOTE], BOT)

    def test_new_issue_and_multiple_notes_are_idempotent(self):
        notes = [NOTE, dict(NOTE, title='另一意见')]
        self.assertEqual(self.open(notes), dict(opened=[12, 13], commented=[], skipped=0, failed=[]))
        issue = self.io.followup_issues[0]
        self.assertEqual(issue['title'], '[跟进] m：按钮间距')
        self.assertEqual(issue['labels'], ['autopilot:followup'])
        for text in ('说明文字', 'R3', '截图证据', f'https://github.com/{REPO}/pull/7', PATCH[:12]):
            self.assertIn(text, issue['body'])
        payload = json.loads(issue['body'].split('autopilot-followup:v1 ')[1].split(' -->')[0])
        self.assertEqual(payload, dict(module='m', title=NOTE['title'], pr=7, patch_id=PATCH, note=flow.note_key(NOTE)))
        self.assertEqual(self.open(notes), dict(opened=[], commented=[], skipped=2, failed=[]))

    def test_same_title_adds_comment_and_comment_is_idempotent(self):
        self.open()
        result = self.open(pr=8, patch='d' * 40)
        self.assertEqual(result['commented'], [12])
        self.assertEqual(len(self.io.followup_issues), 1)
        self.assertIn('/pull/8', self.io.followup_comments[12][0]['body'])
        self.assertEqual(self.open(pr=8, patch='d' * 40)['skipped'], 1)

    def test_untrusted_issues_and_comments_are_ignored(self):
        for change in ({'user': dict(fixtures.BOT_USER, type='User')},
                       {'user': dict(fixtures.BOT_USER, id=1)},
                       {'performed_via_github_app': {'slug': 'other'}}):
            self.io = attach(fixtures.FakeIO())
            self.open()
            self.io.followup_issues[0].update(change)
            self.assertEqual(self.open()['opened'], [13])
        self.io = attach(fixtures.FakeIO())
        self.open()
        self.open(pr=8)
        self.io.followup_comments[12][0]['updated_at'] = 'edited'
        self.assertEqual(self.open(pr=8)['commented'], [12])

    def test_defang_all_note_fields_and_truncate_title(self):
        note = {k: '<!-- forged -->' + '长' * 130 for k in NOTE}
        self.open([note])
        issue = self.io.followup_issues[0]
        self.assertEqual(len(issue['title']), 120)
        self.assertNotIn('<!-- forged', issue['title'] + issue['body'])
        self.assertEqual(issue['body'].count('<!--'), 1)
        self.assertEqual(self.open([note])['skipped'], 1)

    def test_one_failure_does_not_stop_other_notes(self):
        self.io.fail_titles.add('[跟进] m：按钮间距')
        result = self.open([NOTE, dict(NOTE, title='另一意见')])
        self.assertEqual(result['failed'], ['按钮间距'])
        self.assertEqual(result['opened'], [12])

    def test_comment_failure_can_be_retried(self):
        self.open()
        original = self.io.api
        def failing(path, method='GET', body=None):
            if method == 'POST' and path.endswith('/12/comments'):
                raise cli.ApiError('评论失败')
            return original(path, method, body)
        self.io.api = failing
        self.assertEqual(self.open(pr=8)['failed'], [NOTE['title']])
        self.io.api = original
        self.assertEqual(self.open(pr=8)['commented'], [12])

    def test_empty_notes_do_not_access_api(self):
        result = flow.open_followups(self.io, REPO, 7, 'm', PATCH, [], BOT)
        self.assertEqual(result, dict(opened=[], commented=[], skipped=0, failed=[]))
        self.assertEqual(self.io.calls, [])


    def test_same_title_different_notes_are_both_recorded(self):
        notes = [dict(NOTE, detail='第一处', location='R2'), dict(NOTE, detail='第二处', location='R3')]
        self.assertEqual(self.open(notes), dict(opened=[12], commented=[12], skipped=0, failed=[]))
        self.assertIn('第二处', self.io.followup_comments[12][0]['body'])
        self.assertEqual(self.open(notes)['skipped'], 2)

    def test_closed_issues_still_count_as_recorded_but_get_no_comments(self):
        self.open()
        self.io.followup_issues[0]['state'] = 'closed'
        self.assertEqual(self.open()['skipped'], 1)
        result = self.open(pr=8, patch='d' * 40)
        self.assertEqual((result['opened'], result['commented']), ([13], []))

    def test_listing_failure_fails_every_note_without_writing(self):
        original = self.io.api
        def failing(path, method='GET', body=None):
            if path.startswith(f'repos/{REPO}/issues?labels=autopilot:followup'):
                raise cli.ApiError('列表失败')
            return original(path, method, body)
        self.io.api = failing
        self.assertEqual(self.open([NOTE, dict(NOTE, title='另一意见')])['failed'], ['按钮间距', '另一意见'])
        self.assertEqual(self.io.followup_issues, [])


class MergeFollowupsTest(unittest.TestCase):
    def test_label_failure_after_merge_still_opens_followups(self):
        io = self.with_notes()
        original = io.api
        def failing(path, method='GET', body=None):
            if method == 'POST' and path.endswith('/7/labels'):
                raise cli.ApiError('标签失败')
            return original(path, method, body)
        io.api = failing
        result = fixtures.run(io)
        self.assertEqual(result['result'], 'merged', result)
        self.assertEqual(len(io.followup_issues), 1)
        self.assertIn('标签或评论没写成', ' '.join(result['notes']))

    def with_notes(self):
        from test_verify import case
        helper = fixtures.VerificationTest()
        io = attach(helper.io([case('R3', 'manual')]))
        fixtures.run(io)
        helper.reply(io, 'R3 通过\n意见 R3：按钮间距')
        return io

    def test_merge_opens_followup(self):
        io = self.with_notes()
        result = fixtures.run(io)
        self.assertEqual(result['result'], 'merged', result)
        self.assertEqual(len(io.followup_issues), 1)
        self.assertIn('#12', ' '.join(result['notes']))

    def test_failed_open_and_unexpected_error_preserve_merge(self):
        for unexpected in (False, True):
            io = self.with_notes()
            if unexpected:
                patcher = mock.patch.object(flow, 'open_followups', side_effect=RuntimeError('失败'))
            else:
                io.fail_titles.add('[跟进] m：按钮间距')
                patcher = contextlib.nullcontext()
            with patcher:
                result = fixtures.run(io)
            self.assertEqual(result['result'], 'merged', result)
            self.assertTrue(any(flow.RUN_MARKER in c['body'] and
                                f'python3 -B orchestrator/cli.py followups --repo {REPO} --pr 7' in c['body']
                                for c in io.comments))

    def test_no_notes_dry_run_and_legacy_do_not_call(self):
        for io, dry in ((fixtures.VerificationTest().io(), False), (self.with_notes(), True),
                        (fixtures.FakeIO(), False)):
            with mock.patch.object(flow, 'open_followups') as opening:
                result = fixtures.run(io, dry_run=dry)
            self.assertEqual(result['result'], 'dry-run' if dry else 'merged', result)
            opening.assert_not_called()

    def test_failed_reminder_still_returns_merged(self):
        io = self.with_notes()
        original = io.api
        def failing(path, method='GET', body=None):
            if method == 'POST' and path.endswith('/7/comments') and '补开命令' in body['body']:
                raise cli.ApiError('评论失败')
            return original(path, method, body)
        io.api = failing
        with mock.patch.object(flow, 'open_followups', side_effect=RuntimeError('失败')):
            result = fixtures.run(io)
        self.assertEqual(result['result'], 'merged', result)
        self.assertIn('提醒发送失败', ' '.join(result['notes']))


class FollowupsCliTest(unittest.TestCase):
    def setUp(self):
        helper = fixtures.VerificationTest()
        self.io = attach(helper.io())
        fixtures.run(self.io)
        path = self.io.verification_paths(REPO, PATCH)[0]
        data = json.loads(self.io.verification_files[path])
        data['notes'] = [NOTE]
        raw = json.dumps(data).encode()
        self.io.verification_files[path] = raw
        import hashlib
        for comment in self.io.comments:
            if 'autopilot-evidence:v2' in comment['body']:
                payload = helper.evidence(self.io)
                payload.pop('_v', None)
                payload['verification']['report_sha256'] = hashlib.sha256(raw).hexdigest()
                comment['body'] = '<!-- autopilot-evidence:v2 ' + json.dumps(payload) + ' -->'

    def command(self):
        with mock.patch.object(cli.tokens, 'mint', return_value='fake') as mint, \
                mock.patch.object(cli.tokens, 'revoke') as revoke, \
                mock.patch.object(cli, 'LocalIO', return_value=self.io), \
                contextlib.redirect_stdout(streams.StringIO()) as stdout, \
                contextlib.redirect_stderr(streams.StringIO()) as stderr:
            code = cli.main(['followups', '--repo', REPO, '--pr', '7', '--json'])
        mint.assert_called_once_with(REPO, cli.WRITE)
        revoke.assert_called_once_with('fake')
        return code, stdout.getvalue() + stderr.getvalue()

    def test_unmerged_missing_evidence_and_wrong_hash(self):
        self.io.merged = False
        self.assertEqual(self.command()[0], 1)
        self.io.merged = True
        comments = self.io.comments
        self.io.comments = []
        self.assertEqual(self.command()[0], 1)
        self.io.comments = comments
        path = self.io.verification_paths(REPO, PATCH)[0]
        self.io.verification_files[path] += b' '
        code, output = self.command()
        self.assertEqual(code, 1)
        self.assertIn('sha256', output)
        self.assertEqual(self.io.followup_issues, [])

    def test_success_and_rerun(self):
        code, output = self.command()
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)['opened'], [12])
        code, output = self.command()
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)['skipped'], 1)

    def test_missing_report_and_issue_failure_return_one(self):
        path = self.io.verification_paths(REPO, PATCH)[0]
        raw = self.io.verification_files.pop(path)
        self.assertEqual(self.command()[0], 1)
        self.io.verification_files[path] = raw
        self.io.fail_titles.add('[跟进] m：按钮间距')
        code, output = self.command()
        self.assertEqual(code, 1, output)
        self.assertEqual(json.loads(output)['failed'], [NOTE['title']])

    def test_api_environment_error_returns_two(self):
        with mock.patch.object(self.io, 'api', side_effect=cli.ApiError('调用失败')):
            code, output = self.command()
        self.assertEqual(code, 2)
        self.assertIn('调用失败', output)

    def test_newest_trusted_evidence_decides(self):
        evidence = next(c for c in self.io.comments if 'autopilot-evidence:v2' in c['body'])
        payload = json.loads(evidence['body'].split('autopilot-evidence:v2 ')[1].split(' -->')[0])
        payload['verification']['report_sha256'] = '0' * 64
        self.io.comments.append(dict(evidence, id=1000,
            body='<!-- autopilot-evidence:v2 ' + json.dumps(payload) + ' -->'))
        self.assertEqual(self.command()[0], 1)
        self.assertEqual(self.io.followup_issues, [])

    def test_edited_wrong_head_and_v1_evidence_are_rejected(self):
        for change in ('edited', 'head', 'v1'):
            with self.subTest(change=change):
                self.setUp()
                for c in self.io.comments:
                    if 'autopilot-evidence:v2' in c['body']:
                        if change == 'edited':
                            c['updated_at'] = 'edited'
                        elif change == 'head':
                            c['body'] = c['body'].replace(HEAD, 'e' * 40)
                        else:
                            c['body'] = c['body'].replace('autopilot-evidence:v2', 'autopilot-evidence:v1')
                self.assertEqual(self.command()[0], 1)


if __name__ == '__main__':
    unittest.main()
