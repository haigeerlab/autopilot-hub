"""The C1-C14 checklist. Each check returns a finding dict; nothing here changes the repository.

Finding: id, title, scope (仓库/增量), status (ok / missing / unknown), detail (what was seen),
impact (plain-Chinese "缺了会怎样"), fix ({can_generate, how} or None), evidence (extra lines),
skipped (True only for C2 without --run-check).
"""
import json
import os
import re

from checkup import entry, ghprobe, workflow
from checkup.ecosystem import manifest_names, lockfile_installed
from checkup.proc import run

RISK_CONFIGS = (".github/autopr/risk-tiers.yml", ".github/autopr/risk-tiers.json")  # slice 7: .yml first
_AUTOPILOT_HEADING = re.compile(r"^#{1,6}\s+.*(autopilot|autopr)", re.I | re.M)  # autopr: the old name, still used by .github/autopr/

TITLES = {
    "C1": "有能跑的检查命令", "C2": "检查真的能跑", "C3": "分支保护生效", "C4": "主干 CI 是绿的",
    "C5": "PR 会跑检查", "C6": "密钥扫描与推送保护", "C7": "workflow 默认权限只读",
    "C8": "action 固定到 40 位提交", "C9": "没有危险触发写法", "C10": "依赖按锁文件装",
    "C11": "风险分级配置", "C12": "指令文件里的 autopilot 例外", "C13": "风险路径清单（给 spec-guard）",
    "C14": "流程文件",
}
SCOPES = {"C8": "增量", "C9": "增量", "C10": "增量"}
IMPACTS = {
    "C1": "没有它，不能开自动合并（硬卡）：自动合并没有可依赖的检查。",
    "C2": "命令写了但跑不起来，自动合并的证据就是假的。",
    "C3": "有人或 AI 能绕过检查直接改 main。",
    "C4": "在本来就红的基线上开 PR，失败说不清是谁的。",
    "C5": "只在本机跑过，CI 上没人复核。",
    "C6": "AI 提交时可能把密钥带进仓库。",
    "C7": "外部 PR 借 workflow 拿到写权限。",
    "C8": "第三方 action 被篡改时，CI 跟着变。",
    "C9": "外部 PR 能拿到高权限去执行代码。",
    "C10": "同一提交两次装出不同依赖，检查结论不可信。",
    "C11": "没有时按通用规则，很多路径会被当成 T3。",
    "C12": "开「接管到合并」前没有它，AI 在 autopilot/* 分支上提交、推送会被你自己的规则拦住。",
    "C13": "spec-guard 靠它判断重要需求：没有或读不了时，交 Codex 批 Spec 的模块一律按重要处理、Spec 交你批；过期时按旧清单判断。",
    "C14": "没有或读不懂流程文件时，无法按仓库约定执行发布和合并前追加步骤；只提醒，不卡接入。",
}
# how a non-ok finding can be fixed: (can_generate, text)
FIX_HOW = {
    "C1": (False, "不能代劳，要你说用哪条命令（可写进 .github/autopr/interface.yml 的 entries.check.run）"),
    "C2": (False, "不能代劳，先让这条命令在干净克隆里跑通"),
    "C3": (True, "生成分支规则集 JSON 和 gh api 命令（只生成，不执行）"),
    "C4": (False, "不能代劳，先把主干修绿"),
    "C5": (False, "不能代劳（第二批会生成准入调用文件）；先让某个 pull_request 的 workflow 运行检查命令"),
    "C6": (False, "只给开启步骤：仓库 Settings → Code security，开启 Secret scanning 和 Push protection"),
    "C7": (False, "只给开启步骤：仓库 Settings → Actions → General → Workflow permissions，选 Read repository contents"),
    "C8": (False, "不能代劳，已列出要改的引用：把 @版本号 换成 40 位提交 SHA"),
    "C9": (False, "不能代劳，已列出位置：不要在 pull_request_target / workflow_run 里检出或执行 PR 代码"),
    "C10": (False, "不能代劳，已列出问题：补锁文件，CI 里用锁定安装命令"),
    "C11": (True, "生成一行 .github/autopr/risk-tiers.yml（类别按目录特征猜，猜不出就问你）"),
    "C12": (True, "生成 CLAUDE.md / AGENTS.md 里的 autopilot 说明草稿（补丁）"),
    "C13": (True, "生成 .github/autopr/risk-paths.json 的补丁（内容 = risk/cli.py paths --json）；改它算 T4，由你审后提交"),
    "C14": (True, "缺少时按类别生成只含 extends 的 pipeline.patch；改这个文件算 T4，由人审后合并"),
}


def finding(cid, status, detail, evidence=None, skipped=False):
    fix = None
    if status != "ok" and not skipped:
        can, how = FIX_HOW[cid]
        fix = {"can_generate": can, "how": how}
    return {"id": cid, "title": TITLES[cid], "scope": SCOPES.get(cid, "仓库"), "status": status,
            "detail": detail, "impact": IMPACTS[cid], "fix": fix, "evidence": evidence or [], "skipped": skipped}


