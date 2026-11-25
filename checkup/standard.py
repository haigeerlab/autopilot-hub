"""Plugin-standard items in the checkup report (docs/plugin-standard.md). Read-only, stdlib only.

These use the standard's own numbering with a 规范 prefix (规范A3 …), because the checkup's C1-C14 are a
different list. A3 and C1 report cards; the remaining items are reminders.
They only report; nothing here changes the repository or its settings.
"""
import json
import os
import re
import subprocess
import stat

from checkup import yamlsub
from rules.version_consistency import is_declaration

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
REMINDERS = {
    "B2": ("统一发版命令", "发版入口不统一，自动维护无法调用。", "添加 scripts/release.py，并在 interface.yml 的 entries 登记 release_prepare"),
    "B3": ("发版证据统一放", "缺少证据，无法核对安装内容与发布内容。", "在 docs/releases/ 补齐当前版本的 source、package、claude、codex JSON 证据"),
    "B4": ("CHANGELOG 统一格式", "发版命令无法提取待发布说明。", "在根目录 CHANGELOG.md 添加 ## [Unreleased] 段"),
    "C2": ("统一本地检查入口", "本机、CI 和自动维护可能运行不同检查。", "添加 scripts/validate.sh，并支持 --quick"),
    "C3": ("临时 git 仓库关闭后台维护", "后台维护锁会造成测试偶发失败。", "测试创建 git 仓库后设置 maintenance.auto=false 和 gc.auto=0"),
    "C4": ("Python 3.9 与新版本兼容", "用户本机的 Python 可能无法运行插件。", "在名为 CI 的 workflow 同时测试 Python 3.9 和一个更新版本"),
    "C5": ("Bash 3.2 兼容", "新 Bash 语法在 macOS 自带 Bash 上会失败。", "将 shell 脚本中的 Bash 4 语法改为 Bash 3.2 兼容写法"),
    "C6": ("Actions 引用钉完整 SHA", "可移动的标签不能保证运行审过的代码。", "将外部 uses 引用钉为 40 位小写十六进制提交 SHA"),
    "D1": ("插件放在 plugins/<名>/", "目录不统一，发版和检查无法按统一路径发现插件。", "将插件放在 plugins/<名>/；haigeerlab/module-crew 按规范例外保留"),
    "D2": ("Claude 和 Codex 两份清单", "缺少清单会让两侧安装内容无法共同管理。", "为每个插件补齐 .claude-plugin/plugin.json 和 .codex-plugin/plugin.json"),
    "D3": ("测试文件统一命名", "测试发现可能漏掉命名不一致的文件。", "将 tests/ 下的 Python 测试命名为 test_*.py"),
    "E1": ("interface.json 两段版本", "接口版本格式不统一，调用方无法判断兼容性。", "将 interface.json 的 interface 或 version 字段写为主.次两段数字字符串"),
    "F2": ("PR 模板", "审查者无法按固定位置找到验证和风险说明。", "添加 .github/pull_request_template.md 或 PULL_REQUEST_TEMPLATE 模板"),
    "G1": ("必备文档", "用户与开发会话缺少使用、安全或协作说明。", "补齐 README.md、README.en.md、CHANGELOG.md、SECURITY.md、LICENSE、CLAUDE.md、AGENTS.md"),
    "G2": ("共享段一致", "两种会话读到不同项目规则会产生冲突。", "在 CLAUDE.md 和 AGENTS.md 使用 shared:begin/end 标记，并保持其中内容逐字相同"),
    "H1": ("引用 hub 的版本统一", "不同基线的规则可能得出不同结果。", "将 workflow 中所有 hub uses 和 hub_ref 升至 hub 的 standard-target.json 指定基线"),
    "H2": ("风险配置使用 yml", "多种格式并存，风险规则容易维护遗漏。", "将 .github/autopr/risk-tiers.json 迁移为 risk-tiers.yml"),
    "H4": ("rules.json 合法对象", "规则配置读不懂，无法确定启用哪些规则。", "将 .github/autopr/rules.json 写为合法 JSON 对象；不配置则全部启用"),
}
for _number, (_title, _impact, _how) in REMINDERS.items():
    TITLES["规范" + _number] = _title
    IMPACTS["规范" + _number] = _impact
    HOW["规范" + _number] = _how

