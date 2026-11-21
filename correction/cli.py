"""Slice-6 correction capture: record a correction as a GitHub issue, nudge promotion at the 2nd one.

Spec: docs/spec/slice-6.md. Stdlib only; supports Python 3.9.

- `record`  opens an issue labelled `correction` + `correction:<class>` with a machine block; the same
            (class, ref) is never recorded twice. From the 2nd event of a class on, one
            `correction:candidate` issue per class lists every event and asks whether to promote.
- `classes` lists the class labels of a repo with their event counts.

All GitHub traffic goes through an injectable `api(path, method, body)`. Every read happens before the
first write, so an API failure never leaves a half-written record.
"""
import argparse
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from degrade.run import ApiError, gh_api

SLUG = re.compile(r"\A[a-z0-9]+(-[a-z0-9]+)*\Z")
REPO = re.compile(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
EVENT_BLOCK = re.compile(r"<!--\s*autopilot-correction:v1\s+(\{.*?\})\s*-->", re.S)
CANDIDATE_BLOCK = re.compile(r"<!--\s*autopilot-correction-candidate:v1\s+(\{.*?\})\s*-->", re.S)
LABEL = "correction"
CANDIDATE = "correction:candidate"
PROMOTE_AT = 2
LABEL_COLORS = {LABEL: "D93F0B", CANDIDATE: "FBCA04"}


class BadInput(ValueError):
    pass


def validate(repo, slug):
    if not isinstance(repo, str) or not REPO.match(repo):
        raise BadInput(f"--repo 要写成 <owner>/<name>，收到 {repo!r}")
    if not isinstance(slug, str) or not SLUG.match(slug):
        raise BadInput(f"--class 只能是小写英文、数字和连字符（如 weakened-assertion），收到 {slug!r}")


def issue_payload(issue):
    """The machine block of a correction event issue, or None."""
    for block in EVENT_BLOCK.findall(issue.get("body") or ""):
        try:
            payload = json.loads(block)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def label_names(issue):
    return {lb.get("name") for lb in issue.get("labels") or []}


def is_event(issue):
    """A real correction event: an issue (not a PR), not the promotion-candidate list."""
    return "pull_request" not in issue and CANDIDATE not in label_names(issue)


def class_events(api, repo, slug):
    return [i for i in api(f"repos/{repo}/issues?labels={LABEL}:{slug}&state=all&per_page=100") or [] if is_event(i)]


def ensure_label(api, repo, name):
    color = LABEL_COLORS.get(name, "FEF2C0")
    try:
        api(f"repos/{repo}/labels", "POST", {"name": name, "color": color})
    except ApiError:
        pass  # already exists (422); a real failure shows up on the issue write that follows


def event_body(slug, summary, ref):
    block = json.dumps({"class": slug, "ref": ref, "summary": summary}, ensure_ascii=False)
    lines = [f"**纠正的类**：`{slug}`", f"**一句话**：{summary}"]
    if ref:
        lines.append(f"**出处**：{ref}")
    lines += ["", "同一类出现第 2 次时，系统会开「晋升候选」issue，问要不要沉淀成规则。", "",
              f"<!-- autopilot-correction:v1 {block} -->"]
    return "\n".join(lines) + "\n"


def candidate_body(slug, events):
    block = json.dumps({"class": slug}, ensure_ascii=False)
    lines = [f"`{slug}` 这一类已经被纠正 {len(events)} 次。要不要沉淀成规则？", "",
             "沉淀的做法见 `skills/correction-promotion/SKILL.md`：先看能不能改架构或类型，其次检查器，最后才写规则。",
             "", "**全部事件**："]
    for i in events:
        payload = issue_payload(i) or {}
        lines.append(f"- #{i['number']} {i.get('html_url', '')}：{payload.get('summary') or i.get('title', '')}")
    lines += ["", f"<!-- autopilot-correction-candidate:v1 {block} -->"]
    return "\n".join(lines) + "\n"


def record(api, repo, slug, summary, ref=None):
    """Record one correction event. Returns {status, number, url, count, candidate, ask}."""
    validate(repo, slug)
    if not isinstance(summary, str) or not summary.strip():
        raise BadInput("--summary 不能为空")
    summary = " ".join(summary.split())
    ref = ref.strip() if isinstance(ref, str) and ref.strip() else None

    events = class_events(api, repo, slug)  # all reads first
    if ref:
        for i in events:
            if (issue_payload(i) or {}).get("ref") == ref:
                return {"status": "duplicate", "number": i["number"], "url": i.get("html_url"),
                        "count": len(events), "candidate": None, "ask": None}
    existing_candidate = None
    if len(events) + 1 >= PROMOTE_AT:
        found = api(f"repos/{repo}/issues?labels={CANDIDATE},{LABEL}:{slug}&state=open&per_page=100") or []
        existing_candidate = next((i for i in found if "pull_request" not in i), None)

    for name in (LABEL, f"{LABEL}:{slug}"):
        ensure_label(api, repo, name)
    created = api(f"repos/{repo}/issues", "POST", {
        "title": f"纠正：{slug} — {summary[:40]}", "body": event_body(slug, summary, ref),
        "labels": [LABEL, f"{LABEL}:{slug}"]}) or {}
    result = {"status": "created", "number": created.get("number"), "url": created.get("html_url"),
              "count": len(events) + 1, "candidate": None, "ask": None}

    if result["count"] >= PROMOTE_AT:
        listed = sorted(events + [created], key=lambda i: i.get("number") or 0)
        title, body = f"晋升候选：{slug}", candidate_body(slug, listed)
        if existing_candidate is not None:
            api(f"repos/{repo}/issues/{existing_candidate['number']}", "PATCH", {"body": body})
            cand = existing_candidate
        else:
            ensure_label(api, repo, CANDIDATE)
            cand = api(f"repos/{repo}/issues", "POST", {"title": title, "body": body,
                                                        "labels": [CANDIDATE, f"{LABEL}:{slug}"]}) or {}
        result["candidate"] = cand.get("html_url")
        result["ask"] = (f"`{slug}` 这一类已经出现 {result['count']} 次，晋升候选：{result['candidate']}。"
                         "要不要沉淀成规则？")
    return result


def classes(api, repo):
    """[{class, count}] sorted by count desc then name; candidate issues are not events."""
    validate(repo, "x")
    counts = {}
    for i in api(f"repos/{repo}/issues?labels={LABEL}&state=all&per_page=100") or []:
        if not is_event(i):
            continue
        for name in label_names(i):
            if name and name.startswith(f"{LABEL}:") and name != CANDIDATE:
                counts[name[len(LABEL) + 1:]] = counts.get(name[len(LABEL) + 1:], 0) + 1
    return [{"class": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def build_parser():
    p = argparse.ArgumentParser(description="autopilot slice-6 correction capture")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="记一条纠正事件")
    r.add_argument("--repo", required=True)
    r.add_argument("--class", dest="cls", required=True)
    r.add_argument("--summary", required=True)
    r.add_argument("--ref")
    c = sub.add_parser("classes", help="列出类名和次数")
    c.add_argument("--repo", required=True)
    c.add_argument("--json", action="store_true")
    return p


def main(argv=None, api=gh_api, out=None):
    out = out or sys.stdout
    try:
        a = build_parser().parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    try:
        if a.cmd == "record":
            res = record(api, a.repo, a.cls, a.summary, a.ref)
            if res["status"] == "duplicate":
                print(f"已记录过：#{res['number']} {res['url']}（同一类、同一 --ref，不重复记）", file=out)
            else:
                print(f"已记录：#{res['number']} {res['url']}（{a.cls} 第 {res['count']} 次）", file=out)
            if res["ask"]:
                print(res["ask"], file=out)
        else:
            validate(a.repo, "x")
            rows = classes(api, a.repo)
            if a.json:
                print(json.dumps(rows, ensure_ascii=False), file=out)
            else:
                for row in rows:
                    print(f"{row['class']}\t{row['count']}", file=out)
                if not rows:
                    print("还没有纠正记录", file=out)
    except BadInput as e:
        print(f"correction: {e}", file=sys.stderr)
        return 2
    except ApiError as e:
        print(f"correction: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
