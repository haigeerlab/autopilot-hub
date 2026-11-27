"""Check a PR, publish verification on its head, and choose a merge route as the autopilot App bot.

Direct merges still require live admission on the checked head. Flow v2 uses regression verification;
older modules reuse recorded final acceptance. Dependabot PRs only run repository checks and risk grading.
"""
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from urllib.parse import quote

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admission import cli as admission_cli
from admission import decide as d
from admission.gate import EVIDENCE_BLOCK
from checkup import yamlsub
from degrade.run import ApiError
from orchestrator.state import REPAIR_LIMIT, WatchState
from pipeline import model as pipeline_model
from risk import tiers as risk_tiers

RUN_MARKER = "<!-- autopilot-run:v1 -->"
TAIL = 40
AUTHORED = "autopilot:authored"
AUTO_MERGED = "autopilot:auto-merged"
VERIFIED = "autopilot/verified"
RUN_LOCK_WAIT = 60


class Stop(Exception):
    """A planned stop: `step` names where, `reason` says why and who has to act."""

    def __init__(self, step, reason, *, failures=None):
        super().__init__(f"{step}: {reason}")
        self.step, self.reason = step, reason
        self.failures = failures or {}


class ChecksUnconfirmed(Stop):
    """The App could not confirm the required check rules or results."""

    def __init__(self, reason):
        super().__init__("merge", reason)


def block(name, payload):
    return f"<!-- {name}:v1 " + json.dumps(payload, ensure_ascii=False, sort_keys=True) + " -->"


def rest_comment(c):
    """REST issue comment -> the shape admission reads (same fields as admission.yml collects)."""
    user = c.get("user") or {}
    return {"id": c.get("id"), "body": c.get("body") or "",
            "user": {"login": user.get("login"), "id": user.get("id"), "type": user.get("type")},
            "app": (c.get("performed_via_github_app") or {}).get("slug"),
            "created_at": c.get("created_at"), "updated_at": c.get("updated_at")}


def base_file(io, repo, path, ref):
    """Text of `path` on `ref`, or None when it does not exist."""
    data = io.api_optional(f"repos/{repo}/contents/{path}?ref={ref}")
    if not isinstance(data, dict) or "content" not in data:
        return None
    return base64.b64decode(data["content"]).decode("utf-8")


SG_CAPABILITY_MAP = "spec/CAPABILITY-MAP.md"


APPROVED = ("approved", "codex-approved")


def sg_artifact(path):
    return (path.startswith("spec/") or path.startswith("tasks/")) and path != risk_tiers.CAPABILITY_MAP


def verify_artifacts(view, module, changed, flow_v2=False):
    """Verify changed spec/<module>.md and tasks/<module>/** using the head's spec-guard view.
    Old flow: both Spec and Plan have approved/codex-approved status and a reviewApprovedBy value.
    New flow: the requirement is confirmed by the user, and designApproval is explicitly null (bug and small
    modules have no design), not-required, or approved by user/reviewer. Which rule applies is decided by the
    caller from the BASE branch (`flow_v2`), never by the head view, so a PR cannot pick its own rule.
    Other modules and the capability map are never verified.
    """
    mod = next((m for m in (view or {}).get("modules") or [] if m.get("id") == module), None)
    if not mod:
        return []
    if flow_v2:
        if "designApproval" not in mod:
            return []
        requirement = mod.get("requirementConfirm")
        if not isinstance(requirement, dict) or requirement.get("status") != "confirmed" or requirement.get("by") != "user":
            return []
        design = mod.get("designApproval")
        if design is not None and not (isinstance(design, dict) and (
                design.get("status") == "not-required" or (design.get("status") == "approved"
                and design.get("approvedBy") in ("user", "reviewer")))):
            return []
    else:
        review, by = mod.get("review") or {}, mod.get("reviewApprovedBy") or {}
        if any(review.get(k) not in APPROVED or not by.get(k) for k in ("spec", "plan")):
            return []
    own = [p for p in changed if p == f"spec/{module}.md" or p.startswith(f"tasks/{module}/")]
    return sorted(own)


NARROW_REASONS = {"revert": "main 上撤回了 autopilot 自动合并的 PR", "main-red": "autopilot 合并后 main 的主 CI 变红",
                  "regression": "有 issue 被标为回归"}
NARROWED = re.compile(r"<!--\s*autopilot-narrowed:v1\s")
PR_URL = re.compile(r"/pull/(\d+)$")


def degrade_payload(issue):
    for raw in d.DEGRADE_BLOCK.findall(issue.get("body") or ""):
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def pr_module(io, repo, pr_url, bot):
    """spec-guard module named in the latest trusted takeover comment of the PR behind a degrade issue."""
    m = PR_URL.search(pr_url or "")
    if not m:
        return None
    comments = [rest_comment(c) for c in io.api(f"repos/{repo}/issues/{m.group(1)}/comments?per_page=100") or []]
    blocks = [b for c in comments if d.trusted(c, bot) for b in d.TAKEOVER_BLOCK.findall(c["body"])]
    try:
        module = json.loads(blocks[-1]).get("module") if blocks else None
    except (ValueError, AttributeError):
        return None
    return module if isinstance(module, str) and module else None


def narrow_pending(io, repo, base_ref, issues, sg_project, bot, notes, dry_run):
    """B10, the persistent layer: each open degrade issue from a revert, red main or regression narrows the
    takeover one step, once. spec-guard projects get a bot PR running spec-guard's `narrow` (T4: the user
    merges it); other repos get a comment pointing at takeover.yml. The open issue already stops auto-merge."""
    for issue in issues:
        payload = degrade_payload(issue)
        if not payload or payload.get("reason") not in NARROW_REASONS or (payload.get("scope") or {}).get("repo") != repo:
            continue
        number = issue.get("number")
        comments = [rest_comment(c) for c in io.api(f"repos/{repo}/issues/{number}/comments?per_page=100") or []]
        if any(d.trusted(c, bot) and NARROWED.search(c["body"]) for c in comments):
            continue
        if dry_run:
            notes.append(f"降级 issue #{number} 还没收窄（预演不动）")
            continue
        why = NARROW_REASONS[payload["reason"]]
        if not sg_project:
            body = (f"本仓库不是 spec-guard 项目，接管档位在 `.github/autopr/takeover.yml`。这个降级 issue 已经让本仓库不会自动合并；"
                    f"如要长期收窄，请把 `merge` 改为 `human`（T4，由你提交）。\n\n" + block("autopilot-narrowed", {"pr": None}))
            io.api(f"repos/{repo}/issues/{number}/comments", "POST", {"body": body})
            continue
        evidence = payload.get("evidence") or []
        module = pr_module(io, repo, evidence[0] if evidence else None, bot)
        reason = f"降级 #{number}：{why}"[:200]
        url = io.open_narrow_pr(repo, base_ref, module, number, reason)
        target = f"模块 {module}" if module else "整个项目"
        if url:
            body = f"已为{target} 开收窄 PR：{url}（按 spec-guard 规则降一格：合并 → 技术方案 → 不接管）。它是 T4，合并后生效；在那之前本 issue 已阻止自动合并。"
        else:
            body = f"{target}的接管档位已是最严，不需要收窄。"
        io.api(f"repos/{repo}/issues/{number}/comments", "POST",
               {"body": body + "\n\n" + block("autopilot-narrowed", {"pr": url, "module": module})})
        notes.append(f"降级 #{number}：{body}")


def say_takeover(say, module, merge, source, flow=None):
    """`flow` "v2" tells the hard gate this module is verified against its regression document and needs v2
    evidence; without it the gate only accepts v1 evidence."""
    payload = {"module": module, "merge": merge, "source": source}
    if flow:
        payload["flow"] = flow
    say(f"接管档位（来源 {source}" + (f"，模块 {module}" if module else "") + f"）：合并 {merge}\n\n"
        + block("autopilot-takeover", payload) + "\n")


def defang(text):
    """Neutralise HTML comment markers in text copied into bot comments (check output, user note titles), so it
    can neither forge nor break a machine block such as autopilot-takeover or autopilot-evidence."""
    return str(text).replace("<!--", "<!\u2060--").replace("-->", "--\u2060>")


def read_sg_config(io, repo, base_ref, notes):
    """Read raw base configuration for the release engine's existing import contract."""
    path = ".agent/config.json"
    text = base_file(io, repo, path, base_ref)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        notes.append(f"{path} 读不懂，发布按人批")
        return {}
    return data if isinstance(data, dict) else {}


def view_merge(view, module):
    """Read effective merge from the base map view; require the spec-guard 1.22 contract."""
    if not isinstance(view, dict):
        return "human", "基础分支的 /map --json 读不到生效档位，按人合"
    modules = view.get("modules")
    mod = next((m for m in modules if isinstance(m, dict) and m.get("id") == module), None) \
        if isinstance(modules, list) else None
    if mod is None:
        return "human", f"基础分支上还没有模块 {module}，按人合"
    takeover = mod.get("takeover")
    if not isinstance(takeover, dict):
        return "human", f"模块 {module} 的生效接管档位读不懂，按人合"
    if "designForcedBy" not in takeover:
        return "human", "本机 spec-guard 的 map_view 早于接口 1.22，没有生效档位，按人合"
    if not {"design", "merge", "release"} <= set(takeover):
        return "human", f"模块 {module} 的生效接管档位缺 design／merge／release 中的项，按人合"
    if takeover.get("merge") not in ("auto", "human"):
        return "human", f"模块 {module} 的生效 merge 只认 auto／human，按人合"
    return takeover["merge"], None


