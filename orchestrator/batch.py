"""Q9: advance one module-crew local merge through autopilot; all I/O uses io."""
import hashlib
import json
import re
from urllib.parse import quote

from admission.decide import trusted
from degrade.run import NotFound
from orchestrator import flow
from pipeline.engine import payload

MARKER = "autopilot-batch"
SHA = re.compile(r"[0-9a-f]{40}")
MODULE = re.compile(r"[\w.-]+")


def parse_first_stage(text, repo):
    """Validate the plugin boundary before using values in Git refs or command arguments."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as error:
        raise ValueError("module-crew first-stage 输出不是 JSON") from error
    if not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1:
        raise ValueError("module-crew first-stage 的 schemaVersion 必须是 1")
    if data.get("repo") != repo:
        raise ValueError("module-crew first-stage 的 repo 与 --repo 不一致")
    if not isinstance(data.get("baseBranch"), str) or not data["baseBranch"].strip():
        raise ValueError("module-crew first-stage 缺少 baseBranch")
    if not isinstance(data.get("merges"), list):
        raise ValueError("module-crew first-stage 的 merges 必须是列表")
    for item in data["merges"]:
        if (not isinstance(item, dict) or type(item.get("auto")) is not bool
                or not isinstance(item.get("module"), str) or not MODULE.fullmatch(item["module"])
                or item["module"] in (".", "..") or item["module"].endswith(".lock")
                or ".." in item["module"] or item["module"].endswith(".")
                or type(item.get("approval")) is not int or item["approval"] < 1
                or any(not isinstance(item.get(k), str) or not SHA.fullmatch(item[k])
                       for k in ("mergeCommit", "moduleCommit"))
                or (item.get("release") is not None and not isinstance(item["release"], dict))):
            raise ValueError("module-crew first-stage 的合并条目不合法")
    return data


def batch_payload(pr, bot):
    """Only an unedited App bot PR can supply batch provenance (timestamps normalized by io)."""
    comment = flow.rest_comment(pr)
    # GitHub PRs may omit performed_via_github_app; the bot's numeric ID still binds the App.
    if pr.get("performed_via_github_app") is None and bot:
        comment["app"] = bot["app_slug"]
    if not trusted(comment, bot):
        return None
    data = payload(pr.get("body") or "", MARKER)
    if (not data or not isinstance(data.get("module"), str) or not MODULE.fullmatch(data["module"])
            or type(data.get("approval")) is not int or data["approval"] < 1
            or data.get("mergeCommit") != (pr.get("head") or {}).get("sha")):
        return None
    return data


def branch_of(item):
    return "autopilot/batch-" + item["module"]


def choose(io, repo, data):
    """Resume a pending queue PR before comparing new candidates to the remote base."""
    prs = io.api(f"repos/{repo}/pulls?state=all&per_page=100") or []
    pending, opened = [], []
    for item in data["merges"]:
        if not item["auto"]:
            continue
        related = [p for p in prs if (p.get("head") or {}).get("ref") == branch_of(item)
                   and ((p.get("head") or {}).get("repo") or {}).get("full_name") == repo]
        opened.extend((item, pr) for pr in related if pr.get("state") == "open")
        if not any(p.get("merged_at") and (p.get("head") or {}).get("sha") == item["mergeCommit"] for p in related):
            pending.append(item)
    if opened:
        item, pr = next(((item, pr) for item, pr in opened if pr["head"]["sha"] == item["mergeCommit"]), opened[0])
        return item, io.batch_pr(repo, pr["number"])
    if not pending:
        return None, None
    base = io.api(f"repos/{repo}/commits/{quote(data['baseBranch'], safe='')}")
    base_sha = base.get("sha")
    if not isinstance(base_sha, str) or not SHA.fullmatch(base_sha):
        raise ValueError("GitHub 基础分支提交读不到")
    for item in pending:
        try:
            compare = io.api(f"repos/{repo}/compare/{item['mergeCommit']}...{base_sha}")
        except NotFound:
            # module-crew has only merged locally: GitHub cannot compare an unpushed commit.
            return item, None
        if compare.get("status") not in ("ahead", "identical", "behind", "diverged"):
            raise ValueError("GitHub compare 没有返回可识别的祖先关系")
        if compare["status"] not in ("ahead", "identical"):
            return item, None
    return None, None


def carried_manual(io, repo, data, item):
    """A human-approved merge that pushing `item` would carry along, or None.

    module-crew stacks merges on the integration branch, so pushing an automatic merge commit also pushes
    every ancestor the remote lacks. Judged by ancestry, not list order: an `auto=false` merge blocks when it
    is an ancestor of the merge commit in the user's checkout (the commit is not on GitHub yet) and the
    remote base does not contain it (GitHub cannot compare an unpushed commit: that also counts as missing)."""
    base_sha = None
    for other in data["merges"]:
        if other["auto"] or other["mergeCommit"] == item["mergeCommit"]:
            continue
        if not io.is_ancestor(repo, other["mergeCommit"], item["mergeCommit"]):
            continue
        if base_sha is None:
            base = io.api(f"repos/{repo}/commits/{quote(data['baseBranch'], safe='')}")
            base_sha = base.get("sha")
            if not isinstance(base_sha, str) or not SHA.fullmatch(base_sha):
                raise ValueError("GitHub 基础分支提交读不到")
        try:
            status = io.api(f"repos/{repo}/compare/{other['mergeCommit']}...{base_sha}").get("status")
        except NotFound:
            status = None
        if status not in ("ahead", "identical"):
            return other
    return None


def pr_body(item):
    fingerprint = (item.get("release") or {}).get("fingerprint") or "无（将由 autopilot 补最终验收审查）"
    return (f"来源：module-crew 合并第一段，本机已合进集成分支。\n\n"
            f"- 合并提交：`{item['mergeCommit']}`\n- 模块提交：`{item['moduleCommit']}`\n"
            f"- module-crew 批准事项：C{item['approval']}\n- 审查报告指纹：`{fingerprint}`\n\n"
            "由 autopilot 推送 batch 分支、重跑本机检查、完成最终验收并按现有准入规则合并。\n\n"
            + flow.block(MARKER, {k: item[k] for k in ("module", "mergeCommit", "approval")}))


def review_task(io, repo, item):
    evidence = io.local_evidence(repo, item["mergeCommit"])
    if not isinstance(evidence, dict) or not evidence.get("checks"):
        raise flow.Stop("acceptance", "读不到 autopilot 本机检查证据，交你核对")
    lines = [f"对照 spec/{item['module']}.md 审查当前模块的最终验收，报告绑定完整 head 提交。",
             "流程文件 spec/CAPABILITY-MAP.md、用法草案 spec/usage/、.agent/config.json、.agent/state.json 不算实现范围。",
             "autopilot 已在一次性克隆中重跑检查，以下为每条检查的命令、退出码、output_sha256 与最后 10 行 tail："]
    for check in evidence["checks"]:
        lines.append(f"命令：{check['cmd']}；退出码 {check['exit']}；output_sha256：{check['output_sha256']}\n"
                     + "\n".join(check.get("tail", [])[-10:]))
    return "\n\n".join(lines)


def review_head(report):
    scope = report.get("scope") if isinstance(report, dict) else None
    baseline = scope.get("baseline") if isinstance(scope, dict) else None
    return baseline.get("to") if isinstance(baseline, dict) else None


def release_review(io, item):
    release = item.get("release") or {}
    path = release.get("report")
    if not isinstance(path, str) or not path:
        return None
    try:
        raw = io.read_bytes(path)
        if hashlib.sha256(raw).hexdigest() != release.get("reportSha256"):
            return None
        report = json.loads(raw)
    except (OSError, ValueError):
        return None
    if review_head(report) != item["mergeCommit"]:
        return None
    return path, report


def record_acceptance(io, repo, item, number):
    module, head = item["module"], item["mergeCommit"]
    with io.acceptance_worktree(repo, head) as root:
        view = io.acceptance_map(root)
        mod = next((m for m in (view or {}).get("modules", []) if m.get("id") == module), None)
        route = (mod or {}).get("codexRoute")
        if not isinstance(route, dict) or not all(k in route for k in ("spec", "plan")):
            raise flow.Stop("acceptance", "spec-guard 没有返回模块的 codexRoute，交你核对")
        if any(route[k] is not None for k in ("spec", "plan")):
            raise flow.Stop("acceptance", "spec-guard 判定这个模块要人批（codexRoute="
                            + json.dumps(route, ensure_ascii=False) + "），交你")
        started = io.now()
        reused = release_review(io, item)
        if reused:
            path, report = reused
        else:
            base = io.module_start(root, module)
            report = io.review_acceptance(root, base, head, module, review_task(io, repo, item))
            path = io.save_acceptance_review(repo, module, head, report)
        if not flow.conclusive(report):
            status = (report or {}).get("execution_status") or "没有输出"
            io.ledger({"event": "codex_unavailable", "repo": repo, "pr": number, "head_sha": head,
                       "status": status, "outcome": (report or {}).get("review_outcome"),
                       "started_at": started, "stopped_at": io.now()})
            raise flow.Stop("acceptance", f"审查没有结论（{status}）。Codex 不可用时一律停下等你，不退回别的审查方式")
        if flow.blocking_of(report):
            raise flow.Stop("acceptance", f"最终验收审查有 {flow.blocking_of(report)} 条 blocking，交你")
        if review_head(report) != head:
            raise flow.Stop("acceptance", "最终验收审查报告审的不是当前提交")
        try:
            flow.gate_review(report)
        except flow.Stop as error:
            raise flow.Stop("acceptance", error.reason) from error
        result = io.mark_acceptance(root, module, path)
        if result["exit"] != 0:
            output = result["output"].strip()
            excerpt = output[:300]
            codes = sorted(set(re.findall(r"codex-route-(?:takeover|important|pre-declared)", output)))
            missing = [code for code in codes if code not in excerpt]
            if missing:
                excerpt = "、".join(missing) + "；" + excerpt
            raise flow.Stop("acceptance", "spec-guard 拒绝记验收：" + excerpt)
        record = io.shared_acceptance(root, module)
        if not isinstance(record, dict) or record.get("sha") != head:
            raise flow.Stop("acceptance", "spec-guard 记验收后读回新位置的记录与当前提交不符")
        flow.accepted_review(io, repo, module, head)


def run(io, repo, *, dry_run=False):
    """Advance at most one module; environment errors propagate to the CLI as exit 2."""
    data = parse_first_stage(io.first_stage(repo), repo)
    number = None
    item = None
    try:
        bot = io.bot_identity()
        if bot is None:
            raise ValueError("hub 没配机器人身份（bot-identity.json）")
        item, pr = choose(io, repo, data)
        if item is None:
            return {"code": 0, "result": "idle", "reason": "没有待处理的合并", "pr": None, "notes": []}
        number = pr["number"] if pr else None
        blocker = carried_manual(io, repo, data, item)
        if blocker is not None:
            carried = (f"已推上去的 {branch_of(item)} 带上了它，这个 PR 不能自动合并" if pr
                       else f"推 {branch_of(item)} 会把它一起带上")
            raise flow.Stop("batch", f"前面有人批合并 C{blocker['approval']}（模块 {blocker['module']}）还没推送，"
                                     f"{carried}；先由你推送或合并它")
        if pr is not None:
            source = batch_payload(pr, bot)
            if (not source or any(source.get(k) != item[k] for k in ("module", "mergeCommit", "approval"))
                    or (pr.get("base") or {}).get("ref") != data["baseBranch"]):
                raise flow.Stop("batch", f"分支 {branch_of(item)} 已有 PR #{number}，来源或提交不符，交你核对")
        plan = ["续跑已有 PR" if pr else "推送 batch 分支并开 PR、打 autopilot:authored 标签",
                "调用现有 flow.run 重跑本机检查、准入与合并",
                "若缺验收：建 detached worktree、核对 codexRoute、复用或运行审查、记验收并读回，再运行 flow.run"]
        if dry_run:
            return {"code": 0, "result": "dry-run", "reason": "预演：会处理模块 " + item["module"],
                    "module": item["module"], "pr": number, "plan": plan, "notes": []}
        if pr is None:
            io.push_batch(repo, item["mergeCommit"], branch_of(item))
            pr = io.api(f"repos/{repo}/pulls", "POST", {
                "base": data["baseBranch"], "head": branch_of(item),
                "title": f"autopilot：合入模块 {item['module']}（module-crew 合并第一段 C{item['approval']}）",
                "body": pr_body(item)})
            number = pr["number"]
        io.api(f"repos/{repo}/issues/{number}/labels", "POST", {"labels": [flow.AUTHORED]})
        io.extra_evidence = dict(item)
        result = flow.run(io, repo, number, merge_method="merge")
        if result.get("step") == "acceptance" and result.get("result") == "stopped":
            record_acceptance(io, repo, item, number)
            result = flow.run(io, repo, number, merge_method="merge")
        code = 1
        if result["result"] == "merged":
            try:
                compare = io.api(f"repos/{repo}/compare/{item['mergeCommit']}...{quote(data['baseBranch'], safe='')}")
            except NotFound:
                compare = {"status": "not-found"}
            if compare.get("status") in ("ahead", "identical"):
                code = 0
            else:
                result.setdefault("notes", []).append(
                    f"batch PR 已合并，但祖先核对失败：mergeCommit {item['mergeCommit']} 未确认是远端 "
                    f"{data['baseBranch']} 的祖先（compare status={compare.get('status')}），交你核对")
        return dict(result, code=code, pr=number, module=item["module"])
    except flow.Stop as error:
        if number is not None and not dry_run:
            io.api(f"repos/{repo}/issues/{number}/comments", "POST", {
                "body": f"{flow.RUN_MARKER}\nautopilot 停在「{error.step}」：{error.reason}"})
        return {"code": 1 if number is not None else 2, "result": "stopped", "step": error.step,
                "reason": error.reason, "pr": number, "notes": []}
