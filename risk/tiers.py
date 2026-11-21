"""Slice-4 risk tiers: changed files of a PR -> T0..T4, with the rule that decided it.

Spec: docs/spec/slice-4.md. Stdlib only; supports Python 3.9. Reads only PR metadata and patches,
never runs PR code.

Per file:
- Protective rules (T3/T4) from every layer -- common, category template, repo additions -- always
  count, and the highest wins: a repo can add or tighten, never lower them.
- Otherwise the lowest matching T0-T2 rule applies (the most specific ordinary area, e.g. a test
  file under src/ is T1, not T2).
- No rule at all -> the category default (T3 when the repo declares no category).
- spec-guard artifacts: tasks/** whose change is only checkbox ticks, and spec/** whose change is
  only `<!-- spec-guard: ... -->` marker lines, are T1; any other change to them is T4.
PR tier = highest file tier, then bumps: >N non-test lines +1, >M files +1, dependency files at
least T3, weakened assertions T4, more suppressions T4, unreadable patches at least T3 (T4 for
tests with deletions). T0-T2 are eligible for auto-merge; T4 stops for a human.
"""
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admission.decide import glob_match  # noqa: E402

TIERS = ("T0", "T1", "T2", "T3", "T4")
TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates.json")
CHECKBOX = re.compile(r"\[[xX ]\]")
SPEC_GUARD_MARK = re.compile(r"^\s*<!--\s*spec-guard:.*-->\s*$")


def load_templates(path=TEMPLATES):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def rank(tier):
    return TIERS.index(tier)


def bump(tier, n):
    return TIERS[min(rank(tier) + n, len(TIERS) - 1)]


def at_least(tier, floor):
    return tier if rank(tier) >= rank(floor) else floor


def any_match(patterns, path):
    return next((p for p in patterns if glob_match(p, path)), None)


def layers(templates, config, notes):
    """[(label, {tier: [globs]})] and the default tier for unmatched files."""
    out = [("共用规则", templates["common"])]
    category, default = None, "T3"
    if isinstance(config, dict) and config.get("extends") is not None:
        name = config.get("extends")
        if name in templates["categories"]:
            category = name
            cat = templates["categories"][name]
            default = cat.get("default", "T3")
            out.append((f"{name} 模板", {t: cat.get(t, []) for t in TIERS}))
        else:
            notes.append(f"未知类别 {name!r}，按通用处理")
        repo = {t: config.get(t, []) for t in TIERS if isinstance(config.get(t), list)}
        if repo:
            out.append(("本仓库", repo))
    return out, category, default


def changed_lines(patch):
    removed, added = [], []
    for line in (patch or "").splitlines():
        if line.startswith("@@"):
            continue
        if line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
    return removed, added


def spec_guard_tier(templates, f):
    """T1/T4 for spec-guard artifacts, or None when the file is not one."""
    path, patch = f["filename"], f.get("patch")
    sg = templates["spec_guard"]
    if any_match(sg["tasks"], path):
        removed, added = changed_lines(patch)
        ticks_only = patch is not None and sorted(CHECKBOX.sub("[ ]", x) for x in removed) == \
            sorted(CHECKBOX.sub("[ ]", x) for x in added) and (removed or added)
        return ("T1", "tasks/** 只勾选 todo") if ticks_only else ("T4", "tasks/** 内容改动")
    if any_match(sg["spec"], path):
        removed, added = changed_lines(patch)
        marks_only = patch is not None and (removed or added) and all(SPEC_GUARD_MARK.match(x) for x in removed + added)
        return ("T1", "spec/** 只改批准标记") if marks_only else ("T4", "spec/** 内容改动（Spec 本身变了）")
    return None


def file_tier(templates, layer_list, default, f):
    path = f["filename"]
    sg = spec_guard_tier(templates, f)
    protective, ordinary = [], []
    for label, rules in layer_list:
        for t in TIERS:
            hit = any_match(rules.get(t, []), path)
            if hit:
                (protective if rank(t) >= 3 else ordinary).append((t, f"{label} {hit} → {t}"))
    if protective:
        return max(protective, key=lambda x: rank(x[0]))
    if sg:
        return sg[0], sg[1]
    if ordinary:
        return min(ordinary, key=lambda x: rank(x[0]))
    return default, f"没有规则匹配，按默认 {default}"


