"""Risk tiers for a PR's changed files, with the rule that decided each tier.

Spec: spec/hub-auto-merge.md (R4). Stdlib only; supports Python 3.9.
Reads only PR metadata and patches, never runs PR code.

Per file:
- Protective rules (T3/T4) from every layer -- common, category template, repo additions -- always
  count, and the highest wins: a repo can add or tighten, never lower them.
- Otherwise the lowest matching T0-T2 rule applies (the most specific ordinary area, e.g. a test
  file under src/ is T1, not T2).
- No rule at all -> the category default (T2 when the repo declares no category).
- spec-guard artifacts: tasks/** whose change is only checkbox ticks, and spec/** whose change is
  only `<!-- spec-guard: ... -->` marker lines, are T1; content changes use ordinary rules.
PR tier = highest file tier, then bumps: >N non-test lines +1, >M files +1, dependency files at
least T3. Capability map changes, unverified spec-guard content changes, net assertion deletions,
net new suppressions, and unreadable patches that prevent these checks require a human without
changing the tier.
T0-T2 are eligible for auto-merge when no human approval is required; T4 sets stop.
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


META_KEYS = ("extends", "version")


def repo_rules(config, notes):
    """{tier: [globs]} from the repo config. `T4_paths` is read as `T4` (older spelling); any other
    key that is not a tier, `extends`, `version` or `$...` is reported, so a typo never silently
    drops a repo's protective paths."""
    rules = {}
    for key, value in config.items():
        tier = key[:-len("_paths")] if key.endswith("_paths") else key
        if tier in TIERS:
            if key != tier:
                notes.append(f"旧键名 {key} 按 {tier} 处理，建议改名")
            if isinstance(value, list):
                rules.setdefault(tier, []).extend(value)
            else:
                notes.append(f"{key} 不是列表，已忽略")
        elif key not in META_KEYS and not key.startswith("$"):
            notes.append(f"不认识的键名 {key}，已忽略（风险配置只认 extends 和 T0–T4）")
    return rules


def layers(templates, config, notes):
    """[(label, {tier: [globs]})] and the default tier for unmatched files."""
    out = [("共用规则", templates["common"])]
    category, default = None, "T2"
    if isinstance(config, dict):
        name = config.get("extends")
        if name in templates["categories"]:
            category = name
            cat = templates["categories"][name]
            default = cat.get("default", default)
            out.append((f"{name} 模板", {t: cat.get(t, []) for t in TIERS}))
        elif name is not None:
            notes.append(f"未知类别 {name!r}，按通用处理")
        repo = repo_rules(config, notes)
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


def spec_guard_tier(kind, patch):
    """T1 for mechanical spec-guard changes, or None to use ordinary rules."""
    removed, added = changed_lines(patch)
    if not (removed or added):
        return None
    if kind == "tasks":
        ticks_only = sorted(CHECKBOX.sub("[ ]", x) for x in removed) == sorted(CHECKBOX.sub("[ ]", x) for x in added)
        return ("T1", "tasks/** 只勾选 todo") if ticks_only else None
    if kind == "spec" and all(SPEC_GUARD_MARK.match(x) for x in removed + added):
        return "T1", "spec/** 只改批准标记"
    return None


CAPABILITY_MAP = "spec/CAPABILITY-MAP.md"


def file_tier(layer_list, default, path, mechanical):
    protective, ordinary = [], []
    for label, rules in layer_list:
        for t in TIERS:
            hit = any_match(rules.get(t, []), path)
            if hit:
                (protective if rank(t) >= 3 else ordinary).append((t, f"{label} {hit} → {t}"))
    if protective:
        return max(protective, key=lambda x: rank(x[0]))
    if mechanical:
        return mechanical
    if ordinary:
        return min(ordinary, key=lambda x: rank(x[0]))
    return default, f"没有规则匹配，按默认 {default}"


def is_prose(templates, name):
    """Docs mention suppression markers when describing rules; they are not suppressions."""
    return any_match(templates.get("prose_files", []), name) is not None


def count_markers(lines, markers):
    return sum(1 for line in lines for m in markers if m in line)