# --------------------------------------------------------------------------- git helpers

def git_out(root, *args):
    r = run(["git", "-C", root] + list(args), cwd=root, timeout_s=60)
    return r.stdout if r.code == 0 else None


def resolve_base(root, base):
    for cand in (base, "origin/" + base):
        if git_out(root, "rev-parse", "--verify", "--quiet", cand + "^{commit}"):
            return cand
    return None


def changed_files(root, base):
    """(set of repo-relative paths changed against base plus uncommitted/untracked, problem)."""
    ref = resolve_base(root, base)
    if not ref:
        return None, "找不到基线分支 %s（也没有 origin/%s）" % (base, base)
    committed = git_out(root, "diff", "--name-only", "-z", "--diff-filter=d", "%s...HEAD" % ref)
    if committed is None:
        return None, "算不出 %s 到 HEAD 的改动（没有共同祖先？）" % ref
    out = set(p for p in committed.split("\0") if p)
    for args in (("diff", "--name-only", "-z", "--diff-filter=d", "HEAD"),
                 ("ls-files", "-z", "--others", "--exclude-standard")):
        more = git_out(root, *args)
        if more:
            out.update(p for p in more.split("\0") if p)
    return out, None


def tracked_files(root):
    out = git_out(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return set(p for p in (out or "").split("\0") if p)


def is_workflow(path):
    return path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")) and path.count("/") == 2


# --------------------------------------------------------------------------- the checklist

def run_checks(root, base, gh, repo_name, run_check=False, runner=entry.run_check):
    """Return (findings, ctx). gh may be None (no GitHub remote); ctx feeds fixes and the conclusion."""
    findings = []
    ctx = {"repo": repo_name, "default_branch": None, "check_command": None}

    # C1 / C2
    c1 = workflow.find_check(root)
    ctx["check_command"] = c1["command"]
    if c1["command"]:
        src = "interface.yml 登记的 check" if c1["source"] == "interface.yml" \
            else "从 %s（会被 PR 触发）里推断，请确认" % c1["source"]
        findings.append(finding("C1", "ok", "「%s」（%s）" % (c1["command"], src)))
    elif c1["problem"]:
        findings.append(finding("C1", "unknown", c1["problem"]))
    else:
        findings.append(finding("C1", "missing", "interface.yml 没有登记 check，会被 PR 触发的 workflow 里也没找到像测试或构建的步骤"))
    if not run_check:
        findings.append(finding("C2", "unknown", "没有加 --run-check，没有实际运行", skipped=True))
    elif not c1["command"]:
        findings.append(finding("C2", "unknown", "没有可运行的检查命令（见 C1）"))
    else:
        st, detail, lines = runner(root, c1["command"])
        findings.append(finding("C2", st, detail, lines))

    # repository-level GitHub checks
    if gh is not None:
        r = gh.get("repos/%s" % repo_name)
        if r.ok and isinstance(r.data, dict):
            ctx["default_branch"] = r.data.get("default_branch") or "main"

    def need_github(cid, fn):
        if gh is None:
            findings.append(finding(cid, "unknown", "没找到 GitHub 远端，读不了仓库设置"))
        elif ctx["default_branch"] is None:
            findings.append(finding(cid, "unknown", "GitHub 上读不到这个仓库（不存在，或当前登录账号看不到）"))
        else:
            st, detail, ev = fn()
            findings.append(finding(cid, st, detail, ev))

    branch = ctx["default_branch"]
    need_github("C3", lambda: ghprobe.effective_branch_rules(gh, repo_name, branch))
    need_github("C4", lambda: ghprobe.main_ci_green(gh, repo_name, branch))

    st, detail = workflow.pr_runs_check(root, c1["command"])
    findings.append(finding("C5", st, detail))

    need_github("C6", lambda: ghprobe.secret_scanning(gh, repo_name, bool(workflow.workflow_paths(root))))
    need_github("C7", lambda: ghprobe.workflow_permissions(gh, repo_name))

    # C8-C10 (incremental)
    changed, problem = changed_files(root, base)
    if problem:
        for cid in ("C8", "C9", "C10"):
            findings.append(finding(cid, "unknown", problem))
    else:
        names = {os.path.basename(p) for p in changed if is_workflow(p)}
        if not names:
            for cid in ("C8", "C9"):
                findings.append(finding(cid, "ok", "相对 %s 没有改过 workflow 文件，不需要评" % base))
        else:
            wfs, errors = workflow.load_workflows(root, names)
            if errors:
                for cid in ("C8", "C9"):
                    findings.append(finding(cid, "unknown", "改过的 workflow 读不了：%s" % "；".join(errors)))
            else:
                bad = workflow.unpinned_actions(wfs)
                findings.append(finding("C8", "missing", "没有固定到 40 位提交：%s" % "、".join(bad)) if bad
                                else finding("C8", "ok", "改过的 %d 个 workflow 里的 action 都已固定" % len(wfs)))
                bad = workflow.dangerous_triggers(wfs)
                findings.append(finding("C9", "missing", "；".join(bad)) if bad
                                else finding("C9", "ok", "改过的 %d 个 workflow 里没有危险触发写法" % len(wfs)))
        manifests = sorted(p for p in changed if os.path.basename(p) in manifest_names())
        if not manifests:
            findings.append(finding("C10", "ok", "相对 %s 没有改过依赖清单，不需要评" % base))
        else:
            st, detail = lockfile_installed(root, manifests, tracked_files(root))
            findings.append(finding("C10", st, detail))

    # C11
    present = [rel for rel in RISK_CONFIGS if os.path.isfile(os.path.join(root, rel))]
    if present:
        rel = present[0]
        try:
            from risk import tiers
            reader = tiers.read_repo_config_yml if rel.endswith(".yml") else tiers.read_repo_config
            reader(os.path.join(root, rel))
            findings.append(finding("C11", "ok", "%s 存在且读得通" % rel))
        except (OSError, ValueError) as e:
            findings.append(finding("C11", "unknown", "%s 存在但读不了：%s" % (rel, e)))
    else:
        findings.append(finding("C11", "missing", "没有 %s（也没有 .json）" % RISK_CONFIGS[0]))

    # C12
    hit = None
    for f in ("CLAUDE.md", "AGENTS.md"):
        p = os.path.join(root, f)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    if _AUTOPILOT_HEADING.search(fh.read()):
                        hit = f
                        break
            except (OSError, UnicodeDecodeError):
                pass
    findings.append(finding("C12", "ok", "%s 里有 autopilot 一节" % hit) if hit
                    else finding("C12", "missing", "CLAUDE.md / AGENTS.md 里没有标题含 autopilot 的一节（或没有这两个文件）"))

    # C13
    findings.append(risk_paths_finding(root))
    # C14
    findings.append(pipeline_finding(root))
    return findings, ctx


def pipeline_finding(root):
    from pipeline import model
    path = ".github/autopr/pipeline.yml"
    try:
        config = model.read_repo_pipeline(root)
        if config is None:
            return finding("C14", "missing", "没有 %s" % path)
        model.resolve(config)
    except model.PipelineError as error:
        return finding("C14", "unknown", "%s 校验失败：%s" % (path, "；".join(error.problems[:5])))
    except (OSError, ValueError) as error:
        return finding("C14", "unknown", "%s 读不了：%s" % (path, error))
    return finding("C14", "ok", "%s 存在且流程校验通过" % path)


def expected_risk_paths(root):
    """(text, None) or (None, problem): the risk-paths.json this checkout should have."""
    from risk import tiers
    try:
        return tiers.risk_paths_text(tiers.load_templates(), tiers.read_repo_config_at(root)), None
    except (OSError, ValueError) as e:
        return None, "风险分级配置读不了（%s），算不出风险路径清单" % e


def risk_paths_finding(root):
    from risk import tiers
    want, problem = expected_risk_paths(root)
    if problem:
        return finding("C13", "unknown", problem)
    path = os.path.join(root, tiers.RISK_PATHS_FILE)
    if not os.path.isfile(path):
        return finding("C13", "missing", "没有 %s" % tiers.RISK_PATHS_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            have = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        return finding("C13", "unknown", "%s 读不了：%s" % (tiers.RISK_PATHS_FILE, e))
    if have != json.loads(want):
        return finding("C13", "missing", "%s 过期：和现在的 hub 模板加本仓库 risk-tiers 算出来的不一致" % tiers.RISK_PATHS_FILE)
    return finding("C13", "ok", "%s 是最新的" % tiers.RISK_PATHS_FILE)


def conclusion(findings, ran_check):
    """{level, missing, reminders, text}: the highest takeover level that can be enabled now."""
    by = {f["id"]: f for f in findings}
    need = ["C1"] + (["C2"] if ran_check else []) + ["C12"]
    blockers = [c for c in need if by[c]["status"] != "ok"]
    reminders = [f["id"] for f in findings if f["status"] != "ok" and not f["skipped"] and f["id"] not in blockers]
    if not blockers:
        level = "接管到合并"
        text = "结论：现在可以开到「接管到合并」。"
    else:
        level = "只接管 Plan"
        label = {"C1": "没找到", "C2": "没跑通", "C12": "指令文件里还没有 autopilot 例外"}
        why = "、".join("%s（%s）" % (c, label[c] if by[c]["status"] == "missing" else "判不了") for c in blockers)
        text = "结论：现在只能开到「只接管 Plan」；开「接管到合并」还差 %s。" % why
    if reminders:
        text += "其余提醒（不卡接入）：%s。" % "、".join(reminders)
    if level == "接管到合并":
        text += "另外，在真实仓库打开「接管到合并」前还要先建好 GitHub App 机器人身份（合并方案 8.10 前提一）。"
    return {"level": level, "missing": blockers, "reminders": reminders, "text": text}