def read_takeover(io, repo, base_ref, notes):
    """Takeover setting for a repo without spec-guard: `.github/autopr/takeover.yml` on the base branch.
    Absent or unreadable -> merge by a human."""
    text = base_file(io, repo, ".github/autopr/takeover.yml", base_ref)
    out = {"module": None, "merge": "human", "source": ".github/autopr/takeover.yml"}
    if text is None:
        out["source"] = "（没有 .github/autopr/takeover.yml）"
        return out
    from checkup import yamlsub
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "takeover.yml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            data = yamlsub.load(path)
        except (ValueError, TypeError, KeyError, IndexError) as e:
            notes.append(f"takeover.yml 读不懂（{e}），合并按人合")
            return out
    merge = data.get("merge") if isinstance(data, dict) else None
    if merge in ("auto", "human"):
        out["merge"] = merge
    else:
        notes.append(f"takeover.yml 的 merge 是 {merge!r}，只认 auto／human，按人合")
    return out


def active_degrade_issues(io, repo):
    return [{"number": i.get("number"), "body": i.get("body")} for i in
            io.api(f"repos/{repo}/issues?labels=autopilot:degrade&state=open&per_page=100") or []
            if "pull_request" not in i]


def classify_pr_risk(io, repo, pr, verified_artifacts):
    number, base_ref = pr["number"], pr["base"]["ref"]
    files = [{k: f.get(k) for k in ("filename", "status", "additions", "deletions", "patch")}
             for f in io.api(f"repos/{repo}/pulls/{number}/files?per_page=100") or []]
    notes = []
    with tempfile.TemporaryDirectory() as tmp:
        paths = {}
        for ext in ("yml", "json"):
            text = base_file(io, repo, f".github/autopr/risk-tiers.{ext}", base_ref)
            if text is not None:
                paths[ext] = os.path.join(tmp, f"risk-tiers.{ext}")
                with open(paths[ext], "w", encoding="utf-8") as fh:
                    fh.write(text)
        config = admission_cli.load_risk_config(paths.get("yml"), paths.get("json"), notes)
    risk = risk_tiers.classify(files, risk_tiers.load_templates(), config, verified_artifacts)
    risk["notes"] = notes + risk["notes"]
    return risk


def evaluate(io, repo, pr, bot, optimistic_artifacts=None, *, prospective_evidence=None):
    """Admission decision with fresh inputs, exactly as the reusable workflow computes it."""
    number, head = pr["number"], pr["head"]["sha"]
    comments = [rest_comment(c) for c in io.api(f"repos/{repo}/issues/{number}/comments?per_page=100") or []]
    if prospective_evidence is not None:
        # Preview admission using the evidence this App is about to publish. Direct merges use live comments.
        comments.append({"body": prospective_evidence, "user": {"type": "Bot", "id": bot["id"]},
                         "app": bot["app_slug"], "created_at": "pending", "updated_at": "pending"})
    verified = d.verified_artifacts(comments, bot, head) if optimistic_artifacts is None else optimistic_artifacts
    risk = classify_pr_risk(io, repo, pr, verified)
    result = d.decide(risk=risk, comments=comments, degrade_issues=active_degrade_issues(io, repo),
                      repo=repo, bot=bot, head_sha=head)
    result["risk"] = risk
    return result


def blocking_findings(report):
    """Blocking or unknown-severity findings and unresolved findings from the previous review."""
    findings = [f or {} for f in report.get("findings") or [] if (f or {}).get("severity") != "suggestion"]
    findings.extend(item.get("finding") or item for item in ((report.get("recheck") or {}).get("items") or [])
                    if (item or {}).get("status") == "unresolved")
    return findings


def blocking_of(report):
    return len(blocking_findings(report))


def review_failures(findings):
    failures = {}
    for finding in findings:
        title = " ".join(str(finding.get("title") or finding.get("trigger") or "未命名审查意见").split())
        path = finding.get("file")
        identifier = finding.get("id")
        if identifier is None or identifier == "":
            # A line number or an index in a report can change as the patch is repaired.
            summary = json.dumps([path, title], ensure_ascii=False)
            identifier = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:16]
        failures[f"review:{identifier}"] = f"{path}：{title}" if path else title
    return failures


def conclusive(report):
    return isinstance(report, dict) and report.get("execution_status") == "completed" \
        and report.get("review_outcome") in ("findings", "no_findings_in_scope")


ACCEPTANCE_MARKERS = ("codex-approved", "approved")


def check_evidence(repo, head, command, check, ran_at):
    """`autopilot.check-evidence/v1`: the local file spec-guard reads before recording final acceptance."""
    return {"schema": "autopilot.check-evidence/v1", "repo": repo, "head_sha": head,
            "runner": "autopilot-orchestrator", "non_author": True, "clone": "throwaway", "ran_at": ran_at,
            "checks": [{"cmd": command, "exit": check["exit"], "output_sha256": check["output_sha256"],
                        "tail": check["tail"]}]}


def accepted_review(io, repo, module, head):
    """Final acceptance of a spec-guard module, recorded outside the commit
    (shared `git-common-dir/spec-guard/acceptance/<module>.json`, then the legacy per-worktree path).
    Returns (acceptance, report). Raises Stop."""
    found = io.acceptance(repo, module)
    if not found:
        raise Stop("acceptance", f"模块 {module} 还没有最终验收：本机检查证据已写好，等 spec-guard 让 Codex 对照 Spec 验收后再运行")
    rec = found.get("record") or {}
    if rec.get("module") != module or rec.get("marker") not in ACCEPTANCE_MARKERS:
        raise Stop("acceptance", f"模块 {module} 的验收记录不对（模块或标记不符）")
    if rec.get("sha") != head:
        raise Stop("acceptance", f"模块 {module} 的验收绑定的是 {str(rec.get('sha'))[:12]}，不是当前提交 {head[:12]}，要重新验收")
    text = found.get("report_text")
    if text is None or hashlib.sha256(text.encode("utf-8")).hexdigest() != rec.get("report_sha256"):
        raise Stop("acceptance", "验收审查报告读不到，或和记录里的 sha256 对不上")
    try:
        report = json.loads(text)
    except ValueError:
        raise Stop("acceptance", "验收审查报告不是 JSON") from None
    if not conclusive(report):
        raise Stop("acceptance", "验收审查报告没有结论")
    if ((report.get("scope") or {}).get("baseline") or {}).get("to") != head:
        raise Stop("acceptance", "验收审查报告审的不是当前提交")
    return {"marker": rec["marker"], "sha": head}, report


def gate_review(report):
    """Same bar for our own review and a reused acceptance review: all files seen, a fingerprint, no blocking."""
    if report.get("unreviewed"):
        raise Stop("review", "有文件没审到：" + "、".join(str((u or {}).get("path")) for u in report["unreviewed"]))
    fp = ((report.get("scope") or {}).get("fingerprint") or {}).get("sha256")
    if not fp:
        raise Stop("review", "审查报告没有补丁指纹，证据无法绑定提交")
    return fp


def evidence_body(head, command, check, report, rounds, fp, acceptance=None, artifacts=None, extra_checks=(),
                  queue_check=None, *, repair_baseline):
    checks = [dict(check, cmd=command)] + list(extra_checks)
    payload = {"head_sha": head, "runner": "autopilot-orchestrator",
               "checks": [{k: c[k] for k in ("cmd", "exit", "output_sha256")} for c in checks],
               "review": {"backend": report.get("backend"), "status": report.get("execution_status"),
                          "outcome": report.get("review_outcome"), "fingerprint": fp,
                          "blocking": blocking_of(report), "rounds": rounds},
               "acceptance": acceptance}
    if repair_baseline is not None:
        payload["repairBaseline"] = repair_baseline
    if artifacts is not None:
        payload["artifacts"] = artifacts
    check_lines = "\n".join(f"- 检查：`{c['cmd']}`，退出码 {c['exit']}（在一次性克隆里由本机编排重新运行）" for c in checks)
    queue_line = ""
    if queue_check:
        queue_line = (f"module-crew 合并队列检查：{queue_check['command']}，{queue_check['result']}，"
                      f"耗时 {queue_check['seconds']} 秒（额外参考，不替代本机重跑）\n\n")
    tail = defang("\n\n".join(c["cmd"] + "\n" + "\n".join(c["tail"]) for c in checks))
    return (f"**autopilot 硬卡证据**（提交 `{head[:12]}`）\n\n"
            f"{check_lines}\n"
            f"- 审查：delegate／{report.get('backend')}，第 {rounds} 轮，{report.get('review_outcome')}，"
            f"blocking 0，补丁指纹 `{fp[:16]}`\n\n"
            + queue_line + f"<details><summary>检查输出最后 {TAIL} 行</summary>\n\n```\n{tail}\n```\n</details>\n\n"
            + block("autopilot-evidence", payload) + "\n")


def evidence_v2_body(head, base, module, command, check, verification, artifacts, extra_checks=(), queue_check=None,
                     *, repair_baseline):
    """Render flow v2 evidence from the established verification and the checks just executed."""
    checks = [dict(check, cmd=command)] + list(extra_checks)
    payload = {"head_sha": head, "base_sha": base, "patch_id": verification["patch_id"],
               "module": module, "runner": "autopilot-orchestrator",
               "checks": [{k: c[k] for k in ("cmd", "exit", "output_sha256")} for c in checks],
               "verification": {k: verification[k] for k in ("verdict", "report_sha256", "regression_sha256")},
               "artifacts": artifacts}
    if repair_baseline is not None:
        payload["repairBaseline"] = repair_baseline
    check_lines = "\n".join(f"- 检查：`{c['cmd']}`，退出码 {c['exit']}（在一次性克隆里由本机编排重新运行）" for c in checks)
    notes = "\n".join("- 意见：" + defang(title) for title in verification["notes"])
    queue_line = ""
    if queue_check:
        queue_line = (f"module-crew 合并队列检查：{queue_check['command']}，{queue_check['result']}，"
                      f"耗时 {queue_check['seconds']} 秒（额外参考，不替代本机重跑）\n\n")
    tail = defang("\n\n".join(c["cmd"] + "\n" + "\n".join(c["tail"]) for c in checks))
    return (f"**autopilot 硬卡证据**（提交 `{head[:12]}`，补丁指纹 `{verification['patch_id'][:12]}`）\n\n"
            f"{check_lines}\n- 合并前验证：{verification['verdict']}\n{notes}\n\n"
            + queue_line + f"<details><summary>检查输出最后 {TAIL} 行</summary>\n\n```\n{tail}\n```\n</details>\n\n"
            + "<!-- autopilot-evidence:v2 " + json.dumps(payload, ensure_ascii=False, sort_keys=True) + " -->\n")