DEFAULT_TARGET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "standard-target.json")
COPYRIGHT = "Copyright (c) 2026 haigeerlab"
MIT_MARKERS = ("MIT License", "Permission is hereby granted, free of charge")
MANIFESTS = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json")
SKIP_DIRS = {".git", ".claude", ".hub", "node_modules", ".venv", "venv", "__pycache__"}


def _finding(fid, status, detail, evidence=None):
    fix = None if status == "ok" else {"can_generate": False, "how": HOW[fid]}
    return {"id": fid, "title": TITLES[fid], "scope": "仓库", "status": status, "detail": detail,
            "impact": IMPACTS[fid], "fix": fix, "evidence": evidence or [], "skipped": False}


MAX_FILE_BYTES = 1024 * 1024


def _read(root, rel):
    """Read bounded regular files only, including checking directory symlinks."""
    try:
        path = os.path.abspath(os.path.join(root, rel))
        parent = path
        while True:
            info = os.lstat(parent)
            if stat.S_ISLNK(info.st_mode):
                return None
            if parent == path and (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES):
                return None
            ancestor = os.path.dirname(parent)
            if parent == os.path.abspath(root) or ancestor == parent:
                break
            parent = ancestor
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                return None
            data = fh.read(MAX_FILE_BYTES + 1)
        return data.decode("utf-8") if len(data) <= MAX_FILE_BYTES else None
    except (OSError, ValueError, UnicodeError):
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
    unreadable = []
    for rel in _files(root):
        if any(rel == m or rel.endswith("/" + m) for m in MANIFESTS):
            try:
                data = json.loads(_read(root, rel) or "")
            except (ValueError, RecursionError):
                unreadable.append(rel)
                continue
            if isinstance(data, dict) and "license" in data and data["license"] != "MIT":
                problems.append("%s 的 license 是 %r，应为 MIT" % (rel, data["license"]))
    if unreadable:
        return _result("A3", problems, "", unreadable)
    return _finding("规范A3", "missing" if problems else "ok", "；".join(problems) or "MIT，版权行一致")


def _workflows(root):
    out = []
    for rel in _files(root):
        if rel.startswith(".github/workflows/") and rel.count("/") == 2 and rel.endswith((".yml", ".yaml")):
            text = _read(root, rel)
            try:
                doc = yamlsub.loads(text) if text is not None else None
            except (ValueError, RecursionError):
                doc = None
            out.append((rel, doc if isinstance(doc, dict) else None, text or ""))
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
    except (ValueError, RecursionError) as e:
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


def _files(root):
    if os.path.lexists(os.path.join(root, ".git")):
        try:
            result = subprocess.run(["git", "-C", root, "ls-files", "-z"], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=5, check=False)
            if result.returncode == 0:
                paths = result.stdout.decode("utf-8").split("\0")
                for rel in sorted(set(paths)):
                    if rel and not any(part in SKIP_DIRS for part in rel.split("/")[:-1]):
                        yield rel
                return
        except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
            pass
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(directory, d)))
        for name in sorted(files):
            yield os.path.relpath(os.path.join(directory, name), root).replace(os.sep, "/")


def _result(number, problems, success, unreadable=()):
    status = "missing" if problems else "unknown" if unreadable else "ok"
    detail = "；".join(list(problems) + ["读不了或已跳过（仅读取不超过 1 MiB 的普通文件）：" + p for p in unreadable]) or success
    return _finding("规范" + number, status, detail)


def b2(root):
    problems = []
    if not os.path.isfile(os.path.join(root, "scripts/release.py")):
        problems.append("缺少 scripts/release.py")
    rel = ".github/autopr/interface.yml"
    if not os.path.exists(os.path.join(root, rel)):
        return _result("B2", problems + ["缺少 " + rel], "")
    text = _read(root, rel)
    if text is None:
        return _result("B2", problems, "", [rel])
    try:
        doc = yamlsub.loads(text)
    except (ValueError, RecursionError):
        return _result("B2", problems, "", [rel])
    entries = doc.get("entries") if isinstance(doc, dict) else None
    if not isinstance(entries, dict) or "release_prepare" not in entries:
        problems.append("interface.yml 的 entries 没有登记 release_prepare")
    return _result("B2", problems, "发版命令与 release_prepare 入口齐全")


