"""Slice-2 auto-degrade: open `autopilot:degrade` issues for GitHub-side incidents.

Spec: docs/spec/slice-2.md. Stdlib only; supports Python 3.9.

Triggers (all scoped to the whole repo, tier "*"):
- push to the default branch with a `Revert "..."` commit whose reverted commit belongs to a PR
  labelled `autopilot:authored`                                      -> L2, reason "revert"
- workflow_run of the main CI on the default branch (push event) that failed, whose head commit
  belongs to an `autopilot:authored` PR                              -> L2, reason "main-red"
- issues labelled `autopilot:regression`                             -> L2, reason "regression"
- issues labelled `autopilot:freeze`                                 -> L0, reason "freeze"

The same (reason, ref) never opens a second issue while one is open; it gets a comment instead.
Any API failure raises ApiError before anything is written, so the run turns red instead of guessing.
"""
import argparse
import json
import os
import re
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admission.decide import DEGRADE_BLOCK  # noqa: E402

AUTHORED = "autopilot:authored"
DEGRADE_LABEL = "autopilot:degrade"
LABEL_TRIGGERS = {"autopilot:regression": ("regression", "L2"), "autopilot:freeze": ("freeze", "L0")}
REVERT_TITLE = re.compile(r'\ARevert "')
REVERTS_COMMIT = re.compile(r"This reverts commit ([0-9a-f]{40})")
REASON_TEXT = {
    "revert": "main 上撤回了一个 autopilot 的 PR",
    "main-red": "autopilot 的 PR 合并后，main 上的主 CI 失败",
    "regression": "有 issue 被标记为回归（autopilot:regression）",
    "freeze": "有 issue 被打上冻结标签（autopilot:freeze）",
    "readiness-drop": "每周复检发现接入档掉档（repo-readiness 的掉档信号）",
}


class ApiError(Exception):
    pass


class NotFound(ApiError):
    """The API answered 404."""


def parse_pages(text):
    """`gh api --paginate` prints one JSON value per page; merge array pages, return a lone object as is."""
    decoder, pos, values = json.JSONDecoder(), 0, []
    text = text.strip()
    while pos < len(text):
        value, end = decoder.raw_decode(text, pos)
        values.append(value)
        pos = end
        while pos < len(text) and text[pos].isspace():
            pos += 1
    if len(values) == 1 and not isinstance(values[0], list):
        return values[0]
    return [item for page in values for item in (page if isinstance(page, list) else [page])]


