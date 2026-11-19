"""Command-line entry for slice-1 admission. Reads local files, writes a JSON decision and a Markdown summary.

GitHub I/O (fetching the conclusion from the base branch, listing degrade issues, posting the check run,
label and comment) is done by the reusable workflow with `gh`, so this module stays pure and testable.

Exit codes: 0 = a decision was written (any cap, including L0); 2 = bad invocation or unreadable config.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admission import decide as d  # noqa: E402

MARKER = "<!-- autopilot-admission -->"


def read_json(path, *, optional):
    if not path or not os.path.exists(path):
        if optional:
            return None
        raise FileNotFoundError(path)
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except ValueError:
            # A file that exists but is not JSON is a broken input, not a missing one: fail closed downstream.
            return "unparseable"


def sha256_of(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def render_markdown(result, conclusion_sha):
    lines = [MARKER, f"### autopilot/admission：{result['reason']}", ""]
    lines.append("| 步骤 | 结果 | 说明 |")
    lines.append("| --- | --- | --- |")
    for step in result["steps"]:
        lines.append(f"| {step['id']} | {'✅' if step['ok'] else '❌'} | {step['detail']} |")
    lines.append("")
    if conclusion_sha:
        lines.append(f"接入结论文件 sha256：`{conclusion_sha}`")
    for note in result["notes"]:
        lines.append(f"- 注意：{note}")
    lines.append("")
    lines.append("本检查只报告信任上限，不拦截、不合并（切片 1，见 autopilot-hub `docs/spec/slice-1.md`）。")
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description="autopilot slice-1 admission (report-only)")
    p.add_argument("--conclusion", required=True, help="path to readiness-conclusion.json from the BASE branch (may be absent)")
    p.add_argument("--schema", required=True)
    p.add_argument("--allowed-tools", required=True)
    p.add_argument("--trust", help="optional .github/autopr/trust.yml (JSON-compatible YAML only)")
    p.add_argument("--degrade-issues", help="optional JSON array of open autopilot:degrade issues ({body: ...})")
    p.add_argument("--changed-files", help="optional file with one changed path per line")
    p.add_argument("--repo", required=True)
    p.add_argument("--default-branch", required=True)
    p.add_argument("--now", help="RFC 3339 time for tests; defaults to current UTC time")
    p.add_argument("--out-json", required=True)
    p.add_argument("--out-md", required=True)
    a = p.parse_args(argv)

    try:
        schema = read_json(a.schema, optional=False)
    except FileNotFoundError as e:
        print(f"admission: schema not found: {e}", file=sys.stderr)
        return 2
    if not isinstance(schema, dict):
        print("admission: schema is not valid JSON", file=sys.stderr)
        return 2

    conclusion = read_json(a.conclusion, optional=True)
    if conclusion == "unparseable":
        conclusion = {"__unparseable__": True}  # fails schema validation -> L0 with a format reason
    allowed = read_json(a.allowed_tools, optional=True)
    allowed = allowed if isinstance(allowed, dict) else None
    trust = read_json(a.trust, optional=True)
    trust_unreadable = trust is not None and not isinstance(trust, dict)
    if trust_unreadable:
        # Present but unreadable (non-JSON YAML or broken): fail closed instead of silently ignoring the limit.
        trust = {"levels": {"unreadable": "L0"}}
    elif not isinstance(trust, dict):
        trust = None
    issues = read_json(a.degrade_issues, optional=True)
    issues = issues if isinstance(issues, list) else []
    changed = []
    if a.changed_files and os.path.exists(a.changed_files):
        with open(a.changed_files, encoding="utf-8") as f:
            changed = [line.strip() for line in f if line.strip()]

    now = d.parse_time(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
    if now is None:
        print("admission: --now is not an RFC 3339 time with a time zone", file=sys.stderr)
        return 2

    result = d.decide(
        conclusion=conclusion, schema=schema, repo=a.repo, default_branch=a.default_branch, now=now,
        allowed_tools=allowed, changed_files=changed, trust=trust,
        degrade_levels=d.degrade_levels(issues, a.repo),
    )
    if trust_unreadable:
        result["notes"].append("trust.yml 读不懂（切片 1 只支持与 JSON 兼容的 YAML），按 L0 处理")
    result["conclusion_sha256"] = sha256_of(a.conclusion)
    with open(a.out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    with open(a.out_md, "w", encoding="utf-8") as f:
        f.write(render_markdown(result, result["conclusion_sha256"]))
    print(f"admission: cap={result['cap']} report_only=true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
