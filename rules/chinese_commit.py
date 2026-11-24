"""Rule `chinese-commit`: PR title, body and every commit message must contain Chinese.

Spec: docs/spec/slice-6.md (A13 of lint-starter).
Input JSON: {"title": str, "body": str, "commits": [str], "author": str | null}.
author is an optional GitHub PR author login; exactly `dependabot[bot]` exempts the title, body and all commits.
Before looking for CJK characters the text is stripped of conventional prefixes (`fix:`, `feat(x):`),
backticked spans, URLs and file paths, so identifiers cannot count as Chinese. Merge commits are exempt;
an empty PR body is not a violation. A repo opts out (open-source English) with
`.github/autopr/rules.json` {"chinese_commit": false}, passed via --config; absent = enabled.
Exit codes: 0 pass or opted out, 1 violation, 2 bad input.
"""
import argparse
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rules.common import emit, load_json, render

RULE = "chinese-commit"
PREFIX = re.compile(r"\A\s*(?:revert\s*:?\s*)?[A-Za-z]+(?:\([^)]*\))?!?\s*:\s*")
CODE_SPAN = re.compile(r"`[^`]*`")
FENCE = re.compile(r"```.*?```", re.S)
URL = re.compile(r"https?://\S+")
PATH = re.compile(r"[A-Za-z0-9_.~@-][\w.~@-]*(?:/[\w.~@-]*)+")
CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
MERGE = re.compile(r"\A\s*Merge (?:pull request|branch|remote-tracking branch|tag)\b")
TRAILER = re.compile(r"^\s*(?:Co-Authored-By|Signed-off-by):.*$", re.I | re.M)


def has_chinese(text):
    text = FENCE.sub(" ", text or "")
    text = CODE_SPAN.sub(" ", text)
    text = URL.sub(" ", text)
    text = TRAILER.sub(" ", text)
    text = PATH.sub(" ", PREFIX.sub("", text, count=1))
    return CJK.search(text) is not None


def check(data):
    if data.get("author") == "dependabot[bot]":
        return []
    errors = []

    def bad(where, text):
        shown = (text or "").strip().splitlines()[0][:80] if (text or "").strip() else ""
        errors.append(render(RULE, f"{where}没有中文：{shown}", "仓库约定提交和 PR 用中文写，读的人才能一眼看懂改了什么、为什么",
                             "用中文写清改了什么、为什么（`fix:`、`feat:` 前缀、路径和代码标识符可以保留）",
                             "不要只写英文，也不要只靠前缀、路径和反引号里的标识符凑数"))

    if not has_chinese(data.get("title")):
        bad("PR 标题", data.get("title"))
    body = data.get("body")
    if body and body.strip() and not has_chinese(body):
        bad("PR 正文", body)
    for message in data.get("commits") or []:
        if MERGE.match(message or ""):
            continue
        if not has_chinese(message):
            bad("提交说明", message)
    return errors


def main(argv=None):
    p = argparse.ArgumentParser(description="autopilot rule: chinese-commit")
    p.add_argument("--input", required=True)
    p.add_argument("--config", help="path of .github/autopr/rules.json; absent file = rule enabled")
    p.add_argument("--out-md")
    a = p.parse_args(argv)
    try:
        if a.config and os.path.exists(a.config):
            cfg = load_json(a.config)
            if not isinstance(cfg, dict):
                raise ValueError("rules.json 必须是 JSON 对象")
            if cfg.get("chinese_commit") is False:
                return emit([], a.out_md, f"{RULE}（本仓库在 rules.json 里声明跳过）")
        data = load_json(a.input)
        if not isinstance(data, dict):
            raise ValueError("--input 必须是 JSON 对象")
    except (OSError, ValueError) as e:
        print(f"{RULE}: 输入有问题：{e}", file=sys.stderr)
        return 2
    return emit(check(data), a.out_md, RULE)


if __name__ == "__main__":
    sys.exit(main())