def gh_api(path, method="GET", body=None):
    """Default API: the `gh` CLI with the workflow token. Raises ApiError on any failure."""
    args = ["gh", "api", path, "--method", method]
    if method == "GET":
        args.append("--paginate")
    if body is not None:
        args += ["--input", "-"]
    proc = subprocess.run(args, input=json.dumps(body) if body is not None else None,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        err = NotFound if "HTTP 404" in proc.stderr else ApiError
        raise err(f"{method} {path} failed: {proc.stderr.strip()[:300]}")
    try:
        return parse_pages(proc.stdout) if proc.stdout.strip() else None
    except ValueError as e:
        raise ApiError(f"{method} {path}: response is not JSON ({e})") from None


def parse_revert(message):
    """Return the reverted 40-char SHA if `message` is a git revert commit message, else None."""
    if not isinstance(message, str) or not REVERT_TITLE.search(message):
        return None
    m = REVERTS_COMMIT.search(message)
    return m.group(1) if m else None


def authored_pr(api, repo, sha):
    """The `autopilot:authored` PR that contains `sha`, or None."""
    for pr in api(f"repos/{repo}/commits/{sha}/pulls") or []:
        if any(label.get("name") == AUTHORED for label in pr.get("labels") or []):
            return pr
    return None


def open_degrade_keys(api, repo):
    """{(reason, ref): issue_number} for open degrade issues of this repo."""
    keys = {}
    for issue in api(f"repos/{repo}/issues?labels={DEGRADE_LABEL}&state=open&per_page=100") or []:
        for block in DEGRADE_BLOCK.findall(issue.get("body") or ""):
            try:
                payload = json.loads(block)
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("scope", {}).get("repo") == repo:
                keys[(payload.get("reason"), payload.get("ref"))] = issue["number"]
    return keys


def candidates(event_name, event, api, repo, default_branch, notes):
    """List of {reason, level, ref, evidence} the event calls for. Read-only."""
    found = []
    if event_name == "push":
        if event.get("ref") != f"refs/heads/{default_branch}":
            notes.append(f"推送的不是默认分支（{event.get('ref')}），跳过")
            return found
        for commit in event.get("commits") or []:
            reverted = parse_revert(commit.get("message"))
            if not reverted:
                continue
            pr = authored_pr(api, repo, reverted)
            if pr is None:
                notes.append(f"{commit['id'][:12]} 撤回了 {reverted[:12]}，它不属于带 {AUTHORED} 的 PR，不降级")
                continue
            found.append({"reason": "revert", "level": "L2", "ref": commit["id"],
                          "evidence": [pr.get("html_url"), f"https://github.com/{repo}/commit/{commit['id']}"]})
    elif event_name == "workflow_run":
        run = event.get("workflow_run") or {}
        if run.get("conclusion") != "failure" or run.get("event") != "push" or run.get("head_branch") != default_branch:
            notes.append("主 CI 不是默认分支上推送触发的失败，跳过")
            return found
        pr = authored_pr(api, repo, run["head_sha"])
        if pr is None:
            notes.append(f"失败的提交 {run['head_sha'][:12]} 不属于带 {AUTHORED} 的 PR，不降级")
            return found
        found.append({"reason": "main-red", "level": "L2", "ref": run["head_sha"],
                      "evidence": [pr.get("html_url"), run.get("html_url")]})
    elif event_name == "issues":
        if event.get("action") != "labeled":
            return found
        label = (event.get("label") or {}).get("name")
        issue = event.get("issue") or {}
        if label not in LABEL_TRIGGERS:
            return found
        if any(lb.get("name") == DEGRADE_LABEL for lb in issue.get("labels") or []):
            notes.append("标签打在降级 issue 自己身上，跳过")
            return found
        reason, level = LABEL_TRIGGERS[label]
        found.append({"reason": reason, "level": level, "ref": issue["html_url"], "evidence": [issue["html_url"]]})
    else:
        notes.append(f"不处理的事件 {event_name}")
    return found


def issue_body(repo, c):
    block = json.dumps({"scope": {"repo": repo, "tier": "*"}, "level": c["level"], "reason": c["reason"],
                        "ref": c["ref"], "evidence": [e for e in c["evidence"] if e]}, ensure_ascii=False)
    lines = [
        f"**为什么降级**：{REASON_TEXT[c['reason']]}。",
        f"**降到**：{c['level']}。在本 issue 关闭之前，`autopilot/admission` 给这个仓库每个 PR 的上限都不高于 {c['level']}。",
        "**依据**：" + "、".join(e for e in c["evidence"] if e),
        "",
        "**怎么解除**：确认问题已处理后，关闭本 issue。关闭需要仓库写权限，系统不会自动关闭。",
        "",
        f"<!-- autopilot-degrade:v1 {block} -->",
    ]
    return "\n".join(lines) + "\n"


def run(event_name, event, api):
    repo = event["repository"]["full_name"]
    default_branch = event["repository"]["default_branch"]
    notes = []
    found = candidates(event_name, event, api, repo, default_branch, notes)
    result = {"opened": 0, "commented": 0, "notes": notes, "actions": []}
    if not found:
        return result
    existing = open_degrade_keys(api, repo)  # read everything before the first write
    for c in found:
        number = existing.get((c["reason"], c["ref"]))
        if number is not None:
            api(f"repos/{repo}/issues/{number}/comments", "POST",
                {"body": f"同一原因再次触发：{REASON_TEXT[c['reason']]}（{c['ref']}）。降级仍然有效。"})
            result["commented"] += 1
            result["actions"].append({"comment": number, **c})
            continue
        created = api(f"repos/{repo}/issues", "POST", {
            "title": f"autopilot 降级：{c['reason']}（{c['ref'][:12] if len(c['ref']) == 40 else c['ref']}）",
            "body": issue_body(repo, c), "labels": [DEGRADE_LABEL]})
        result["opened"] += 1
        result["actions"].append({"opened": (created or {}).get("html_url"), **c})
        if c["reason"] == "revert":
            record_revert_correction(api, repo, c, notes)
    return result


def record_revert_correction(api, repo, c, notes):
    """Slice 6: a revert of an autopilot PR is also a correction event of class `revert`.

    Goes through the same code path as `correction/cli.py record` (ref dedupe, promotion candidate at 2).
    A failure here must not hide the degrade that was just opened, so it is reported as a note.
    """
    from correction.cli import record  # lazy: correction.cli imports this module
    pr_url = (c["evidence"] or [None])[0]
    try:
        res = record(api, repo, "revert", f"main 上撤回了 autopilot 的 PR（{pr_url}）", c["ref"])
    except ApiError as e:
        notes.append(f"纠错记录没写成（降级已开）：{e}")
        return
    if res.get("ask"):
        notes.append(res["ask"])


def main(argv=None):
    p = argparse.ArgumentParser(description="autopilot slice-2 auto-degrade")
    p.add_argument("--event-name", required=True)
    p.add_argument("--event-path", required=True)
    p.add_argument("--out-md", required=True)
    a = p.parse_args(argv)
    with open(a.event_path, encoding="utf-8") as f:
        event = json.load(f)
    try:
        result = run(a.event_name, event, gh_api)
    except ApiError as e:
        print(f"degrade: {e}", file=sys.stderr)
        return 1
    lines = [f"### autopilot-degrade：新开 {result['opened']} 个、追加评论 {result['commented']} 个", ""]
    lines += [f"- {a.get('opened') or ('#' + str(a.get('comment')))}：{a['reason']} → {a['level']}（{a['ref']}）"
              for a in result["actions"]]
    lines += [f"- 说明：{n}" for n in result["notes"]]
    with open(a.out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"degrade: opened={result['opened']} commented={result['commented']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