def classify(files, templates, config=None, verified=()):
    """Classify a PR under R4. `verified` contains paths with still-valid approval under slice 12."""
    verified = set(verified or ())
    notes, reasons, human_reasons, per_file = [], [], [], []
    layer_list, category, default = layers(templates, config, notes)
    if not files:
        return {"tier": "T0", "reasons": ["没有改动文件"], "auto_merge_eligible": True, "stop": False,
                "human_only": False, "human_reasons": [],
                "category": category, "files": [], "notes": notes}
    spec_guard = templates["spec_guard"]
    for f in files:
        path = f["filename"]
        kind = next((kind for kind in ("tasks", "spec") if any_match(spec_guard[kind], path)), None)
        mechanical = spec_guard_tier(kind, f.get("patch")) if kind else None
        t, why = file_tier(layer_list, default, path, mechanical)
        per_file.append({"file": path, "tier": t, "rule": why})
        if path == CAPABILITY_MAP:
            human_reasons.append("能力图改动要用户批")
        elif kind and not mechanical and path not in verified:
            human_reasons.append(f"spec-guard 产物没核对到有效的批准：{path}")
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

    weakened, missing_patches = [], []
    for f in files:
        patch = f.get("patch")
        if is_test(f["filename"]):
            if patch is None:
                if (f.get("deletions") or 0) > 0:
                    missing_patches.append(f["filename"])
                continue
            removed, added = changed_lines(patch)
            mk = templates["assertion_markers"]
            if count_markers(removed, mk) > count_markers(added, mk):
                weakened.append(f["filename"])
        elif patch is None and ((f.get("additions") or 0) > 0):
            missing_patches.append(f["filename"])
    if weakened:
        human_reasons.append("删除或削弱了测试断言：" + "、".join(weakened))

    removed_all, added_all = [], []
    for f in files:
        if is_prose(templates, f["filename"]):
            continue
        r_, a_ = changed_lines(f.get("patch"))
        removed_all += r_
        added_all += a_
    sm = templates["suppression_markers"]
    if count_markers(added_all, sm) > count_markers(removed_all, sm):
        human_reasons.append("压制注释净增加")
    if missing_patches:
        human_reasons.append("补丁读不到，无法检查测试断言或压制注释：" + "、".join(missing_patches))

    human_only = bool(human_reasons)
    return {"tier": tier, "reasons": reasons, "human_only": human_only, "human_reasons": human_reasons,
            "auto_merge_eligible": rank(tier) <= 2 and not human_only, "stop": tier == "T4",
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
    for g in templates["dependency_files"]:
        if g not in out["T3"]:
            out["T3"].append(g)
    return out


REPO_CONFIGS = (".github/autopr/risk-tiers.yml", ".github/autopr/risk-tiers.json")  # .yml wins (slice 7)
RISK_PATHS_FILE = ".github/autopr/risk-paths.json"  # what spec-guard 0.91 reads; written by the checkup (C13)


def risk_paths_text(templates, config=None):
    """Canonical text of risk-paths.json: exactly what `risk/cli.py paths --json` prints."""
    return json.dumps(risk_paths(templates, config), ensure_ascii=False, indent=1) + "\n"


def read_repo_config_at(root):
    """The repo's risk config from a checkout: .yml first, then .json; None when neither exists."""
    yml, js = (os.path.join(root, rel) for rel in REPO_CONFIGS)
    if os.path.exists(yml):
        return read_repo_config_yml(yml)
    return read_repo_config(js)


def read_repo_config(path):
    """Config dict, None when absent; raises ValueError when present but not valid JSON object."""
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("risk-tiers.json 必须是一个 JSON 对象")
    return data


def read_repo_config_yml(path):
    """Like read_repo_config for risk-tiers.yml (YAML subset); raises ValueError when unreadable."""
    if not path or not os.path.exists(path):
        return None
    from checkup import yamlsub  # local: keeps checkup out of the import path of plain classify callers
    data = yamlsub.load(path)
    if not isinstance(data, dict):
        raise ValueError("risk-tiers.yml 必须是一个映射")
    return data
