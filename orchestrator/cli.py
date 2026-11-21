"""`autopilot run`: drive one PR with the local orchestrator (slice 10). See orchestrator/flow.py.

    python3 -B orchestrator/cli.py run --repo haigeerlab/autopilot-sandbox --pr 1 [--dry-run] [--json]

Exit codes: 0 merged (or dry run passed); 1 stopped (the reason is on the PR and printed); 2 bad
invocation or environment (no token, no delegate).
"""
import argparse
import base64
import contextlib
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
from checkup import workflow
from degrade.run import ApiError, NotFound, parse_pages
from orchestrator import flow, token as tokens

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.expanduser("~/.autopilot")
DELEGATE_CACHE = os.path.expanduser("~/.claude/plugins/cache/delegate-marketplace/delegate")
SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME", "SHELL", "TERM")
CHECK_TIMEOUT = 15 * 60
REVIEW_TIMEOUT = 30 * 60
WRITE = {"contents": "write", "pull_requests": "write", "issues": "write"}
READ = {"contents": "read", "pull_requests": "read", "issues": "read"}


def find_delegate():
    path = os.environ.get("AUTOPILOT_DELEGATE")
    if path:
        return path
    try:
        versions = [v for v in os.listdir(DELEGATE_CACHE) if re.fullmatch(r"\d+\.\d+\.\d+", v)]
    except OSError:
        return None
    versions = [v for v in versions if tuple(map(int, v.split("."))) >= (0, 10, 0)]
    if not versions:
        return None
    best = max(versions, key=lambda v: tuple(map(int, v.split("."))))
    return os.path.join(DELEGATE_CACHE, best, "scripts", "delegate-exec.sh")


