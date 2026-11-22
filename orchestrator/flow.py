"""Slice-10 local orchestrator: drive one PR from takeover to a verified merge, as the autopilot App bot.

Spec: docs/spec/slice-10.md. Stdlib only; supports Python 3.9. All I/O goes through `io` (see cli.LocalIO),
so the flow itself is testable with fakes. Every stop leaves one bot comment saying where and why; nothing
is merged unless the local admission -- the same code as the reusable workflow, fed fresh inputs -- says
"可以自动合并" on the exact head SHA that was checked and reviewed.

Steps: PR guards -> open degrade issues -> takeover (spec-guard projects: base-branch .agent/config.json once the
module is known from its acceptance record; others: .github/autopr/takeover.yml) -> risk pre-check -> checks in a throwaway clone
-> local check evidence file (read by spec-guard) -> review: spec-guard modules reuse the final-acceptance
review recorded outside the commit, other repos run delegate (round 2 rechecks against round 1) -> evidence
comment -> admission -> merge --match-head-commit -> read back -> labels.
"""
import base64
import hashlib
import json
import os
import re
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admission import cli as admission_cli
from admission import decide as d
from checkup import yamlsub
from pipeline import model as pipeline_model
from risk import tiers as risk_tiers

RUN_MARKER = "<!-- autopilot-run:v1 -->"
MAX_REVIEW_ROUNDS = 2
TAIL = 40
AUTHORED = "autopilot:authored"
AUTO_MERGED = "autopilot:auto-merged"


class Stop(Exception):
    """A planned stop: `step` names where, `reason` says why and who has to act."""

    def __init__(self, step, reason):
        super().__init__(f"{step}: {reason}")
        self.step, self.reason = step, reason


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
SG_CONFIG = ".agent/config.json"
SG_PRESETS = {"none": "human", "plan": "human", "merge": "auto"}
SG_ITEMS = {"spec": ("human", "codex"), "plan": ("human", "codex"), "merge": ("human", "auto"),
            "release": ("human", "prepare", "auto-confirm", "auto")}


APPROVED = ("approved", "codex-approved")


def sg_artifact(path):
    return (path.startswith("spec/") or path.startswith("tasks/")) and path != risk_tiers.CAPABILITY_MAP


def verify_artifacts(view, module, changed):
    """Slice 12: changed spec/<module>.md and tasks/<module>/** count as verified only when spec-guard (run on the
    head commit) says both the module's Spec and Plan approvals are still in effect. Other modules' files and
    the capability map are never verified."""
    mod = next((m for m in (view or {}).get("modules") or [] if m.get("id") == module), None)
    if not mod:
        return []
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
            body = f"已为{target} 开收窄 PR：{url}（按 spec-guard 规则降一格：合并 → Plan → 不接管）。它是 T4，合并后生效；在那之前本 issue 已阻止自动合并。"
        else:
            body = f"{target}的接管档位已是最严，不需要收窄。"
        io.api(f"repos/{repo}/issues/{number}/comments", "POST",
               {"body": body + "\n\n" + block("autopilot-narrowed", {"pr": url, "module": module})})
        notes.append(f"降级 #{number}：{body}")


def say_takeover(say, module, merge, source):
    say(f"接管档位（来源 {source}" + (f"，模块 {module}" if module else "") + f"）：合并 {merge}\n\n"
        + block("autopilot-takeover", {"module": module, "merge": merge, "source": source}) + "\n")


def read_sg_config(io, repo, base_ref, notes):
    """spec-guard's `.agent/config.json` on the BASE branch (a PR cannot widen its own takeover); {} when absent."""
    text = base_file(io, repo, SG_CONFIG, base_ref)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        notes.append(f"{SG_CONFIG} 读不懂，合并一律按人合")
        return {}
    return data if isinstance(data, dict) else {}


def takeover_merge(value):
    """`merge` of one spec-guard takeover value (preset or item object, spec/takeover-config.md); None if invalid."""
    if isinstance(value, str):
        return SG_PRESETS.get(value)
    if isinstance(value, dict) and all(k in SG_ITEMS and v in SG_ITEMS[k] for k, v in value.items()):
        return value.get("merge", "human")
    return None


