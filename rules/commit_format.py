"""Plugin standard F1: commit subject is `type(scope)!: 说明` and the description is Chinese (card).

Spec: docs/plugin-standard.md (F1). Only the first line of each commit message counts. Types: feat, fix, docs,
chore, test, ci, refactor. Chinese is judged like rules/chinese_commit.py (prefix, backticks, URLs and paths do not
count). Passes: commits with two or more parents, and git's standard `Revert "…"` subject. Release commits get no
exemption. The PR title/body check stays in chinese_commit.py and is not part of this card.
Exit codes: 0 pass, 1 violation, 2 bad input.

    python3 -B rules/commit_format.py (--input commits.json | --message-file <file>) [--out-md out.md]
    commits.json: {"commits": [{"message": str, "parents": int}, ...]} (plain strings are read as one parent)
"""
import argparse
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rules.chinese_commit import has_chinese

RULE = "commit-format"
SPEC = "docs/plugin-standard.md（F1）"
TYPES = ("feat", "fix", "docs", "chore", "test", "ci", "refactor")
SUBJECT = re.compile(r"\A(?:%s)(?:\([^()\s][^()]*\))?!?: \S" % "|".join(TYPES))
REVERT = re.compile(r'\ARevert ".+"\Z')


def subject(message):
    return (message or "").strip("\n").split("\n", 1)[0].rstrip()


def problem(message, parents=1):
    """Why this commit fails F1, or None."""
    if parents >= 2:
        return None
    first = subject(message)
    if REVERT.match(first):
        return None
    if not SUBJECT.match(first):
        return "第一行要是「类型(可选范围): 说明」，类型限 " + "/".join(TYPES)
    if not has_chinese(first):
        return "第一行的说明里没有中文（前缀、反引号内容、路径和网址不算）"
    return None


def check(commits):
    errors = []
    for c in commits or []:
        message, parents = (c, 1) if isinstance(c, str) else (c.get("message"), int(c.get("parents") or 1))
        why = problem(message, parents)
        if why:
            errors.append(f"{subject(message)[:80]!r}：{why}")
    return errors


def render(errors):
    if not errors:
        return f"{RULE}: 通过"
    return "\n".join([f"{RULE}: {len(errors)} 条提交说明不合规"] + [f"- {e}" for e in errors] + [
        "为什么：用户读提交历史要一眼看懂改了什么；类型前缀让发版说明能按类型归类。",
        "改成：例如 `fix(rules): 修正误报`、`chore(release): 发布 v0.94.0`；改历史提交用 `git commit --amend` 或交互变基。",
        "不要：只写英文，或用前缀、路径、反引号里的标识符凑中文。",
        f"详见：{SPEC}"])


def main(argv=None):
    p = argparse.ArgumentParser(description="插件开发规范 F1：提交说明中文加类型前缀")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help='{"commits": [{"message", "parents"}]} 的 JSON')
    src.add_argument("--message-file", help="单条提交说明（commit-msg 钩子用）")
    p.add_argument("--out-md")
    a = p.parse_args(argv)
    try:
        if a.input:
            with open(a.input, encoding="utf-8") as fh:
                data = json.load(fh)
            commits = data.get("commits") if isinstance(data, dict) else None
            if not isinstance(commits, list):
                raise ValueError("--input 要有 commits 列表")
        else:
            with open(a.message_file, encoding="utf-8") as fh:
                lines = [x for x in fh.read().splitlines() if not x.startswith("#")]
            commits = ["\n".join(lines)]
    except (OSError, ValueError) as e:
        print(f"{RULE}: 输入读不了：{e}", file=sys.stderr)
        return 2
    errors = check(commits)
    text = render(errors)
    print(text)
    if a.out_md:
        with open(a.out_md, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
