"""Slice-7 admission: can this PR be merged automatically? (report-only)

Spec: docs/spec/slice-7.md. Four items -- risk tier, takeover merge setting, hard-gate evidence, open
degrade issues -- all of which must hold. Stdlib only; supports Python 3.9.
"""
import json
import re

TAKEOVER_BLOCK = re.compile(r"<!--\s*autopilot-takeover:v1\s+(.*?)\s*-->", re.S)
DEGRADE_BLOCK = re.compile(r"<!--\s*autopilot-degrade:v1\s+(.*?)\s*-->", re.S)
AUTHOR_NOTE = "接管评论的作者暂不校验（agent 身份在第二批建立）；本检查只报告"
GATE_DETAIL = "还没有“非作者在最终提交上跑过检查”的证据（第二批）"


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


def takeover_item(comments):
    """Latest `autopilot-takeover:v1` block in the PR comments; only its `merge` field matters here."""
    blocks = [b for c in comments for b in TAKEOVER_BLOCK.findall(c or "")]
    if not blocks:
        return {"id": "takeover", "ok": False, "detail": "没有接管评论，合并按“人合”处理"}, "没有接管评论，合并按人合"
    try:
        payload = json.loads(blocks[-1])
        if not isinstance(payload, dict):
            raise ValueError("不是 JSON 对象")
    except ValueError as e:
        return ({"id": "takeover", "ok": False, "detail": f"接管评论读不懂（{e}），合并按“人合”处理"},
                "接管评论读不懂")
    merge = payload.get("merge")
    if merge == "auto":
        return {"id": "takeover", "ok": True, "detail": "接管档位：合并 auto"}, ""
    return ({"id": "takeover", "ok": False, "detail": f"接管档位：合并 {merge!r}，按“人合”处理"},
            "接管档位：合并由人")


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


def decide(*, risk, comments, degrade_issues, repo, gate=None):
    """`gate` is the hard-gate item ({"id": "gate", "ok", "detail"}); batch 2 supplies it from the evidence
    comment. Until then it is always "no evidence"."""
    gate = gate or {"id": "gate", "ok": False, "detail": GATE_DETAIL}
    items, shorts = [], []
    for item, short in (risk_item(risk), takeover_item(comments), (gate, "硬卡：还没有证据"),
                        degrade_item(degrade_issues, repo)):
        items.append(item)
        if not item["ok"]:
            shorts.append(short)
    ok = not shorts
    headline = "可以自动合并" if ok else "不会自动合并（" + "；".join(shorts) + "）"
    return {"auto_merge": ok, "headline": headline, "items": items, "notes": [AUTHOR_NOTE] + list(risk.get("notes", []))}
