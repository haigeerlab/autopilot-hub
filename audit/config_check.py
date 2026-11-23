"""Slice 18a: configuration side of the compliance audit (research audit-spec M3).

Every check reads a throwaway clone of the repository's default branch, compares it with the approved
baseline and returns problems: {"kind", "severity": "严重" | "提醒", "detail"}. Severe problems are the
ones that widen what AI may do without the user (M6: report only, the user decides).
"""
import json
import os
import re

from checkup import yamlsub

ORDER = {"spec": ("human", "codex"), "plan": ("human", "codex"), "merge": ("human", "auto"),
         "release": ("human", "prepare", "auto-confirm", "auto")}
PRESETS = {"none": {}, "plan": {"plan": "codex"}, "merge": {"spec": "codex", "plan": "codex", "merge": "auto"}}
CALLERS = (".github/workflows/autopilot-admission.yml", ".github/workflows/autopilot-degrade.yml")
USES = re.compile(r"uses:\s*haigeerlab/autopilot-hub/\S+@([0-9a-f]{40})")
HUB_REF = re.compile(r"hub_ref:\s*([0-9a-f]{40})")
DEGRADE_BLOCK = re.compile(r"<!--\s*autopilot-degrade:v1\s+(.*?)\s*-->", re.S)


def problem(kind, severity, detail):
    return {"kind": kind, "severity": severity, "detail": detail}


def items_of(value):
    """All four items of a takeover value (missing items are human), or None when it is not valid."""
    if isinstance(value, str):
        chosen = PRESETS.get(value)
    elif isinstance(value, dict) and all(k in ORDER and v in ORDER[k] for k, v in value.items()):
        chosen = value
    else:
        chosen = None
    if chosen is None:
        return None
    return {k: chosen.get(k, "human") for k in ORDER}


def wider(current, base):
    """[(item, base value, current value)] for every item where current lets AI do more than base."""
    return [(k, base[k], current[k]) for k in ORDER if ORDER[k].index(current[k]) > ORDER[k].index(base[k])]


def _read(root, rel):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def read_takeover(root):
    """{"project": value, "modules": {id: value}} from a checkout, resolved the way spec-guard 0.92 does."""
    if _read(root, "spec/CAPABILITY-MAP.md") is not None:
        try:
            config = json.loads(_read(root, ".agent/config.json") or "{}")
        except ValueError:
            config = {}
        config = config if isinstance(config, dict) else {}
        modules = config.get("moduleTakeover", {})
        if not isinstance(modules, dict) or any(items_of(v) is None for v in modules.values()):
            modules = {}  # spec-guard: one bad entry invalidates the whole key
        return {"project": config.get("takeover", "none"), "modules": dict(modules)}
    text = _read(root, ".github/autopr/takeover.yml")
    if text is None:
        return {"project": "none", "modules": {}}
    try:
        data = yamlsub.loads(text)
    except ValueError:
        data = None
    data = data if isinstance(data, dict) else {}
    return {"project": {k: data[k] for k in ("merge", "release") if k in data}, "modules": {}}


def check_takeover(current, baseline):
    base = baseline["takeover"]
    base_project = items_of(base.get("project"))
    out = []
    now_project = items_of(current.get("project"))
    if now_project is None:
        return [problem("takeover-unreadable", "严重", "接管档位读不懂：%r（按人批处理，但请核对）" % (current.get("project"),))]
    for item, was, now in wider(now_project, base_project):
        out.append(problem("takeover-wider", "严重", "项目接管档位 %s 从 %s 放宽到 %s" % (item, was, now)))
    modules = set(current.get("modules", {})) | set(base.get("modules", {}))
    for module in sorted(modules):
        now = items_of(current["modules"][module]) if module in current.get("modules", {}) else now_project
        was = items_of(base["modules"][module]) if module in base.get("modules", {}) else base_project
        if now is None:
            out.append(problem("takeover-unreadable", "严重", "模块 %s 的接管档位读不懂" % module))
            continue
        for item, before, after in wider(now, was):
            out.append(problem("takeover-wider", "严重", "模块 %s 的接管档位 %s 从 %s 放宽到 %s" % (module, item, before, after)))
    return out


def read_pins(root):
    """{caller file: [every pinned SHA found]}; a missing file maps to None."""
    pins = {}
    for rel in CALLERS:
        text = _read(root, rel)
        pins[rel] = None if text is None else USES.findall(text) + HUB_REF.findall(text)
    return pins


def check_pins(pins, baseline):
    if baseline["hub_pin"] is None:  # registered without caller files: they must stay absent until re-registered
        return [problem("pin-unregistered", "提醒", "%s 是基线之后新加的，钉 %s；请更新基线" % (
            rel, "、".join(sorted({s[:12] for s in found})) or "（无）")) for rel, found in pins.items() if found is not None]
    out = []
    for rel, found in pins.items():
        if not found:
            out.append(problem("pin-missing", "提醒", "%s 不存在或没有钉 autopilot-hub" % rel))
            continue
        bad = sorted(set(found) - {baseline["hub_pin"]})
        if bad:
            out.append(problem("pin-changed", "提醒", "%s 钉的是 %s，基线是 %s" % (
                rel, "、".join(s[:12] for s in bad), baseline["hub_pin"][:12])))
    return out


def check_checkup(statuses, baseline):
    """Only checks that were ok in the baseline and are not ok now count."""
    return [problem("checkup-regressed", "提醒", "%s：基线是 ok，现在是 %s" % (cid, statuses.get(cid, "缺失")))
            for cid, was in sorted(baseline.get("checkup", {}).items(), key=lambda kv: int(kv[0][1:]))
            if was == "ok" and statuses.get(cid) != "ok"]


def check_degrade(issues, repo):
    numbers = []
    for issue in issues:
        for raw in DEGRADE_BLOCK.findall(issue.get("body") or ""):
            try:
                scope = json.loads(raw).get("scope", {}).get("repo")
            except (ValueError, AttributeError):
                scope = repo
            if scope == repo:
                numbers.append(issue.get("number"))
                break
    if not numbers:
        return []
    return [problem("degrade-open", "提醒", "有未关闭的降级 issue：" + "、".join("#%s" % n for n in numbers))]