VERIFY_BLOCK = re.compile(r'<!--\s*autopilot-verify:v1\s+(.*?)\s*-->', re.S)
FOLLOWUP_BLOCK = re.compile(r'<!--\s*autopilot-followup:v1\s+(.*?)\s*-->', re.S)


def bot_issue(issue, bot):
    """Identify an App-authored issue independently of its moving updated_at.

    GitHub leaves performed_via_github_app empty on App-created issues (same rule as pipeline/engine.py and
    audit/run.py); when it is present it must still name our App."""
    user = issue.get('user') or {}
    app = issue.get('performed_via_github_app')
    return bool(bot and user.get('type') == 'Bot' and user.get('id') == bot['id']
                and (app is None or (isinstance(app, dict) and app.get('slug') == bot['app_slug'])))


def note_key(note):
    """Short fingerprint telling apart notes that share a title (title, detail and location)."""
    raw = json.dumps([note.get('title'), note.get('detail'), note.get('location')], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]


def open_followups(io, repo, pr_number, module, patch_id, notes, bot):
    """Record each note once per PR, patch and note; group open issues by module and title.

    Recorded notes are looked up in open AND closed bot issues, so a re-run never opens a duplicate; new notes are
    appended only to an open issue of the same module and title, otherwise a new issue is opened."""
    result = dict(opened=[], commented=[], skipped=0, failed=[])
    if not notes:
        return result

    def payloads(body):
        found = []
        for raw in FOLLOWUP_BLOCK.findall(body or ''):
            try:
                value = json.loads(raw)
            except ValueError:
                continue
            if isinstance(value, dict):
                found.append(value)
        return found

    try:
        issues = [i for i in io.api(f'repos/{repo}/issues?labels=autopilot:followup&state=all&per_page=100') or []
                  if 'pull_request' not in i and bot_issue(i, bot)]
        seen = []  # (issue, is_open, body blocks, all trusted blocks)
        for issue in issues:
            body_blocks = payloads(issue.get('body'))
            blocks = list(body_blocks)
            for comment in io.api(f"repos/{repo}/issues/{issue['number']}/comments?per_page=100") or []:
                if d.trusted(rest_comment(comment), bot):
                    blocks.extend(payloads(comment.get('body')))
            seen.append((issue['number'], issue.get('state') != 'closed', body_blocks, blocks))
    except Exception:
        # Without the existing issues nothing can be de-duplicated safely: record every note as failed.
        result['failed'] = [n.get('title') for n in notes]
        return result

    for note in notes:
        try:
            payload = dict(module=defang(module), title=defang(note['title']), pr=pr_number, patch_id=patch_id,
                           note=note_key(note))
            if any(all(p.get(k) == payload[k] for k in payload) for _, _, _, blocks in seen for p in blocks):
                result['skipped'] += 1
                continue
            target = next((number for number, is_open, body_blocks, _ in seen if is_open and any(
                p.get('module') == payload['module'] and p.get('title') == payload['title'] for p in body_blocks)), None)
            lines = [f"说明：{defang(note.get('detail') or note['title'])}",
                     f"位置：{defang(note.get('location') or '未提供')}"]
            if note.get('evidence'):
                lines.append(f"证据：{defang(note['evidence'])}")
            lines.extend([f'来源 PR：https://github.com/{repo}/pull/{pr_number}',
                          f'补丁指纹：`{patch_id[:12]}`', '', block('autopilot-followup', payload)])
            body = '\n\n'.join(lines)
            if target is not None:
                io.api(f'repos/{repo}/issues/{target}/comments', 'POST', {'body': body})
                result['commented'].append(target)
                entry = next(e for e in seen if e[0] == target)
                entry[3].append(payload)
            else:
                issue = io.api(f'repos/{repo}/issues', 'POST', {
                    'title': f"[跟进] {defang(module)}：{payload['title']}"[:120],
                    'labels': ['autopilot:followup'], 'body': body})
                result['opened'].append(issue['number'])
                seen.append((issue['number'], True, [payload], [payload]))
        except Exception:
            # A failed note must not prevent recording the remaining notes.
            result['failed'].append(note.get('title'))
    return result


def verification_listing(module, groups, payload):
    lines = [f"模块 {module} 的合并前验证需要你回复。", ""]
    if groups['confirm']:
        lines.append("要确认执行的命令用例：")
    for case in groups['confirm']:
        lines.extend([f"{case['id']}：{case['title']}", "```", case['command'], "```"])
    if groups['manual']:
        lines.append("要你测的人工用例（含 ai-operate）：")
    for case in groups['manual']:
        lines.extend([f"{case['id']}：{case['title']}", f"步骤：{case['steps']}", f"预期：{case['expected']}"])
        reason = payload.get('aiUnavailable', {}).get(case['id'])
        if reason:
            lines.append(f"AI 执行不可用：{reason}")
    lines.extend(["", "请逐行回复，例如：确认执行 R5 R6；R2 通过；R3 不通过：说明；意见 R2：文字。",
                  "只认仓库所有者在最新清单之后、没有编辑过的评论。", block('autopilot-verify', payload)])
    return '\n'.join(lines)


def verification_issue(io, repo, pr, module, groups, patch_id, document_sha, ai_unavailable):
    bot = io.bot_identity()
    payload = dict(pr=pr['number'], patchId=patch_id, regressionSha256=document_sha,
                   confirm=[c['id'] for c in groups['confirm']], manual=[c['id'] for c in groups['manual']])
    if ai_unavailable:
        payload['aiUnavailable'] = ai_unavailable
    def listings(source, issue_body=False):
        # REST updated_at of an issue moves on every comment, so the issue body is identified by its bot
        # author and app only; listing comments must also be unedited.
        if issue_body:
            if not bot_issue(source, bot):
                return []
        elif not d.trusted(rest_comment(source), bot):
            return []
        found = []
        for raw in VERIFY_BLOCK.findall(source.get('body') or ''):
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get('pr') == pr['number']:
                found.append(item)
        return found
    issue = next((i for i in io.api(f'repos/{repo}/issues?labels=autopilot:verify&state=open&per_page=100') or []
                  if 'pull_request' not in i and listings(i, True)), None)
    body = verification_listing(module, groups, payload)
    if issue is None:
        issue = io.api(f'repos/{repo}/issues', 'POST', {
            'title': f"autopilot：PR #{pr['number']} 合并前验证等你确认", 'labels': ['autopilot:verify'], 'body': body})
        return issue, [], 0
    comments = io.api(f"repos/{repo}/issues/{issue['number']}/comments?per_page=100") or []
    latest, after_id = listings(issue, True)[-1], 0
    for comment in sorted(comments, key=lambda c: c.get('id', 0)):
        found = listings(comment)
        if found:
            latest, after_id = found[-1], comment['id']
    def same_cases(group):
        previous = latest.get(group)
        return (isinstance(previous, list) and all(isinstance(cid, str) for cid in previous)
                and set(previous) == set(payload[group]))
    if (latest.get('patchId') != patch_id or latest.get('regressionSha256') != document_sha
            or not same_cases('confirm') or not same_cases('manual')
            or latest.get('aiUnavailable', {}) != ai_unavailable):
        added = io.api(f"repos/{repo}/issues/{issue['number']}/comments", 'POST', {'body': body})
        after_id = added['id']
    return issue, comments, after_id


def verification_state(view, module):
    return next((m.get('verification') or {} for m in (view or {}).get('modules', [])
                 if m.get('id') == module), {})


def case_failures(results):
    return {f"case:{case['id']}": f"{case['id']}（{case['failCategory']}）"
            for case in results if case['result'] == 'fail'}


def run_ai_case(work, case, head, author_host):
    if author_host == 'codex':
        return None, '非作者后端 claude-code 不支持 delegate run'
    if author_host != 'claude-code':
        return None, '未传 --author-host，无法选择非作者后端'
    backend = 'codex'
    cid = case['id']
    task = (f"用例 {cid}：{case['title']}\n步骤：{case['steps']}\n预期：{case['expected']}\n"
            f"证据要求：{case['evidence']}\n"
            f'执行该用例，并把最后一行写成独立 JSON，不加代码围栏：'
            f'{{"case":"{cid}","result":"pass 或 fail","evidence":"实际观察和证据"}}。'
            'result 必须是 pass 或 fail，evidence 写下克隆删除后仍可核对的证据。')
    output, reason = work.run_delegate(backend, head, task)
    answer, separator, footer = (output or '').rpartition('\n---\n模型：')
    if not separator or '\n过程日志（未进入本会话上下文）：' not in footer:
        answer, footer = output or '', ''
    if (answer.lstrip().startswith('NEED_USER:')
            or re.search(r'^delegate: run_id=\S+ status=needs-input(?: |$)', footer, re.M)):
        return None, 'delegate 要人回答，交你执行用例'
    if reason:
        return None, reason
    authorization = re.search(r'^写入授权：文件 ([^\n]+)（sha256 ([0-9a-f]{12})）$', footer, re.M)
    if authorization is None:
        return None, 'delegate 没有记录授权来源'
    try:
        data = json.loads(answer.strip().rpartition('\n')[2])
    except ValueError:
        return None, 'delegate 结尾 JSON 无法解析'
    if not isinstance(data, dict) or data.get('result') not in ('pass', 'fail') or not data.get('evidence'):
        return None, 'delegate 结尾 JSON 缺少有效结果或证据'
    if data.get('case') != cid:
        return None, 'delegate 返回的用例编号不匹配'
    source = dict(path=authorization[1], sha256Prefix=authorization[2], line=authorization[0])
    return dict(executor=f'delegate:{backend}', result=data['result'], evidence=data['evidence'], exit=0,
                authorization=source), None


