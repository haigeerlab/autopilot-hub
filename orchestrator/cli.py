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
SPEC_GUARD_CACHE = os.path.expanduser("~/.claude/plugins/cache/spec-guard-marketplace/spec-guard")
SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME", "SHELL", "TERM")
CHECK_TIMEOUT = 15 * 60
REVIEW_TIMEOUT = 30 * 60
WRITE = {"contents": "write", "pull_requests": "write", "issues": "write"}
READ = {"contents": "read", "pull_requests": "read", "issues": "read"}


def _newest(cache, minimum, rel):
    try:
        versions = [v for v in os.listdir(cache) if re.fullmatch(r"\d+\.\d+\.\d+", v)]
    except OSError:
        return None
    versions = [v for v in versions if tuple(map(int, v.split("."))) >= minimum]
    if not versions:
        return None
    best = max(versions, key=lambda v: tuple(map(int, v.split("."))))
    return os.path.join(cache, best, rel)


def find_delegate():
    return os.environ.get("AUTOPILOT_DELEGATE") or _newest(DELEGATE_CACHE, (0, 10, 0), "scripts/delegate-exec.sh")


def find_project_config():
    """spec-guard 0.91+ `hooks/project_config.py` (slice 11 runs its `narrow`)."""
    return os.environ.get("AUTOPILOT_SPEC_GUARD_CONFIG") or _newest(SPEC_GUARD_CACHE, (0, 91, 0), "hooks/project_config.py")


