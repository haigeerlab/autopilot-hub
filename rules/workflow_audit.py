"""Plugin standard C8, static half: new or changed workflows start from no permissions and drop checkout credentials.

Spec: docs/plugin-standard.md (C8). For every workflow file the PR adds or changes:
  - the top level says `permissions: {}` (each job asks for what it needs);
  - every `actions/checkout` step sets `persist-credentials: false`, unless it is `true` with a `#` comment on the
    same line giving the reason (a job that really has to push).
The zizmor half (high-severity findings) runs in rules.yml on the files listed by --list-changed.
Exit codes: 0 pass, 1 violation, 2 bad input.

    python3 -B rules/workflow_audit.py --pr-files files.json (--repo <head checkout> | --list-changed) [--out-md out.md]
"""
import argparse
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkup import yamlsub

RULE = "workflow-audit"
SPEC = "docs/plugin-standard.md（C8）"
WORKFLOW = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
KEEP_WITH_REASON = re.compile(r"persist-credentials:\s*true\s*#\s*\S")


def changed_workflows(files):
    return [f["filename"] for f in files if f.get("status") != "removed" and WORKFLOW.match(f.get("filename") or "")]


def _steps(doc):
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    for job_id, job in (jobs or {}).items() if isinstance(jobs, dict) else ():
        for step in (job or {}).get("steps") or [] if isinstance(job, dict) else ():
            if isinstance(step, dict):
                yield job_id, step


def check_file(rel, text):
    try:
        doc = yamlsub.loads(text)
    except yamlsub.YamlSubsetError as e:
        return [f"{rel}：读不懂（{e}）"]
    if not isinstance(doc, dict):
        return [f"{rel}：读不懂（顶层不是键值对）"]
    errors = []
    if "permissions" not in doc or doc["permissions"] != {}:
        errors.append(f"{rel}：顶层要写 `permissions: {{}}`，再按任务单独放权（现在是 {doc.get('permissions', '没写')!r}）")
    keep_lines = sum(1 for line in text.splitlines() if KEEP_WITH_REASON.search(line))
    for job_id, step in _steps(doc):
        uses = step.get("uses")
        if not (isinstance(uses, str) and uses.startswith("actions/checkout@")):
            continue
        value = (step.get("with") or {}).get("persist-credentials") if isinstance(step.get("with"), dict) else None
        if value is False:
            continue
        if value is True and keep_lines:
            keep_lines -= 1
            continue
        errors.append(f"{rel}：任务 {job_id} 的 actions/checkout 要设 `persist-credentials: false`"
                      "（确实要推送的，写 `true` 并在同一行用 # 注明原因）")
    return errors


def check(root, files):
    errors = []
    for rel in changed_workflows(files):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            errors.append(f"{rel}：读不了（{e}）")
            continue
        errors += check_file(rel, text)
    return errors


def render(errors):
    if not errors:
        return f"{RULE}: 通过"
    return "\n".join([f"{RULE}: {len(errors)} 个问题"] + [f"- {e}" for e in errors] + [
        "为什么：权限过宽、凭证留在磁盘上被后续步骤读走，钉 SHA 挡不住；CI 里有机器人私钥。",
        "改成：顶层 `permissions: {}`，任务里只给需要的权限；checkout 加 `with: persist-credentials: false`。",
        f"详见：{SPEC}"])


def main(argv=None):
    p = argparse.ArgumentParser(description="插件开发规范 C8：workflow 安全审计（静态部分）")
    p.add_argument("--pr-files", required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--repo", help="PR head 的检出（只读其中的 workflow 文件）")
    mode.add_argument("--list-changed", action="store_true", help="只打印本次新增或改动的 workflow 路径（给 zizmor 用）")
    p.add_argument("--out-md")
    a = p.parse_args(argv)
    try:
        with open(a.pr_files, encoding="utf-8") as fh:
            files = json.load(fh)
        if not isinstance(files, list):
            raise ValueError("--pr-files 要是文件列表")
    except (OSError, ValueError) as e:
        print(f"{RULE}: 输入读不了：{e}", file=sys.stderr)
        return 2
    if a.list_changed:
        print("\n".join(changed_workflows(files)))
        return 0
    errors = check(a.repo, files)
    text = render(errors)
    print(text)
    if a.out_md:
        with open(a.out_md, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
