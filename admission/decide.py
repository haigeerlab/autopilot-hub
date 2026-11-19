"""Slice-1 admission: compute a PR's trust cap from a repo-readiness conclusion (report-only).

Spec: docs/spec/slice-1.md §4. Every failing step fails closed to L0.
Stdlib only; supports Python 3.9.
"""
import datetime as dt
import json
import re

LEVELS = ("L0", "L1", "L2", "L3", "L4", "L5")
STEP_IDS = ("exists", "schema", "repo", "valid_until", "tool", "app_cap", "limits")
ATTESTATION_NOTE = "发布证明未校验：attestation 机制尚未定（repo-readiness 接口清单 #14）"
LEVELS_BLOCK = re.compile(r"<!--\s*autopilot-levels:v1\s+(.*?)\s*-->", re.S)
DEFAULT_PLUGIN_LEVEL = "L2"
DEGRADE_BLOCK = re.compile(r"<!--\s*autopilot-degrade:v1\s+(.*?)\s*-->", re.S)


def rank(level):
    return LEVELS.index(level)


def lowest(levels):
    return min(levels, key=rank)


def as_level(value):
    """Conclusion caps are free strings or null (the tool copies them from the ruler): anything else is L0."""
    return value if value in LEVELS else "L0"


def parse_time(value):
    """RFC 3339 timestamp -> aware datetime, or None if it is not one."""
    if not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "null": lambda v: v is None,
}


def parse_date(value):
    try:
        return dt.date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        return None


def validate(value, schema, path="$", root=None):
    """JSON Schema subset used by repo-readiness conclusion.v1: type (incl. lists and null), const, enum,
    required, properties, additionalProperties=false, items, minItems, pattern, minLength,
    format date-time / date, and local $ref to #/$defs/<name>. Unknown keywords are ignored."""
    root = schema if root is None else root
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if not schema["$ref"].startswith("#/$defs/") or name not in root.get("$defs", {}):
            return [f"{path}: 无法解析的 $ref {schema['$ref']}"]
        return validate(value, root["$defs"][name], path, root)
    errors = []
    expected = schema.get("type")
    if expected:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(TYPE_CHECKS[t](value) for t in allowed):
            return [f"{path}: 应为 {'/'.join(allowed)}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: 应为 {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: 取值 {value!r} 不在 {schema['enum']}")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: 不符合格式 {schema['pattern']}")
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: 长度不足")
        if schema.get("format") == "date-time" and parse_time(value) is None:
            errors.append(f"{path}: 不是带时区的 RFC 3339 时间")
        if schema.get("format") == "date" and parse_date(value) is None:
            errors.append(f"{path}: 不是 YYYY-MM-DD 日期")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: 缺少必填字段")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                errors.extend(validate(value[key], sub, f"{path}.{key}", root))
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}.{key}: 不允许的字段")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: 至少 {schema['minItems']} 项")
        if "items" in schema:
            for i, item in enumerate(value):
                errors.extend(validate(item, schema["items"], f"{path}[{i}]", root))
    return errors


