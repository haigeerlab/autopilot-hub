"""Slice-10 local orchestrator: drive one PR from takeover to a verified merge, as the autopilot App bot.

Spec: docs/spec/slice-10.md. Stdlib only; supports Python 3.9. All I/O goes through `io` (see cli.LocalIO),
so the flow itself is testable with fakes. Every stop leaves one bot comment saying where and why; nothing
is merged unless the local admission -- the same code as the reusable workflow, fed fresh inputs -- says
"可以自动合并" on the exact head SHA that was checked and reviewed.

Steps: PR guards -> open degrade issues -> takeover comment -> risk pre-check -> checks in a throwaway clone
-> delegate review (round 2 rechecks against round 1) -> evidence comment -> admission -> merge
--match-head-commit -> read back -> labels.
"""
import base64
import hashlib
import json
import os
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admission import cli as admission_cli
from admission import decide as d
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


def evaluate(io, repo, pr, bot):
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
    risk = risk_tiers.classify(files, risk_tiers.load_templates(), config)
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
    if report.get("unreviewed"):
        raise Stop("review", "有文件没审到：" + "、".join(str((u or {}).get("path")) for u in report["unreviewed"]))
    fp = ((report.get("scope") or {}).get("fingerprint") or {}).get("sha256")
    if not fp:
        raise Stop("review", "审查报告没有补丁指纹，证据无法绑定提交")
    n = blocking_of(report)
    if n and rounds >= MAX_REVIEW_ROUNDS:
        raise Stop("review", f"复审 {rounds} 轮后仍有 {n} 条 blocking，停下交你")
    if n:
        raise Stop("review", f"审查有 {n} 条 blocking（第 {rounds} 轮），交作者返工后再运行；下一轮会对照这一轮复审")
    return report, rounds, fp


def evidence_body(head, command, check, report, rounds, fp):
    payload = {"head_sha": head, "runner": "autopilot-orchestrator",
               "checks": [{"cmd": command, "exit": check["exit"], "output_sha256": check["output_sha256"]}],
               "review": {"backend": report.get("backend"), "status": report.get("execution_status"),
                          "outcome": report.get("review_outcome"), "fingerprint": fp,
                          "blocking": blocking_of(report), "rounds": rounds},
               "acceptance": None}
    tail = "\n".join(check["tail"])
    return (f"**autopilot 硬卡证据**（提交 `{head[:12]}`）\n\n"
            f"- 检查：`{command}`，退出码 {check['exit']}（在一次性克隆里由本机编排重新运行）\n"
            f"- 审查：delegate／{report.get('backend')}，第 {rounds} 轮，{report.get('review_outcome')}，"
            f"blocking 0，补丁指纹 `{fp[:16]}`\n\n"
            f"<details><summary>检查输出最后 {TAIL} 行</summary>\n\n```\n{tail}\n```\n</details>\n\n"
            + block("autopilot-evidence", payload) + "\n")


def merge_method(repo_info):
    if repo_info.get("allow_squash_merge"):
        return "squash"
    if repo_info.get("allow_merge_commit"):
        return "merge"
    return "rebase"


def run(io, repo, number, *, dry_run=False):
    """Returns {"result": "merged" | "stopped", "step", "reason", "notes", "head_sha"}."""
    notes, pr = [], None
    bot = io.bot_identity()

    def say(text):
        if not dry_run:
            io.api(f"repos/{repo}/issues/{number}/comments", "POST", {"body": text})

    try:
        if bot is None:
            raise Stop("identity", "hub 没配机器人身份（bot-identity.json），不能以机器人身份做任何事")
        pr = io.api(f"repos/{repo}/pulls/{number}")
        head = pr["head"]["sha"]
        if pr.get("state") != "open" or pr.get("draft"):
            raise Stop("pr", "PR 不是打开状态，或还是草稿")
        if (pr["head"].get("repo") or {}).get("full_name") != repo:
            raise Stop("pr", "PR 来自 fork，本机编排不在你的 Mac 上运行别人仓库的代码")
        allowed = {bot["login"], pr["base"]["repo"]["owner"]["login"]}
        if (pr.get("user") or {}).get("login") not in allowed:
            raise Stop("pr", f"PR 作者 {pr['user']['login']} 不是 autopilot 机器人或仓库所有者，不在本机跑它的代码")

        degrade = d.degrade_item([{"number": i.get("number"), "body": i.get("body")} for i in
                                  io.api(f"repos/{repo}/issues?labels=autopilot:degrade&state=open&per_page=100") or []
                                  if "pull_request" not in i], repo)[0]
        if not degrade["ok"]:
            raise Stop("degrade", degrade["detail"])

        takeover = read_takeover(io, repo, pr["base"]["ref"], notes)
        say(f"接管档位（来源 {takeover['source']}）：合并 {takeover['merge']}\n\n"
            + block("autopilot-takeover", {"module": takeover["module"], "merge": takeover["merge"],
                                           "source": takeover["source"]}) + "\n")
        if takeover["merge"] != "auto":
            raise Stop("takeover", "接管档位里合并由人，autopilot 不合并，交你")

        pre = evaluate(io, repo, pr, bot)
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
            report, rounds, fp = review(io, repo, pr, work, notes)

        if dry_run:
            return {"result": "dry-run", "step": "evidence", "reason": "预演：检查和审查都过了，没有发证据、没有合并",
                    "notes": notes, "head_sha": head}
        say(evidence_body(head, command, check, report, rounds, fp))

        decision = evaluate(io, repo, pr, bot)
        if not decision["auto_merge"]:
            raise Stop("admission", decision["headline"])

        repo_info = io.api(f"repos/{repo}")
        io.api(f"repos/{repo}/pulls/{number}/merge", "PUT", {"sha": head, "merge_method": merge_method(repo_info)})
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


def check_result(exit_code, output):
    lines = [ln for ln in (output or "").strip().splitlines() if ln.strip()]
    return {"exit": exit_code, "output_sha256": hashlib.sha256((output or "").encode("utf-8")).hexdigest(),
            "tail": lines[-TAIL:]}
