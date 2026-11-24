"""Plugin standard A2: no well-formed secrets in new or changed lines (card).

Spec: docs/plugin-standard.md (A2). Only formats that are unambiguous are flagged; the pattern list is fixed
here and versioned. The only pass is an exact fake literal from ALLOWED_LITERALS: the word EXAMPLE elsewhere on
the line or in the file does not waive anything. Messages never repeat the secret, only its kind and place.
(Named no_secrets.py so running it as a script does not shadow the stdlib `secrets` module.)
Exit codes: 0 pass, 1 violation, 2 bad input.

    python3 -B rules/no_secrets.py (--pr-files files.json | --diff <file|->) [--out-md out.md]
"""
import argparse
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rules.diffs import added_lines, load_files, unchecked

RULE = "no-secrets"
SPEC = "docs/plugin-standard.md（A2）"
PATTERNS_VERSION = "1"
PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("github-token", re.compile(r"(?<![A-Za-z0-9_])gh[pos]_[A-Za-z0-9]{36}(?![A-Za-z0-9])")),
    ("github-pat", re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{60,}")),
    ("aws-access-key", re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])")),
    ("anthropic-key", re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"(?<![A-Za-z0-9_-])sk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
)
# Exact fake values that documentation may show. Anything else that matches a pattern is a violation.
ALLOWED_LITERALS = frozenset((
    "AKIA" + "IOSFODNN7EXAMPLE",                      # the AWS documentation's own example key id
    "gh" + "p_" + "EXAMPLE" + "0" * 29,               # 36 characters after the prefix
))


def check(files):
    """[{file, line, kind}] for every secret-shaped token on an added line (the token itself is not kept)."""
    out = []
    for f in files:
        if f.get("status") == "removed":
            continue
        for line, text in added_lines(f):
            for kind, rx in PATTERNS:
                if any(m.group(0) not in ALLOWED_LITERALS for m in rx.finditer(text)):
                    out.append({"file": f.get("filename"), "line": line, "kind": kind})
    return out


def render(found, skipped=()):
    lines = []
    if found:
        lines.append(f"{RULE}: {len(found)} 处新增内容像是真实密钥（模式清单第 {PATTERNS_VERSION} 版）")
        lines += [f"- {x['file']}:{x['line']}：{x['kind']}" for x in found]
        lines += ["为什么：密钥进了公开仓库，删掉提交也撤不回来，只能吊销重发。",
                  "改成：从环境变量或 GitHub Secrets 读；文档示例用 hub 名单里的精确假值。",
                  "不要：在行里加 EXAMPLE 字样来绕过；如果是真密钥，先吊销再改。",
                  f"详见：{SPEC}"]
    else:
        lines.append(f"{RULE}: 通过")
    if skipped:
        lines.append("没有补丁、没法检查的文件（二进制或过大）：" + "、".join(skipped))
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description="插件开发规范 A2：不提交密钥")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--pr-files", help="GitHub API 的 PR 文件列表 JSON")
    src.add_argument("--diff", help="git diff 的输出文件，- 表示标准输入")
    p.add_argument("--out-md")
    a = p.parse_args(argv)
    try:
        files = load_files(a.pr_files, a.diff)
    except (OSError, ValueError) as e:
        print(f"{RULE}: 输入读不了：{e}", file=sys.stderr)
        return 2
    found = check(files)
    text = render(found, unchecked(files))
    print(text)
    if a.out_md:
        with open(a.out_md, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
