"""Slice-9 hard gate: evidence that a process other than the author ran the checks and the review on the
final commit. Spec: docs/spec/slice-9.md. Stdlib only; supports Python 3.9.

The local orchestrator posts, as the App bot, one comment per run:
  <!-- autopilot-evidence:v1 {"head_sha": "<40 hex>", "runner": "...",
       "checks": [{"cmd": "...", "exit": 0, "output_sha256": "..."}],
       "review": {"backend": "...", "status": "completed", "fingerprint": "...", "blocking": 0, "rounds": 1},
       "acceptance": {"marker": "codex-approved" | "approved", "sha": "<40 hex>"} | null} -->
Flow v2 modules replace review/acceptance with:
  <!-- autopilot-evidence:v2 {"head_sha": "<40 hex>", "base_sha": "<40 hex>",
       "patch_id": "<40 hex>", "module": "<module>", "runner": "autopilot-orchestrator",
       "checks": [{"cmd": "...", "exit": 0, "output_sha256": "<64 hex>"}],
       "verification": {"verdict": "pass" | "pass-with-notes", "report_sha256": "<64 hex>",
                        "regression_sha256": "<64 hex>"},
       "artifacts": {"module": "<module>", "verified": ["spec/<module>.md"]}} -->
Both versions bind to the current head. The workflow does not recompute patch IDs: after a rebase the
orchestrator reruns, reuses verification by patch ID, and posts evidence bound to the new head.
Only trusted comments (bot, never edited) whose head_sha equals the PR head count; the latest one decides.
"""
import json
import re

EVIDENCE_BLOCK = re.compile(r"<!--\s*autopilot-evidence:v([12])\s+(.*?)\s*-->", re.S)
ACCEPTED_MARKERS = ("codex-approved", "approved")
NO_EVIDENCE = "还没有当前提交上的证据：要有 autopilot 机器人发的、非作者在最终提交上跑过检查和审查的证据评论"


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def problems(p, head_sha, module):
    """Reasons this evidence payload does not satisfy the gate; empty list means it does."""
    out = []
    checks = p.get("checks")
    if not isinstance(checks, list) or not checks:
        out.append("没有检查记录")
    else:
        for c in checks:
            if not isinstance(c, dict) or not isinstance(c.get("cmd"), str) or not c["cmd"].strip() \
                    or not _is_int(c.get("exit")):
                out.append("检查记录格式不对")
            elif c["exit"] != 0:
                out.append(f"检查 `{c['cmd']}` 退出码 {c['exit']}")
    if p.get("_v") == 2:
        for key in ("patch_id", "base_sha"):
            if not isinstance(p.get(key), str) or not re.fullmatch(r"[0-9a-fA-F]{40}", p[key]):
                out.append(f"证据的 {key} 格式不对")
        verification = p.get("verification")
        if not isinstance(verification, dict):
            out.append("没有合并前验证记录")
        else:
            verdict = verification.get("verdict")
            if verdict == "fail":
                out.append("合并前验证不通过")
            elif verdict not in ("pass", "pass-with-notes"):
                out.append("合并前验证结论格式不对")
            for key in ("report_sha256", "regression_sha256"):
                if not isinstance(verification.get(key), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", verification[key]):
                    out.append(f"合并前验证的 {key} 格式不对")
        if not isinstance(p.get("module"), str) or not p["module"].strip():
            out.append("证据没有模块")
        if module is None:
            out.append("v2 证据只用于 spec-guard 模块")
        elif p.get("module") != module:
            out.append("证据的模块和接管评论不一致")
        if p.get("head_sha") != head_sha:
            out.append("证据绑定的不是当前提交")
        return out
    review = p.get("review")
    if not isinstance(review, dict):
        out.append("没有审查记录")
    elif review.get("status") != "completed" or not isinstance(review.get("fingerprint"), str) \
            or not review["fingerprint"] or not _is_int(review.get("blocking")):
        out.append("审查没有完成或记录格式不对")
    elif review["blocking"] != 0:
        out.append(f"审查还有 {review['blocking']} 条 blocking")
    if module:
        acc = p.get("acceptance")
        if not isinstance(acc, dict) or acc.get("marker") not in ACCEPTED_MARKERS:
            out.append(f"模块 {module} 没有最终验收标记")
        elif acc.get("sha") != head_sha:
            out.append("最终验收绑定的不是当前提交")
    return out


def current_evidence(comments, bot, head_sha, notes=None):
    """Payloads of trusted evidence comments bound to `head_sha`, oldest first."""
    from admission.decide import as_comment, trusted  # local: decide imports this module
    notes = [] if notes is None else notes
    comments = [as_comment(c) for c in comments]
    with_block = [c for c in comments if EVIDENCE_BLOCK.search(c.get("body") or "")]
    good = [c for c in with_block if trusted(c, bot)]
    if len(with_block) > len(good):
        notes.append(f"忽略了 {len(with_block) - len(good)} 条证据评论：不是 autopilot 机器人发的，或发出后被编辑过")
    current, stale, unreadable = [], 0, 0
    for c in good:
        for version, block in EVIDENCE_BLOCK.findall(c.get("body") or ""):
            try:
                p = json.loads(block)
            except ValueError:
                unreadable += 1
                continue
            if not isinstance(p, dict):
                unreadable += 1
            elif p.get("head_sha") == head_sha:
                p["_v"] = int(version)
                current.append(p)
            else:
                stale += 1
    if stale:
        notes.append(f"有 {stale} 条证据评论绑定的不是当前提交 {head_sha[:12]}，不算")
    if unreadable:
        notes.append(f"有 {unreadable} 条证据评论读不懂，不算")
    return current


def gate_item(comments, bot, head_sha, module, notes):
    """The `gate` admission item. `module` is the spec-guard module from the trusted takeover comment
    (None for repos without spec-guard). V1 modules need final acceptance; v2 needs module-bound verification."""
    from admission.decide import BOT_MISSING_SHORT  # local: decide imports this module
    if bot is None:
        return {"id": "gate", "ok": False, "detail": "hub 没配机器人身份，不认任何证据评论"}, BOT_MISSING_SHORT
    if not isinstance(head_sha, str) or len(head_sha) != 40:
        return {"id": "gate", "ok": False, "detail": "不知道当前提交（head SHA），无法核对证据"}, "硬卡：不知道当前提交"
    current = current_evidence(comments, bot, head_sha, notes)
    if not current:
        return {"id": "gate", "ok": False, "detail": NO_EVIDENCE}, "硬卡：还没有证据"
    bad = problems(current[-1], head_sha, module)
    if bad:
        return {"id": "gate", "ok": False, "detail": "证据不合格：" + "；".join(bad)}, "硬卡：证据不合格"
    if current[-1]["_v"] == 2:
        p = current[-1]
        return {"id": "gate", "ok": True, "detail": f"证据绑定当前提交 {head_sha[:12]}：检查全过、合并前验证 "
                f"{p['verification']['verdict']}（补丁 {p['patch_id'][:12]}）"}, ""
    return {"id": "gate", "ok": True, "detail": f"证据绑定当前提交 {head_sha[:12]}：检查全过、审查没有 blocking"
            + ("、最终验收已过" if module else "")}, ""
