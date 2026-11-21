"""Command-line entry for slice-7 admission. Reads local files, writes a JSON decision and a Markdown summary.

GitHub I/O (fetching risk config from the base branch, listing comments and degrade issues, posting the
check run, label and comment) is done by the reusable workflow with `gh`, so this module stays pure.

Exit codes: 0 = a decision was written; 2 = bad invocation or unreadable required input.
"""
import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admission import decide as d  # noqa: E402
from risk import tiers as risk_tiers  # noqa: E402

MARKER = "<!-- autopilot-admission -->"
HUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MISMATCH_NOTE = "risk-tiers.yml 和 .json 不一致，以 .yml 为准"


def read_json(path, *, optional):
    if not path or not os.path.exists(path):
        if optional:
            return None
        raise FileNotFoundError(path)
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except ValueError:
            return None


def load_risk_config(yml_path, json_path, notes):
    """.yml wins over .json; both present and different -> note. Unreadable config -> None (generic rules)."""
    yml_exists = bool(yml_path) and os.path.exists(yml_path)
    json_exists = bool(json_path) and os.path.exists(json_path)
    yml = js = None
    if yml_exists:
        try:
            yml = risk_tiers.read_repo_config_yml(yml_path)
        except (OSError, ValueError):
            notes.append("risk-tiers.yml 读不懂，按通用规则定风险档（没匹配到的路径算 T3）")
            return None
    if json_exists:
        try:
            js = risk_tiers.read_repo_config(json_path)
        except (OSError, ValueError):
            if not yml_exists:
                notes.append("risk-tiers.json 读不懂，按通用规则定风险档（没匹配到的路径算 T3）")
                return None
    if yml_exists:
        if json_exists and js is not None and js != yml:
            notes.append(MISMATCH_NOTE)
        return yml
    return js


def render_markdown(result):
    lines = [MARKER, f"### autopilot/admission：{result['headline']}", "", "| 项 | 结果 | 说明 |", "| --- | --- | --- |"]
    for item in result["items"]:
        if item["ok"]:
            mark = "✅"
        elif item["id"] == "risk" and result["risk"]["stop"]:
            mark = "⛔"
        else:
            mark = "—"
        lines.append(f"| {item['id']} | {mark} | {item['detail']} |")
    lines.append("")
    for note in result["notes"]:
        lines.append(f"- 注意：{note}")
    lines.append("")
    lines.append("本检查只报告，不拦截、不合并（切片 7，见 autopilot-hub `docs/spec/slice-7.md`）。")
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description="autopilot slice-7 admission (report-only)")
    p.add_argument("--pr-files", required=True,
                   help="JSON array of PR files ({filename, status, additions, deletions, patch})")
    p.add_argument("--comments", help="optional JSON array of PR comments, oldest first: REST objects with "
                   "body, user {login, id, type}, app (performed_via_github_app.slug), created_at, updated_at")
    p.add_argument("--bot-identity", default=os.path.join(HUB_ROOT, "bot-identity.json"),
                   help="bot identity file (default: the hub's bot-identity.json); absent -> trust no comment")
    p.add_argument("--degrade-issues", help="optional JSON array of open autopilot:degrade issues ({number, body})")
    p.add_argument("--risk-config", help="risk-tiers config from the BASE branch; .yml/.yaml or .json by suffix")
    p.add_argument("--risk-config-yml", help="risk-tiers.yml from the BASE branch (wins over the .json)")
    p.add_argument("--risk-config-json", help="risk-tiers.json from the BASE branch")
    p.add_argument("--repo", required=True)
    p.add_argument("--default-branch", help="accepted for compatibility; not used")
    p.add_argument("--out-json", required=True)
    p.add_argument("--out-md", required=True)
    a = p.parse_args(argv)

    try:
        pr_files = read_json(a.pr_files, optional=False)
    except FileNotFoundError as e:
        print(f"admission: pr files not found: {e}", file=sys.stderr)
        return 2
    if not isinstance(pr_files, list):
        print("admission: --pr-files is not a JSON array", file=sys.stderr)
        return 2
    pr_files = [x for x in pr_files if isinstance(x, dict) and "filename" in x]

    comments = read_json(a.comments, optional=True)
    comments = [c for c in comments if isinstance(c, (str, dict))] if isinstance(comments, list) else []
    issues = read_json(a.degrade_issues, optional=True)
    issues = [i for i in issues if isinstance(i, dict)] if isinstance(issues, list) else []

    yml_path, json_path = a.risk_config_yml, a.risk_config_json
    if a.risk_config:
        if a.risk_config.endswith((".yml", ".yaml")):
            yml_path = yml_path or a.risk_config
        else:
            json_path = json_path or a.risk_config
    config_notes = []
    risk_config = load_risk_config(yml_path, json_path, config_notes)

    risk = risk_tiers.classify(pr_files, risk_tiers.load_templates(), risk_config)
    risk["notes"] = config_notes + risk["notes"]
    result = d.decide(risk=risk, comments=comments, degrade_issues=issues, repo=a.repo,
                      bot=d.load_bot_identity(a.bot_identity))
    result["risk"] = risk
    with open(a.out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    with open(a.out_md, "w", encoding="utf-8") as f:
        f.write(render_markdown(result))
    print(f"admission: auto_merge={str(result['auto_merge']).lower()} report_only=true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