def verify_module(io, repo, pr, work, module, notes, dry_run, *, author_host=None):
    from verify.core import select_cases, parse_replies, case_result, build_report, new_executor
    head = pr['head']['sha']
    base_code, base_doc = work.regression(module, ref=work.base_tip)
    code, doc = work.regression(module, ref=head)
    def problems(data):
        data = data or {}
        return '、'.join(str(p.get('code')) for p in data.get('problems', [])) or str(data.get('error') or '输出读不懂')
    if code != 0 or not isinstance(doc, dict) or doc.get('problems'):
        raise Stop('verification', 'head 回归文档读不了或有问题：' + problems(doc))
    if base_code == 3 and (base_doc or {}).get('error') in ('not-at-ref', 'no-document'):
        base_doc = None
    elif base_code != 0 or not isinstance(base_doc, dict):
        raise Stop('verification', '基础提交回归文档读不了：' + problems(base_doc))
    groups = select_cases(base_doc, doc)
    if dry_run:
        notes.append('合并前验证分组：' + json.dumps({k: [c['id'] for c in v] for k, v in groups.items()}, ensure_ascii=False))
        return dict(result='dry-run', step='verification', reason='预演：已读取回归文档并分组', notes=notes, head_sha=head)
    ai_approved = not groups['ai'] or bool(verify_artifacts(work.module_view(), module, [doc['document']], flow_v2=True))
    patch_id = work.patch_id()
    report_path, logs, cache_path = io.verification_paths(repo, patch_id)
    def read_json(path):
        try:
            return json.loads(io.read_bytes(path))
        except (OSError, ValueError):
            return None
    def write_json(path, value):
        io.write_verification_file(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    def evidence(path, text, kind='log', source=None):
        raw = text.encode('utf-8', errors='backslashreplace')
        io.write_verification_file(path, raw)
        return dict(type=kind, path=os.path.relpath(path, os.path.dirname(report_path)),
                    sha256=hashlib.sha256(raw).hexdigest(), source=source)
    report = read_json(report_path)
    reused = False
    if (ai_approved and isinstance(report, dict) and report.get('regression', {}).get('sha256') == doc['sha256']
            and not any(c.get('method') == 'ai-operate' and not (c.get('executor') and c.get('authorization'))
                        for c in report.get('cases', []))):
        with io.verification_worktree(repo, head, pr['base']['ref']) as root:
            state = verification_state(io.verification_view(root), module)
        digest = hashlib.sha256(io.read_bytes(report_path)).hexdigest()
        # Rebuild failed reports so flaky command cases can be re-executed.
        reused = (state.get('current') is True and state.get('reportSha256') == digest
                  and report.get('verdict') != 'fail')
    if not reused:
        ai_results, ai_unavailable = {}, {}
        for case in groups['ai']:
            cid = case['id']
            if not ai_approved:
                groups['manual'].append(case)
                ai_unavailable[cid] = '未确认模块已批准，不能委托执行回归用例'
                continue
            key = hashlib.sha256(json.dumps([patch_id, cid, case['sha256']]).encode('utf-8')).hexdigest()
            outcome_path = f'{report_path}.{key}.ai.json'
            with io.verification_case_lock(outcome_path):
                outcome = read_json(outcome_path)
                result = outcome.get('result') if isinstance(outcome, dict) else None
                reason = None
                if not isinstance(result, dict) or result.get('result') != 'pass' or not result.get('authorization'):
                    execution, reason = run_ai_case(work, case, head, author_host)
                    result = None
                    if execution is not None:
                        text = execution['evidence']
                        if not isinstance(text, str):
                            text = json.dumps(text, ensure_ascii=False, indent=2)
                        execution['evidence'] = [evidence(os.path.join(logs, f'{cid}-delegate-{key}.log'), text)]
                        result = case_result(case, execution)
                        result['authorization'] = execution['authorization']
                        if result['result'] == 'pass':
                            write_json(outcome_path, dict(result=result, reason=None))
            if reason is not None:
                groups['manual'].append(case)
                ai_unavailable[cid] = reason
            else:
                ai_results[cid] = result
        replies = dict(results={}, confirmed=set(), notes=[])
        issue = None
        if groups['confirm'] or groups['manual']:
            owner = pr['base']['repo']['owner']
            if owner.get('type') == 'Organization':  # checked before any tracking-issue write
                raise Stop('verification', '仓库属于组织，跟踪 issue 只认仓库所有者本人的答复，组织仓库还不支持，交你处理')
            issue, comments, after_id = verification_issue(io, repo, pr, module, groups, patch_id, doc['sha256'],
                                                           ai_unavailable)
            owner_id = owner.get('id')
            if owner_id is None:
                owner_id = (io.api(f'repos/{repo}').get('owner') or {}).get('id')
            replies = parse_replies(comments, owner_id, after_id)
        cache = read_json(cache_path) or {}
        results, pending_confirm, pending_manual = [], [], []
        for case in doc['cases']:
            cid = case['id']
            if cid in ai_results:
                results.append(ai_results[cid])
                continue
            if case in groups['manual']:
                reply = replies['results'].get(cid)
                if reply is None:
                    pending_manual.append(cid)
                    continue
                comment = reply['comment']
                ev = evidence(os.path.join(logs, cid+'-quote.txt'), comment['body'], 'user-quote', comment['html_url'])
                results.append(case_result(case, evidence=[ev], reply=reply))
                continue
            if case in groups['confirm'] and cid not in replies['confirmed']:
                pending_confirm.append(cid)
                continue
            cached = cache.get(cid)
            if isinstance(cached, dict) and cached.get('sha256') == case['sha256']:
                results.append(cached['result'])
                continue
            execution = work.run_case(case['command'], head)
            ev = evidence(os.path.join(logs, cid+'.log'), execution['output'])
            baseline, baseline_ev = None, []
            if not execution.get('timeout') and execution.get('exit') not in (None, 0):
                baseline = work.run_case(case['command'], work.merge_base)
                baseline_ev = [evidence(os.path.join(logs, cid+'-base.log'), baseline['output'])]
            result = case_result(case, execution, [ev], baseline, baseline_ev, work.merge_base)
            if result['result'] != 'fail':  # a failure (possibly flaky) is re-run next time
                cache[cid] = dict(sha256=case['sha256'], result=result)
                write_json(cache_path, cache)
            results.append(result)
        if pending_confirm or pending_manual:
            wanted = (["确认执行 " + ' '.join(pending_confirm)] if pending_confirm else []) \
                + (["要你测 " + ' '.join(pending_manual)] if pending_manual else [])
            raise Stop('verification', f"等你在跟踪 issue #{issue['number']} 回复：" + '；'.join(wanted),
                       failures=case_failures(results))
        report = build_report(module, doc, work.merge_base, head, patch_id, new_executor(), results, replies['notes'])
        write_json(report_path, report)
    digest = hashlib.sha256(io.read_bytes(report_path)).hexdigest()
    failures = case_failures(report['cases']) if report['verdict'] == 'fail' else {}
    try:
        with io.verification_worktree(repo, head, pr['base']['ref']) as root:
            code, data, text = io.verification_check(root, module, report_path)
            if code:
                prefix = 'spec-guard 判报告不成立：' if code == 1 else ('spec-guard 判不了：' if code == 3 else 'spec-guard 校验失败：')
                raise Stop('verification', prefix + problems(data))
            code, data, text = io.verification_record(root, module, report_path)
            if code:
                raise Stop('verification', 'spec-guard 记录失败：' + problems(data))
            state = verification_state(io.verification_view(root), module)
            if not (state.get('current') is True and state.get('reportSha256') == digest and state.get('verdict') == report['verdict']):
                raise Stop('verification', '记录后 map view 的 verification 与报告对不上（current／reportSha256／verdict）')
    except Stop as error:
        error.failures.update(failures)
        raise
    if report['verdict'] == 'fail':
        raise Stop('verification', '合并前验证不通过：' + '、'.join(failures.values()), failures=failures)
    return dict(verdict=report['verdict'], report_sha256=digest, regression_sha256=doc['sha256'],
                patch_id=patch_id, notes=[note['title'] for note in report.get('notes', [])])


def unrecognised_reason(work, base_view):
    """Why a spec-guard PR with no acceptance record stopped, naming the unfinished flow v2 modules whose scope holds it.

    Flow v2 modules are recognised only by `spec/<id>.md` or `tasks/<id>/` in the diff; an implementation PR that ticks
    nothing would otherwise get the old-flow message. The scope only explains the stop; it never selects a module.
    """
    pending = [m["id"] for m in (base_view or {}).get("modules") or []
               if isinstance(m, dict) and m.get("tier") is not None and m.get("status") != "done"]
    owners = [module for module in pending if work.in_scope(module)]
    if len(owners) == 1:
        module = owners[0]
        return (f"改动都在新流程模块 {module} 的范围里，但这个 PR 没改 spec/{module}.md 或 tasks/{module}/，"
                f"编排只按这两处认新流程模块。如果它是 {module} 的实现，在 tasks/{module}/todo.md 勾上完成的任务再运行；"
                "本机检查证据已写好")
    if owners:
        return ("改动同时在新流程模块 " + "、".join(owners) + " 的范围里，这个 PR 又没改任何一个模块的 spec/<id>.md "
                "或 tasks/<id>/，拿不准属于哪个；勾上所属模块 tasks/<id>/todo.md 里完成的任务再运行；本机检查证据已写好")
    reason = ("这是 spec-guard 项目，但当前提交没有最终验收记录（不属于任何模块的 PR 也停在这里）。"
              "本机检查证据已写好，等 spec-guard 验收后再运行")
    if pending:
        reason += ("。新流程模块按 PR 是否改了 spec/<id>.md 或 tasks/<id>/ 来认；正在做的新流程模块有 "
                   + "、".join(pending) + "，如果这是它的实现，要同时勾上 tasks/<id>/todo.md")
    return reason


def default_merge_method(repo_info):
    if repo_info.get("allow_squash_merge"):
        return "squash"
    if repo_info.get("allow_merge_commit"):
        return "merge"
    return "rebase"


def hub_app_id(config, bot):
    return next((value for value in (bot.get("app_id"), config.get("appId"), config.get("app_id"))
                 if type(value) is int and value > 0), None)


def verified_requirements(io, repo, base_ref, app_id, *, all_checks=False):
    """Read effective rulesets and, when needed, classic branch protection."""
    def contexts(checks, source_field):
        return {(c["context"], c[source_field] if type(c.get(source_field)) is int and c[source_field] > 0 else None)
                for c in checks if c.get("context")}

    branch = quote(base_ref, safe="")
    rules = io.api_optional(f"repos/{repo}/rules/branches/{branch}") or []
    parameters = [rule.get("parameters") or {} for rule in rules if rule.get("type") == "required_status_checks"]
    checks = contexts((c for rule in parameters for c in rule.get("required_status_checks", [])), "integration_id")
    pinned = app_id is not None and (VERIFIED, app_id) in checks
    strict = any(rule.get("strict_required_status_checks_policy") is True and rule.get("required_status_checks")
                 for rule in parameters)
    if pinned and strict and not all_checks:
        return True, True, True, checks
    legacy = io.legacy_status_checks(repo, branch, need_source=app_id is not None and not pinned,
                                     need_sync=pinned and not strict, need_all=all_checks) or {}
    classic_checks = contexts(legacy.get("checks") or [], "app_id")
    classic_names = {name for name, _ in classic_checks}
    checks |= classic_checks | {(name, None) for name in legacy.get("contexts") or [] if name not in classic_names}
    required = any(name == VERIFIED for name, _ in checks)
    pinned = app_id is not None and (VERIFIED, app_id) in checks
    classic_strict = legacy.get("strict") is True and bool(legacy.get("contexts") or legacy.get("checks"))
    return required, pinned, strict or classic_strict, checks


def verified_rule_reasons(required, pinned, strict, app_id):
    reasons = []
    if not required:
        reasons.append(f"主干未要求 {VERIFIED}")
    if app_id is None:
        reasons.append("没有配置 hub App ID")
    elif required and not pinned:
        reasons.append(f"{VERIFIED} 的来源未限定为 hub App")
    if not strict:
        reasons.append("主干规则未要求分支与主干同步")
    return reasons


def automerge_hold_reasons(state, repo):
    snapshot = state.read()
    reasons = []
    if snapshot["frozen"]:
        reasons.append("watch 已冻结")
    if "narrowed" in snapshot["repos"].get(repo, {}):
        reasons.append("仓库已收紧")
    return reasons


def choose_route(io, repo, base_ref, state, config, bot, *, direct, dependabot, flow_v2, decision, reasons):
    """Select a route, collecting unmet preconditions in the order they are checked."""
    if direct:
        return "direct", ["mergeMode=direct", *reasons]
    holds = automerge_hold_reasons(state, repo)
    reasons = [*reasons, *holds]
    if dependabot:
        return "manual", [*reasons, "Dependabot 依赖更新由人合并"]
    if not flow_v2:
        reasons.append("老流程模块由人合并")
    if decision is not None and not decision["auto_merge"]:
        failures = [item["detail"] for item in decision["items"] if not item["ok"] and item["detail"] not in reasons]
        reasons.append("准入未通过" + ("：" + "；".join(failures) if failures else ""))
    if holds or not flow_v2:
        return "manual", reasons
    app_id = hub_app_id(config, bot)
    try:
        required, pinned, strict, _ = verified_requirements(io, repo, base_ref, app_id)
    except ChecksUnconfirmed as error:
        reasons.append(error.reason)
    else:
        reasons.extend(verified_rule_reasons(required, pinned, strict, app_id))
    if not state.heartbeat_is_fresh(15):
        reasons.append("watch 心跳不在 15 分钟内")
    return ("manual" if reasons else "automerge"), reasons


def verification_status(io, repo, head, state, description, evidence_url=None):
    payload = {"context": VERIFIED, "state": state, "description": description}
    if evidence_url is not None:
        payload["target_url"] = evidence_url
    io.api(f"repos/{repo}/statuses/{head}", "POST", payload)


def disable_queued_automerge(io, repo, number):
    # REST exposes the current GraphQL autoMergeRequest as auto_merge and the PR ID as node_id.
    pr = io.api(f"repos/{repo}/pulls/{number}")
    if pr.get("auto_merge") is None:
        return
    pr_id = pr.get("node_id")
    if not pr_id:
        raise ApiError("PR 已排队自动合并，但读不到节点 ID")
    query = ("mutation($pullRequestId:ID!){disablePullRequestAutoMerge(input:{pullRequestId:$pullRequestId})"
             "{pullRequest{id}}}")
    result = io.api("graphql", "POST", {"query": query, "variables": {"pullRequestId": pr_id}})
    try:
        if not result.get("errors") and result["data"]["disablePullRequestAutoMerge"]["pullRequest"]["id"] == pr_id:
            return
    except (AttributeError, KeyError, TypeError):
        pass
    raise ApiError("GitHub 未确认已关闭 PR 的自动合并")


def withdraw_automerge(io, repo, number, head, *, verification_failed):
    try:
        if verification_failed:
            verification_status(io, repo, head, "failure", "合并前验证未通过")
    finally:
        disable_queued_automerge(io, repo, number)


def annotate_evidence(body, match, payload, metadata):
    data = json.dumps(dict(payload, **metadata), ensure_ascii=False, sort_keys=True)
    # Paths in admission reasons can contain comment delimiters. Preserve them as data.
    data = data.replace("<", "\\u003c").replace(">", "\\u003e")
    summary = f"合并路线 `route={metadata['route']}`\n" + "".join(
        f"- {defang(reason)}\n" for reason in metadata["routeReasons"])
    # Some evidence readers recognize only a terminal machine block.
    return body[:match.start()] + summary + "\n" + body[match.start():match.start(2)] + data + body[match.end(2):]


def hand_to_user(io, repo, number, head, base, notify, state, bot, say, reasons):
    message = f"autopilot：由你合并。已验证提交 `{head[:12]}`。\n" + "\n".join(f"- {defang(reason)}" for reason in reasons)
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    marker = block("autopilot-handoff", {"head": head, "message": digest})
    path = f"repos/{repo}/issues/{number}/comments?per_page=100"
    with state.lock(wait_seconds=RUN_LOCK_WAIT):
        if not any(d.trusted(rest_comment(c), bot) and marker in c["body"] for c in io.api(path) or []):
            say(f"{RUN_MARKER}\n{message}\n\n{marker}")
        state.set_pr(repo, number, state="handed-to-user", validatedHeadOid=head, validatedBaseOid=base,
                     since=io.now(), notify=notify, pause=None)
        if notify:
            key = f"{repo}:{number}:{head}:handoff:{digest}:{notify}"
            state.enqueue_notification(notify, f"{repo}#{number} {message}", key)


def pending_automerge_intents(state, repo, number):
    return {key: detail for key, detail in state.pending_intents().items()
            if detail.get("action") == "enable-automerge" and detail.get("repo") == repo and detail.get("pr") == number}


def record_automerge_intent(io, repo, pr, base, notify, state, evidence_url, status_description, bot):
    """Check the queue before publishing success, then reconcile or record an intent."""
    number, head = pr["number"], pr["head"]["sha"]
    prefix = f"{repo}:{number}:{head}:{base}:automerge:"
    fields = dict(validatedHeadOid=head, validatedBaseOid=base, notify=notify, since=io.now())
    with state.lock(wait_seconds=RUN_LOCK_WAIT):
        pending = pending_automerge_intents(state, repo, number)
        latest_key = next(reversed(pending), None)
        pending_key = next((key for key in reversed(pending) if key.startswith(prefix)), latest_key)
        previous = pending[pending_key] if pending_key is not None else state.get_pr(repo, number) or {}
        matches_validation = previous.get("validatedHeadOid") == head and previous.get("validatedBaseOid") == base
        queue = None
        if pending or previous.get("state") == "automerge-on" or pr.get("auto_merge") is not None:
            fresh = io.api(f"repos/{repo}/pulls/{number}")
            queue = fresh.get("auto_merge")
            if queue is not None:
                require_same_pr_target(fresh, pr["base"]["ref"])
                live_head = (fresh.get("head") or {}).get("sha")
                # REST may only expose the current PR head, without the mutation's expected head.
                expected_head = queue.get("expectedHeadOid") or live_head
                if live_head != head or expected_head != head or previous.get("validatedHeadOid") != head:
                    raise Stop("merge", "已排队自动合并的 head 与验证提交不一致")
                if not matches_validation:
                    raise Stop("merge", "已排队自动合并的验证基础提交已变化")
                if (queue.get("enabled_by") or {}).get("id") not in (None, bot["id"]):
                    raise Stop("merge", "已排队的自动合并由其他身份打开")
        require_automerge_available(state, repo)
        verification_status(io, repo, head, "success", status_description, evidence_url)
        if queue is not None:
            if pending:
                fields["notify"] = notify or previous.get("notify")
                state.set_pr(repo, number, state="automerge-on", pause=None, **fields)
                for key in pending:
                    state.mark_done(key)
            elif notify and notify != previous.get("notify"):
                state.set_pr(repo, number, notify=notify)
            return None
        key = pending_key if pending_key is not None and matches_validation else prefix + evidence_url
        for stale in pending:
            if stale != key:
                state.mark_done(stale)
        state.intend(key, dict(action="enable-automerge", repo=repo, pr=number, **fields))
        state.set_pr(repo, number, state="automerge-requested", pause=None, **fields)
    return key


def require_automerge_available(state, repo):
    holds = automerge_hold_reasons(state, repo)
    if holds:
        raise Stop("merge", "；".join(holds))


def require_same_pr_target(pr, base_ref):
    if (pr.get("base") or {}).get("ref") != base_ref:
        raise Stop("merge", "PR 的目标分支被改了")
    if pr.get("state") != "open" or pr.get("draft") or pr.get("merged"):
        raise Stop("merge", "PR 已关闭、合并或改为草稿")


def try_enable_automerge(io, repo, pr, head, method, sleep, state):
    pr_id = pr.get("node_id")
    if not pr_id:
        raise ApiError("读不到 PR 节点 ID，无法打开自动合并")
    number, base_ref = pr["number"], pr["base"]["ref"]
    path = f"repos/{repo}/pulls/{number}"
    require_same_pr_target(io.api(path), base_ref)
    # The additive label endpoint also succeeds when the PR already has the label.
    labels = io.api(f"repos/{repo}/issues/{number}/labels", "POST", {"labels": [AUTHORED]})
    if not any(label.get("name") == AUTHORED for label in labels or []):
        raise ApiError(f"GitHub 未确认已添加 {AUTHORED} 标签")
    for attempt in range(5):
        if attempt:
            sleep(2)
        require_automerge_available(state, repo)
        rejection = io.enable_automerge(pr_id, head, method)
        if not rejection or not any(text in rejection.lower() for text in ("expected head oid", "does not match")):
            return rejection
    fresh = io.api(path)
    if (fresh.get("head") or {}).get("sha") != head:
        raise Stop("merge", "head 被改了")
    require_same_pr_target(fresh, base_ref)
    require_automerge_available(state, repo)
    return io.enable_automerge(pr_id, head, method)


def required_check_failures(checks, runs, statuses, bot, app_id):
    reasons = []
    for name, source in sorted(checks, key=lambda check: (check[0], check[1] or 0)):
        candidates = [run for run in runs if run.get("name") == name]
        contexts = [status for status in statuses if status.get("context") == name]
        source = app_id if name == VERIFIED and source is None else source
        if source is not None:
            matching = [run for run in candidates if (run.get("app") or {}).get("id") == source]
            # A status creator's user ID is distinct from the App ID.
            trusted = [status for status in contexts if source == app_id
                       and (status.get("creator") or {}).get("id") == bot["id"]]
            if name == VERIFIED and (source != app_id or ((candidates or contexts) and not (matching or trusted))):
                reasons.append(f"{VERIFIED} 的来源不是 hub App")
                continue
            candidates, contexts = matching, trusted
        if not (candidates or contexts):
            reasons.append(f"缺少必需检查 {name} 的结果")
        elif (any(run.get("status") != "completed" or run.get("conclusion") != "success" for run in candidates)
              or any(status.get("state") != "success" for status in contexts)):
            reasons.append(f"必需检查 {name} 未成功")
    return list(dict.fromkeys(reasons))


def check_automerge_fallback(io, repo, number, head, base_ref, app_id, bot):
    pr = io.api(f"repos/{repo}/pulls/{number}")
    if (pr.get("head") or {}).get("sha") != head:
        raise Stop("merge", "head 被改了")
    require_same_pr_target(pr, base_ref)
    decision = evaluate(io, repo, pr, bot)
    if not decision["auto_merge"]:
        raise Stop("admission", decision["headline"])
    required, pinned, strict, checks = verified_requirements(io, repo, base_ref, app_id, all_checks=True)
    reasons = verified_rule_reasons(required, pinned, strict, app_id)
    if reasons:
        raise Stop("merge", "；".join(reasons))
    runs, statuses = io.commit_check_results(repo, head)
    reasons = required_check_failures(checks, runs, statuses, bot, app_id)
    if reasons:
        raise Stop("merge", "；".join(reasons))
    if pr.get("mergeable_state") == "behind" or not io.base_is_ancestor(repo, base_ref, head):
        raise Stop("merge", "PR 落后主干")


def pause_automerge(io, repo, number, head, reason, notify, state):
    disable_queued_automerge(io, repo, number)
    recovery = "重新 run 通过"
    with state.lock(wait_seconds=RUN_LOCK_WAIT):
        state.pause_pr(repo, number, reason, recovery)
        for key in pending_automerge_intents(state, repo, number):
            state.mark_done(key)
        if notify:
            digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()
            state.enqueue_notification(notify, f"{repo}#{number} 已暂停：{reason}。恢复条件：{recovery}。",
                                       f"{repo}:{number}:{head}:automerge-paused:{digest}:{notify}")


def record_automerge_on(io, repo, number, head, key, state, bot, say, notes):
    message = f"已打开自动合并，绑定提交 `{head[:12]}`"
    marker = block("autopilot-automerge", {"head": head})
    with state.lock(wait_seconds=RUN_LOCK_WAIT):
        if key is not None:
            state.set_pr(repo, number, state="automerge-on")
            state.mark_done(key)
        try:
            path = f"repos/{repo}/issues/{number}/comments?per_page=100"
            if not any(d.trusted(rest_comment(c), bot) and marker in c["body"] for c in io.api(path) or []):
                say(f"{RUN_MARKER}\n{message}\n\n{marker}")
        except ApiError as error:
            notes.append(f"已打开自动合并，但 PR 评论没写成：{str(error)[:200]}")
    return message


def protected_repair_paths(paths):
    # Use the same template and matcher as the risk classifier's is_test predicate.
    test_patterns = risk_tiers.load_templates()["test_files"]
    return [path for path in paths if risk_tiers.any_match(test_patterns, path) is not None
            or re.fullmatch(r"tasks/[^/]+/regression\.md", path)]


def pause_for_repair(state, repo, number, head, stop):
    if stop.step != "repair-guard" and not stop.failures:
        return None
    with state.lock(wait_seconds=RUN_LOCK_WAIT):
        notices = []
        if stop.step == "repair-guard":
            notices.append((stop.reason, head, stop.reason))
        for key, detail in sorted(stop.failures.items()):
            commits = state.record_failure(repo, number, key, head)
            if len(commits) < REPAIR_LIMIT:
                continue
            reason = f"同一原因修了两次仍失败：{key}"
            body = (f"{reason}。{detail}。已试过提交 {'、'.join(commits)}。\n"
                    "可让会话继续修并重新验证，也可改方案后重新确认，或交给你手工处理。")
            notices.append((reason, ",".join(commits), body))
        if not notices:
            return None
        reason = notices[0][0]
        recovery = "用户决定"
        state.pause_pr(repo, number, reason, recovery)
        for notice_reason, notice_id, body in notices:
            digest = hashlib.sha256(f"{notice_reason}\0{notice_id}".encode("utf-8")).hexdigest()
            state.enqueue_notification("user", f"{repo}#{number} 已暂停。{defang(body)}\n等待{recovery}。",
                                       f"{repo}:{number}:repair-paused:{digest}")
        return reason


def run(io, repo, number, *, dry_run=False, merge_method=None, state=None, notify=None, author_host=None,
        sleep=time.sleep):
    """Check one PR and return its result, route, and evidence context."""
    notes, pr, head = [], None, None
    result_info, manual_reasons = {}, []
    checking = False
    automerge_attempt = merged = False
    automerge_key = None
    queue_check = None
    repair_baseline = None
    fallback_note = ""
    bot = io.bot_identity()
    # Batch uses the existing explicit direct-merge path and its existing token permissions.
    batch_merge = merge_method is not None
    state = state if state is not None else WatchState()

    def say(text):
        if not dry_run:
            return io.api(f"repos/{repo}/issues/{number}/comments", "POST", {"body": text})

    def finish(result, step, reason):
        return dict(result_info, result=result, step=step, reason=reason, notes=notes,
                    head_sha=(pr or {}).get("head", {}).get("sha"))

    try:
        if bot is None:
            raise Stop("identity", "hub 没配机器人身份（bot-identity.json），不能以机器人身份做任何事")
        pr = io.api(f"repos/{repo}/pulls/{number}")
        head = pr["head"]["sha"]
        extra = getattr(io, "extra_evidence", None)
        if extra is not None:
            from orchestrator.batch import batch_payload
            pr = io.batch_pr(repo, number, pr=pr)
            source = batch_payload(pr, bot)
            if not source or any(source.get(k) != extra.get(k) for k in ("module", "mergeCommit", "approval")):
                raise Stop("batch", "batch PR 来源、正文或提交已变化，交你核对")
            check_info = extra.get("check")
            if (isinstance(check_info, dict) and isinstance(check_info.get("command"), str)
                    and isinstance(check_info.get("result"), str)
                    and type(check_info.get("seconds")) in (int, float)):
                queue_check = check_info
        if pr.get("state") != "open" or pr.get("draft"):
            raise Stop("pr", "PR 不是打开状态，或还是草稿")
        if (pr["head"].get("repo") or {}).get("full_name") != repo:
            raise Stop("pr", "PR 来自 fork，本机编排不在你的 Mac 上运行别人仓库的代码")
        allowed = {bot["login"], pr["base"]["repo"]["owner"]["login"]}
        author = (pr.get("user") or {}).get("login")
        dependabot = author == "dependabot[bot]"
        if author not in allowed and not dependabot:
            raise Stop("pr", f"PR 作者 {author} 不是 autopilot 机器人或仓库所有者，不在本机跑它的代码")

        open_degrade = active_degrade_issues(io, repo)
        degrade = d.degrade_item(open_degrade, repo)[0]
        if not degrade["ok"]:
            sg_project = base_file(io, repo, SG_CAPABILITY_MAP, pr["base"]["ref"]) is not None
            narrow_pending(io, repo, pr["base"]["ref"], open_degrade, sg_project, bot, notes, dry_run)
            raise Stop("degrade", degrade["detail"])

        base_ref = pr["base"]["ref"]
        sg_project = base_file(io, repo, SG_CAPABILITY_MAP, base_ref) is not None
        if not sg_project:
            takeover = read_takeover(io, repo, base_ref, notes)
            say_takeover(say, None, "human", takeover["source"])
            raise Stop("takeover", "本仓库不是 spec-guard 项目，没有回归文档；按已批方案一律交你合")

        config = {} if batch_merge else io.local_config()
        direct = not dependabot and (batch_merge or config.get("mergeMode") == "direct")
        if direct and not batch_merge:
            result_info.update(route="direct", routeReasons=["mergeMode=direct"])
        with io.checkout(repo, pr) as work:
            base_view = None
            new_modules, module_candidates = [], []
            if not dependabot:
                changed = [f.get("filename") for f in io.api(f"repos/{repo}/pulls/{number}/files?per_page=100") or []]
                base_view = work.base_view()
                modules = base_view.get("modules") if isinstance(base_view, dict) else None
                new_modules = [m['id'] for m in modules or [] if isinstance(m, dict) and m.get('tier') is not None
                               and any(p == f"spec/{m['id']}.md" or p.startswith(f"tasks/{m['id']}/") for p in changed)]
                if len(new_modules) > 1:
                    raise Stop('verification', '这个 PR 改到了多个新流程模块（' + '、'.join(new_modules) + '），拿不准属于哪个')
                if new_modules and extra is not None and extra.get('module') != new_modules[0]:
                    raise Stop('batch', 'batch 的模块与新流程识别结果不符')
                module_candidates = new_modules or io.find_modules(repo, head)

            checking = True
            if not batch_merge:
                scope_module = module_candidates[0] if len(module_candidates) == 1 else None
                repair_baseline, existing_paths = work.changed_existing_files(scope_module)
                result_info["repairBaseline"] = repair_baseline
                if repair_baseline["source"] == "merge-base":
                    baseline = repair_baseline["sha"]
                    why = ("无法确认 spec-guard scope_base 是 PR merge base 的祖先"
                           if repair_baseline.get("fallbackReason") == "scope-base-not-ancestor"
                           else "取不到 spec-guard scope_base")
                    fallback_note = f"{why}，保护已有测试使用 PR 的 merge base `{baseline}`。"
                    notes.append(fallback_note)
                protected = protected_repair_paths(existing_paths)
                if protected:
                    raise Stop("repair-guard", "修复时改了已有测试／回归测试文档：" + "、".join(protected))

            if not dependabot:
                merges = [view_merge(base_view, m.get("id")) for m in modules if isinstance(m, dict)] \
                    if isinstance(modules, list) else []
                if not any(merge == "auto" for merge, _ in merges):
                    notes.extend(reason for _, reason in merges if reason)
                    if not merges:
                        notes.append("基础分支的 /map --json 读不到生效档位或没有模块，按人合")
                    reason = "spec-guard 项目的接管配置里没有任何模块的合并是 auto，交你"
                    if batch_merge:
                        say_takeover(say, None, "human", "/map --json（基础分支）")
                        raise Stop("takeover", reason)
                    manual_reasons.append(reason)

                # Spec/tasks artifacts are verified after checks; assume approval for the risk pre-check.
                optimistic = {p for p in changed if sg_artifact(p)}
                pre = evaluate(io, repo, pr, bot, optimistic)
                risk_item = next(i for i in pre["items"] if i["id"] == "risk")
                if not risk_item["ok"]:
                    if batch_merge:
                        raise Stop("risk", risk_item["detail"])
                    manual_reasons.append(risk_item["detail"])

            command = work.find_check()
            if not command:
                raise Stop("check", "基础分支上找不到检查命令（interface.yml 的 entries.check.run 或 PR workflow），硬卡无法满足")
            check = work.run_check(command)
            if check["exit"] != 0:
                reason = f"检查 `{command}` 退出码 {check['exit']}，交作者修"
                raise Stop("check", reason, failures={f"check:{command}": reason})
            evidence = check_evidence(repo, head, command, check, io.now())
            io.write_check_evidence(repo, evidence)
            try:
                pipeline_text = work.read_pipeline()
                steps = pipeline_model.resolve(yamlsub.loads(pipeline_text))["steps"] if pipeline_text is not None else []
            except (OSError, ValueError) as error:
                raise Stop("pre-merge", "基础分支 pipeline.yml 读不了或校验失败：%s" % error) from error
            for step in steps:
                if step["at"] != "pre-merge":
                    continue
                identifier, cmd = step["id"], step["run"]
                if step["by"] != "ai" or cmd.startswith("ask:"):
                    raise Stop("pre-merge", "合并前追加步骤 %s 要人做：%s" % (identifier, cmd))
                try:
                    result = work.run_check(cmd)
                except Stop as error:
                    failures = {f"check:{identifier}": error.reason} if error.failures else None
                    raise Stop("pre-merge", "合并前追加步骤 %s：%s" % (identifier, error.reason), failures=failures) from error
                evidence["checks"].append(dict(result, cmd=cmd))
                io.write_check_evidence(repo, evidence)
                if result["exit"] != 0:
                    reason = "合并前追加步骤 %s `%s` 退出码 %d，交作者修" % (identifier, cmd, result["exit"])
                    raise Stop("pre-merge", reason, failures={f"check:{identifier}": reason})
            module = acceptance = artifacts = verification = None
            flow_v2 = False
            if not dependabot:
                modules = module_candidates
                if not modules:
                    raise Stop("acceptance", unrecognised_reason(work, base_view))
                if len(modules) > 1:
                    raise Stop("acceptance", "当前提交有多条验收记录（" + "、".join(sorted(modules)) + "），拿不准属于哪个模块")
                module = modules[0]
                flow_v2 = any(isinstance(m, dict) and m.get("id") == module and m.get("tier") is not None
                              for m in (base_view or {}).get("modules") or [])
                merge, reason = view_merge(base_view, module)
                if reason:
                    notes.append(reason)
                say_takeover(say, module, merge, "/map --json（基础分支）", flow="v2" if flow_v2 else None)
                if merge != "auto":
                    detail = f"模块 {module} 的接管档位里合并由人，交你"
                    if batch_merge:
                        raise Stop("takeover", detail)
                    manual_reasons.append(f"{detail}（{reason}）" if reason else detail)
                if flow_v2:
                    verification = verify_module(io, repo, pr, work, module, notes, dry_run, author_host=author_host)
                else:
                    acceptance, report = accepted_review(io, repo, module, head)
                    fp, rounds = gate_review(report), 1
                    findings = blocking_findings(report)
                    if findings:
                        raise Stop("review", f"验收审查报告里还有 {len(findings)} 条 blocking",
                                   failures=review_failures(findings))
                    notes.append(f"模块 {module} 沿用 spec-guard 的验收审查报告，不另审")
                if not (dry_run and flow_v2):
                    artifacts = {"module": module, "verified": verify_artifacts(work.module_view(), module, changed,
                                                                               flow_v2=flow_v2)}

            base_tip = work.base_tip
            merge_base = work.merge_base
            if not (dry_run or batch_merge):
                patch_id = verification["patch_id"] if flow_v2 else work.patch_id()

        if dependabot:
            dependency_risk = classify_pr_risk(io, repo, pr, set())
            risk_item, _ = d.risk_item(dependency_risk)
            if not risk_item["ok"]:
                manual_reasons.append(risk_item["detail"])

        if dry_run:
            route, reasons = choose_route(io, repo, base_ref, state, config, bot, direct=direct,
                                          dependabot=dependabot, flow_v2=flow_v2, decision=None, reasons=manual_reasons)
            result_info.update(route=route, routeReasons=reasons)
            if route == "automerge":
                notes.append("预演：验证和准入通过后，写入 automerge-requested，添加 autopilot:authored，"
                             f"再调用 enablePullRequestAutoMerge（expectedHeadOid={head}）。"
                             "成功后记录 automerge-on 并在 PR 评论。被拒后复核 head、必需检查与主干同步，"
                             "符合条件才直接合并并收尾，否则暂停。")
            if flow_v2:
                return finish("dry-run", "verification", verification["reason"])
            return finish("dry-run", "evidence", "预演：已跑完仓库检查，没有发证据、没有合并")
        if flow_v2:
            body = evidence_v2_body(head, merge_base, module, command, check, verification, artifacts,
                                    extra_checks=evidence["checks"][1:], queue_check=queue_check,
                                    repair_baseline=repair_baseline)
        elif dependabot:
            checks = [{k: c[k] for k in ("cmd", "exit", "output_sha256")} for c in evidence["checks"]]
            body = (f"**autopilot 依赖更新检查证据**（提交 `{head[:12]}`）\n\n"
                    + "\n".join(f"- 检查：`{defang(c['cmd'])}`，退出码 {c['exit']}" for c in checks) + "\n\n"
                    + block("autopilot-evidence", {"head_sha": head, "runner": "autopilot-orchestrator",
                                                   "checks": checks, "repairBaseline": repair_baseline}) + "\n")
        else:
            body = evidence_body(head, command, check, report, rounds, fp, acceptance, artifacts,
                                 extra_checks=evidence["checks"][1:], queue_check=queue_check,
                                 repair_baseline=repair_baseline)

        blocks = list(EVIDENCE_BLOCK.finditer(body))
        match = blocks[0] if len(blocks) == 1 else None
        try:
            payload = json.loads(match[2]) if match else None
        except ValueError:
            payload = None
        expected = {k: verification[k] for k in ("verdict", "report_sha256", "regression_sha256")} if flow_v2 else None
        if not isinstance(payload, dict) or (flow_v2 and (match[1] != "2" or payload.get("verification") != expected)):
            raise Stop("verification", "证据评论的合并前验证与已成立的报告不一致，没有发证据")
        checking = False

        if not batch_merge:
            decision = None
            if not (direct or dependabot):
                decision = evaluate(io, repo, pr, bot, prospective_evidence=body)
            route, reasons = choose_route(io, repo, base_ref, state, config, bot, direct=direct,
                                          dependabot=dependabot, flow_v2=flow_v2, decision=decision, reasons=manual_reasons)
            result_info.update(route=route, routeReasons=reasons, validatedHeadOid=head, validatedBaseOid=base_tip)
            metadata = {k: result_info[k] for k in ("route", "routeReasons", "validatedHeadOid", "validatedBaseOid")}
            metadata.update(patch_id=patch_id, authorHost=author_host)
            if dependabot:
                metadata["riskTier"] = dependency_risk["tier"]
            if route != "automerge":
                # A new success status can unblock a queue left by an earlier run.
                disable_queued_automerge(io, repo, number)
            comment = say(annotate_evidence(body, match, payload, metadata))
            evidence_url = (comment or {}).get("html_url")
            if not evidence_url:
                raise ApiError("读不到验证证据评论的地址")
            status_description = f"补丁 {patch_id[:12]}，基础提交 {base_tip[:12]}"
            if route != "automerge":
                verification_status(io, repo, head, "success", status_description, evidence_url)
            if route == "manual":
                if dependabot:
                    late_degrade, _ = d.degrade_item(active_degrade_issues(io, repo), repo)
                    if not late_degrade["ok"]:
                        raise Stop("admission", late_degrade["detail"])
                elif any(item["id"] == "degrade" and not item["ok"] for item in decision["items"]):
                    raise Stop("admission", decision["headline"])
                hand_to_user(io, repo, number, head, base_tip, notify, state, bot, say, reasons)
                return finish("handed-to-user", "merge", "由你合并")
            if route == "automerge":
                live = evaluate(io, repo, pr, bot)
                if not live["auto_merge"]:
                    disable_queued_automerge(io, repo, number)
                    verification_status(io, repo, head, "success", status_description, evidence_url)
                    raise Stop("admission", live["headline"])
                method = default_merge_method(io.api(f"repos/{repo}"))
                automerge_attempt = True
                automerge_key = record_automerge_intent(io, repo, pr, base_tip, notify, state, evidence_url,
                                                        status_description, bot)
                if automerge_key is None or try_enable_automerge(io, repo, pr, head, method, sleep, state) is None:
                    message = record_automerge_on(io, repo, number, head, automerge_key, state, bot, say, notes)
                    return finish("automerge-on", "merge", message)
                check_automerge_fallback(io, repo, number, head, base_ref, hub_app_id(config, bot), bot)
                require_automerge_available(state, repo)
        else:
            say(body)

        if automerge_key is None:
            decision = evaluate(io, repo, pr, bot)
            if not decision["auto_merge"]:
                raise Stop("admission", decision["headline"])
            if manual_reasons:
                raise Stop("admission", "；".join(manual_reasons))

            repo_info = io.api(f"repos/{repo}")
            if merge_method == "merge" and not repo_info.get("allow_merge_commit"):
                raise Stop("merge", "仓库不允许 merge commit，module-crew 的合并是叠起来的，不能压平或变基，交你在仓库设置里打开 Allow merge commits")
            method = default_merge_method(repo_info) if merge_method is None else merge_method
        io.api(f"repos/{repo}/pulls/{number}/merge", "PUT", {"sha": head, "merge_method": method})
        after = io.api(f"repos/{repo}/pulls/{number}")
        if not after.get("merged") or not after.get("merge_commit_sha"):
            raise Stop("readback", "已请求合并，但读回 PR 没有显示已合并")
        merged = True
        # The merge is done: a failure from here on must not hide the post-merge follow-ups.
        for step in (lambda: io.api(f"repos/{repo}/issues/{number}/labels", "POST", {"labels": [AUTHORED, AUTO_MERGED]}),
                     lambda: say(f"{RUN_MARKER}\nautopilot 已合并：提交 `{head[:12]}` → `{after['merge_commit_sha'][:12]}`。")):
            try:
                step()
            except Exception as error:
                notes.append(f"已合并，但合并后的标签或评论没写成：{str(error)[:200]}")
        if verification and verification.get('notes'):
            recovery = f'python3 -B orchestrator/cli.py followups --repo {repo} --pr {number}'
            try:
                path = io.verification_paths(repo, verification['patch_id'])[0]
                raw = io.read_bytes(path)
                if hashlib.sha256(raw).hexdigest() != verification['report_sha256']:
                    raise ValueError('报告 sha256 与合并前证据不符')
                followups = open_followups(io, repo, number, module, verification['patch_id'],
                                           json.loads(raw)['notes'], bot)
                for key, label in (('opened', '已开后续 issue '), ('commented', '追加到 ')):
                    if followups[key]:
                        notes.append(label + '、'.join(f'#{n}' for n in followups[key]))
                if followups['skipped']:
                    notes.append(f"已跳过 {followups['skipped']} 条已记录意见")
                failed = followups['failed']
            except Exception:
                failed = verification['notes']
            if failed:
                message = ('已合并，后续意见未开成 issue：' + '、'.join(defang(t) for t in failed)
                           + f'。补开命令：`{recovery}`')
                notes.append(message)
                try:
                    say(f'{RUN_MARKER}\n{message}')
                except Exception:
                    notes.append('PR 补开提醒发送失败，请按上述命令补开')
        if automerge_key is not None:
            with state.lock(wait_seconds=RUN_LOCK_WAIT):
                state.set_pr(repo, number, state="post-merge-done")
                state.mark_done(automerge_key)
        return finish("merged", "merge", after["merge_commit_sha"])
    except Stop as s:
        if automerge_attempt:
            pause_automerge(io, repo, number, head, s.reason, notify, state)
            return finish("paused", s.step, s.reason)
        if pr is not None and not dry_run:
            withdraw_automerge(io, repo, number, head, verification_failed=checking and not batch_merge)
        pause_reason = None if dry_run or batch_merge else pause_for_repair(state, repo, number, head, s)
        if manual_reasons and "route" not in result_info:
            result_info.update(route="manual", routeReasons=manual_reasons)
        reason = pause_reason or s.reason
        if pr is not None and s.step != "identity":
            message = f"{RUN_MARKER}\nautopilot 停在「{s.step}」：{defang(reason)}"
            say(message + ("\n" + fallback_note if fallback_note else ""))
        return finish("paused" if pause_reason else "stopped", s.step, reason)
    except Exception as error:
        if pr is not None and not (dry_run or merged):
            try:
                withdraw_automerge(io, repo, number, head, verification_failed=checking and not batch_merge)
            except Exception as cleanup_error:
                raise ApiError(f"{error}；处理验证状态或关闭自动合并失败：{cleanup_error}") from cleanup_error
        raise


REWORK_DAYS = 14


def _ts(text):
    import datetime
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def stats(io, repo):
    """B12: auto-merged PRs; how many of them were reverted (PR evidence in revert degrade issues) or reworked (an
    autopilot:regression issue referencing the PR within 14 days of the merge); stops because Codex was
    unavailable (count and total minutes, from the local ledger)."""
    merged = [i for i in io.api(f"repos/{repo}/issues?labels={AUTO_MERGED}&state=all&per_page=100") or []
              if "pull_request" in i]
    regressions = [i for i in io.api(f"repos/{repo}/issues?labels=autopilot:regression&state=all&per_page=100") or []
                   if "pull_request" not in i]
    reverted, reworked = set(), set()
    merged_numbers = {pr.get("number") for pr in merged}
    for issue in io.api(f"repos/{repo}/issues?labels=autopilot:degrade&state=all&per_page=100") or []:
        if "pull_request" in issue:
            continue
        for raw in d.DEGRADE_BLOCK.findall(issue.get("body") or ""):
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(payload, dict) or payload.get("reason") != "revert":
                continue
            scope = payload.get("scope") or {}
            if not isinstance(scope, dict) or scope.get("repo") != repo:
                continue
            for evidence in payload.get("evidence") or []:
                match = PR_URL.search(evidence) if isinstance(evidence, str) else None
                if match:
                    n = int(match.group(1))
                    if n in merged_numbers:
                        reverted.add(n)
                    break
    for pr in merged:
        n = pr.get("number")
        done = _ts(pr.get("closed_at"))
        refs = re.compile(r"(?:#|/pull/)%d\b" % n)
        for issue in regressions:
            opened = _ts(issue.get("created_at"))
            if refs.search(issue.get("body") or "") and done and opened and 0 <= (opened - done).days <= REWORK_DAYS:
                reworked.add(n)
    events = [e for e in io.ledger_events() if e.get("event") == "codex_unavailable" and e.get("repo") == repo]
    minutes = 0.0
    for e in events:
        a, b = _ts(e.get("started_at")), _ts(e.get("stopped_at"))
        if a and b:
            minutes += (b - a).total_seconds() / 60
    return {"repo": repo, "auto_merged": len(merged), "reverted": len(reverted), "reworked": len(reworked - reverted),
            "reverted_or_reworked": len(reverted | reworked), "codex_unavailable_stops": len(events),
            "codex_unavailable_minutes": round(minutes, 1)}


def check_result(exit_code, output):
    lines = [ln for ln in (output or "").strip().splitlines() if ln.strip()]
    return {"exit": exit_code, "output_sha256": hashlib.sha256((output or "").encode("utf-8")).hexdigest(),
            "tail": lines[-TAIL:]}
