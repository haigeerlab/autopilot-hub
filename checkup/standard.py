"""Plugin-standard items in the checkup report (docs/plugin-standard.md). Read-only, stdlib only.

These use the standard's own numbering with a 规范 prefix (规范A3 …), because the checkup's C1-C14 are a
different list. Covered here: A3 (MIT LICENSE, card), C1 (a `CI` workflow whose `CI required` aggregate is the
required check, card), C9 (Dependabot for Actions only, monthly, 7-day cooldown), C10 (CI checks the tree is clean).
They only report; nothing here changes the repository or its settings.
"""
import json
import os
import re

from checkup import yamlsub

TITLES = {"规范A3": "LICENSE 统一为 MIT（卡）", "规范C1": "CI 且 CI required 为必需检查（卡）",
          "规范C9": "Dependabot 只升级 Actions、冷却 7 天", "规范C10": "CI 最后检查工作区干净"}
IMPACTS = {
    "规范A3": "没有许可证别人按法律不能用；版权署名不一致，分不清版权归谁。",
    "规范C1": "没有 CI 或不设为必须通过，CI 红了照样能合；加了路径过滤的必需检查会让无关 PR 一直卡住。",
    "规范C9": "钉了 SHA 以后就没人升级 action，安全修复也跟不上；冷却期能避开刚发布就被投毒的版本。",
    "规范C10": "生成文件或版本号改了却漏提交，本机看着好，别人拉下来就不对。",
}
HOW = {
    "规范A3": "照 hub 的 LICENSE 放 MIT 全文，版权行写 `Copyright (c) 2026 haigeerlab`；清单的 license 字段写 MIT",
    "规范C1": "照 hub 的 examples/caller-ci-standard.yml 写 CI；改主干保护（只要求 CI required）要用户原话",
    "规范C9": "照 hub 的 .github/dependabot.yml：只开 github-actions，每月一次，cooldown.default-days: 7",
    "规范C10": "在 CI 里检查跑完后加一步 `git diff --exit-code`（模板里已有）",
}
COPYRIGHT = "Copyright (c) 2026 haigeerlab"
MIT_MARKERS = ("MIT License", "Permission is hereby granted, free of charge")
MANIFESTS = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json")
SKIP_DIRS = {".git", ".claude", ".hub", "node_modules", ".venv", "venv", "__pycache__"}


def _finding(fid, status, detail, evidence=None):
    fix = None if status == "ok" else {"can_generate": False, "how": HOW[fid]}
    return {"id": fid, "title": TITLES[fid], "scope": "仓库", "status": status, "detail": detail,
            "impact": IMPACTS[fid], "fix": fix, "evidence": evidence or [], "skipped": False}


def _read(root, rel):
    try:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def a3(root):
    text = _read(root, "LICENSE")
    if text is None:
        return _finding("规范A3", "missing", "仓库根目录没有 LICENSE")
    problems = []
    if not all(m in text for m in MIT_MARKERS):
        problems.append("LICENSE 不是 MIT 许可证全文")
    if COPYRIGHT not in [line.strip() for line in text.splitlines()]:
        problems.append("版权行不是 `%s`" % COPYRIGHT)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            if any(rel == m or rel.endswith("/" + m) for m in MANIFESTS):
                try:
                    data = json.loads(_read(root, rel) or "")
                except ValueError:
                    continue
                if isinstance(data, dict) and "license" in data and data["license"] != "MIT":
                    problems.append("%s 的 license 是 %r，应为 MIT" % (rel, data["license"]))
    return _finding("规范A3", "missing" if problems else "ok", "；".join(problems) or "MIT，版权行一致")


def _workflows(root):
    base = os.path.join(root, ".github", "workflows")
    out = []
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if name.endswith((".yml", ".yaml")):
            rel = ".github/workflows/" + name
            text = _read(root, rel) or ""
            try:
                doc = yamlsub.loads(text)
            except yamlsub.YamlSubsetError:
                doc = None
            out.append((rel, doc if isinstance(doc, dict) else None, text))
    return out


def _ci(root):
    return next(((rel, doc, text) for rel, doc, text in _workflows(root) if doc and doc.get("name") == "CI"), None)


def _path_filtered(doc):
    triggers = doc.get("on")
    if not isinstance(triggers, dict):
        return False
    return any(isinstance(v, dict) and ("paths" in v or "paths-ignore" in v) for v in triggers.values())