def b3(root):
    if not any(is_declaration(rel) for rel in _files(root)):
        return _finding("规范B3", "ok", "不是插件仓库")
    declarations, errors = {}, []
    files = set(_files(root))
    for rel in sorted(files):
        if not is_declaration(rel):
            continue
        try:
            doc = json.loads(_read(root, rel) or "")
        except (ValueError, RecursionError):
            errors.append(rel)
            continue
        if not isinstance(doc, dict):
            errors.append(rel)
            continue
        entries = doc.get("plugins", []) if rel.endswith("marketplace.json") else [doc]
        if not isinstance(entries, list):
            errors.append(rel)
            continue
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            name = entry["name"]
            if name in seen:
                errors.append(rel)
            seen.add(name)
            if "version" in entry:
                declarations.setdefault(name, []).append(entry["version"])
    if errors or len(declarations) != 1:
        return _finding("规范B3", "unknown", "没有唯一且版本一致的插件清单，判断不了当前版本")
    values = next(iter(declarations.values()))
    if not values or not all(isinstance(v, str) and v == values[0] for v in values):
        return _finding("规范B3", "unknown", "没有唯一且版本一致的插件清单，判断不了当前版本")
    version = values[0]
    if not isinstance(version, str) or not version or "/" in version or "\\" in version:
        return _finding("规范B3", "unknown", "插件版本号无效，判断不了证据路径")
    required = ["docs/releases/v%s-%s.json" % (version, kind) for kind in ("source", "package", "claude", "codex")]
    missing = [p for p in required if p not in files or _read(root, p) is None]
    return _result("B3", missing, "当前版本 %s 的四份发版证据齐全" % version)


def b4(root):
    text = _read(root, "CHANGELOG.md")
    if text is None and os.path.exists(os.path.join(root, "CHANGELOG.md")):
        return _result("B4", [], "", ["CHANGELOG.md"])
    valid = text is not None and any(line.startswith("## [Unreleased]") for line in text.splitlines())
    return _result("B4", [] if valid else ["CHANGELOG.md 缺少 ## [Unreleased] 段"], "CHANGELOG.md 有 Unreleased 段")


def c2(root):
    rel = "scripts/validate.sh"
    text = _read(root, rel)
    if text is None and os.path.exists(os.path.join(root, rel)):
        return _result("C2", [], "", [rel])
    return _result("C2", [] if text is not None and "--quick" in text else ["缺少 scripts/validate.sh 或 --quick 快档"], "本地检查入口包含 --quick")


def c3(root):
    texts, unreadable, created = [], [], []
    for rel in _files(root):
        is_test = "tests" in rel.split("/")[:-1] or os.path.basename(rel).startswith("test_")
        is_settings = rel == "scripts/validate.sh" or (rel.startswith(".github/workflows/") and rel.endswith((".yml", ".yaml")))
        if not (is_test or is_settings):
            continue
        text = _read(root, rel)
        if text is None:
            unreadable.append(rel)
            continue
        texts.append(text)
        if is_test and any("git init" in line or ("git" in line and re.search(r'''["']init["']''', line)) for line in text.splitlines()):
            created.append(rel)
    combined = "\n".join(texts)
    missing = [key for key in ("maintenance.auto", "gc.auto") if key not in combined] if created else []
    problems = ["测试创建 git 仓库但没有设置 %s（%s）" % (", ".join(missing), "、".join(created))] if missing and not unreadable else []
    return _result("C3", problems, "测试已关闭后台维护" if created else "测试没有创建 git 仓库", unreadable)