def glob_match(pattern, path):
    """Path glob: `**` crosses directories, `*` and `?` stay within one segment."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(out), path) is not None


def degrade_levels(issues, repo):
    """Levels from open `autopilot:degrade` issues scoped to this repo. Unparseable blocks fail closed to L0."""
    found = []
    for issue in issues:
        for block in DEGRADE_BLOCK.findall(issue.get("body") or ""):
            try:
                payload = json.loads(block)
                scope_repo = payload["scope"]["repo"]
                level = payload["level"]
            except (ValueError, KeyError, TypeError):
                found.append("L0")
                continue
            if scope_repo != repo:
                continue
            found.append(level if level in LEVELS else "L0")
    return found


def tool_allowed(tool, allowed):
    if not isinstance(allowed, dict):
        return False
    for entry in allowed.get("tools", []):
        if (entry.get("name") == "repo-readiness"
                and entry.get("version") == tool.get("version")
                and entry.get("commit") == tool.get("commit")):
            return True
    return False


def app_cap(conclusion, changed_files):
    """Cap for the changed files. A file belongs to every app whose paths match it (all their caps apply);
    a file no app claims gets the repo cap. The result is the lowest cap involved. Null or non-level caps are L0."""
    repo_cap = as_level(conclusion["trust_cap"])
    apps = conclusion.get("apps") or []
    if not changed_files:
        return repo_cap, "没有改动文件，按仓库级上限"
    caps, owners = [], set()
    for path in changed_files:
        matched = [a for a in apps if any(glob_match(p, path) for p in a["paths"])]
        if not matched:
            caps.append(repo_cap)
            owners.add("（仓库级）")
        for app in matched:
            caps.append(as_level(app["trust_cap"]))
            owners.add(app["id"])
    return lowest(caps), "涉及：" + "、".join(sorted(owners))


PLUGIN_SOURCES = (
    # (label, key in the levels block, field holding the tier)
    ("spec-guard 模块档位", "spec_guard", "tier"),
    ("module-crew 生效档位", "module_crew", "effective"),
)


def plugin_limits(comments, tier_map):
    """Limits from the latest `autopilot-levels:v1` block in the PR comments, written by the local orchestrator.

    Missing block or missing source -> L2 (design §3.2). Unparseable latest block or unknown tier -> L0.
    Returns [{"source", "level", "detail"}]. The comment author is not verified yet (agent identity, design Q2).
    """
    tiers = tier_map["tiers"]
    blocks = [b for c in comments for b in LEVELS_BLOCK.findall(c or "")]
    if not blocks:
        return [{"source": label, "level": DEFAULT_PLUGIN_LEVEL, "detail": f"缺少等级评论，{label}按 {DEFAULT_PLUGIN_LEVEL}"}
                for label, _, _ in PLUGIN_SOURCES]
    try:
        payload = json.loads(blocks[-1])
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        return [{"source": label, "level": "L0", "detail": f"等级评论读不懂，{label}按 L0"} for label, _, _ in PLUGIN_SOURCES]
    out = []
    for label, key, field in PLUGIN_SOURCES:
        entry = payload.get(key)
        tier = entry.get(field) if isinstance(entry, dict) else None
        if tier is None:
            out.append({"source": label, "level": DEFAULT_PLUGIN_LEVEL, "detail": f"缺少{label}，按 {DEFAULT_PLUGIN_LEVEL}"})
        elif tier in tiers and tiers[tier] in LEVELS:
            out.append({"source": label, "level": tiers[tier], "detail": f"{label} {tier} → {tiers[tier]}"})
        else:
            out.append({"source": label, "level": "L0", "detail": f"{label} {tier!r} 不认识，按 L0"})
    return out


def valid_tier_map(tier_map):
    return (isinstance(tier_map, dict) and isinstance(tier_map.get("tiers"), dict) and tier_map["tiers"]
            and all(v in LEVELS for v in tier_map["tiers"].values()))


def decide(*, conclusion, schema, repo, default_branch, now, allowed_tools,
           changed_files, plugin_limits, degrade_levels, attestation_note=True):
    steps, notes = [], []

    def finish(cap, reason):
        return {"cap": cap, "report_only": True, "reason": reason, "steps": steps, "notes": notes}

    def fail(step_id, detail):
        steps.append({"id": step_id, "ok": False, "detail": detail})
        return finish("L0", f"信任上限 L0（只报告）：{detail}")

    def ok(step_id, detail):
        steps.append({"id": step_id, "ok": True, "detail": detail})

    if not isinstance(conclusion, dict):
        return fail("exists", "没有接入结论（.github/autopr/readiness-conclusion.json）")
    ok("exists", "找到接入结论")

    errors = validate(conclusion, schema)
    if errors:
        return fail("schema", "接入结论格式不合法：" + "；".join(errors[:5]))
    ok("schema", "格式校验通过")

    if conclusion["repo"] != repo:
        return fail("repo", f"结论针对的仓库是 {conclusion['repo']}，不是 {repo}")
    if conclusion["default_branch"] != default_branch:
        return fail("repo", f"结论里的默认分支是 {conclusion['default_branch']}，实际是 {default_branch}")
    ok("repo", f"仓库 {repo}、默认分支 {default_branch} 一致")

    valid_until = parse_time(conclusion["valid_until"])
    if now >= valid_until:
        return fail("valid_until", f"接入结论已于 {conclusion['valid_until']} 过期")
    ok("valid_until", f"有效期至 {conclusion['valid_until']}")

    tool = conclusion["basis"]["tool"]
    if not tool_allowed(tool, allowed_tools):
        return fail("tool", f"工具 repo-readiness {tool.get('version')}@{tool.get('commit')} 不在允许列表里")
    ok("tool", f"工具 repo-readiness {tool['version']}@{tool['commit'][:12]} 在允许列表里")
    if attestation_note:
        notes.append(ATTESTATION_NOTE)

    cap, scope = app_cap(conclusion, changed_files)
    ok("app_cap", f"结论给出的上限 {cap}（{scope}）")

    limits = [("接入结论", cap)]
    limit_notes = []
    for item in plugin_limits:
        limits.append((item["source"], item["level"]))
        limit_notes.append(item["detail"])
    if degrade_levels:
        limits.append(("降级 issue", lowest(degrade_levels)))
        limit_notes.append(f"未关闭的降级 issue 最低 {lowest(degrade_levels)}")
    final = lowest([level for _, level in limits])
    ok("limits", "；".join(limit_notes) if limit_notes else "没有额外限制")

    reason = f"信任上限 {final}（只报告）"
    if final != cap:
        by = [name for name, level in limits[1:] if level == final]
        reason += "，被" + "、".join(by) + "限制"
    return finish(final, reason)
