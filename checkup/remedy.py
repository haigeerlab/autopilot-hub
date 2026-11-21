"""Fix generation (A6): drafts only, nothing is executed against the repository or GitHub.

Ported from repo-readiness src/repo_readiness/remedy/__init__.py at commit ac13f17: the branch
ruleset body and its `gh api` command (generator github.branch-rules), `_patch` (difflib unified
diff) and the autopilot-exception section of the instructions file (generator
instructions.autopr-exception). Messages rewritten in Chinese. The risk-tiers.json guess is new.
"""
import difflib
import json
import os

FRONTEND_DEPS = ("react", "vue", "svelte", "@angular/core", "next", "nuxt", "vite")
RISK_TIERS_PATH = ".github/autopr/risk-tiers.json"


def unified_patch(path, old, new):
    a = (old or "").splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    out = []
    for line in difflib.unified_diff(a, b, fromfile="/dev/null" if old is None else "a/" + path, tofile="b/" + path):
        # difflib glues a final line without newline to the next one; git needs the explicit marker.
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def ruleset_body(branch):
    return {
        "name": "%s: changes only through pull requests (autopilot checkup)" % branch,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/%s" % branch], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "pull_request", "parameters": {
                "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": False,
                "require_code_owner_review": False, "require_last_push_approval": False,
                "required_review_thread_resolution": False,
                # GitHub fills an omitted key with true, which blocked agent-relay#187 even at 0 approvals.
                "require_extra_approval_for_unattributed_changes": False,
                "allowed_merge_methods": ["merge", "squash", "rebase"]}},
        ],
        "bypass_actors": [],
    }


def guess_category(root):
    """'plugin' / 'frontend' / None (do not guess)."""
    if os.path.isdir(os.path.join(root, ".claude-plugin")):
        return "plugin"
    try:
        with open(os.path.join(root, "package.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        deps = set()
        for k in ("dependencies", "devDependencies", "peerDependencies"):
            if isinstance(data.get(k), dict):
                deps.update(data[k])
        if deps & set(FRONTEND_DEPS):
            return "frontend"
    return None


def autopilot_section(command):
    check = "`%s`" % command if command else "`<检查命令>`（没找到，请填写）"
    return (
        "\n## autopilot（自动维护）\n\n"
        "- 检查入口：%s\n"
        "- 先问：推送 main、打 tag、发版、改 CI 与分支保护、改本节的“先问”边界，一律先问用户。\n"
        "- autopilot 例外：autopilot agent 可以在 `autopilot/*` 分支上提交、推送并开 PR；"
        "main、tag、发版仍然要用户本人来。\n"
    ) % check


def instructions_patch(root, command):
    """(target file name, unified diff) adding the autopilot section to CLAUDE.md / AGENTS.md."""
    target = next((f for f in ("CLAUDE.md", "AGENTS.md") if os.path.isfile(os.path.join(root, f))), "CLAUDE.md")
    path = os.path.join(root, target)
    old = None
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            old = fh.read()
    new = (old or "") + ("" if old is None or old.endswith("\n") else "\n") + autopilot_section(command)
    return target, unified_patch(target, old, new)


def build_fixes(root, name, branch, findings, command, out_dir):
    """Fixes for the findings that are not ok. Writes files into out_dir when given.

    Returns [{id, finding, title, files, commands, note}]; the files are relative to out_dir.
    """
    status = {f["id"]: f["status"] for f in findings}
    fixes = []

    def write(rel, text):
        if not out_dir:
            return None
        p = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return rel

    def add(fix_id, finding, title, files, commands, note=None):
        fixes.append({"id": fix_id, "finding": finding, "title": title, "files": [f for f in files if f],
                      "commands": commands, "note": note})

    if status.get("C3") in ("missing", "unknown") and name:
        body = json.dumps(ruleset_body(branch), ensure_ascii=False, indent=2) + "\n"
        rs = write("ruleset-%s.json" % branch, body)
        cmd = "gh api --method POST repos/%s/rulesets --input %s" % (
            name, os.path.abspath(os.path.join(out_dir, rs)) if rs else "ruleset-%s.json" % branch)
        write("ruleset-command.txt", "# 分支规则集：禁止强推、禁止删除、只能经 PR 合入（审批人数 0）。\n"
              "# 只有你明确同意后才执行；撤回：gh api --method DELETE repos/%s/rulesets/<上一条命令返回的 id>\n"
              "# 需要仓库管理员权限。原有的旧版分支保护不动。\n%s\n" % (name, cmd))
        add("F1", "C3", "分支规则集", [rs, "ruleset-command.txt" if out_dir else None], [cmd],
            "需要仓库管理员权限；执行前须经你明确同意")
    if status.get("C11") == "missing":
        cat = guess_category(root)
        if cat:
            rel = write(RISK_TIERS_PATH, json.dumps({"extends": cat}) + "\n")
            add("F2", "C11", "风险分级配置", [rel], ["# 复制到仓库：%s" % RISK_TIERS_PATH],
                "类别「%s」是按目录特征猜的，请确认" % cat)
        else:
            add("F2", "C11", "风险分级配置", [], [],
                "看不出仓库类别（可选 plugin / contract / frontend / content），没有生成，请告诉我是哪一类")
    if status.get("C12") == "missing":
        target, patch = instructions_patch(root, command)
        rel = write("instructions-%s.patch" % target.lower().replace(".", "-"), patch)
        add("F3", "C12", "指令文件里的 autopilot 例外说明", [rel],
            ["git apply %s   # 在仓库的新分支上应用，提交后开 PR" % (rel or "<补丁>")],
            "改的是 agent 指令里的“先问”边界，须经你同意")
    return fixes
