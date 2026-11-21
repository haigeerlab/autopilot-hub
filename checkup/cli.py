"""Command line for slice-5 checkup (docs/spec/slice-5.md).

  python3 -B checkup/cli.py --repo <local checkout> [--base main] [--run-check] [--out <dir>] [--json]

Only reminds, never blocks. Exit codes: 0 checkup done (whatever the findings); 2 bad arguments or an
unreadable repository; 3 environment not ready (gh not logged in, network). Fixes are generated only,
never executed. Stdlib only; runs on Python 3.9+.
"""
import argparse
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkup import checks, entry, ghprobe, remedy  # noqa: E402
from checkup.proc import run  # noqa: E402

_REMOTE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
_MARK = {"ok": "有", "missing": "缺", "unknown": "判不了"}


def repo_name_from_remote(root):
    """'owner/name' of the GitHub remote (origin first), or None."""
    r = run(["git", "-C", root, "remote", "-v"], cwd=root, timeout_s=30)
    urls = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            urls.setdefault(parts[0], parts[1])
    for remote in sorted(urls, key=lambda n: (n != "origin", n)):
        m = _REMOTE.search(urls[remote])
        if m:
            return "%s/%s" % (m.group(1), m.group(2))
    return None


def render(result):
    lines = ["体检报告：%s" % (result["repo"] or result["path"]),
             "本地检出：%s；增量检查（C8–C10）对比基线：%s%s" % (
                 result["path"], result["base"], "；已运行检查命令（--run-check）" if result["run_check"] else ""),
             ""]
    for f in result["findings"]:
        tag = "未执行" if f["skipped"] else _MARK[f["status"]]
        lines.append("[%s] %s %s（%s）" % (tag, f["id"], f["title"], f["scope"]))
        lines.append("    现状：%s" % f["detail"])
        for e in f["evidence"]:
            lines.append("    · %s" % e)
        if f["fix"]:
            lines.append("    缺了会怎样：%s" % f["impact"])
            lines.append("    要不要帮你补：%s%s" % ("能。" if f["fix"]["can_generate"] else "不能代劳。", f["fix"]["how"]))
        lines.append("")
    if result["fixes"]:
        lines.append("可生成的补丁与命令（只生成，不执行）：")
        for fx in result["fixes"]:
            lines.append("  %s（%s）%s" % (fx["id"], fx["finding"], fx["title"]))
            for f in fx["files"]:
                lines.append("    文件：%s" % f)
            for c in fx["commands"]:
                lines.append("    命令：%s" % c)
            if fx["note"]:
                lines.append("    注意：%s" % fx["note"])
        if not result["out"]:
            lines.append("  （加 --out <目录> 才会把文件写出来）")
        else:
            lines.append("  已写到：%s" % result["out"])
        lines.append("")
    lines.append(result["conclusion"]["text"])
    return "\n".join(lines) + "\n"


def main(argv=None, transport=None, runner=entry.run_check, stdout=None, stderr=None):
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    p = argparse.ArgumentParser(description="autopilot 体检：只提醒，不卡接入")
    p.add_argument("--repo", required=True, help="本地检出的目录")
    p.add_argument("--base", default="main", help="增量检查的对比基线分支（默认 main）")
    p.add_argument("--run-check", action="store_true", help="在一次性克隆里真跑一遍 C1 的检查命令")
    p.add_argument("--out", help="补丁和命令写到这个目录；不给就只在报告里列出")
    p.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    a = p.parse_args(argv)  # bad arguments exit with code 2

    root = os.path.abspath(a.repo)
    if not os.path.isdir(root) or run(["git", "-C", root, "rev-parse", "--verify", "HEAD"],
                                      cwd=root if os.path.isdir(root) else "/", timeout_s=30).code != 0:
        print("checkup: %s 不是一个有提交的 git 检出" % a.repo, file=stderr)
        return 2
    root = run(["git", "-C", root, "rev-parse", "--show-toplevel"], cwd=root, timeout_s=30).stdout.strip() or root

    name = repo_name_from_remote(root)
    gh = ghprobe.GitHub(transport or ghprobe.LiveTransport()) if name else None
    try:
        findings, ctx = checks.run_checks(root, a.base, gh, name, a.run_check, runner)
    except ghprobe.EnvNotReady as e:
        print("checkup: 环境没准备好（gh 没登录或网络不通）：%s\n先运行 gh auth login，或检查网络后重试。" % e, file=stderr)
        return 3
    fixes = remedy.build_fixes(root, name, ctx["default_branch"] or a.base, findings, ctx["check_command"], a.out)
    concl = checks.conclusion(findings, a.run_check)
    result = {"repo": name, "path": root, "base": a.base, "run_check": a.run_check,
              "out": os.path.abspath(a.out) if a.out else None,
              "findings": findings, "fixes": fixes, "conclusion": concl}
    if a.json:
        stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    else:
        stdout.write(render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
