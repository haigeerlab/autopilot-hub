"""Plugin standard A1: no real home-directory paths or the local user name in new or changed lines (card).

Spec: docs/plugin-standard.md (A1). Flags `/Users/<name>/`, `/home/<name>/` and the Claude project-dir
encoding `-Users-<name>-` where <name> is letters, digits, `.`, `_`, `-`, unless <name> is on the closed
placeholder list below. Off CI the current login name is also searched, case-insensitive, as a whole word.
Only added lines are checked (a new file is all added lines). Exit codes: 0 pass, 1 violation, 2 bad input.

    python3 -B rules/local_paths.py (--pr-files files.json | --diff <file|->) [--out-md out.md]
"""
import argparse
import getpass
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rules.diffs import added_lines, files_from_diff, load_files, unchecked  # noqa: F401 (re-exported)

RULE = "local-paths"
SPEC = "docs/plugin-standard.md（A1）"
# Closed list from the standard; adding or removing a name goes through PR review.
PLACEHOLDERS = frozenset(("me", "<user>", "someone", "example", "alice", "bob", "x", "u", "private", "fixture"))
# The check itself and its fixtures, exempt by exact path (the standard allows this, file by file).
EXEMPT_FILES = frozenset(("rules/local_paths.py", "tests/test_rules_standard_a.py"))
NAME = r"[A-Za-z0-9._-]+"
HOME = re.compile(r"/(?:Users|home)/(" + NAME + r")/")
ENCODED = re.compile(r"(?<![A-Za-z0-9])-(?:Users|home)-([A-Za-z0-9._]+)-")
GENERIC_LOGINS = frozenset(("root", "runner", "user", "admin"))


def login_pattern(login):
    """Whole-word, case-insensitive search for the login; None when it is too short or generic to mean anything."""
    if not login or len(login) < 4 or login.lower() in GENERIC_LOGINS:
        return None
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(login) + r"(?![A-Za-z0-9])", re.I)


def check(files, login=None):
    """[{file, line, what}] for every offending added line."""
    by_login = login_pattern(login)
    out = []
    for f in files:
        name = f.get("filename") or ""
        if name in EXEMPT_FILES or f.get("status") == "removed":
            continue
        for line, text in added_lines(f):
            hits = [m.group(0) for rx in (HOME, ENCODED) for m in rx.finditer(text) if m.group(1) not in PLACEHOLDERS]
            if by_login and by_login.search(text):
                hits.append("本机登录名")
            if hits:
                out.append({"file": name, "line": line, "what": "、".join(hits)})
    return out


def render(found, skipped):
    lines = []
    if found:
        lines.append(f"{RULE}: {len(found)} 处新增内容带有本机家目录路径或本机用户名")
        lines += [f"- {x['file']}:{x['line']}：{x['what']}" for x in found]
        lines += ["为什么：公开仓库一旦带上本机用户名就撤不回来（S107）。",
                  "改成：`~/...`、`$HOME/...`、`<repo>/...`，或占位名（me、someone、example、alice、bob 等）。",
                  "不要：把真实路径换个大小写或拆开写来绕过检查。",
                  f"详见：{SPEC}"]
    else:
        lines.append(f"{RULE}: 通过")
    if skipped:
        lines.append("没有补丁、没法检查的文件（二进制或过大）：" + "、".join(skipped))
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description="插件开发规范 A1：本机路径和用户名不进仓库")
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
    login = None
    if not os.environ.get("GITHUB_ACTIONS"):
        try:
            login = getpass.getuser()
        except (KeyError, OSError):
            login = None
    found = check(files, login)
    text = render(found, unchecked(files))
    print(text)
    if a.out_md:
        with open(a.out_md, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
