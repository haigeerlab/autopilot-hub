"""Slice 18b: Plan-approval side of the compliance audit (research audit-spec M2).

Reads spec-guard's `map_view --json` (0.92, interface 1.16) of the repository's default branch and only
reports what spec-guard itself records (M1): autopilot does not re-judge approvals.
Slice 18d: with interface 1.17 `reviewRounds`, Codex approvals are checked against the round facts the same
way spec-guard's review_mark gates them (spec-guard leaves that verdict to the caller).
"""
from audit.config_check import problem

APPROVED = ("approved", "codex-approved")
UNJUDGED = ("none",)  # no markers: the project may not use them; says nothing about approval
ROUNDS_NOTE = ("两轮仍有阻断是否转人批：这个 spec-guard 版本的 map_view 没有轮次数据（要 0.93 以上），"
               "依赖 review_mark 兜底，本项未独立核验")
ROUND_CAP = 2  # spec-guard review_mark: blocking rounds before a file must go to the user
STEP_NAMES = {"spec": "Spec", "plan": "Plan"}


def started(module):
    """Work has begun: the module is done, or its todo already has checked items."""
    return module.get("status") == "done" or ((module.get("progress") or {}).get("done") or 0) > 0


def check_rounds(mid, step, rounds):
    """Problems with a Codex approval of `step`, judged from its review rounds in file order."""
    name = STEP_NAMES[step]
    if not rounds:
        return [problem("codex-approved-without-rounds", "提醒",
                        "模块 %s 的 %s 由 Codex 批，但文件里没有审查轮次记录，看不到审查依据" % (mid, name))]
    last = rounds[-1]
    if (last.get("blocking") or 0) > 0:
        return [problem("codex-approved-with-blocking", "严重", "模块 %s 的 %s 由 Codex 批，但批准那一轮（第 %s 轮）"
                        "还有 %s 条阻断" % (mid, name, last.get("round"), last.get("blocking")))]
    # review_mark refuses any further Codex round once the file has ROUND_CAP blocking rounds and the latest
    # of them is blocking: a round recorded past that point means the hand-off to the user was skipped.
    blocked = 0
    for i, r in enumerate(rounds):
        if blocked >= ROUND_CAP and (rounds[i - 1].get("blocking") or 0) > 0:
            return [problem("rounds-not-escalated", "严重", "模块 %s 的 %s 在第 %s 轮前已有 %d 轮审查带阻断，应转你批，"
                            "却又由 Codex 审批" % (mid, name, r.get("round"), blocked))]
        blocked += 1 if (r.get("blocking") or 0) > 0 else 0
    return []


def check_plan(view):
    """{"problems": [...], "notes": [...]} for every module of a map view; None means map_view failed."""
    if not isinstance(view, dict) or not isinstance(view.get("modules"), list):
        return {"problems": [problem("map-unavailable", "提醒", "读不到 spec-guard 的 map_view，Plan 审批判不了")],
                "notes": []}
    problems, notes, unmarked, flow_v2 = [], [], [], []
    if any("reviewRounds" not in m for m in view["modules"]):
        notes.append(ROUNDS_NOTE)
    for m in view["modules"]:
        mid = m.get("id")
        if not started(m):
            continue
        if m.get("tier") is not None:
            # spec-guard flow v2 (interface 1.18+): the Plan is approved automatically on write; requirement
            # confirmation, the design approval and verification are what count, and S110's checks judge them.
            flow_v2.append(mid)
            continue
        review = m.get("review") or {}
        by = m.get("reviewApprovedBy") or {}
        route = m.get("codexRoute") or {}
        plan = review.get("plan")
        if plan in UNJUDGED:
            unmarked.append(mid)
        elif plan == "revised":
            problems.append(problem("plan-revised", "提醒", "模块 %s 已开工，Plan 批准后又改过，需要重新审" % mid))
        elif plan == "codex-approved" and not by.get("plan"):
            problems.append(problem("codex-not-allowed", "严重", "模块 %s 已开工，Plan 由 Codex 批，但接管档位不允许"
                                    "（codexRoute.plan=%s），相当于没批" % (mid, route.get("plan"))))
        elif plan not in APPROVED or not by.get("plan"):
            problems.append(problem("plan-not-approved", "严重", "模块 %s 已开工，Plan 没有生效的批准（%s）" % (mid, plan)))
        rounds = m.get("reviewRounds")
        if isinstance(rounds, dict):
            for step in ("spec", "plan"):
                if review.get(step) == "codex-approved" and by.get(step) == "codex":
                    problems += check_rounds(mid, step, rounds.get(step) or [])
        reasons = (m.get("important") or {}).get("reasons") or []
        spec = review.get("spec")
        # Only a Codex approval or no approval at all breaks the rule; "revised" means the user approved it
        # and the file was edited later, and "none" means the project uses no markers (cannot be judged).
        if reasons and (spec == "codex-approved" or spec in ("pending", "invalid")):
            problems.append(problem("important-not-user", "严重", "模块 %s 是重要需求（%s），已开工，但 Spec 不是你批的（%s）" % (
                mid, "、".join(reasons), spec)))
    if flow_v2:
        shown = "、".join(str(m) for m in flow_v2[:8]) + ("等" if len(flow_v2) > 8 else "")
        notes.append("%d 个新流程模块改由新流程检查判断（需求确认、重要需求技术方案、合并前验证）：%s" % (len(flow_v2), shown))
    if unmarked:
        shown = "、".join(str(m) for m in unmarked[:8]) + ("等" if len(unmarked) > 8 else "")
        notes.append("%d 个已开工模块的 Plan 没有批准标记（多为用批准标记之前的旧模块），判不了：%s" % (len(unmarked), shown))
    return {"problems": problems, "notes": notes}
