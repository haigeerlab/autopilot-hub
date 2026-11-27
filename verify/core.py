"""Pure pre-merge verification decisions and report construction."""
import re
import secrets


def select_cases(base_view, head_view):
    base = {c['id']: c for c in (base_view or {}).get('cases', [])}
    groups = {'auto': [], 'confirm': [], 'manual': [], 'ai': []}
    for case in head_view['cases']:
        if case['method'] == 'ai-operate':
            group = 'ai'
        elif case['method'] == 'manual':
            group = 'manual'
        else:
            old = base.get(case['id'])
            group = 'auto' if old and old.get('sha256') == case['sha256'] else 'confirm'
        groups[group].append(case)
    return groups


def parse_replies(comments, owner_id, after_id):
    results, confirmed, notes = {}, set(), []
    for comment in sorted(comments, key=lambda c: c.get('id', 0)):
        if (owner_id is None or (comment.get('user') or {}).get('id') != owner_id
                or not comment.get('created_at') or comment.get('updated_at') != comment['created_at']
                or comment.get('id', 0) <= after_id):
            continue
        for line in (comment.get('body') or '').splitlines():
            line = line.strip()
            match = re.fullmatch(r'确认执行\s+(R\d+(?:[\s、,，]+R\d+)*)', line)
            if match:
                confirmed.update(re.findall(r'R\d+', match.group(1)))
                continue
            match = re.fullmatch(r'(R\d+)\s+(通过|不通过)(?:[：:]\s*(.*))?', line)
            if match:
                results[match.group(1)] = {'result': 'pass' if match.group(2) == '通过' else 'fail',
                                           'detail': match.group(3) or '', 'comment': comment}
                continue
            match = re.fullmatch(r'意见(?:\s+(R\d+))?\s*[：:]\s*(.+)', line)
            if match:
                detail = match.group(2)
                notes.append(dict(title=detail[:40], detail=detail, location=match.group(1), evidence=None))
    return dict(results=results, confirmed=confirmed, notes=notes)


def case_result(case, execution=None, evidence=(), baseline=None, baseline_evidence=(), base=None, reply=None):
    result, category, baseline_commit = 'pass', None, None
    evidence = list(evidence)
    execution_fields = {}
    if reply is not None:
        result = reply['result']
        if result == 'fail':
            category = 'user-bug'
    elif execution.get('executor'):
        execution_fields = {key: execution[key] for key in ('executor', 'exit')}
        evidence.extend(execution['evidence'])
        if execution['exit'] != 0:
            result, category = 'fail', 'run-failure'
        else:
            result = execution['result']
            if result == 'fail':
                category = 'check-failure'
    elif execution.get('timeout') or execution.get('exit') is None:
        result, category = 'fail', 'run-failure'
    elif execution['exit'] != 0:
        if baseline and not baseline.get('timeout') and baseline.get('exit') not in (None, 0):
            result, baseline_commit = 'baseline-fail', base
            evidence.extend(baseline_evidence)
        else:
            result, category = 'fail', 'check-failure'
            evidence.extend(baseline_evidence)
    return dict(id=case['id'], method=case['method'], result=result, failCategory=category,
                baselineCommit=baseline_commit, evidence=evidence, **execution_fields)


def new_executor():
    return dict(sessionId='autopilot-'+secrets.token_hex(6), host='autopilot', vendor='autopilot', sameVendorAsAuthor=False)


def build_report(module, regression_doc, base, head, patch_id, executor, cases, notes):
    by_id = {c['id']: c for c in cases}
    ordered = [by_id[c['id']] for c in regression_doc['cases']]
    verdict = 'fail' if any(c['result'] == 'fail' for c in ordered) else ('pass-with-notes' if notes else 'pass')
    return dict(schema='spec-guard.verification/v1', module=module, usage=None,
                regression={k: regression_doc[k] for k in ('document', 'sha256')}, base=base, head=head,
                patchId=patch_id, executor=executor, verdict=verdict, cases=ordered, notes=notes)
