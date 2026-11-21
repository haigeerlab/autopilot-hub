"""Rule `no-weakened-assertion`: a PR must not remove more test assertions than it adds.

Spec: docs/spec/slice-6.md (A14 of lint-starter). Input: the PR files JSON admission already uses
(filename, status, additions, deletions, patch). Only the PR's own patch is looked at, so existing
code is never flagged. A same-line waiver on an added line of the file clears that file.
Exit codes: 0 pass, 1 violation, 2 bad input.
"""
import argparse
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk.tiers import any_match, changed_lines, count_markers, load_templates
from rules.common import emit, load_json, parse_today, render, waived

RULE = "no-weakened-assertion"


def check(files, templates, today):
    errors = []
    for f in files:
        name = f["filename"]
        if not any_match(templates["test_files"], name):
            continue
        patch = f.get("patch")
        if patch is None:
            if (f.get("deletions") or 0) > 0:
                errors.append(render(RULE, f"{name}：补丁读不到，但有 {f['deletions']} 行删除，无法确认没删断言",
                                     "删掉断言是最常见的让自己变绿的方式，读不到补丁就没法排除",
                                     "把这个测试文件的改动拆小，让补丁能读到；或转交人审", "不要靠删测试让检查变绿"))
            continue
        removed, added = changed_lines(patch)
        markers = templates["assertion_markers"]
        gone, new = count_markers(removed, markers), count_markers(added, markers)
        if gone <= new:
            continue
        if any(waived(line, RULE, today) for line in added):
            continue
        errors.append(render(RULE, f"{name}：删除了 {gone} 处断言，只新增 {new} 处",
                             "断言少了，测试还绿也证明不了行为没变",
                             "保留原断言；行为确实要变，在 PR 里写明原因并转交人审", "不要删断言、注释掉断言或改成永远为真"))
    return errors


def main(argv=None):
    p = argparse.ArgumentParser(description="autopilot rule: no-weakened-assertion")
    p.add_argument("--pr-files", required=True)
    p.add_argument("--templates")
    p.add_argument("--today", help="YYYY-MM-DD, default UTC today")
    p.add_argument("--out-md")
    a = p.parse_args(argv)
    try:
        files = load_json(a.pr_files)
        if not isinstance(files, list):
            raise ValueError("--pr-files 必须是 JSON 数组")
        templates = load_templates(a.templates) if a.templates else load_templates()
        today = parse_today(a.today)
    except (OSError, ValueError, KeyError) as e:
        print(f"{RULE}: 输入有问题：{e}", file=sys.stderr)
        return 2
    return emit(check(files, templates, today), a.out_md, RULE)


if __name__ == "__main__":
    sys.exit(main())