def find_map_view():
    """spec-guard 0.91+ `hooks/map_view.py` (slice 12 reads each module's review state on the head commit)."""
    return os.environ.get("AUTOPILOT_SPEC_GUARD_MAP") or _newest(SPEC_GUARD_CACHE, (0, 91, 0), "hooks/map_view.py")


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

    def module_view(self):
        """spec-guard's map view of the head commit (the clone is on head after run_check); None if unavailable."""
        script = find_map_view()
        if not script or not os.path.exists(script):
            return None
        self.git("checkout", "-q", "--detach", self.head)
        try:
            r = subprocess.run(["python3", "-B", script, "--json", "--project", self.root], cwd=self.root,
                               env=_clean_env(), capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        except subprocess.TimeoutExpired:
            return None
        try:
            return json.loads(r.stdout) if r.returncode == 0 else None
        except ValueError:
            return None

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

    def open_narrow_pr(self, repo, base_ref, module, issue_number, reason):
        """Slice 11: in a throwaway clone of the base branch run spec-guard's `narrow`, commit as the bot, push
        `autopilot/narrow-<issue>` and open a PR. Returns its URL, or None when already the strictest.
        Raises flow.Stop when spec-guard 0.91+ is not installed or a step fails."""
        script = find_project_config()
        if not script or not os.path.exists(script):
            raise flow.Stop("narrow", "找不到 spec-guard 0.91 以上的 project_config.py，没法收窄")
        bot = self.bot_identity()
        branch = "autopilot/narrow-%d" % issue_number
        pr = {"head": {"sha": None}, "base": {"ref": base_ref}}
        with tempfile.TemporaryDirectory(prefix="autopilot-narrow-") as tmp:
            work = Work.__new__(Work)
            work.root = tmp
            auth = base64.b64encode(("x-access-token:%s" % self.token).encode()).decode()
            work.git_env = dict(_clean_env(), GIT_CONFIG_COUNT="1", GIT_TERMINAL_PROMPT="0",
                                GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                                GIT_CONFIG_VALUE_0="AUTHORIZATION: basic %s" % auth,
                                GIT_AUTHOR_NAME=bot["login"], GIT_COMMITTER_NAME=bot["login"],
                                GIT_AUTHOR_EMAIL="%d+%s@users.noreply.github.com" % (bot["id"], bot["login"]),
                                GIT_COMMITTER_EMAIL="%d+%s@users.noreply.github.com" % (bot["id"], bot["login"]))
            url = "https://github.com/%s" % repo
            work.git("init", "-q")
            work.git("fetch", "-q", "--no-tags", url, "+refs/heads/%s:refs/remotes/base" % base_ref)
            work.git("checkout", "-q", "-b", branch, "refs/remotes/base")
            args = ["python3", "-B", script, "narrow", "--project", tmp, "--source", "autopilot", "--reason", reason,
                    "--confirm"] + (["--module", module] if module else [])
            r = subprocess.run(args, cwd=tmp, env=_clean_env(), capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=120)
            if r.returncode != 0:
                raise flow.Stop("narrow", "spec-guard 收窄失败：%s" % (r.stdout + r.stderr).strip()[:300])
            if not work.git("status", "--porcelain").strip():
                return None
            target = "模块 %s" % module if module else "整个项目"
            work.git("add", "-A")
            work.git("commit", "-q", "-m", "chore: 收窄%s的接管档位（降级 #%d）\n\n%s" % (target, issue_number, reason))
            work.git("push", "-q", "--force", url, "HEAD:refs/heads/%s" % branch)
        created = self.api("repos/%s/pulls" % repo, "POST", {
            "title": "autopilot 收窄接管档位：%s（降级 #%d）" % (target, issue_number), "head": branch, "base": base_ref,
            "body": ("%s。\n\n按 spec-guard 的收窄规则降一格（合并 → Plan → 不接管），由 `project_config.py narrow --source "
                     "autopilot` 生成。改的是 `.agent/config.json`，属于 T4，请你审后合并；在那之前降级 issue #%d 已阻止自动合并。"
                     % (reason, issue_number))})
        return (created or {}).get("html_url")

    @staticmethod
    def write_check_evidence(repo, data):
        """~/.autopilot/evidence/<owner>__<repo>/<head sha>.json, read by spec-guard's final acceptance."""
        path = os.path.join(STATE, "evidence", repo.replace("/", "__"), "%s.json" % data["head_sha"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def find_modules(self, repo, head):
        """Modules whose final-acceptance record (outside the commit) is bound to `head`, read from the user's
        checkout: `git rev-parse --git-path spec-guard/acceptance`/*.json with sha == head."""
        root = self.checkout_of(repo)
        if not root:
            return []
        r = subprocess.run(["git", "-C", root, "rev-parse", "--git-path", "spec-guard/acceptance"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        if r.returncode != 0:
            return []
        folder = r.stdout.strip()
        folder = folder if os.path.isabs(folder) else os.path.join(root, folder)
        found = []
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return []
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("sha") == head and isinstance(rec.get("module"), str):
                found.append(rec["module"])
        return found

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
    def ledger_events():
        try:
            with open(os.path.join(STATE, "ledger.jsonl"), encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except (OSError, ValueError):
            return []

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
    st = sub.add_parser("stats", help="the three counts: auto-merged, reverted or reworked, Codex-unavailable stops")
    st.add_argument("--repo", required=True, help="owner/name")
    st.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", a.repo):
        print("autopilot: --repo 要写成 owner/name", file=sys.stderr)
        return 2
    if a.cmd == "stats":
        try:
            token = tokens.mint(a.repo, READ)
        except tokens.TokenError as e:
            print("autopilot: 换不出机器人令牌：%s" % e, file=sys.stderr)
            return 2
        try:
            out = flow.stats(LocalIO(token, None), a.repo)
        except ApiError as e:
            print("autopilot: GitHub 接口失败：%s" % e, file=sys.stderr)
            return 2
        finally:
            tokens.revoke(token)
        if a.json:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        else:
            print("autopilot stats %s：自动合并 %d 次，其中被撤回或返修 %d 次（撤回 %d、返修 %d）；"
                  "因 Codex 不可用停下 %d 次，共 %.1f 分钟" % (
                      a.repo, out["auto_merged"], out["reverted_or_reworked"], out["reverted"], out["reworked"],
                      out["codex_unavailable_stops"], out["codex_unavailable_minutes"]))
        return 0
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
