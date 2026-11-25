"""Slice-7 admission: can this PR be merged automatically? (report-only)

Spec: docs/spec/slice-7.md, docs/spec/slice-8.md, docs/spec/slice-9.md (gate: admission/gate.py). Four items -- risk tier, takeover merge setting, hard-gate
evidence, open degrade issues -- all of which must hold. Stdlib only; supports Python 3.9.

Slice 8: takeover comments count only when posted by the autopilot App bot (numeric user id + App slug from
bot-identity.json) and never edited (updated_at == created_at). Without a bot identity nothing is trusted, so
the PR can never be merged automatically.
"""
import json
import re

TAKEOVER_BLOCK = re.compile(r"<!--\s*autopilot-takeover:v1\s+(.*?)\s*-->", re.S)
DEGRADE_BLOCK = re.compile(r"<!--\s*autopilot-degrade:v1\s+(.*?)\s*-->", re.S)
BOT_MISSING = "hub 没配机器人身份（bot-identity.json），不认任何接管评论，合并按“人合”处理"
BOT_MISSING_SHORT = "没配机器人身份"


def glob_match(pattern, path):
    """Path glob: `**` crosses directories, `*` and `?` stay within one segment."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(out), path) is not None


def risk_item(risk):
    verdict = "停下交你" if risk["stop"] else ("可自动合" if risk["auto_merge_eligible"] else "要你合")
    detail = f"风险档 {risk['tier']}，{verdict}：{'；'.join(risk['reasons'])}"
    return {"id": "risk", "ok": bool(risk["auto_merge_eligible"]), "detail": detail}, f"风险档 {risk['tier']}"


def load_bot_identity(path):
    """{"app_slug", "id", ...} from bot-identity.json, or None when absent or malformed."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("app_slug"), str) or not data["app_slug"]:
        return None
    if not isinstance(data.get("id"), int) or isinstance(data.get("id"), bool):
        return None
    return data


def as_comment(c):
    """Comments arrive as REST objects; a bare string (older callers) has no author and is never trusted."""
    return c if isinstance(c, dict) else {"body": c if isinstance(c, str) else ""}


def trusted(comment, bot):
    """Posted by the App bot and never edited. Missing fields fail closed."""
    user = comment.get("user") or {}
    created = comment.get("created_at")
    return (bot is not None and user.get("type") == "Bot" and user.get("id") == bot["id"]
            and comment.get("app") == bot["app_slug"] and bool(created) and comment.get("updated_at") == created)


def takeover_item(comments, bot, notes):
    """Latest trusted `autopilot-takeover:v1` block; only its `merge` field matters here."""
    if bot is None:
        return {"id": "takeover", "ok": False, "detail": BOT_MISSING}, BOT_MISSING_SHORT, None
    comments = [as_comment(c) for c in comments]
    with_block = [c for c in comments if TAKEOVER_BLOCK.search(c.get("body") or "")]
    good = [c for c in with_block if trusted(c, bot)]
    if len(with_block) > len(good):
        notes.append(f"忽略了 {len(with_block) - len(good)} 条接管评论：不是 autopilot 机器人发的，或发出后被编辑过")
    blocks = [b for c in good for b in TAKEOVER_BLOCK.findall(c.get("body") or "")]
    if not blocks:
        return {"id": "takeover", "ok": False, "detail": "没有接管评论，合并按“人合”处理"}, "没有接管评论，合并按人合", None
    try:
        payload = json.loads(blocks[-1])
        if not isinstance(payload, dict):
            raise ValueError("不是 JSON 对象")
    except ValueError as e:
        return ({"id": "takeover", "ok": False, "detail": f"接管评论读不懂（{e}），合并按“人合”处理"},
                "接管评论读不懂", None)
    merge = payload.get("merge")
    if merge == "auto":
        return {"id": "takeover", "ok": True, "detail": "接管档位：合并 auto"}, "", payload
    return ({"id": "takeover", "ok": False, "detail": f"接管档位：合并 {merge!r}，按“人合”处理"},
            "接管档位：合并由人", payload)


def verified_artifacts(comments, bot, head_sha):
    """spec/** and tasks/** paths the latest trusted v1/v2 evidence on `head_sha` lists as verified (slice 12)."""
    from admission.gate import current_evidence  # local: gate imports helpers from this module
    if bot is None or not isinstance(head_sha, str) or len(head_sha) != 40:
        return set()
    current = current_evidence(comments, bot, head_sha)
    if not current:
        return set()
    paths = ((current[-1].get("artifacts") or {}).get("verified")) or []
    return {p for p in paths if isinstance(p, str) and (p.startswith("spec/") or p.startswith("tasks/"))}


def degrade_item(issues, repo):
    """Open `autopilot:degrade` issues scoped to this repo block auto-merge; the block's level is ignored.
    A block that cannot be read counts as blocking (its scope is unknown)."""
    blocking = []
    for issue in issues:
        for block in DEGRADE_BLOCK.findall(issue.get("body") or ""):
            try:
                scope_repo = json.loads(block)["scope"]["repo"]
            except (ValueError, KeyError, TypeError):
                scope_repo = repo
            if scope_repo == repo:
                blocking.append(issue.get("number"))
                break
    if not blocking:
        return {"id": "degrade", "ok": True, "detail": "没有未关闭的降级 issue"}, ""
    names = "、".join(f"#{n}" if n is not None else "（编号未知）" for n in blocking)
    return {"id": "degrade", "ok": False, "detail": f"有未关闭的降级 issue：{names}"}, f"有未关闭的降级 issue {names}"


def decide(*, risk, comments, degrade_issues, repo, bot=None, head_sha=None):
    """`bot` is the loaded bot identity (None trusts no comment); `head_sha` is the PR head the evidence must
    be bound to. The spec-guard module named in the trusted takeover comment also requires final acceptance."""
    from admission.gate import gate_item  # local: gate imports helpers from this module
    notes = []
    takeover, takeover_short, payload = takeover_item(comments, bot, notes)
    module = payload.get("module") if isinstance(payload, dict) else None
    module = module if isinstance(module, str) and module else None
    flow = payload.get("flow") if isinstance(payload, dict) else None
    items, shorts = [], []
    for item, short in (risk_item(risk), (takeover, takeover_short), gate_item(comments, bot, head_sha, module, notes, flow),
                        degrade_item(degrade_issues, repo)):
        items.append(item)
        if not item["ok"]:
            shorts.append(short)
    ok = not shorts
    headline = "可以自动合并" if ok else "不会自动合并（" + "；".join(shorts) + "）"
    return {"auto_merge": ok, "headline": headline, "items": items, "notes": notes + list(risk.get("notes", []))}