def module_merge(config, module):
    """Effective merge item for `module`, as spec-guard 0.91 resolves it: the module's own moduleTakeover entry,
    else the project takeover, else none. Any bad moduleTakeover entry invalidates the whole key; anything
    unreadable means human."""
    project = takeover_merge(config.get("takeover", "none")) or "human"
    entries = config.get("moduleTakeover", {})
    if not isinstance(entries, dict) or any(takeover_merge(v) is None for v in entries.values()):
        return project
    return takeover_merge(entries[module]) if module in entries else project


def any_auto_merge(config):
    """Cheap pre-check before running checks: could any module of this project merge automatically?"""
    entries = config.get("moduleTakeover", {})
    modules = list(entries) if isinstance(entries, dict) else []
    return module_merge(config, None) == "auto" or any(module_merge(config, m) == "auto" for m in modules)


def read_takeover(io, repo, base_ref, notes):
    """Takeover setting for a repo without spec-guard: `.github/autopr/takeover.yml` on the base branch.
    Absent or unreadable -> merge by a human. (spec-guard `/map --json` is read once spec-guard 0.91 ships.)"""
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


def evaluate(io, repo, pr, bot, optimistic_artifacts=None):
    """Admission decision with fresh inputs, exactly as the reusable workflow computes it."""
    number, head, base_ref = pr["number"], pr["head"]["sha"], pr["base"]["ref"]
    comments = [rest_comment(c) for c in io.api(f"repos/{repo}/issues/{number}/comments?per_page=100") or []]
    files = [{k: f.get(k) for k in ("filename", "status", "additions", "deletions", "patch")}
             for f in io.api(f"repos/{repo}/pulls/{number}/files?per_page=100") or []]
    issues = [{"number": i.get("number"), "body": i.get("body")} for i in
              io.api(f"repos/{repo}/issues?labels=autopilot:degrade&state=open&per_page=100") or []
              if "pull_request" not in i]
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
    verified = d.verified_artifacts(comments, bot, head) if optimistic_artifacts is None else optimistic_artifacts
    risk = risk_tiers.classify(files, risk_tiers.load_templates(), config, verified)
    risk["notes"] = notes + risk["notes"]
    result = d.decide(risk=risk, comments=comments, degrade_issues=issues, repo=repo, bot=bot, head_sha=head)
    result["risk"] = risk
    return result


def blocking_of(report):
    """Blocking set of a conclusive delegate report: blocking/unknown-severity findings + unresolved rechecks."""
    n = sum(1 for f in report.get("findings") or [] if (f or {}).get("severity") != "suggestion")
    n += sum(1 for i in ((report.get("recheck") or {}).get("items") or []) if (i or {}).get("status") == "unresolved")
    return n


def conclusive(report):
    return isinstance(report, dict) and report.get("execution_status") == "completed" \
        and report.get("review_outcome") in ("findings", "no_findings_in_scope")


def review(io, repo, pr, work, notes):
    """Run (or reuse) the delegate review for this head; returns (report, rounds). Raises Stop."""
    head = pr["head"]["sha"]
    prev = io.load_review(repo, pr["number"])  # {"head_sha", "rounds", "report"} or None
    if prev and prev.get("head_sha") == head and conclusive(prev.get("report")):
        notes.append("这个提交已经审过，沿用上次的审查报告")
        report, rounds = prev["report"], prev["rounds"]
    else:
        has_prev = bool(prev) and conclusive(prev.get("report"))
        rounds = prev["rounds"] + 1 if has_prev else 1
        previous = prev["report"] if has_prev else None
        started = io.now()
        report = work.review(previous)
        if not conclusive(report):
            status = (report or {}).get("execution_status") or "没有输出"
            io.ledger({"event": "codex_unavailable", "repo": repo, "pr": pr["number"], "head_sha": head,
                       "status": status, "outcome": (report or {}).get("review_outcome"),
                       "started_at": started, "stopped_at": io.now()})
            raise Stop("review", f"审查没有结论（{status}）。Codex 不可用时一律停下等你，不退回别的审查方式")
        io.save_review(repo, pr["number"], {"head_sha": head, "rounds": rounds, "report": report})
    fp = gate_review(report)
    n = blocking_of(report)
    if n and rounds >= MAX_REVIEW_ROUNDS:
        raise Stop("review", f"复审 {rounds} 轮后仍有 {n} 条 blocking，停下交你")
    if n:
        raise Stop("review", f"审查有 {n} 条 blocking（第 {rounds} 轮），交作者返工后再运行；下一轮会对照这一轮复审")
    return report, rounds, fp


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


