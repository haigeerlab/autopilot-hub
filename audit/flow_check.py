"""S110 S5: requirement, important-design and pre-merge verification compliance."""
from audit.config_check import problem
from audit.plan_check import started


def check_flow(view, records):
    """Check started new-flow modules; None records means the local checkout is unavailable."""
    if not isinstance(view, dict) or not isinstance(view.get("modules"), list):
        return {"problems": [problem("map-unavailable", "提醒", "读不到 spec-guard 的 map_view，新流程合规判不了")],
                "notes": []}
    problems, notes, checked, unavailable = [], [], [], []
    for m in view["modules"]:
        if m.get("tier") is None or not started(m):
            continue
        mid = m.get("id")
        checked.append(mid)
        confirmation = m.get("requirementConfirm")
        status = confirmation.get("status") if isinstance(confirmation, dict) else None
        if (not isinstance(confirmation, dict) or status in ("pending", "invalid")
                or (status == "confirmed" and confirmation.get("by") != "user")):
            problems.append(problem("requirement-not-confirmed", "严重",
                                    "模块 %s 已开工，需求没有经你确认（%s）" % (mid, status)))
        elif status == "revised":
            problems.append(problem("requirement-revised", "提醒", "模块 %s 的需求确认后又改过，要你重新确认" % mid))
        if m.get("tier") == "important":
            approval = m.get("designApproval")
            status = approval.get("status") if isinstance(approval, dict) else None
            by = approval.get("approvedBy") if isinstance(approval, dict) else None
            if status != "approved" or by != "user":
                problems.append(problem("important-design-not-user", "严重",
                                        "模块 %s 是重要需求，已开工，但技术方案不是你批的（%s／%s）" % (mid, status, by)))
        if m.get("status") == "done":
            if records is None:
                unavailable.append(mid)
            else:
                record = records.get(mid)
                if record is None or record.get("verdict") not in ("pass", "pass-with-notes"):
                    problems.append(problem("merged-without-verification", "严重",
                                            "模块 %s 已完成，但本机没有成立的合并前验证记录" % mid))
    if unavailable:
        notes.append("本机没有配置这个仓库的检出或读不到验证记录目录，合并前验证判不了：%s" %
                     "、".join(str(mid) for mid in unavailable))
    if checked:
        shown = "、".join(str(mid) for mid in checked[:8]) + ("等" if len(checked) > 8 else "")
        notes.append("%d 个已开工新流程模块已检查（需求确认、重要需求技术方案、合并前验证）：%s" % (len(checked), shown))
    return {"problems": problems, "notes": notes}
