"""Slice-3 recheck: weekly readiness recheck on the user's Mac; drop signals open `autopilot:degrade` issues.

Spec: docs/spec/slice-3.md. Stdlib only; supports Python 3.9.

Runs on the user's Mac (not in CI): branch-protection and secret-scanning probes need the admin-level
read access of the user's `gh` login, which the Actions token does not have.

Per repo: read the conclusion from the default branch -> fresh clone of the default branch ->
allow-listed repo-readiness release (clean checkout, verified commit) -> rulers must match the
conclusion's basis.rulesets by sha256 -> `repo-readiness check --baseline <previous report>` ->
`drop_signals` open one degrade issue per report (reason "readiness-drop", ref = report file sha256)
-> the new report becomes the next baseline. Any failure leaves the baseline untouched and opens nothing.
"""
import argparse
import base64
import datetime as dt
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admission.decide import as_level, lowest  # noqa: E402
from degrade import run as degrade  # noqa: E402

HUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_URL = "https://github.com/haigeerlab/repo-readiness"
CONCLUSION_PATH = ".github/autopr/readiness-conclusion.json"


class Failed(Exception):
    pass


def default_shell(args, cwd=None):
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=900)
    return proc.returncode, proc.stdout, proc.stderr


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def must(sh, args, what):
    rc, out, err = sh(args)
    if rc != 0:
        raise Failed(f"{what} 失败：{err.strip()[:300]}")
    return out


def fetch_conclusion(api, repo):
    branch = api(f"repos/{repo}")["default_branch"]
    try:
        doc = api(f"repos/{repo}/contents/{CONCLUSION_PATH}?ref={branch}")
    except degrade.NotFound:
        return branch, None
    return branch, json.loads(base64.b64decode(doc["content"]).decode("utf-8"))


def prepare_tool(sh, state_dir, version, commit):
    """Clean checkout of repo-readiness at the allow-listed commit."""
    path = os.path.join(state_dir, "tools", f"repo-readiness-{version}")
    if not os.path.isdir(path):
        must(sh, ["git", "clone", "--quiet", TOOL_URL, path], "取 repo-readiness")
        must(sh, ["git", "-C", path, "checkout", "--quiet", commit], "切到允许的提交")
    head = must(sh, ["git", "-C", path, "rev-parse", "HEAD"], "读工具提交").strip()
    if head != commit:
        raise Failed(f"工具检出在 {head[:12]}，不是允许列表里的 {commit[:12]}")
    if must(sh, ["git", "-C", path, "status", "--porcelain"], "检查工具检出").strip():
        raise Failed("工具检出有未提交的改动，不用它复检")
    return path


def find_rulers(tool_path, rulesets):
    """Map each conclusion ruleset to a file in the tool checkout with the same sha256, or None."""
    by_sha = {}
    for f in glob.glob(os.path.join(tool_path, "**", "*.json"), recursive=True):
        if f"{os.sep}.git{os.sep}" not in f:
            by_sha.setdefault(sha256_file(f), f)
    return [(rs, by_sha.get(rs.get("sha256"))) for rs in rulesets]


def pick_baseline(state_file, seed, conclusion):
    if os.path.exists(state_file):
        return state_file
    if seed and os.path.exists(seed) and sha256_file(seed) == conclusion["basis"]["report_sha256"]:
        return seed
    return None


def issue_body(repo, sha, level, signals, report_name):
    lines = [f"- {s.get('clause')}（{s.get('rule')}，连续 {s.get('streak')} 次）：{s.get('from_tier')} → "
             f"{s.get('to_tier') or '未达标'}，上限 {s.get('to_trust_cap')}" for s in signals]
    c = {"reason": "readiness-drop", "level": level, "ref": sha,
         "evidence": [f"{report_name} sha256 {sha}"] + [f"{s.get('clause')}:{s.get('rule')}" for s in signals]}
    return degrade.issue_body(repo, c).replace("\n\n**怎么解除**", "\n\n**掉档信号**：\n" + "\n".join(lines)
                                               + "\n\n**怎么解除**", 1)


