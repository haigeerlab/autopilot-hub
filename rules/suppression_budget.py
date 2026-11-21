"""Rule `suppression-budget`: suppressions only shrink, waivers are well-formed and expire.

Spec: docs/spec/slice-6.md (A15 of lint-starter).
(a) --pr-files: suppression markers added by the PR must not outnumber the removed ones. Added lines
    carrying a valid unexpired waiver for this rule are not counted. A waiver written on an added line
    must itself be well-formed and unexpired.
(b) --scan <dir>: every waiver in the tracked files of <dir> must match the full format and not be
    expired. Prose and `<placeholder>` mentions are not waivers.
The waiver token itself is not a "suppression" for (a); (b) is what polices it. Legacy prefix
`autopr-allow` is accepted. Exit codes: 0 pass, 1 violation, 2 bad input.
"""
import argparse
import os
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk.tiers import changed_lines, count_markers, load_templates
from rules.common import emit, load_json, parse_today, render, waiver_state

RULE = "suppression-budget"
MAX_SCAN_BYTES = 1_000_000
FORMAT_FIX = "`# autopilot-allow <规则>: <理由> until <YYYY-MM-DD> approved-by <人>`，日期没过期"
STATE_TEXT = {"malformed": "豁免格式不完整", "expired": "豁免已过期"}


def check_patches(files, templates, today):
    markers = [m for m in templates["suppression_markers"] if m not in ("autopilot-allow", "autopr-allow")]
    added_n = removed_n = 0
    errors, where = [], []
    for f in files:
        removed, added = changed_lines(f.get("patch"))
        removed_n += count_markers(removed, markers)
        for line in added:
            state = waiver_state(line, None, today)
            if state in STATE_TEXT:
                errors.append(render(RULE, f"{f['filename']}：新增的{STATE_TEXT[state]}：{line.strip()[:120]}",
                                     "格式不完整或已过期的豁免等于没有审批", f"写成 {FORMAT_FIX}", "不要用过期日期或省略审批人"))
            if waiver_state(line, RULE, today) == "ok":
                continue
            n = count_markers([line], markers)
            if n:
                added_n += n
                where.append(f"{f['filename']}")
    if added_n > removed_n:
        errors.append(render(RULE, f"压制注释净增加 {added_n - removed_n}（新增 {added_n}，删除 {removed_n}），涉及 "
                             + "、".join(sorted(set(where))),
                             "压制注释一多，检查器就形同虚设",
                             f"修掉被压制的问题；确实要压制，在同一行写 {FORMAT_FIX}", "不要把压制挪到别的文件或换一种写法绕过"))
    return errors


def tracked_files(directory):
    proc = subprocess.run(["git", "-C", directory, "ls-files", "-z"], capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise OSError(f"git ls-files 失败：{proc.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return [n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n]


def scan(directory, today):
    errors = []
    for name in tracked_files(directory):
        path = os.path.join(directory, name)
        try:
            if os.path.islink(path) or os.path.getsize(path) > MAX_SCAN_BYTES:
                continue
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for no, line in enumerate(lines, 1):
            state = waiver_state(line, None, today)
            if state in (None, "ok"):
                continue
            errors.append(render(RULE, f"{name}:{no}：{STATE_TEXT[state]}：{line.strip()[:120]}",
                                 "格式不完整或已过期的豁免等于没有审批", f"写成 {FORMAT_FIX}；问题已修就删掉这行",
                                 "不要把日期往后改而不重新审批"))
    return errors


def main(argv=None):
    p = argparse.ArgumentParser(description="autopilot rule: suppression-budget")
    p.add_argument("--pr-files")
    p.add_argument("--scan", help="directory of a git checkout; checks every waiver in tracked files")
    p.add_argument("--templates")
    p.add_argument("--today", help="YYYY-MM-DD, default UTC today")
    p.add_argument("--out-md")
    a = p.parse_args(argv)
    if not a.pr_files and not a.scan:
        print(f"{RULE}: 至少要给 --pr-files 或 --scan 之一", file=sys.stderr)
        return 2
    errors = []
    try:
        today = parse_today(a.today)
        if a.pr_files:
            files = load_json(a.pr_files)
            if not isinstance(files, list):
                raise ValueError("--pr-files 必须是 JSON 数组")
            errors += check_patches(files, load_templates(a.templates) if a.templates else load_templates(), today)
        if a.scan:
            errors += scan(a.scan, today)
    except (OSError, ValueError, KeyError) as e:
        print(f"{RULE}: 输入有问题：{e}", file=sys.stderr)
        return 2
    return emit(errors, a.out_md, RULE)


if __name__ == "__main__":
    sys.exit(main())
