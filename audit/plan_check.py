"""Slice 18b: Plan-approval side of the compliance audit (research audit-spec M2).

Reads spec-guard's `map_view --json` (0.92, interface 1.16) of the repository's default branch and only
reports what spec-guard itself records (M1): autopilot does not re-judge approvals.
"""
from audit.config_check import problem

APPROVED = ("approved", "codex-approved")
UNJUDGED = ("none",)  # no markers: the project may not use them; says nothing about approval
ROUNDS_NOTE = ("两轮仍有阻断是否转人批：spec-guard 0.92 的 review_mark 自己兜底，map_view 没有轮次数据，"
               "本项未独立核验")


def started(module):
    """Work has begun: the module is done, or its todo already has checked items."""
    return module.get("status") == "done" or ((module.get("progress") or {}).get("done") or 0) > 0


def check_plan(view):
    """{"problems": [...], "notes": [...]} for every module of a map view; None means map_view failed."""
    if not isinstance(view, dict) or not isinstance(view.get("modules"), list):
        return {"problems": [problem("map-unavailable", "提醒", "读不到 spec-guard 的 map_view，Plan 审批判不了")],
                "notes": []}
    problems, notes = [], [ROUNDS_NOTE]
    for m in view["modules"]:
        mid = m.get("id")
        if not started(m):
            continue
        review = m.get("review") or {}
        by = m.get("reviewApprovedBy") or {}
        route = m.get("codexRoute") or {}
        plan = review.get("plan")
        if plan in UNJUDGED:
            notes.append("模块 %s 的 Plan 没有批准标记，判不了" % mid)
        elif plan == "revised":
            problems.append(problem("plan-revised", "提醒", "模块 %s 已开工，Plan 批准后又改过，需要重新审" % mid))
        elif plan == "codex-approved" and not by.get("plan"):
            problems.append(problem("codex-not-allowed", "严重", "模块 %s 已开工，Plan 由 Codex 批，但接管档位不允许"
                                    "（codexRoute.plan=%s），相当于没批" % (mid, route.get("plan"))))
        elif plan not in APPROVED or not by.get("plan"):
            problems.append(problem("plan-not-approved", "严重", "模块 %s 已开工，Plan 没有生效的批准（%s）" % (mid, plan)))
        reasons = (m.get("important") or {}).get("reasons") or []
        spec = review.get("spec")
        # Only a Codex approval or no approval at all breaks the rule; "revised" means the user approved it
        # and the file was edited later, and "none" means the project uses no markers (cannot be judged).
        if reasons and (spec == "codex-approved" or spec in ("pending", "invalid")):
            problems.append(problem("important-not-user", "严重", "模块 %s 是重要需求（%s），已开工，但 Spec 不是你批的（%s）" % (
                mid, "、".join(reasons), spec)))
    return {"problems": problems, "notes": notes}