def evidence_body(head, command, check, report, rounds, fp, acceptance=None, artifacts=None, extra_checks=(), queue_check=None):
    checks = [dict(check, cmd=command)] + list(extra_checks)
    payload = {"head_sha": head, "runner": "autopilot-orchestrator",
               "checks": [{k: c[k] for k in ("cmd", "exit", "output_sha256")} for c in checks],
               "review": {"backend": report.get("backend"), "status": report.get("execution_status"),
                          "outcome": report.get("review_outcome"), "fingerprint": fp,
                          "blocking": blocking_of(report), "rounds": rounds},
               "acceptance": acceptance}
    if artifacts is not None:
        payload["artifacts"] = artifacts
    check_lines = "\n".join(f"- 检查：`{c['cmd']}`，退出码 {c['exit']}（在一次性克隆里由本机编排重新运行）" for c in checks)
    queue_line = ""
    if queue_check:
        queue_line = (f"module-crew 合并队列检查：{queue_check['command']}，{queue_check['result']}，"
                      f"耗时 {queue_check['seconds']} 秒（额外参考，不替代本机重跑）\n\n")
    tail = "\n\n".join(c["cmd"] + "\n" + "\n".join(c["tail"]) for c in checks)
    return (f"**autopilot 硬卡证据**（提交 `{head[:12]}`）\n\n"
            f"{check_lines}\n"
            f"- 审查：delegate／{report.get('backend')}，第 {rounds} 轮，{report.get('review_outcome')}，"
            f"blocking 0，补丁指纹 `{fp[:16]}`\n\n"
            + queue_line + f"<details><summary>检查输出最后 {TAIL} 行</summary>\n\n```\n{tail}\n```\n</details>\n\n"
            + block("autopilot-evidence", payload) + "\n")


def default_merge_method(repo_info):
    if repo_info.get("allow_squash_merge"):
        return "squash"
    if repo_info.get("allow_merge_commit"):
        return "merge"
    return "rebase"