def _aggregate(doc):
    for job in (doc.get("jobs") or {}).values() if isinstance(doc.get("jobs"), dict) else ():
        if isinstance(job, dict) and job.get("name") == "CI required" and "always()" in str(job.get("if", "")):
            return True
    return False


def _required_contexts(gh, repo, branch):
    """Required status check names on the branch from rulesets and classic protection; None if unreadable."""
    names, readable = set(), False
    r = gh.get("repos/%s/rules/branches/%s" % (repo, branch))
    if r.ok and isinstance(r.data, list):
        readable = True
        for rule in r.data:
            if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
                for c in ((rule.get("parameters") or {}).get("required_status_checks") or []):
                    if isinstance(c, dict) and c.get("context"):
                        names.add(c["context"])
    r = gh.get("repos/%s/branches/%s/protection" % (repo, branch))
    if r.ok and isinstance(r.data, dict):
        readable = True
        rsc = r.data.get("required_status_checks") or {}
        names.update(rsc.get("contexts") or [])
        names.update(c.get("context") for c in rsc.get("checks") or [] if isinstance(c, dict) and c.get("context"))
    elif r.status == "not_found":
        readable = True  # "Branch not protected": readable, just nothing there
    return names if readable else None


def c1(root, gh, repo, branch):
    ci = _ci(root)
    if ci is None:
        return _finding("规范C1", "missing", "没有名为 CI 的 workflow")
    rel, doc, _ = ci
    problems = []
    if _path_filtered(doc):
        problems.append("%s 的触发条件加了路径过滤" % rel)
    if not _aggregate(doc):
        problems.append("%s 里没有带 if: always() 的汇总任务 `CI required`" % rel)
    if gh is None or not repo:
        status = "missing" if problems else "unknown"
        return _finding("规范C1", status, "；".join(problems + ["没有 GitHub 远端，判断不了主干保护"]))
    required = _required_contexts(gh, repo, branch)
    if required is None:
        problems.append("读不了 %s 的主干保护" % branch)
        status = "unknown" if len(problems) == 1 else "missing"
        return _finding("规范C1", status, "；".join(problems))
    if required != {"CI required"}:
        problems.append("%s 的必需检查是 %s，应只有 `CI required`" % (branch, "、".join(sorted(required)) or "（无）"))
    return _finding("规范C1", "missing" if problems else "ok", "；".join(problems) or "%s 有 CI required，且是 %s 唯一的必需检查" % (rel, branch))


def c9(root):
    text = _read(root, ".github/dependabot.yml") or _read(root, ".github/dependabot.yaml")
    if text is None:
        return _finding("规范C9", "missing", "没有 .github/dependabot.yml")
    try:
        doc = yamlsub.loads(text)
    except yamlsub.YamlSubsetError as e:
        return _finding("规范C9", "unknown", "dependabot.yml 读不懂（%s）" % e)
    updates = doc.get("updates") if isinstance(doc, dict) else None
    if not isinstance(updates, list) or not updates:
        return _finding("规范C9", "missing", "dependabot.yml 里没有 updates")
    problems = []
    for u in updates:
        if not isinstance(u, dict):
            continue
        eco = u.get("package-ecosystem")
        if eco != "github-actions":
            problems.append("开了 %s（只应开 github-actions）" % eco)
            continue
        if (u.get("schedule") or {}).get("interval") != "monthly":
            problems.append("github-actions 不是每月一次")
        days = (u.get("cooldown") or {}).get("default-days")
        if not isinstance(days, int) or days < 7:
            problems.append("cooldown.default-days 不是 7 天以上（现在是 %r）" % (days,))
    if not any(isinstance(u, dict) and u.get("package-ecosystem") == "github-actions" for u in updates):
        problems.append("没有开 github-actions")
    return _finding("规范C9", "missing" if problems else "ok", "；".join(problems) or "只升级 github-actions，每月一次，冷却 7 天以上")


def c10(root):
    ci = _ci(root)
    if ci is None:
        return _finding("规范C10", "missing", "没有名为 CI 的 workflow")
    rel, _, text = ci
    if re.search(r"git\s+diff\s+--exit-code", text):
        return _finding("规范C10", "ok", "%s 里有 `git diff --exit-code`" % rel)
    return _finding("规范C10", "missing", "%s 里没有检查工作区干净的一步" % rel)


def findings(root, gh, repo, branch):
    return [a3(root), c1(root, gh, repo, branch or "main"), c9(root), c10(root)]