def c4(root):
    if not any(rel.endswith(".py") for rel in _files(root)):
        return _finding("规范C4", "ok", "没有 Python 文件")
    ci = _ci(root)
    if ci is None:
        return _finding("规范C4", "unknown", "没有名为 CI 的 workflow（见规范C1）")
    workflows = {rel: (doc, text) for rel, doc, text in _workflows(root)}
    pending, visited, versions, old, unreadable = [ci[0]], set(), set(), False, []
    while pending:
        rel = pending.pop()
        if rel in visited:
            continue
        visited.add(rel)
        doc, text = workflows.get(rel, (None, ""))
        if doc is None:
            unreadable.append(rel)
            continue
        jobs = doc.get("jobs")
        for job in jobs.values() if isinstance(jobs, dict) else ():
            if not isinstance(job, dict):
                continue
            uses = job.get("uses")
            if isinstance(uses, str) and uses.startswith("./.github/workflows/"):
                pending.append(uses[2:])
            for _, value in _values(job, {"python-version", "matrix"}):
                candidates = [value]
                while candidates:
                    candidate = candidates.pop()
                    if isinstance(candidate, dict):
                        candidates.extend(candidate.values())
                    elif isinstance(candidate, list):
                        candidates.extend(candidate)
                    elif isinstance(candidate, (str, int, float)):
                        versions.update(re.findall(r"(?<![\w.])3\.[0-9]+(?:\.[0-9]+)?(?![\w.])", str(candidate)))
            runner = job.get("runs-on")
            macos = isinstance(runner, str) and runner.startswith("macos-")
            if isinstance(runner, str) and runner.startswith("${{"):
                matrix = (job.get("strategy") or {}).get("matrix")
                macos = any(isinstance(v, str) and v.startswith("macos-") for _, v in _values(matrix, {"os"}))
                if isinstance(matrix, dict) and isinstance(matrix.get("os"), list):
                    macos = any(isinstance(v, str) and v.startswith("macos-") for v in matrix["os"])
            if macos:
                old = old or "/usr/bin/python3" in text or bool(re.search(
                    r'''\bPATH\s*(?:=|:)\s*["']?/usr/bin(?=[:\s"';]|$)''', text))
    old = old or any(v.split(".")[:2] == ["3", "9"] for v in versions)
    new = any(10 <= int(v.split(".")[1]) <= 19 for v in versions)
    if old and new:
        return _result("C4", [], "CI 覆盖 Python 3.9 与更新版本")
    if unreadable or not versions:
        return _finding("规范C4", "unknown", "CI 的 Python 版本覆盖判断不了" + ("；读不了：" + "、".join(unreadable) if unreadable else ""))
    return _result("C4", [ci[0] + " 没有同时覆盖 Python 3.9 和一个更新版本"], "")


def c5(root):
    pattern = re.compile(r"\b(?:mapfile|readarray)\b|\bdeclare\s+-A\b|\$\{[A-Za-z_][A-Za-z_0-9]*(?:,,|\^\^)\}")
    problems, unreadable = [], []
    for rel in _files(root):
        if not rel.endswith(".sh"):
            continue
        text = _read(root, rel)
        if text is None:
            unreadable.append(rel)
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if not line.lstrip().startswith("#") and pattern.search(line):
                problems.append("%s:%s 使用 Bash 4 语法" % (rel, n))
    return _result("C5", problems, "shell 脚本未发现指定的 Bash 4 语法", unreadable)


def _values(doc, keys):
    if isinstance(doc, dict):
        for key, value in doc.items():
            if key in keys:
                yield key, value
            yield from _values(value, keys)
    elif isinstance(doc, list):
        for value in doc:
            yield from _values(value, keys)


def c6(root):
    problems, unreadable = [], []
    for rel, doc, _ in _workflows(root):
        if doc is None:
            unreadable.append(rel)
            continue
        for _, ref in _values(doc, {"uses"}):
            if isinstance(ref, str) and ref.startswith(("./", "docker://")):
                continue
            if not isinstance(ref, str) or not re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", ref):
                problems.append("%s: uses: %s" % (rel, ref))
    return _result("C6", problems, "外部 Actions 引用均钉完整 SHA", unreadable)


def _plugin_dirs(root):
    return sorted({rel.rsplit("/", 2)[0] if rel.count("/") >= 2 else ""
                   for rel in _files(root) if any(rel == m or rel.endswith("/" + m) for m in MANIFESTS)})