def run(io, repo, number, *, dry_run=False, merge_method=None):
    """Returns {"result": "merged" | "stopped", "step", "reason", "notes", "head_sha"}."""
    notes, pr = [], None
    queue_check = None
    bot = io.bot_identity()

    def say(text):
        if not dry_run:
            io.api(f"repos/{repo}/issues/{number}/comments", "POST", {"body": text})

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
        if (pr.get("user") or {}).get("login") not in allowed:
            raise Stop("pr", f"PR 作者 {pr['user']['login']} 不是 autopilot 机器人或仓库所有者，不在本机跑它的代码")

        open_degrade = [{"number": i.get("number"), "body": i.get("body")} for i in
                        io.api(f"repos/{repo}/issues?labels=autopilot:degrade&state=open&per_page=100") or []
                        if "pull_request" not in i]
        degrade = d.degrade_item(open_degrade, repo)[0]
        if not degrade["ok"]:
            sg_project = base_file(io, repo, SG_CAPABILITY_MAP, pr["base"]["ref"]) is not None
            narrow_pending(io, repo, pr["base"]["ref"], open_degrade, sg_project, bot, notes, dry_run)
            raise Stop("degrade", degrade["detail"])

        base_ref = pr["base"]["ref"]
        sg_config = None
        if base_file(io, repo, SG_CAPABILITY_MAP, base_ref) is not None:
            sg_config = read_sg_config(io, repo, base_ref, notes)
            if not any_auto_merge(sg_config):
                say_takeover(say, None, "human", SG_CONFIG)
                raise Stop("takeover", "spec-guard 项目的接管配置里没有任何模块的合并是 auto，交你")
        else:
            takeover = read_takeover(io, repo, base_ref, notes)
            say_takeover(say, None, takeover["merge"], takeover["source"])
            if takeover["merge"] != "auto":
                raise Stop("takeover", "接管档位里合并由人，autopilot 不合并，交你")

        changed = [f.get("filename") for f in io.api(f"repos/{repo}/pulls/{number}/files?per_page=100") or []]
        # spec-guard projects: spec/tasks artifacts are verified after the checks; assume they will be for the pre-check
        optimistic = {p for p in changed if sg_artifact(p)} if sg_config is not None else None
        pre = evaluate(io, repo, pr, bot, optimistic)
        risk_item = next(i for i in pre["items"] if i["id"] == "risk")
        if not risk_item["ok"]:
            raise Stop("risk", risk_item["detail"])

        with io.checkout(repo, pr) as work:
            command = work.find_check()
            if not command:
                raise Stop("check", "基础分支上找不到检查命令（interface.yml 的 entries.check.run 或 PR workflow），硬卡无法满足")
            check = work.run_check(command)
            if check["exit"] != 0:
                raise Stop("check", f"检查 `{command}` 退出码 {check['exit']}，交作者修")
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
                    raise Stop("pre-merge", "合并前追加步骤 %s：%s" % (identifier, error.reason)) from error
                evidence["checks"].append(dict(result, cmd=cmd))
                io.write_check_evidence(repo, evidence)
                if result["exit"] != 0:
                    raise Stop("pre-merge", "合并前追加步骤 %s `%s` 退出码 %d，交作者修" % (identifier, cmd, result["exit"]))
            acceptance = artifacts = None
            if sg_config is not None:
                modules = io.find_modules(repo, head)
                if not modules:
                    raise Stop("acceptance", "这是 spec-guard 项目，但当前提交没有最终验收记录（不属于任何模块的 PR 也停在这里）。"
                                             "本机检查证据已写好，等 spec-guard 验收后再运行")
                if len(modules) > 1:
                    raise Stop("acceptance", "当前提交有多条验收记录（" + "、".join(sorted(modules)) + "），拿不准属于哪个模块")
                module = modules[0]
                merge = module_merge(sg_config, module)
                say_takeover(say, module, merge, SG_CONFIG)
                if merge != "auto":
                    raise Stop("takeover", f"模块 {module} 的接管档位里合并由人，交你")
                acceptance, report = accepted_review(io, repo, module, head)
                fp, rounds = gate_review(report), 1
                if blocking_of(report):
                    raise Stop("review", f"验收审查报告里还有 {blocking_of(report)} 条 blocking")
                notes.append(f"模块 {module} 沿用 spec-guard 的验收审查报告，不另审")
                artifacts = {"module": module, "verified": verify_artifacts(work.module_view(), module, changed)}
            else:
                report, rounds, fp = review(io, repo, pr, work, notes)

        if dry_run:
            return {"result": "dry-run", "step": "evidence", "reason": "预演：检查和审查都过了，没有发证据、没有合并",
                    "notes": notes, "head_sha": head}
        say(evidence_body(head, command, check, report, rounds, fp, acceptance, artifacts,
                          extra_checks=evidence["checks"][1:], queue_check=queue_check))

        decision = evaluate(io, repo, pr, bot)
        if not decision["auto_merge"]:
            raise Stop("admission", decision["headline"])

        repo_info = io.api(f"repos/{repo}")
        if merge_method == "merge" and not repo_info.get("allow_merge_commit"):
            raise Stop("merge", "仓库不允许 merge commit，module-crew 的合并是叠起来的，不能压平或变基，交你在仓库设置里打开 Allow merge commits")
        method = default_merge_method(repo_info) if merge_method is None else merge_method
        io.api(f"repos/{repo}/pulls/{number}/merge", "PUT", {"sha": head, "merge_method": method})
        after = io.api(f"repos/{repo}/pulls/{number}")
        if not after.get("merged") or not after.get("merge_commit_sha"):
            raise Stop("readback", "已请求合并，但读回 PR 没有显示已合并")
        io.api(f"repos/{repo}/issues/{number}/labels", "POST", {"labels": [AUTHORED, AUTO_MERGED]})
        say(f"{RUN_MARKER}\nautopilot 已合并：提交 `{head[:12]}` → `{after['merge_commit_sha'][:12]}`。")
        return {"result": "merged", "step": "merge", "reason": after["merge_commit_sha"], "notes": notes,
                "head_sha": head}
    except Stop as s:
        if pr is not None and s.step != "identity":
            say(f"{RUN_MARKER}\nautopilot 停在「{s.step}」：{s.reason}")
        return {"result": "stopped", "step": s.step, "reason": s.reason, "notes": notes,
                "head_sha": (pr or {}).get("head", {}).get("sha")}


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
