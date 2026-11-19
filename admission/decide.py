"""Slice-1 admission: compute a PR's trust cap from a repo-readiness conclusion (report-only).

Spec: docs/spec/slice-1.md §4. Every failing step fails closed to L0.
Stdlib only; supports Python 3.9.
"""
import datetime as dt
import json
import re

LEVELS = ("L0", "L1", "L2", "L3", "L4")
STEP_IDS = ("exists", "schema", "repo", "valid_until", "tool", "app_cap", "limits")
ATTESTATION_NOTE = "发布证明未校验：attestation 机制尚未定（repo-readiness 接口清单 #14）"
DEGRADE_BLOCK = re.compile(r"<!--\s*autopr-degrade:v1\s+(.*?)\s*-->", re.S)


def rank(level):
    return LEVELS.index(level)


def lowest(levels):
    return min(levels, key=rank)


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


def validate(value, schema, path="$"):
    """Minimal JSON Schema subset: type, required, properties, items, enum, pattern, minLength, format=date-time."""
    errors = []
    expected = schema.get("type")
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
    }
    if expected and not checks[expected](value):
        return [f"{path}: 应为 {expected}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: 取值 {value!r} 不在 {schema['enum']}")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: 不符合格式 {schema['pattern']}")
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: 长度不足")
        if schema.get("format") == "date-time" and parse_time(value) is None:
            errors.append(f"{path}: 不是带时区的 RFC 3339 时间")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: 缺少必填字段")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors.extend(validate(value[key], sub, f"{path}.{key}"))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
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
    """Levels from open `autopr:degrade` issues scoped to this repo. Unparseable blocks fail closed to L0."""
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
                and entry.get("sha") == tool.get("sha")):
            return True
    return False


def app_cap(conclusion, changed_files):
    """Cap for the changed files: one app -> that app's cap; several -> lowest; any unmapped path -> repo cap joins."""
    repo_cap = conclusion["trust_cap"]
    apps = conclusion.get("apps") or []
    if not changed_files:
        return repo_cap, "没有改动文件，按仓库级上限"
    caps, owners = [], set()
    for path in changed_files:
        owner = next((a for a in apps if any(glob_match(p, path) for p in a["paths"])), None)
        if owner is None:
            caps.append(repo_cap)
            owners.add("（仓库级）")
        else:
            caps.append(owner["trust_cap"])
            owners.add(owner["id"])
    return lowest(caps), "涉及：" + "、".join(sorted(owners))


def trust_limit(trust):
    """Slice 1 computes no risk tier, so the strictest configured level applies."""
    if not isinstance(trust, dict):
        return None
    levels = [v for v in (trust.get("levels") or {}).values() if v in LEVELS]
    return lowest(levels) if levels else None


def decide(*, conclusion, schema, repo, default_branch, now, allowed_tools,
           changed_files, trust, degrade_levels, attestation_note=True):
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
        return fail("tool", f"工具 repo-readiness {tool.get('version')}@{tool.get('sha')} 不在允许列表里")
    ok("tool", f"工具 repo-readiness {tool['version']}@{tool['sha'][:12]} 在允许列表里")
    if attestation_note:
        notes.append(ATTESTATION_NOTE)

    cap, scope = app_cap(conclusion, changed_files)
    ok("app_cap", f"结论给出的上限 {cap}（{scope}）")

    limits = [cap]
    limit_notes = []
    t = trust_limit(trust)
    if t is not None:
        limits.append(t)
        limit_notes.append(f"trust.yml 最严档 {t}")
    if degrade_levels:
        limits.append(lowest(degrade_levels))
        limit_notes.append(f"未关闭的降级 issue 最低 {lowest(degrade_levels)}")
    final = lowest(limits)
    ok("limits", "；".join(limit_notes) if limit_notes else "没有额外限制")

    reason = f"信任上限 {final}（只报告）"
    if final != cap:
        reason += "，被" + ("trust" if t is not None and t == final else "降级") + "限制"
    return finish(final, reason)
