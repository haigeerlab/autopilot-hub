"""Slice 18c: one audit pass over registered repos -> weekly report comment + de-duplicated violation issues.

Pure logic over an `io` object (see audit/cli.py LiveIO for the real one, tests/test_audit_run.py for a fake):
  io.bot_identity() / io.baselines() -> {repo: entry} / io.snapshot(repo, ref) / io.degrade_issues(repo)
  io.today() -> "YYYY-MM-DD" / io.hub_api(path, method, body)
  io.edited_at(number) -> when the hub issue body was last edited, or None (REST updated_at moves on comments)
Report only (M6): never changes a takeover level, never closes an issue.
"""
import json
import re

from admission import decide as d
from audit import config_check as cc
from audit import plan_check as pc

HUB_REPO = "haigeerlab/autopilot-hub"
REPORT_LABEL = "autopilot:audit-report"
VIOLATION_LABEL = "autopilot:audit"
REPORT = "autopilot-audit-report"
VIOLATION = "autopilot-audit"
RESOLVED = "autopilot-audit-resolved"
REPORT_TITLE = "autopilot 合规周报"
ICON = {"合规": "✅", "提醒": "⚠️", "违规": "⛔", "判不了": "❔", "未登记": "➖"}


def block(name, payload):
    return "<!-- %s:v1 %s -->" % (name, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def payload(text, name):
    m = re.search(r"<!--\s*%s:v1\s+(.*?)\s*-->" % re.escape(name), text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def trusted_issue(issue, bot):
    """Same rule as pipeline/engine.py: GitHub leaves performed_via_github_app empty on App-created issues."""
    user = issue.get("user") or {}
    app = issue.get("performed_via_github_app")
    created = issue.get("created_at")
    return (bot is not None and user.get("type") == "Bot" and user.get("id") == bot["id"]
            and (app is None or app.get("slug") == bot["app_slug"])
            and bool(created) and issue.get("updated_at") == created)


def trusted_comment(c, bot):
    user = c.get("user") or {}
    shaped = {"user": {"id": user.get("id"), "type": user.get("type")},
              "app": (c.get("performed_via_github_app") or {}).get("slug"),
              "created_at": c.get("created_at"), "updated_at": c.get("updated_at")}
    return d.trusted(shaped, bot)


def audit_repo(io, repo, base, ref=None):
    """{"repo", "status", "problems", "notes"} for one repo."""
    out = {"repo": repo, "ref": ref, "status": "合规", "problems": [], "notes": []}
    if base is None:
        out.update(status="未登记", notes=["hub audit/baselines.json 里没有这个仓库，没有批准基线可比"])
        return out
    try:
        snap = io.snapshot(repo, ref)
        degrade = io.degrade_issues(repo)
    except Exception as e:  # clone, checkup or API failure: say so, never guess
        out.update(status="判不了", notes=["读不到仓库现状：%s" % str(e)[:300]])
        return out
    problems = (cc.check_takeover(snap["takeover"], base) + cc.check_pins(snap["pins"], base)
                + cc.check_checkup(snap["checkup"], base) + cc.check_degrade(degrade, repo))
    if snap.get("spec_guard"):
        plan = pc.check_plan(snap.get("view"))
        problems += plan["problems"]
        out["notes"] += plan["notes"]
    else:
        out["notes"].append("不是 spec-guard 项目，Plan 审批不查")
    problems.sort(key=lambda p: p["severity"] != "严重")
    out["problems"] = problems
    if any(p["severity"] == "严重" for p in problems):
        out["status"] = "违规"
    elif problems:
        out["status"] = "提醒"
    return out


def render(results, date):
    lines = ["## autopilot 合规报告 %s" % date, ""]
    severe = [(r["repo"], p) for r in results for p in r["problems"] if p["severity"] == "严重"]
    if severe:
        lines += ["### ⛔ 严重项（需要你决定，autopilot 不会自动降档）", ""]
        lines += ["- **%s** `%s`：%s" % (repo, p["kind"], p["detail"]) for repo, p in severe]
        lines.append("")
    lines += ["### 各仓库", ""]
    for r in results:
        where = r["repo"] + ("（%s）" % r["ref"] if r.get("ref") else "")
        lines.append("- %s **%s**：%s" % (ICON[r["status"]], where, r["status"]))
        for p in r["problems"]:
            lines.append("  - %s `%s`：%s" % ("⛔" if p["severity"] == "严重" else "⚠️", p["kind"], p["detail"]))
        for n in r["notes"]:
            lines.append("  - 说明：%s" % n)
    if not any(r["problems"] for r in results):
        lines += ["", "✅ 没有发现违规。"]
    return "\n".join(lines) + "\n"


def _open_issues(io, label, bot):
    out = []
    for issue in io.hub_api("/repos/%s/issues?labels=%s&state=open&per_page=100" % (HUB_REPO, label)) or []:
        if "pull_request" in issue:
            continue
        if issue.get("updated_at") != issue.get("created_at"):
            issue = dict(issue, updated_at=io.edited_at(issue["number"]) or issue.get("created_at"))
        if trusted_issue(issue, bot):
            out.append(issue)
    return out


def _comment(io, number, body):
    io.hub_api("/repos/%s/issues/%d/comments" % (HUB_REPO, number), "POST", {"body": body})


def post_report(io, bot, text):
    issue = next((i for i in _open_issues(io, REPORT_LABEL, bot) if payload(i.get("body"), REPORT) is not None), None)
    if issue is None:
        body = block(REPORT, {}) + "\n每次运行 `autopilot audit` 由机器人追加一条评论。只报告，不改任何接管档位。\n"
        issue = io.hub_api("/repos/%s/issues" % HUB_REPO, "POST",
                           {"title": REPORT_TITLE, "body": body, "labels": [REPORT_LABEL]})
    _comment(io, issue["number"], text)
    return issue["number"]


def _grouped(results):
    """{(repo, kind): [problem, ...]} for repos that were actually judged this run."""
    groups = {}
    for r in results:
        for p in r["problems"]:
            groups.setdefault((r["repo"], p["kind"]), []).append(p)
    return groups


def post_violations(io, bot, results, date):
    judged = {r["repo"] for r in results if r["status"] in ("合规", "提醒", "违规")}
    groups = _grouped(results)
    existing = {}
    for issue in _open_issues(io, VIOLATION_LABEL, bot):
        key = payload(issue.get("body"), VIOLATION)
        if key and isinstance(key.get("repo"), str) and isinstance(key.get("kind"), str):
            existing.setdefault((key["repo"], key["kind"]), issue)
    touched = []
    for (repo, kind), problems in sorted(groups.items()):
        details = "\n".join("- %s" % p["detail"] for p in problems)
        issue = existing.get((repo, kind))
        if issue is None:
            severity = "严重" if any(p["severity"] == "严重" for p in problems) else "提醒"
            body = (block(VIOLATION, {"repo": repo, "kind": kind}) + "\n**仓库**：%s\n**类别**：`%s`（%s）\n"
                    "**首次发现**：%s\n\n%s\n\n违规消失后这里会追加说明，但不会自动关闭，由人关。\n" % (
                        repo, kind, severity, date, details))
            created = io.hub_api("/repos/%s/issues" % HUB_REPO, "POST", {
                "title": "[%s] %s：%s" % (severity, repo, kind), "body": body, "labels": [VIOLATION_LABEL]})
            touched.append(created["number"])
        else:
            _comment(io, issue["number"], "%s 复查：仍存在。\n\n%s\n" % (date, details))
            touched.append(issue["number"])
    for (repo, kind), issue in sorted(existing.items()):
        if repo not in judged or (repo, kind) in groups:
            continue
        comments = io.hub_api("/repos/%s/issues/%d/comments?per_page=100" % (HUB_REPO, issue["number"])) or []
        mine = [c for c in comments if trusted_comment(c, bot)]
        if mine and payload(mine[-1].get("body"), RESOLVED) is not None:
            continue  # already said so, and nothing reappeared since
        _comment(io, issue["number"], block(RESOLVED, {}) + "\n%s 复查：未再发现这一类违规。"
                 "确认无误后请人工关闭本 issue。\n" % date)
    return touched


def run(io, repos=None, ref=None, post=True):
    """{"code", "repos", "text"}; code 0 = no violation, 1 = violation, 2 = some repo could not be judged."""
    bot = io.bot_identity()
    bases = io.baselines()
    names = repos or sorted(bases)
    date = io.today()
    results = [audit_repo(io, repo, bases.get(repo), ref) for repo in names]
    text = render(results, date)
    if post:
        if bot is None:
            raise RuntimeError("hub 没配机器人身份（bot-identity.json），不写报告")
        post_report(io, bot, text)
        post_violations(io, bot, results, date)
    code = 0
    if any(r["problems"] for r in results):
        code = 1
    elif any(r["status"] == "判不了" for r in results):
        code = 2
    return {"code": code, "repos": results, "text": text}