def d1(root, repo=None):
    manifests = [rel for rel in _files(root) if rel == MANIFESTS[0] or rel.endswith("/" + MANIFESTS[0])]
    if not manifests:
        return _finding("规范D1", "ok", "不是插件仓库")
    if not repo:
        try:
            remote = subprocess.run(["git", "-C", root, "remote", "-v"], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, errors="replace", timeout=5, check=False).stdout
            if re.search(r"github\.com[:/]haigeerlab/module-crew(?:\.git)?(?:\s|/|$)", remote):
                repo = "haigeerlab/module-crew"
        except (OSError, subprocess.TimeoutExpired):
            pass
    if repo == "haigeerlab/module-crew":
        return _finding("规范D1", "ok", "规范例外：haigeerlab/module-crew 保留根目录插件")
    problems = [rel for rel in manifests if not re.fullmatch(r"plugins/[^/]+/\.claude-plugin/plugin\.json", rel)]
    return _result("D1", problems, "插件均位于 plugins/<名>/")


def d2(root):
    plugins = _plugin_dirs(root)
    files = set(_files(root))
    problems = [os.path.join(plugin, manifest).replace(os.sep, "/") for plugin in plugins for manifest in MANIFESTS
                if os.path.join(plugin, manifest).replace(os.sep, "/").lstrip("/") not in files]
    return _result("D2", problems, "两份插件清单齐全" if plugins else "不是插件仓库")


def d3(root):
    problems, unreadable = [], []
    for rel in _files(root):
        if "tests" not in rel.split("/")[:-1] or not rel.endswith(".py") or os.path.basename(rel).startswith("test_"):
            continue
        text = _read(root, rel)
        if text is None:
            unreadable.append(rel)
            continue
        if re.search(r"^[ \t]*(?:async[ \t]+)?def[ \t]+test_", text, re.MULTILINE):
            problems.append(rel)
    return _result("D3", problems, "tests/ 下的测试文件命名符合规范", unreadable)


def e1(root):
    problems, unreadable, found = [], [], False
    for plugin in _plugin_dirs(root):
        rel = os.path.join(plugin, "interface.json").replace(os.sep, "/")
        if not os.path.exists(os.path.join(root, rel)):
            continue
        found = True
        text = _read(root, rel)
        if text is None:
            unreadable.append(rel)
            continue
        try:
            doc = json.loads(text)
        except (ValueError, RecursionError):
            doc = None
        version = doc.get("interface", doc.get("version")) if isinstance(doc, dict) else None
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", version):
            problems.append(rel + " 的接口版本不是主.次两段数字")
    return _result("E1", problems, "接口版本格式正确" if found else "没有对外接口文件", unreadable)


def f2(root):
    present = any(rel.lower() in {"pull_request_template.md", "docs/pull_request_template.md", ".github/pull_request_template.md"}
                  or rel.startswith(".github/PULL_REQUEST_TEMPLATE/") for rel in _files(root))
    if not os.path.lexists(os.path.join(root, ".git")):
        present = present or os.path.isdir(os.path.join(root, ".github/PULL_REQUEST_TEMPLATE"))
    return _result("F2", [] if present else ["没有 PR 模板"], "已有 PR 模板")


def g1(root):
    required = ("README.md", "README.en.md", "CHANGELOG.md", "SECURITY.md", "LICENSE", "CLAUDE.md", "AGENTS.md")
    return _result("G1", [p for p in required if not os.path.isfile(os.path.join(root, p))], "必备文档齐全")


def g2(root):
    sections, problems = [], []
    for rel in ("CLAUDE.md", "AGENTS.md"):
        text = _read(root, rel)
        if text is None:
            return _finding("规范G2", "unknown", rel + " 缺失或读不了（缺文件由规范G1报告）")
        matches = re.findall(r"<!-- shared:begin -->(.*?)<!-- shared:end -->", text, re.DOTALL)
        if not matches:
            problems.append(rel + " 没有完整共享段标记")
        else:
            sections.append(matches)
    if len(sections) == 2 and sections[0] != sections[1]:
        problems.append("CLAUDE.md 和 AGENTS.md 的共享段不一致")
    return _result("G2", problems, "两份共享段逐字一致")