def is_prose(templates, name):
    """Docs mention markers like `# noqa` when describing rules; they are not suppressions."""
    return any_match(templates.get("prose_files", []), name) is not None


def count_markers(lines, markers):
    return sum(1 for line in lines for m in markers if m in line)


def classify(files, templates, config=None):
    notes, reasons, per_file = [], [], []
    layer_list, category, default = layers(templates, config, notes)
    if not files:
        return {"tier": "T0", "reasons": ["没有改动文件"], "auto_merge_eligible": True, "stop": False,
                "category": category, "files": [], "notes": notes}
    for f in files:
        t, why = file_tier(templates, layer_list, default, f)
        per_file.append({"file": f["filename"], "tier": t, "rule": why})
    top = max(per_file, key=lambda x: rank(x["tier"]))
    tier = top["tier"]
    reasons.append(f"最高的文件档 {tier}（{top['file']}：{top['rule']}）")

    tests = templates["test_files"]
    is_test = lambda name: any_match(tests, name) is not None  # noqa: E731
    non_test = [f for f in files if not is_test(f["filename"])]
    lines = sum((f.get("additions") or 0) + (f.get("deletions") or 0) for f in non_test)
    b = templates["bumps"]
    if lines > b["non_test_lines_over"]:
        tier = bump(tier, 1)
        reasons.append(f"非测试改动 {lines} 行，超过 {b['non_test_lines_over']}，升一档 → {tier}")
    if len(files) > b["files_over"]:
        tier = bump(tier, 1)
        reasons.append(f"改动 {len(files)} 个文件，超过 {b['files_over']}，升一档 → {tier}")

    deps = [f["filename"] for f in files if any_match(templates["dependency_files"], f["filename"])]
    if deps and rank(tier) < 3:
        tier = "T3"
        reasons.append(f"改了依赖文件 {', '.join(deps)}，至少 T3")

    weakened, missing_code = [], []
    for f in files:
        patch = f.get("patch")
        if is_test(f["filename"]):
            if patch is None:
                if (f.get("deletions") or 0) > 0:
                    weakened.append(f"{f['filename']}（补丁读不到，有删除行）")
                continue
            removed, added = changed_lines(patch)
            mk = templates["assertion_markers"]
            if count_markers(removed, mk) > count_markers(added, mk):
                weakened.append(f["filename"])
        elif patch is None and ((f.get("additions") or 0) > 0):
            missing_code.append(f["filename"])
    if weakened:
        tier = "T4"
        reasons.append("删除或削弱了测试断言：" + "、".join(weakened) + " → T4")

    removed_all, added_all = [], []
    for f in files:
        if is_prose(templates, f["filename"]):
            continue
        r_, a_ = changed_lines(f.get("patch"))
        removed_all += r_
        added_all += a_
    sm = templates["suppression_markers"]
    if count_markers(added_all, sm) > count_markers(removed_all, sm):
        tier = "T4"
        reasons.append("压制注释净增加 → T4")
    if missing_code and rank(tier) < 3:
        tier = "T3"
        reasons.append("补丁读不到，无法检查压制注释：" + "、".join(missing_code) + "，至少 T3")

    return {"tier": tier, "reasons": reasons, "auto_merge_eligible": rank(tier) <= 2, "stop": tier == "T4",
            "category": category, "files": per_file, "notes": notes}


def risk_paths(templates, config=None):
    notes = []
    layer_list, category, default = layers(templates, config, notes)
    out = {"schema": "autopilot.risk-paths/v1", "category": category, "default": default, "T4": [], "T3": [],
           "notes": notes}
    for _, rules in layer_list:
        for t in ("T4", "T3"):
            for g in rules.get(t, []):
                if g not in out[t]:
                    out[t].append(g)
    for g in templates["spec_guard"]["spec"] + templates["spec_guard"]["tasks"]:
        if g not in out["T4"]:
            out["T4"].append(g)
    for g in templates["dependency_files"]:
        if g not in out["T3"]:
            out["T3"].append(g)
    return out


def read_repo_config(path):
    """Config dict, None when absent; raises ValueError when present but not valid JSON object."""
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("risk-tiers.json 必须是一个 JSON 对象")
    return data