def _clean_env():
    return {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")}


class Work:
    """A throwaway clone with the base branch tip and the PR head fetched."""

    def __init__(self, root, repo, pr, token, delegate):
        self.root, self.pr, self.delegate = root, pr, delegate
        self.head, self.base_ref = pr["head"]["sha"], pr["base"]["ref"]
        auth = base64.b64encode(("x-access-token:%s" % token).encode()).decode()
        self.git_env = dict(_clean_env(), GIT_CONFIG_COUNT="1", GIT_TERMINAL_PROMPT="0",
                            GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                            GIT_CONFIG_VALUE_0="AUTHORIZATION: basic %s" % auth)
        self.git("init", "-q")
        self.git("fetch", "-q", "--no-tags", "https://github.com/%s" % repo,
                 "+refs/heads/%s:refs/remotes/base" % self.base_ref, self.head)
        self.base_tip = self.git("rev-parse", "refs/remotes/base").strip()
        self.merge_base = self.git("merge-base", self.base_tip, self.head).strip()

    def git(self, *args):
        r = subprocess.run(["git", "-C", self.root] + list(args), env=self.git_env, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=300)
        if r.returncode != 0:
            raise flow.Stop("clone", "git %s 失败：%s" % (args[0], r.stderr.strip()[:300]))
        return r.stdout

    def find_check(self):
        """The check command as registered on the BASE branch, so a PR cannot swap it for `true`."""
        self.git("checkout", "-q", "--detach", self.base_tip)
        return workflow.find_check(self.root)["command"]

    def run_check(self, command):
        self.git("checkout", "-q", "--detach", self.head)
        env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
        try:
            r = subprocess.run(["/bin/sh", "-c", command], cwd=self.root, env=env, capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=CHECK_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise flow.Stop("check", "检查 `%s` 超过 %d 分钟没结束" % (command, CHECK_TIMEOUT // 60))
        return flow.check_result(r.returncode, r.stdout + "\n" + r.stderr)

    def review(self, previous):
        args = ["bash", self.delegate, "review", "--repo", self.root, "--base", self.merge_base, "--head", self.head]
        with tempfile.TemporaryDirectory() as tmp:
            if previous is not None:
                prev = os.path.join(tmp, "previous.json")
                with open(prev, "w", encoding="utf-8") as fh:
                    json.dump(previous, fh)
                r = self._delegate(args + ["--previous-report", prev])
                if r.returncode != 127:
                    return self._parse(r)
                # 127 = previous report's scope does not match (files added/removed): review afresh
            return self._parse(self._delegate(args))

    def _delegate(self, args):
        try:
            return subprocess.run(args, cwd=self.root, env=_clean_env(), capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=REVIEW_TIMEOUT)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 124, "", "timeout")

    @staticmethod
    def _parse(r):
        try:
            report = json.loads(r.stdout)
        except ValueError:
            return {"execution_status": "failed", "review_outcome": None}
        return report if isinstance(report, dict) else {"execution_status": "failed"}


class LocalIO:
    def __init__(self, token, delegate):
        self.token, self.delegate = token, delegate

    def api(self, path, method="GET", body=None):
        args = ["gh", "api", path, "--method", method]
        if method == "GET":
            args.append("--paginate")
        if body is not None:
            args += ["--input", "-"]
        r = subprocess.run(args, input=json.dumps(body) if body is not None else None, capture_output=True,
                           text=True, timeout=60, env=dict(os.environ, GH_TOKEN=self.token))
        if r.returncode != 0:
            err = NotFound if "HTTP 404" in r.stderr else ApiError
            raise err("%s %s 失败：%s" % (method, path, r.stderr.strip()[:300]))
        return parse_pages(r.stdout) if r.stdout.strip() else None

    def api_optional(self, path):
        try:
            return self.api(path)
        except NotFound:
            return None

    @staticmethod
    def bot_identity():
        return d.load_bot_identity(os.path.join(HUB, "bot-identity.json"))

    @contextlib.contextmanager
    def checkout(self, repo, pr):
        with tempfile.TemporaryDirectory(prefix="autopilot-run-") as tmp:
            yield Work(tmp, repo, pr, self.token, self.delegate)

    @staticmethod
    def _review_path(repo, number):
        return os.path.join(STATE, "reviews", repo.replace("/", "__"), "%d.json" % number)

    def load_review(self, repo, number):
        try:
            with open(self._review_path(repo, number), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def save_review(self, repo, number, data):
        path = self._review_path(repo, number)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

    @staticmethod
    def write_check_evidence(repo, data):
        """~/.autopilot/evidence/<owner>__<repo>/<head sha>.json, read by spec-guard's final acceptance."""
        path = os.path.join(STATE, "evidence", repo.replace("/", "__"), "%s.json" % data["head_sha"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    @staticmethod
    def spec_guard_module(repo, pr):
        """The spec-guard module this PR belongs to. spec-guard 0.91's `/map --json` is not released yet, so
        no repo is treated as a spec-guard module for now (takeover comes from takeover.yml only)."""
        return None

    @staticmethod
    def checkout_of(repo):
        """The user's own checkout of `repo`, from ~/.autopilot/config.json {"checkouts": {"owner/name": path}}."""
        try:
            with open(os.path.join(STATE, "config.json"), encoding="utf-8") as fh:
                path = (json.load(fh).get("checkouts") or {}).get(repo)
        except (OSError, ValueError, AttributeError):
            return None
        return os.path.expanduser(path) if isinstance(path, str) and path else None

    def acceptance(self, repo, module):
        """{"record", "report_text"} from `git rev-parse --git-path spec-guard/acceptance/<module>.json` in the
        user's checkout, or None when there is no record (or no checkout configured)."""
        root = self.checkout_of(repo)
        if not root or not re.fullmatch(r"[\w.-]+", module or ""):
            return None
        r = subprocess.run(["git", "-C", root, "rev-parse", "--git-path", "spec-guard/acceptance/%s.json" % module],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        if r.returncode != 0:
            return None
        rec_path = r.stdout.strip()
        rec_path = rec_path if os.path.isabs(rec_path) else os.path.join(root, rec_path)
        try:
            with open(rec_path, encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            return None
        text = None
        if isinstance(record, dict) and isinstance(record.get("report"), str):
            try:
                with open(os.path.expanduser(record["report"]), encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                text = None
        return {"record": record if isinstance(record, dict) else {}, "report_text": text}

    @staticmethod
    def ledger(event):
        os.makedirs(STATE, exist_ok=True)
        with open(os.path.join(STATE, "ledger.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    @staticmethod
    def now():
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None):
    p = argparse.ArgumentParser(description="autopilot local orchestrator (slice 10)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="drive one PR: checks, review, evidence, admission, merge")
    r.add_argument("--repo", required=True, help="owner/name")
    r.add_argument("--pr", required=True, type=int)
    r.add_argument("--dry-run", action="store_true", help="read-only token; check and review, post nothing, never merge")
    r.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", a.repo):
        print("autopilot: --repo 要写成 owner/name", file=sys.stderr)
        return 2
    delegate = find_delegate()
    if not delegate or not os.path.exists(delegate):
        print("autopilot: 找不到 delegate 0.10.0 以上的 delegate-exec.sh（可设 AUTOPILOT_DELEGATE）", file=sys.stderr)
        return 2
    try:
        token = tokens.mint(a.repo, READ if a.dry_run else WRITE)
    except tokens.TokenError as e:
        print("autopilot: 换不出机器人令牌：%s" % e, file=sys.stderr)
        return 2
    try:
        result = flow.run(LocalIO(token, delegate), a.repo, a.pr, dry_run=a.dry_run)
    except ApiError as e:
        print("autopilot: GitHub 接口失败：%s" % e, file=sys.stderr)
        return 2
    finally:
        tokens.revoke(token)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print("autopilot run %s#%d：%s（%s）%s" % (a.repo, a.pr, result["result"], result["step"], result["reason"]))
        for n in result["notes"]:
            print("  说明：" + n)
    return 0 if result["result"] in ("merged", "dry-run") else 1


if __name__ == "__main__":
    sys.exit(main())