def h1(root, target_path=None, repo=None):
    if repo == "haigeerlab/autopilot-hub":
        return _finding("规范H1", "ok", "hub 本身，不适用")
    path = target_path if target_path is not None else DEFAULT_TARGET
    try:
        target = json.loads(_read(os.path.dirname(os.path.abspath(path)), os.path.basename(path)) or "")
    except (OSError, ValueError, RecursionError):
        target = None
    sha = target.get("hub_target") if isinstance(target, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return _finding("规范H1", "unknown", "standard-target.json 读不了或 hub_target 无效")
    problems, unreadable, found = [], [], False
    for rel, doc, _ in _workflows(root):
        if doc is None:
            unreadable.append(rel)
            continue
        for key, value in _values(doc, {"uses", "with"}):
            if key == "uses":
                if not isinstance(value, str) or not value.startswith(("haigeerlab/autopilot-hub/", "haigeerlab/autopilot-hub@")):
                    continue
                ref = value.rsplit("@", 1)[1] if "@" in value else None
            else:
                if not isinstance(value, dict):
                    continue
                refs = [("hub_ref", value.get("hub_ref"))]
                if value.get("repository") == "haigeerlab/autopilot-hub":
                    refs.append(("ref", value.get("ref")))
                for ref_key, ref_value in refs:
                    if not isinstance(ref_value, str) or ref_value.startswith("${{"):
                        continue
                    found = True
                    if ref_value != sha:
                        problems.append("%s: %s: %s（目标 %s）" % (rel, ref_key, ref_value, sha))
                continue
            found = True
            if ref != sha:
                problems.append("%s: %s: %s（目标 %s）" % (rel, key, value, sha))
    return _result("H1", problems, "hub 引用均为目标基线" if found else "没有接入 hub workflow", unreadable)


def h2(root):
    if os.path.isfile(os.path.join(root, ".github/autopr/risk-tiers.yml")):
        return _finding("规范H2", "ok", "风险配置使用 risk-tiers.yml")
    if os.path.exists(os.path.join(root, ".github/autopr/risk-tiers.json")):
        return _finding("规范H2", "missing", "只有 risk-tiers.json，应使用 risk-tiers.yml")
    return _finding("规范H2", "ok", "没有风险配置（缺失由体检 C11 报告）")


def h4(root):
    rel = ".github/autopr/rules.json"
    if not os.path.exists(os.path.join(root, rel)):
        return _finding("规范H4", "ok", "没有 rules.json，按全部启用")
    text = _read(root, rel)
    if text is None:
        return _result("H4", [], "", [rel])
    try:
        doc = json.loads(text.lstrip("\ufeff"))
    except (ValueError, RecursionError):
        doc = None
    return _result("H4", [] if isinstance(doc, dict) else [rel + " 不是合法 JSON 对象"], "rules.json 是合法 JSON 对象")


def findings(root, gh, repo, branch, target_path=None):
    checks = [(a3, (root,)), (b2, (root,)), (b3, (root,)), (b4, (root,)),
              (c1, (root, gh, repo, branch or "main")), (c2, (root,)), (c3, (root,)),
              (c4, (root,)), (c5, (root,)), (c6, (root,)), (c9, (root,)), (c10, (root,)),
              (d1, (root, repo)), (d2, (root,)), (d3, (root,)), (e1, (root,)),
              (f2, (root,)), (g1, (root,)), (g2, (root,)), (h1, (root, target_path, repo)),
              (h2, (root,)), (h4, (root,))]
    numbers = ("A3", "B2", "B3", "B4", "C1", "C2", "C3", "C4", "C5", "C6", "C9", "C10",
               "D1", "D2", "D3", "E1", "F2", "G1", "G2", "H1", "H2", "H4")
    results = []
    for number, (check, args) in zip(numbers, checks):
        try:
            results.append(check(*args))
        except Exception as error:
            results.append(_finding("规范" + number, "unknown", "体检项出错：" + type(error).__name__))
    return results
