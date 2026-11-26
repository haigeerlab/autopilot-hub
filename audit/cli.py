"""`autopilot audit` (slice 18c): compare registered repos with their approved baselines, report on the hub.

    python3 -B audit/cli.py [--repo owner/name ...] [--ref BRANCH] [--no-post] [--json]

Reads each repo from a throwaway clone of its default branch (or --ref, used by drills) with a read-only bot
token; writes only to the hub: one comment on the "autopilot 合规周报" issue, plus one `autopilot:audit` issue per
repo and kind of violation (later runs comment on it; nothing is ever closed automatically).
Exit codes: 0 no violation; 1 violations found; 2 environment or invocation problem, or a repo could not be judged.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admission import decide as d
from audit import baselines as bl
from audit import config_check as cc
from audit import run as ar
from degrade.run import parse_pages
from orchestrator import token as tokens
from orchestrator.cli import HUB, LocalIO, _clean_env, _git_env, find_map_view

REPO_READ = {"contents": "read", "issues": "read"}
HUB_WRITE = {"issues": "write"}


def _api(token, path, method="GET", body=None):
    args = ["gh", "api", path, "--method", method]
    if method == "GET":
        args.append("--paginate")
    if body is not None:
        args += ["--input", "-"]
    r = subprocess.run(args, input=json.dumps(body) if body is not None else None, capture_output=True,
                       text=True, timeout=60, env=dict(_clean_env(), GH_TOKEN=token))
    if r.returncode != 0:
        raise RuntimeError("%s %s 失败：%s" % (method, path, r.stderr.strip()[:300]))
    return parse_pages(r.stdout) if r.stdout.strip() else None


class LiveIO:
    def __init__(self, hub_token=None):
        self.hub_token = hub_token
        self.tokens = {}

    def close(self):
        for tok in self.tokens.values():
            tokens.revoke(tok)

    def _token(self, repo):
        if repo not in self.tokens:
            self.tokens[repo] = tokens.mint(repo, REPO_READ)
        return self.tokens[repo]

    @staticmethod
    def bot_identity():
        return d.load_bot_identity(os.path.join(HUB, "bot-identity.json"))

    @staticmethod
    def baselines():
        return bl.load()

    @staticmethod
    def today():
        return datetime.date.today().isoformat()

    def degrade_issues(self, repo):
        return _api(self._token(repo), "/repos/%s/issues?labels=autopilot:degrade&state=open&per_page=100" % repo) or []

    def edited_at(self, number):
        owner, name = ar.HUB_REPO.split("/", 1)
        query = ("query($owner:String!,$name:String!,$number:Int!){"
                 "repository(owner:$owner,name:$name){issue(number:$number){lastEditedAt}}}")
        result = _api(self.hub_token, "graphql", "POST", {"query": query, "variables": {
            "owner": owner, "name": name, "number": number}})
        try:
            return result["data"]["repository"]["issue"]["lastEditedAt"]
        except (KeyError, TypeError) as error:
            raise RuntimeError("读不到 hub issue #%d 的正文编辑时间" % number) from error

    def hub_api(self, path, method="GET", body=None):
        return _api(self.hub_token, path, method, body)

    @staticmethod
    def verification_records(repo, module_ids):
        """Read verification records from the configured checkout's common git directory."""
        root = LocalIO.checkout_of(repo)
        if not root or not os.path.isdir(root):
            return None
        try:
            r = subprocess.run(["git", "-C", root, "rev-parse", "--git-common-dir"],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0 or not r.stdout.strip():
            return None
        common = r.stdout.strip()
        if not os.path.isabs(common):
            common = os.path.join(root, common)
        records = {}
        for mid in module_ids:
            records[mid] = None
            if not isinstance(mid, str) or not re.fullmatch(r"[\w.-]+", mid) or mid in (".", ".."):
                continue
            try:
                with open(os.path.join(common, "spec-guard", "verification", mid + ".json"), encoding="utf-8") as fh:
                    record = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(record, dict):
                records[mid] = record
        return records

    def snapshot(self, repo, ref):
        token = self._token(repo)
        with tempfile.TemporaryDirectory(prefix="autopilot-audit-") as tmp:
            root = os.path.join(tmp, "repo")
            cmd = ["git", "clone", "-q", "--no-tags"] + (["--branch", ref] if ref else []) + [
                "https://github.com/%s" % repo, root]
            r = subprocess.run(cmd, env=_git_env(token), capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=300)
            if r.returncode != 0:
                raise RuntimeError("克隆失败：%s" % r.stderr.strip()[:300])
            r = subprocess.run(["python3", "-B", os.path.join(HUB, "checkup", "cli.py"), "--repo", root, "--json"],
                               env=dict(_clean_env(), GH_TOKEN=token), capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=600)
            if r.returncode != 0:
                raise RuntimeError("体检失败（退出码 %d）：%s" % (r.returncode, r.stderr.strip()[:300]))
            checkup = {f["id"]: f["status"] for f in json.loads(r.stdout)["findings"]}
            spec_guard = os.path.exists(os.path.join(root, "spec", "CAPABILITY-MAP.md"))
            return {"takeover": cc.read_takeover(root), "pins": cc.read_pins(root), "checkup": checkup,
                    "spec_guard": spec_guard, "view": self._view(root) if spec_guard else None}

    @staticmethod
    def _view(root):
        script = find_map_view()
        if not script or not os.path.exists(script):
            return None
        try:
            r = subprocess.run(["python3", "-B", script, "--json", "--project", root], cwd=root, env=_clean_env(),
                               capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
            return json.loads(r.stdout) if r.returncode == 0 else None
        except (subprocess.TimeoutExpired, ValueError):
            return None


def main(argv=None):
    p = argparse.ArgumentParser(description="autopilot audit：对照批准基线查合规（切片 18）")
    p.add_argument("--repo", action="append", help="owner/name，可重复；不写则查基线里登记的全部仓库")
    p.add_argument("--ref", help="查这个分支而不是默认分支（演练用）")
    p.add_argument("--no-post", action="store_true", help="只打印，不在 hub 写报告和 issue")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    for repo in a.repo or []:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            print("autopilot audit: --repo 要写成 owner/name", file=sys.stderr)
            return 2
    try:
        bl.load()
    except bl.BaselineError as e:
        print("autopilot audit: 基线文件不合格：%s" % "；".join(e.problems), file=sys.stderr)
        return 2
    io = LiveIO()
    try:
        if not a.no_post:
            io.hub_token = tokens.mint(ar.HUB_REPO, HUB_WRITE)
        result = ar.run(io, repos=a.repo, ref=a.ref, post=not a.no_post)
    except (tokens.TokenError, RuntimeError) as e:
        print("autopilot audit: %s" % e, file=sys.stderr)
        return 2
    finally:
        io.close()
        if io.hub_token:
            tokens.revoke(io.hub_token)
    print(json.dumps(result, ensure_ascii=False, indent=1) if a.json else result["text"], end="" if not a.json else "\n")
    return result["code"]


if __name__ == "__main__":
    sys.exit(main())