def recheck_repo(entry, *, api, sh, state_dir, allowed_tools, tool_version):
    repo = entry["repo"]
    notes = []
    result = {"repo": repo, "status": "ok", "notes": notes, "opened": None}
    try:
        branch, conclusion = fetch_conclusion(api, repo)
        if conclusion is None:
            notes.append("默认分支上没有接入结论，跳过")
            result["status"] = "skipped"
            return result
        tool = next((t for t in (allowed_tools or {}).get("tools", [])
                     if t.get("name") == "repo-readiness" and t.get("version") == tool_version), None)
        if tool is None:
            notes.append(f"repo-readiness {tool_version} 不在允许列表里，跳过")
            result["status"] = "skipped"
            return result
        tool_path = prepare_tool(sh, state_dir, tool_version, tool["commit"])
        rulers = find_rulers(tool_path, conclusion["basis"]["rulesets"])
        missing = [rs.get("id") for rs, f in rulers if f is None]
        if missing:
            notes.append(f"尺子已变（{'、'.join(missing)} 在 {tool_version} 里找不到同一份），不判掉档；需要重新签发结论")
            result["status"] = "skipped"
            return result

        slug = repo.replace("/", "__")
        repo_state = os.path.join(state_dir, slug)
        state_file = os.path.join(repo_state, "report.json")
        baseline = pick_baseline(state_file, entry.get("seed_report"), conclusion)
        work = os.path.join(state_dir, "work", slug)
        shutil.rmtree(work, ignore_errors=True)
        must(sh, ["git", "clone", "--quiet", "--depth", "1", "--branch", branch,
                  f"https://github.com/{repo}", os.path.join(work, "repo")], "取被测仓库")
        out_dir = os.path.join(work, "out")
        args = [os.path.join(tool_path, "bin", "repo-readiness"), "check", os.path.join(work, "repo"),
                "--repo-name", repo, "--out", out_dir]
        for rs, f in rulers:
            args += ["--clauses" if rs.get("kind") == "base" else "--stack-clauses", f]
        if baseline:
            with open(baseline, encoding="utf-8") as fh:
                category = json.load(fh).get("category")
            if category:
                args += ["--category", category]
            args += ["--baseline", baseline]
        rc, _, err = sh(args)
        if rc not in (0, 1):
            raise Failed(f"repo-readiness check 退出 {rc}：{err.strip()[:300]}")
        new_report = os.path.join(out_dir, "report.json")
        if not os.path.exists(new_report):
            raise Failed("repo-readiness check 没有写出 report.json")
        sha = sha256_file(new_report)
        with open(new_report, encoding="utf-8") as fh:
            report = json.load(fh)
        for req in report.get("judgment_requests") or []:
            notes.append(f"待裁决：{req.get('clause')}（证据变了，复检不裁决）")

        if baseline is None:
            notes.append("第一次复检，没有可用的基线：只建立基线，不判掉档")
            result["status"] = "baseline-created"
        else:
            signals = report.get("drop_signals") or []
            if signals:
                level = lowest([as_level(s.get("to_trust_cap")) for s in signals])
                existing = degrade.open_degrade_keys(api, repo)
                if ("readiness-drop", sha) in existing:
                    notes.append(f"这份报告的降级 issue 已开（#{existing[('readiness-drop', sha)]}），不重复开")
                else:
                    created = api(f"repos/{repo}/issues", "POST", {
                        "title": f"autopilot 降级：readiness-drop（{sha[:12]}）",
                        "body": issue_body(repo, sha, level, signals, "report.json"),
                        "labels": [degrade.DEGRADE_LABEL]})
                    result["opened"] = (created or {}).get("html_url")
                    notes.append(f"掉档信号 {len(signals)} 条，降到 {level}")
            else:
                notes.append("没有掉档信号")
        os.makedirs(repo_state, exist_ok=True)
        shutil.copyfile(new_report, state_file)  # only after every step above succeeded
        return result
    except (Failed, degrade.ApiError, OSError, ValueError, KeyError) as e:
        notes.append(f"失败：{e}")
        result["status"] = "failed"
        return result


def main(argv=None):
    home = os.path.expanduser("~/.autopilot/recheck")
    p = argparse.ArgumentParser(description="autopilot slice-3 weekly readiness recheck (runs on the user's Mac)")
    p.add_argument("--config", default=os.path.join(home, "config.json"))
    p.add_argument("--state-dir", default=home)
    a = p.parse_args(argv)
    with open(a.config, encoding="utf-8") as f:
        config = json.load(f)
    with open(os.path.join(HUB_ROOT, "allowed-tools.json"), encoding="utf-8") as f:
        allowed = json.load(f)
    results = [recheck_repo(e, api=degrade.gh_api, sh=default_shell, state_dir=a.state_dir,
                            allowed_tools=allowed, tool_version=config["tool_version"])
               for e in config["repos"]]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    lines = [f"# autopilot 掉档复检 {stamp}", ""]
    for res in results:
        lines.append(f"## {res['repo']}：{res['status']}" + (f"，已开 {res['opened']}" if res["opened"] else ""))
        lines += [f"- {n}" for n in res["notes"]] + [""]
    text = "\n".join(lines)
    os.makedirs(os.path.join(a.state_dir, "logs"), exist_ok=True)
    with open(os.path.join(a.state_dir, "logs", f"{stamp}.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 1 if any(res["status"] == "failed" for res in results) else 0


if __name__ == "__main__":
    sys.exit(main())
