"""`autopilot run`: drive one PR with the local orchestrator (slice 10). See orchestrator/flow.py.

    python3 -B orchestrator/cli.py run --repo haigeerlab/autopilot-sandbox --pr 1 [--dry-run] [--json]
        [--notify SESSION] [--author-host claude-code|codex]
    python3 -B orchestrator/cli.py followups --repo haigeerlab/autopilot-sandbox --pr 1 [--json]
    python3 -B orchestrator/cli.py batch --repo haigeerlab/autopilot-sandbox [--dry-run]
    python3 -B orchestrator/cli.py release --repo haigeerlab/autopilot-sandbox --version 1.0.0 [--dry-run]
    python3 -B orchestrator/cli.py release --repo haigeerlab/autopilot-sandbox --resume 9 [--confirm "..."]

Run exit codes: 0 merged, auto-merge enabled, handed to the user, or successful dry run;
1 stopped or paused; 2 bad invocation or environment. Release uses 0 for completed and 1 for waiting.
"""
import argparse
import base64
import contextlib
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admission import decide as d
from checkup import workflow
from checkup import yamlsub
from degrade.run import ApiError, NotFound, parse_pages
from orchestrator import batch, flow, token as tokens
from orchestrator.state import StateCorruptError, StateLockedError, WatchState
from pipeline import engine, model as pipeline_model
from risk.tiers import REPO_CONFIGS

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.expanduser("~/.autopilot")
DELEGATE_CACHE = os.path.expanduser("~/.claude/plugins/cache/delegate-marketplace/delegate")
SPEC_GUARD_CACHE = os.path.expanduser("~/.claude/plugins/cache/spec-guard-marketplace/spec-guard")
SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME", "SHELL", "TERM")
CHECK_TIMEOUT = 15 * 60
REVIEW_TIMEOUT = 30 * 60
RELEASE_TIMEOUT = 30 * 60
WRITE = {"contents": "write", "pull_requests": "write", "issues": "write"}
RUN_WRITE = dict(WRITE, statuses="write")
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
    root = os.environ.get("AUTOPILOT_SPEC_GUARD_ROOT")
    if root:
        return os.path.join(root, "hooks", "map_view.py")
    return os.environ.get("AUTOPILOT_SPEC_GUARD_MAP") or _newest(SPEC_GUARD_CACHE, (0, 91, 0), "hooks/map_view.py")


def find_spec_guard_root(minimum=(0, 92, 0)):
    """Find the installed plugin satisfying the requested minimum version."""
    root = os.environ.get("AUTOPILOT_SPEC_GUARD_ROOT")
    if root and minimum >= (0, 96, 0):
        try:
            metadata = json.loads((Path(root) / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
            version = tuple(int(part) for part in metadata["version"].split("."))
            if version < minimum:
                return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
    return root or _newest(SPEC_GUARD_CACHE, minimum, "")


def verification_plugin():
    root = find_spec_guard_root((0, 96, 0))
    if not root or any(not os.path.isfile(os.path.join(root, "hooks", name))
                       for name in ("regression_view.py", "verification.py", "map_view.py")):
        raise flow.Stop("verification", "找不到 spec-guard 0.96.0 以上版本，可设 AUTOPILOT_SPEC_GUARD_ROOT")
    return root


def _git_env(token):
    auth = base64.b64encode(("x-access-token:%s" % token).encode()).decode()
    return dict(_clean_env(), GIT_CONFIG_COUNT="1", GIT_TERMINAL_PROMPT="0",
                GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                GIT_CONFIG_VALUE_0="AUTHORIZATION: basic %s" % auth)


def _clean_env():
    return {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")}


class Work:
    """A throwaway clone with the base branch tip and the PR head fetched."""

    def __init__(self, root, repo, pr, token, delegate):
        self.root, self.pr, self.delegate = root, pr, delegate
        self.head, self.base_ref = pr["head"]["sha"], pr["base"]["ref"]
        self.git_env = _git_env(token)
        self.git("init", "-q")
        self.git("fetch", "-q", "--no-tags", "https://github.com/%s" % repo,
                 "+refs/heads/%s:refs/remotes/base" % self.base_ref, self.head)
        # Let spec-guard's scope_base find the fetched base when trimming a rework.
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/base")
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

    def read_pipeline(self):
        """Read the fetched base commit without changing the PR checkout or its artifacts."""
        path = ".github/autopr/pipeline.yml"
        if not self.git("ls-tree", "--name-only", self.base_tip, "--", path).strip():
            return None
        return self.git("show", self.base_tip + ":" + path)

    def run_check(self, command):
        self.git("checkout", "-q", "--detach", self.head)
        env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
        try:
            r = subprocess.run(["/bin/sh", "-c", command], cwd=self.root, env=env, capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=CHECK_TIMEOUT)
        except subprocess.TimeoutExpired:
            reason = "检查 `%s` 超过 %d 分钟没结束" % (command, CHECK_TIMEOUT // 60)
            raise flow.Stop("check", reason, failures={f"check:{command}": reason})
        return flow.check_result(r.returncode, r.stdout + "\n" + r.stderr)

    def regression(self, module, ref):
        plugin = verification_plugin()
        try:
            result = subprocess.run(["python3", "-B", os.path.join(plugin, "hooks", "regression_view.py"),
                                     "--json", "--project", self.root, "--module", module, "--ref", ref],
                                    cwd=self.root, env=_clean_env(), capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=120)
            try:
                data = json.loads(result.stdout)
            except ValueError:
                data = None
            return result.returncode, data
        except (OSError, subprocess.TimeoutExpired):
            return 4, {"error": "unreadable"}

    def patch_id(self):
        try:
            diff = self.git("diff", self.merge_base, self.head)
            result = subprocess.run(["git", "patch-id", "--stable"], input=diff, cwd=self.root,
                                    env=_clean_env(), capture_output=True, text=True, timeout=120)
            value = result.stdout.split()[0] if result.stdout.split() else ""
            if result.returncode or not re.fullmatch(r"[0-9a-f]{40}", value):
                raise flow.Stop("verification", "算不出补丁指纹")
            return value
        except (OSError, subprocess.TimeoutExpired, flow.Stop) as error:
            raise flow.Stop("verification", "算不出补丁指纹") from error

    def run_case(self, command, sha):
        env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
        try:
            # Checkout and cleanup also receive no inherited Git authentication.
            for args in (("checkout", "-q", "-f", "--detach", sha), ("clean", "-q", "-ffdx")):
                result = subprocess.run(["git", "-C", self.root] + list(args), env=env,
                                        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
                if result.returncode:
                    return {"exit": None, "timeout": False, "output": "准备验证检出失败"}
            result = subprocess.run(["/bin/sh", "-c", command], cwd=self.root, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    errors="replace", stdin=subprocess.DEVNULL, timeout=CHECK_TIMEOUT)
            return {"exit": result.returncode, "timeout": False, "output": result.stdout}
        except subprocess.TimeoutExpired as error:
            output = error.output or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            return {"exit": None, "timeout": True, "output": output + "\n执行超时"}
        except OSError:
            return {"exit": None, "timeout": False, "output": "验证命令无法启动"}

    def run_release(self, command, version, repo, issue):
        """Run at the pinned release commit with combined output and no GitHub tokens."""
        # Steps intentionally have no hub credentials for git push: release preparation
        # only creates local commits. Authentication stays in git_env for hub Git calls.
        env = dict(_clean_env(), AUTOPILOT_VERSION=version, AUTOPILOT_REPO=repo,
                   AUTOPILOT_RELEASE_ISSUE=str(issue), AUTOPILOT_HUB=HUB)
        try:
            result = subprocess.run(["/bin/sh", "-c", command], cwd=self.root, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    errors="replace", stdin=subprocess.DEVNULL, timeout=RELEASE_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            output = error.output or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            return {"exit": 124, "output": output + "\n命令超过 30 分钟，已停止"}
        except OSError as error:
            raise flow.Stop("release", "发布命令无法启动：%s" % error) from error
        return {"exit": result.returncode, "output": result.stdout}

    def file_evidence(self, path):
        """Hash an existing file only when its resolved path stays inside the clone."""
        root = Path(self.root).resolve()
        try:
            artifact = (root / path).resolve()
            relative = artifact.relative_to(root)
            if not artifact.is_file():
                return None
            digest = hashlib.sha256()
            with artifact.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except (OSError, ValueError, RuntimeError):
            return None
        return {"path": relative.as_posix(), "sha256": digest.hexdigest()}

    def base_view(self):
        """Run spec-guard's map view on the fetched base commit; None if unavailable."""
        self.git("checkout", "-q", "--detach", self.base_tip)
        script = find_map_view()
        if not script or not os.path.exists(script):
            return None
        try:
            result = subprocess.run(["python3", "-B", script, "--json", "--project", self.root], cwd=self.root,
                                    env=_clean_env(), capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=120)
            return json.loads(result.stdout) if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None

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

    def in_scope(self, module):
        """True when spec-guard's scopeCheck puts every file changed since the merge base inside the module's scope.

        Only used to explain a stop; any failure (no plugin, refused, unreadable) is simply False.
        """
        root = find_spec_guard_root((0, 96, 0))
        script = os.path.join(root, "hooks", "module_scope.py") if root else None
        if not script or not os.path.isfile(script):
            return False
        try:
            # scopeCheck also counts uncommitted and untracked files, so drop what the checks left behind.
            self.git("checkout", "-q", "-f", "--detach", self.head)
            self.git("clean", "-q", "-ffdx")
            r = subprocess.run(["python3", "-B", script, "check", "--project", self.root, "--module", module,
                                "--base", self.merge_base, "--format", "json"], cwd=self.root, env=_clean_env(),
                               capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
            data = json.loads(r.stdout) if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError, flow.Stop):
            return False
        return isinstance(data, dict) and data.get("outside") == 0 and bool(data.get("files"))

    def changed_existing_files(self, module):
        """Return baseline evidence and changed paths already present at that baseline."""
        baseline = {"sha": self.merge_base, "source": "merge-base"}
        root = find_spec_guard_root((0, 96, 0)) if module else None
        hooks = os.path.join(root, "hooks") if root else None
        if hooks and os.path.isfile(os.path.join(hooks, "module_scope.py")):
            # scope_base also works for modules without an explicit scope boundary.
            code = ("import sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
                    "from module_scope import scope_base\nprint(scope_base(Path(sys.argv[2]), sys.argv[3]) or '')")
            try:
                self.git("checkout", "-q", "--detach", self.base_tip)
                result = subprocess.run([sys.executable, "-I", "-B", "-c", code, hooks, self.root, module],
                                        cwd=self.root, env=_clean_env(), capture_output=True, text=True,
                                        stdin=subprocess.DEVNULL, timeout=120)
                candidate = result.stdout.strip()
                if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", candidate):
                    try:
                        candidate = self.git("rev-parse", "--verify", candidate + "^{commit}").strip()
                        self.git("merge-base", "--is-ancestor", candidate, self.merge_base)
                    except flow.Stop:
                        baseline["fallbackReason"] = "scope-base-not-ancestor"
                    else:
                        baseline = {"sha": candidate, "source": "scope_base"}
            except (OSError, subprocess.TimeoutExpired, UnicodeError, flow.Stop):
                pass
        # Disabling rename detection treats a moved test as a deletion of the existing path.
        paths = self.git("diff", "--name-only", "--no-renames", "--diff-filter=a", "-z", baseline["sha"], self.head, "--")
        return baseline, [path for path in paths.split("\0") if path]

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

    def api(self, path, method="GET", body=None, *, graphql_rejection=False, paginate=True):
        args = ["gh", "api", path, "--method", method]
        if method == "GET" and paginate:
            args.append("--paginate")
        if body is not None:
            args += ["--input", "-"]
        try:
            r = subprocess.run(args, input=json.dumps(body) if body is not None else None, capture_output=True,
                               text=True, timeout=60, env=dict(os.environ, GH_TOKEN=self.token))
        except subprocess.TimeoutExpired as error:
            raise ApiError(f"{method} {path} 超时") from error
        if r.returncode != 0:
            # gh reports a GraphQL rejection both as a nonzero exit and in the response body.
            if graphql_rejection and r.stdout.strip():
                try:
                    response = json.loads(r.stdout)
                except ValueError:
                    response = None
                if isinstance(response, dict) and response.get("errors"):
                    return response
            err = NotFound if "HTTP 404" in r.stderr else ApiError
            raise err("%s %s 失败：%s" % (method, path, r.stderr.strip()[:300]))
        try:
            return parse_pages(r.stdout) if r.stdout.strip() else None
        except ValueError as error:
            raise ApiError(f"{method} {path} 返回了无法读取的 JSON") from error

    def enable_automerge(self, pr_id, head, method):
        """Return the GraphQL rejection message, or None after confirmed success."""
        query = ("mutation($pullRequestId:ID!,$expectedHeadOid:GitObjectID!,$mergeMethod:PullRequestMergeMethod!)"
                 "{enablePullRequestAutoMerge(input:{pullRequestId:$pullRequestId,"
                 "expectedHeadOid:$expectedHeadOid,mergeMethod:$mergeMethod}){pullRequest{id}}}")
        result = self.api("graphql", "POST", {"query": query, "variables": {
            "pullRequestId": pr_id, "expectedHeadOid": head, "mergeMethod": method.upper(),
        }}, graphql_rejection=True)
        try:
            if result.get("errors"):
                message = "; ".join(error["message"] for error in result["errors"]).strip()
                if message:
                    return message
            if result["data"]["enablePullRequestAutoMerge"]["pullRequest"]["id"] == pr_id:
                return None
        except (AttributeError, KeyError, TypeError):
            pass
        raise ApiError("GitHub 未确认已打开 PR 的自动合并")

    def api_optional(self, path):
        try:
            return self.api(path)
        except NotFound:
            return None

    def legacy_status_checks(self, repo, branch, *, need_source, need_sync, need_all=False):
        path = f"repos/{repo}/branches/{branch}"
        # The ordinary branch response can tell us whether a classic check is relevant.
        protection = (self.api_optional(path) or {}).get("protection") or {}
        checks = protection.get("required_status_checks") or {}
        contexts = set(checks.get("contexts") or []) | {c.get("context") for c in checks.get("checks") or []}
        if protection.get("enabled") is False:
            return None
        if not ((need_source and flow.VERIFIED in contexts) or ((need_sync or need_all) and contexts)):
            return checks
        # GitHub requires Administration: read for the classic details, including the pinned App ID.
        try:
            token = tokens.mint(repo, {"administration": "read"})
        except tokens.TokenError as error:
            raise flow.ChecksUnconfirmed(
                "无法确认旧版分支保护的必需检查及来源，需要 hub App 的 Administration: read 权限") from error
        try:
            details = LocalIO(token, None).api_optional(path + "/protection/required_status_checks")
        finally:
            tokens.revoke(token)
        if need_all and details is None:
            raise ApiError("读不到旧版分支保护的必需检查与来源")
        return details

    def commit_check_results(self, repo, head):
        """Read check runs and latest commit statuses across all result pages."""
        try:
            token = tokens.mint(repo, {"checks": "read"})
        except tokens.TokenError as error:
            raise flow.ChecksUnconfirmed("无法确认必需检查的结论，需要 hub App 的 Checks: read 权限") from error
        path = f"repos/{repo}/commits/{head}"
        try:
            run_pages = LocalIO(token, None).api(f"{path}/check-runs")
        finally:
            tokens.revoke(token)

        def entries(result, field):
            pages = result if isinstance(result, list) else [result]
            if not pages or any(not isinstance(page, dict) or not isinstance(page.get(field), list) for page in pages):
                raise ApiError(f"GitHub 返回的 {field} 数据不完整")
            return [entry for page in pages for entry in page[field]]

        runs = entries(run_pages, "check_runs")
        statuses = entries(self.api(f"{path}/status"), "statuses")
        # The combined response omits creators. Match its status IDs so an older
        # result from a trusted creator cannot vouch for a newer status.
        missing = {status["id"] for status in statuses if "id" in status and not status.get("creator")}
        if missing:
            history = self.api(f"{path}/statuses")
            if not isinstance(history, list):
                raise ApiError("GitHub 返回的提交状态明细数据不完整")
            creators = {status["id"]: status.get("creator") for status in history if status.get("id") in missing}
            statuses = [dict(status, creator=creators[status["id"]]) if status.get("id") in creators else status
                        for status in statuses]
        return runs, statuses

    def base_is_ancestor(self, repo, base_ref, head):
        """Compare the live base branch to the validated head regardless of mergeability caching."""
        ref = quote(base_ref, safe="")
        comparison = self.api(f"repos/{repo}/compare/{ref}...{head}?per_page=1", paginate=False)
        status = comparison.get("status") if isinstance(comparison, dict) else None
        if status not in ("ahead", "identical", "behind", "diverged"):
            raise ApiError("GitHub 返回的主干提交比较数据不完整")
        return status in ("ahead", "identical")

    @staticmethod
    def bot_identity():
        return d.load_bot_identity(os.path.join(HUB, "bot-identity.json"))

    @contextlib.contextmanager
    def checkout(self, repo, pr):
        with tempfile.TemporaryDirectory(prefix="autopilot-run-") as tmp:
            yield Work(tmp, repo, pr, self.token, self.delegate)

    @contextlib.contextmanager
    def release_checkout(self, repo, base_ref, commit):
        pr = {"head": {"sha": commit}, "base": {"ref": base_ref}}
        with tempfile.TemporaryDirectory(prefix="autopilot-release-") as tmp:
            work = Work(tmp, repo, pr, self.token, None)
            work.git("remote", "add", "-t", base_ref, "origin", "https://github.com/%s" % repo)
            work.git("fetch", "-q", "origin",
                     "+refs/heads/%s:refs/remotes/origin/%s" % (base_ref, base_ref),
                     "+refs/tags/*:refs/tags/*")
            work.git("checkout", "-q", "--detach", commit)
            yield work

    @staticmethod
    def _batch_process(args, *, cwd=None, timeout=120, env=None):
        # Plugin subprocesses receive no hub token or inherited Git authentication.
        if env is None:
            env = {k: v for k, v in _clean_env().items() if not k.startswith("GIT_CONFIG_")}
        return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout)

    def first_stage(self, repo):
        root = self.checkout_of(repo)
        if not root:
            raise ValueError("~/.autopilot/config.json 的 checkouts 没有配置 " + repo)
        executable = os.environ.get("AUTOPILOT_MODULE_CREW") or "module-crew"
        result = self._batch_process([executable, "merge", "first-stage", "--json"], cwd=root)
        if result.returncode != 0:
            raise ValueError("module-crew first-stage 失败（退出码 %d）：%s" % (
                result.returncode, (result.stdout + result.stderr).strip()[:300]))
        return result.stdout

    def batch_pr(self, repo, number, pr=None):
        """REST PR timestamps change on comments; lastEditedAt identifies actual body edits."""
        pr = self.api(f"repos/{repo}/pulls/{number}") if pr is None else pr
        owner, name = repo.split("/", 1)
        query = ("query($owner:String!,$name:String!,$number:Int!){"
                 "repository(owner:$owner,name:$name){pullRequest(number:$number){lastEditedAt}}}")
        result = self.api("graphql", "POST", {"query": query, "variables": {
            "owner": owner, "name": name, "number": number}})
        try:
            edited = result["data"]["repository"]["pullRequest"]["lastEditedAt"]
        except (KeyError, TypeError) as error:
            raise ApiError("读不到 batch PR 的正文编辑时间，无法验证来源") from error
        # A non-null edit is disqualifying even when GitHub timestamps share one second.
        return dict(pr, updated_at=pr.get("created_at") if edited is None else None)

    def is_ancestor(self, repo, ancestor, descendant):
        """Whether `ancestor` is reachable from `descendant` in the user's checkout (module-crew merges there)."""
        result = self._batch_process(["git", "-C", self.checkout_of(repo), "merge-base", "--is-ancestor",
                                      ancestor, descendant])
        if result.returncode not in (0, 1):
            raise ValueError("本机检出里核对不了提交的祖先关系：" + (result.stdout + result.stderr).strip()[:300])
        return result.returncode == 0

    def push_batch(self, repo, head, branch):
        result = self._batch_process(["git", "-C", self.checkout_of(repo), "push", "--force",
                                      "https://github.com/" + repo, head + ":refs/heads/" + branch],
                                     timeout=300, env=_git_env(self.token))
        if result.returncode != 0:
            raise ValueError("推送 batch 分支失败：" + (result.stdout + result.stderr).strip()[:300])

    @contextlib.contextmanager
    def acceptance_worktree(self, repo, head):
        root = self.checkout_of(repo)
        with tempfile.TemporaryDirectory(prefix="autopilot-acceptance-") as tmp:
            target = os.path.join(tmp, "checkout")
            added = False
            try:
                result = self._batch_process(["git", "-C", root, "worktree", "add", "--detach", target, head],
                                             timeout=300)
                if result.returncode != 0:
                    raise flow.Stop("acceptance", "建立验收 worktree 失败：" + (result.stdout + result.stderr).strip()[:300])
                added = True
                yield target
            finally:
                result = self._batch_process(["git", "-C", root, "worktree", "remove", "--force", target], timeout=300)
                if result.returncode != 0 and added:
                    raise flow.Stop("acceptance", "清理验收 worktree 失败：" + (result.stdout + result.stderr).strip()[:300])

    @contextlib.contextmanager
    def verification_worktree(self, repo, head, base_ref):
        root = self.checkout_of(repo)
        if not root or not os.path.isdir(root):
            raise flow.Stop("verification", "用户检出不存在，请配置 checkouts")
        def git(*args, authenticated=False):
            try:
                return self._batch_process(["git", "-C", root] + list(args), timeout=300,
                                           env=_git_env(self.token) if authenticated else None)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise flow.Stop("verification", "准备验证 worktree 失败") from error
        result = git("fetch", "-q", "--no-tags", "https://github.com/" + repo,
                     "+refs/heads/%s:refs/remotes/origin/%s" % (base_ref, base_ref), authenticated=True)
        if result.returncode:
            raise flow.Stop("verification", "获取 PR 基础分支失败")
        if git("cat-file", "-e", head + "^{commit}").returncode:
            if git("fetch", "-q", "--no-tags", "https://github.com/" + repo, head, authenticated=True).returncode:
                raise flow.Stop("verification", "获取 PR head 失败")
        default = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if not ((default.returncode == 0 and default.stdout.strip() == "refs/remotes/origin/" + base_ref)
                or (default.returncode == 1 and base_ref == "main")):
            raise flow.Stop("verification", "用户检出的默认分支不是 PR 的基础分支，spec-guard 算的补丁会对不上")
        with tempfile.TemporaryDirectory(prefix="autopilot-verification-") as tmp:
            target = os.path.join(tmp, "checkout")
            if git("worktree", "add", "--detach", target, head).returncode:
                raise flow.Stop("verification", "建立验证 worktree 失败")
            failed = True
            try:
                yield target
                failed = False
            finally:
                removed = git("worktree", "remove", "--force", target).returncode == 0
                if not removed:
                    git("worktree", "prune")
                    if not failed:  # never mask the stop raised inside the block
                        raise flow.Stop("verification", "清理验证 worktree 失败")

    def _verification_command(self, root, module, report, action):
        plugin = verification_plugin()
        args = ["python3", "-B", os.path.join(plugin, "hooks", "verification.py"), action,
                "--project", root, "--module", module, "--report", str(report)]
        args += ["--json"] if action == "check" else ["--confirm"]
        try:
            result = self._batch_process(args, cwd=root)
            try:
                data = json.loads(result.stdout)
            except ValueError:
                data = None
            return result.returncode, data, result.stdout + result.stderr
        except (OSError, subprocess.TimeoutExpired):
            return 4, None, "验证脚本无法执行或超时"

    def verification_check(self, root, module, report):
        return self._verification_command(root, module, report, "check")

    def verification_record(self, root, module, report):
        return self._verification_command(root, module, report, "record")

    def verification_view(self, root):
        plugin = verification_plugin()
        try:
            result = self._batch_process(["python3", "-B", os.path.join(plugin, "hooks", "map_view.py"),
                                          "--json", "--project", root], cwd=root)
            data = json.loads(result.stdout) if result.returncode == 0 else None
            return data if isinstance(data, dict) else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None

    @staticmethod
    def verification_paths(repo, patch_id):
        directory = os.path.join(STATE, "evidence", repo.replace("/", "__"))
        prefix = os.path.join(directory, patch_id)
        return prefix + ".verification.json", prefix + ".logs", prefix + ".cases.json"

    @staticmethod
    def write_verification_file(path, content):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = content.encode("utf-8") if isinstance(content, str) else content
        temporary = str(path) + ".tmp"
        Path(temporary).write_bytes(raw)
        os.replace(temporary, path)

    def acceptance_map(self, root):
        plugin = find_spec_guard_root()
        if not plugin:
            raise flow.Stop("acceptance", "找不到 spec-guard 0.92.0 以上版本，可设 AUTOPILOT_SPEC_GUARD_ROOT")
        result = self._batch_process(["python3", "-B", os.path.join(plugin, "hooks", "map_view.py"),
                                      "--json", "--project", root], cwd=root)
        try:
            data = json.loads(result.stdout) if result.returncode == 0 else None
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("modules"), list):
            raise flow.Stop("acceptance", "spec-guard map_view 读不了：" + (result.stdout + result.stderr).strip()[:300])
        return data

    def module_start(self, root, module):
        result = self._batch_process(["git", "-C", root, "log", "--diff-filter=A", "--format=%H", "--", f"spec/{module}.md"])
        commits = result.stdout.strip().splitlines()
        if result.returncode != 0 or not commits:
            raise flow.Stop("acceptance", "找不到模块 Spec 最初加入的提交，交你核对审查范围")
        result = self._batch_process(["git", "-C", root, "rev-parse", commits[-1] + "^"])
        if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", result.stdout.strip()):
            raise flow.Stop("acceptance", "找不到模块开工前的父提交，交你核对审查范围")
        return result.stdout.strip()

    @staticmethod
    def read_bytes(path):
        return Path(os.path.expanduser(path)).read_bytes()

    def local_evidence(self, repo, head):
        path = os.path.join(STATE, "evidence", repo.replace("/", "__"), head + ".json")
        try:
            return json.loads(self.read_bytes(path))
        except (OSError, ValueError):
            return None

    def review_acceptance(self, root, base, head, module, task):
        if not self.delegate:
            return {"execution_status": "failed", "review_outcome": None}
        args = ["bash", self.delegate, "review", "--repo", root, "--base", base, "--head", head,
                "--spec", f"spec/{module}.md", "--", task]
        try:
            return Work._parse(self._batch_process(args, cwd=root, timeout=REVIEW_TIMEOUT))
        except (OSError, subprocess.TimeoutExpired):
            return {"execution_status": "failed", "review_outcome": None}

    @staticmethod
    def save_acceptance_review(repo, module, head, report):
        path = os.path.join(STATE, "reviews", repo.replace("/", "__"), f"acceptance-{module}-{head[:12]}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=1)
        return path

    def mark_acceptance(self, root, module, report):
        plugin = find_spec_guard_root()
        if not plugin:
            raise flow.Stop("acceptance", "找不到 spec-guard 0.92.0 以上版本")
        result = self._batch_process(["python3", "-B", os.path.join(plugin, "hooks", "review_mark.py"),
                                      "--project", root, "--module", module, "--target", "acceptance",
                                      "--state", "codex-approved", "--review", os.path.expanduser(report), "--confirm"], cwd=root)
        return {"exit": result.returncode, "output": result.stdout + result.stderr}

    def shared_acceptance(self, root, module):
        result = self._batch_process(["git", "-C", root, "rev-parse", "--git-common-dir"])
        if result.returncode != 0 or not result.stdout.strip():
            return None
        common = result.stdout.strip()
        common = common if os.path.isabs(common) else os.path.join(root, common)
        try:
            return json.loads(self.read_bytes(os.path.join(common, "spec-guard", "acceptance", module + ".json")))
        except (OSError, ValueError):
            return None

    def resolve_pipeline(self, repo, ref, notes):
        """Resolve base-branch configuration with the hub's installed templates."""
        text = flow.base_file(self, repo, ".github/autopr/pipeline.yml", ref)
        if text is not None:
            try:
                config = yamlsub.loads(text)
            except yamlsub.YamlSubsetError as error:
                raise pipeline_model.PipelineError([
                    ".github/autopr/pipeline.yml 第 %d 行 YAML 格式错误，请检查语法" % error.line]) from error
        else:
            config = None
            for path in REPO_CONFIGS:
                text = flow.base_file(self, repo, path, ref)
                if text is not None:
                    config = yamlsub.loads(text) if path.endswith(".yml") else json.loads(text)
                    if not isinstance(config, dict):
                        raise ValueError("风险配置必须是映射")
                    break
            category = config.get("extends") if isinstance(config, dict) else None
            template = "plugin" if category == "plugin" else "site" if category == "frontend" else None
            if template is None:
                raise pipeline_model.PipelineError(["没有 pipeline.yml，无法从风险配置识别流程类别，请声明 extends"])
            notes.append("没有 pipeline.yml，按类别 %s 套模板 %s" % (category, template))
            config = {"extends": template}
        return pipeline_model.resolve(config)

    def release_issue(self, repo, number):
        issue = self.api("repos/%s/issues/%d" % (repo, number))
        # REST updated_at also changes on comments and labels. GraphQL distinguishes body edits.
        if issue.get("updated_at") != issue.get("created_at"):
            owner, name = repo.split("/", 1)
            query = ("query($owner:String!,$name:String!,$number:Int!){"
                     "repository(owner:$owner,name:$name){issue(number:$number){lastEditedAt}}}")
            result = self.api("graphql", "POST", {"query": query, "variables": {
                "owner": owner, "name": name, "number": number}})
            try:
                edited_at = result["data"]["repository"]["issue"]["lastEditedAt"]
            except (KeyError, TypeError) as error:
                raise ApiError("读不到发布 issue 的正文编辑时间，无法验证开始块") from error
            issue = dict(issue, updated_at=edited_at or issue.get("created_at"))
        return issue

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
            "body": ("%s。\n\n按 spec-guard 的收窄规则降一格（合并 → 技术方案 → 不接管），由 `project_config.py narrow --source "
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

    @staticmethod
    def acceptance_paths(root):
        """Shared Git directory first, then the legacy per-worktree location."""
        paths = []
        for args in (("--git-common-dir",), ("--git-path", "spec-guard/acceptance")):
            r = subprocess.run(["git", "-C", root, "rev-parse"] + list(args),
                               capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
            if r.returncode != 0 or not r.stdout.strip():
                continue
            path = r.stdout.strip()
            path = path if os.path.isabs(path) else os.path.join(root, path)
            if args == ("--git-common-dir",):
                path = os.path.join(path, "spec-guard", "acceptance")
            path = os.path.normpath(path)
            if path not in paths:
                paths.append(path)
        return paths

    def find_modules(self, repo, head):
        """Read acceptance records in priority order; newer records shadow legacy ones."""
        root = self.checkout_of(repo)
        if not root:
            return []
        found, seen = set(), set()
        for folder in self.acceptance_paths(root):
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                continue
            for name in names:
                if not name.endswith(".json") or name in seen:
                    continue
                seen.add(name)
                try:
                    with open(os.path.join(folder, name), encoding="utf-8") as fh:
                        rec = json.load(fh)
                except (OSError, ValueError):
                    continue
                if isinstance(rec, dict) and rec.get("sha") == head and isinstance(rec.get("module"), str):
                    found.add(rec["module"])
        return sorted(found)

    @staticmethod
    def local_config():
        try:
            with open(os.path.join(STATE, "config.json"), encoding="utf-8") as fh:
                config = json.load(fh)
        except (OSError, ValueError):
            return {}
        return config if isinstance(config, dict) else {}

    @staticmethod
    def checkout_of(repo):
        """The user's own checkout of `repo`, from ~/.autopilot/config.json {"checkouts": {"owner/name": path}}."""
        checkouts = LocalIO.local_config().get("checkouts")
        path = checkouts.get(repo) if isinstance(checkouts, dict) else None
        return os.path.expanduser(path) if isinstance(path, str) and path else None

    def acceptance(self, repo, module):
        """Read the preferred record and its report from the user's configured checkout."""
        root = self.checkout_of(repo)
        if not root or not re.fullmatch(r"[\w.-]+", module or "") or module in (".", ".."):
            return None
        for folder in self.acceptance_paths(root):
            rec_path = os.path.join(folder, module + ".json")
            if not os.path.lexists(rec_path):
                continue
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
                    pass
            return {"record": record if isinstance(record, dict) else {}, "report_text": text}
        return None

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


def batch_command(args):
    try:
        token = tokens.mint(args.repo, READ if args.dry_run else WRITE)
    except tokens.TokenError as error:
        print("autopilot: 换不出机器人令牌：%s" % error, file=sys.stderr)
        return 2
    try:
        result = batch.run(LocalIO(token, find_delegate()), args.repo, dry_run=args.dry_run)
    except (ApiError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("autopilot: batch 环境或接口校验失败：%s" % error, file=sys.stderr)
        return 2
    finally:
        tokens.revoke(token)
    stream = sys.stderr if result["code"] == 2 else sys.stdout
    print("autopilot batch %s：%s" % (args.repo, result["reason"]), file=stream)
    if result.get("pr") is not None:
        print("PR：https://github.com/%s/pull/%s" % (args.repo, result["pr"]), file=stream)
    for step in result.get("plan", []):
        print("  会执行：" + step, file=stream)
    for note in result.get("notes", []):
        print("  说明：" + note, file=stream)
    return result["code"]


def release(args):
    if ((args.confirm is not None and args.resume is None) or (args.dry_run and args.resume is not None)
            or (args.resume is not None and args.resume < 1)
            or (args.version is not None and not args.version.strip())
            or (args.confirm is not None and not args.confirm.strip())):
        print("autopilot: --confirm 只用于续跑且不能为空，--resume 必须为正数，"
              "--dry-run 只用于新发布，版本不能为空", file=sys.stderr)
        return 2
    permissions = {"contents": "read", "issues": "read" if args.dry_run else "write"}
    try:
        token = tokens.mint(args.repo, permissions)
    except tokens.TokenError as error:
        print("autopilot: 换不出机器人令牌：%s" % error, file=sys.stderr)
        return 2
    try:
        result = engine.run(LocalIO(token, None), args.repo, args.version,
                            resume=args.resume, confirm=args.confirm, dry_run=args.dry_run)
    except (ApiError, OSError, ValueError) as error:
        print("autopilot: 发布环境或流程校验失败：%s" % error, file=sys.stderr)
        return 2
    finally:
        tokens.revoke(token)
    stream = sys.stderr if result["code"] == 2 else sys.stdout
    print("autopilot release %s：%s" % (args.repo, result["reason"]), file=stream)
    for note in result["notes"]:
        print("  说明：" + note, file=stream)
    if result["result"] == "dry-run":
        print("原始发布档位：" + result["level_raw"])
        print("实际发布档位：" + result["level"])
        if result["level_cap_reason"] is None:
            print("档位说明：未降档")
        for step in result["plan"]:
            action = "会执行" if step["status"] == "execute" else "会停下等人（%s）" % step["reason"]
            print("  %s：%s" % (step["id"], action))
    if result["url"]:
        print("跟踪 issue：" + result["url"], file=stream)
    if result["code"] == 1:
        suffix = ' --confirm "用户原话"' if result["result"] == "waiting" else ""
        print("续跑示例：" + result["resume_command"] + suffix, file=stream)
    return result["code"]


def followups(args):
    """Recover post-merge notes only from trusted evidence and its exact local report."""
    from admission.gate import current_evidence
    try:
        token = tokens.mint(args.repo, WRITE)
    except tokens.TokenError as error:
        print('autopilot: 换不出机器人令牌：%s' % error, file=sys.stderr)
        return 2
    try:
        io = LocalIO(token, None)
        bot = io.bot_identity()
        if not bot:
            raise flow.Stop('followups', 'hub 没配机器人身份，不能补开后续 issue')
        pr = io.api(f'repos/{args.repo}/pulls/{args.pr}')
        if not pr.get('merged'):
            raise flow.Stop('followups', 'PR 还没有合并，不能开后续 issue')
        comments = io.api(f'repos/{args.repo}/issues/{args.pr}/comments?per_page=100') or []
        evidence = [p for p in current_evidence(
            [flow.rest_comment(c) for c in sorted(comments, key=lambda c: c.get('id', 0))], bot, pr['head']['sha'])
                    if p.get('_v') == 2]
        if not evidence:
            raise flow.Stop('followups', '没有绑定合并前 head 的机器人 v2 证据')
        latest = evidence[-1]
        module, patch_id = latest.get('module'), latest.get('patch_id')
        verification = latest.get('verification')
        if not isinstance(module, str) or not module.strip() or not isinstance(patch_id, str) \
                or not re.fullmatch(r'[0-9a-fA-F]{40}', patch_id) or not isinstance(verification, dict):
            raise flow.Stop('followups', 'v2 证据缺少有效的模块、补丁指纹或报告摘要')
        try:
            raw = io.read_bytes(io.verification_paths(args.repo, patch_id)[0])
        except FileNotFoundError:
            raise flow.Stop('followups', '本机验证报告不存在，不能补开后续 issue') from None
        if hashlib.sha256(raw).hexdigest() != verification.get('report_sha256'):
            raise flow.Stop('followups', '本机验证报告 sha256 与 v2 证据不符，不能补开后续 issue')
        try:
            report = json.loads(raw)
        except ValueError:
            raise flow.Stop('followups', '本机验证报告不是有效 JSON') from None
        if not isinstance(report, dict) or not isinstance(report.get('notes'), list) or any(
                not isinstance(note, dict) or not isinstance(note.get('title'), str) for note in report['notes']):
            raise flow.Stop('followups', '本机验证报告的意见格式不对')
        result = flow.open_followups(io, args.repo, args.pr, module, patch_id, report['notes'], bot)
    except flow.Stop as error:
        print('autopilot: ' + error.reason, file=sys.stderr)
        return 1
    except (ApiError, OSError, ValueError, KeyError, TypeError) as error:
        print('autopilot: 补开后续 issue 的环境或调用失败：%s' % error, file=sys.stderr)
        return 2
    finally:
        tokens.revoke(token)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print('autopilot：新开 %s；追加 %s；跳过 %d 条；失败 %s' % (
            '、'.join('#%s' % n for n in result['opened']) or '无',
            '、'.join('#%s' % n for n in result['commented']) or '无', result['skipped'],
            '、'.join(result['failed']) or '无'))
    return 1 if result['failed'] else 0


def main(argv=None):
    p = argparse.ArgumentParser(description="autopilot local orchestrator (slice 10)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="drive one PR: checks, review, evidence, admission, merge")
    r.add_argument("--repo", required=True, help="owner/name")
    r.add_argument("--pr", required=True, type=int)
    r.add_argument("--dry-run", action="store_true", help="read-only token; check and review, post nothing, never merge")
    r.add_argument("--json", action="store_true")
    r.add_argument("--notify", metavar="SESSION", help="queue handoff notices for this session")
    r.add_argument("--author-host", choices=("claude-code", "codex"), help="record the author's host in evidence")
    follow = sub.add_parser('followups', help='在 PR 合并后补开验证意见的后续 issue')
    follow.add_argument('--repo', required=True, help='owner/name')
    follow.add_argument('--pr', required=True, type=int)
    follow.add_argument('--json', action='store_true')
    bat = sub.add_parser("batch", help="推进一个 module-crew 合并第一段模块")
    bat.add_argument("--repo", required=True, help="owner/name")
    bat.add_argument("--dry-run", action="store_true", help="只读 first-stage 与 GitHub，不推分支、不开 PR、不验收、不合并")
    st = sub.add_parser("stats", help="the three counts: auto-merged, reverted or reworked, Codex-unavailable stops")
    st.add_argument("--repo", required=True, help="owner/name")
    st.add_argument("--json", action="store_true")
    rel = sub.add_parser("release", help="按流程发布，用跟踪 issue 留证据并支持续跑")
    rel.add_argument("--repo", required=True, help="owner/name")
    target = rel.add_mutually_exclusive_group(required=True)
    target.add_argument("--version", help="本次发布版本")
    target.add_argument("--resume", type=int, help="续跑的跟踪 issue 号")
    rel.add_argument("--confirm", help="人工确认原话，原样记录")
    rel.add_argument("--dry-run", action="store_true", help="只读预演，不开 issue、不执行命令")
    a = p.parse_args(argv)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", a.repo):
        print("autopilot: --repo 要写成 owner/name", file=sys.stderr)
        return 2
    if a.cmd == "batch":
        return batch_command(a)
    if a.cmd == "release":
        return release(a)
    if a.cmd == 'followups':
        return followups(a)
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
    try:
        token = tokens.mint(a.repo, READ if a.dry_run else RUN_WRITE)
    except tokens.TokenError as e:
        print("autopilot: 换不出机器人令牌：%s" % e, file=sys.stderr)
        return 2
    state = WatchState(STATE)
    try:
        result = flow.run(LocalIO(token, None), a.repo, a.pr, dry_run=a.dry_run,
                          state=state, notify=a.notify, author_host=a.author_host)
    except (ApiError, OSError, StateCorruptError, StateLockedError) as error:
        summary = " ".join(str(error).replace(token, "[redacted]").split())[:350]
        source, recovery_check = (("GitHub 接口", "GitHub 接口和 App 权限") if isinstance(error, ApiError)
                                  else ("本机运行", "本机运行环境和 watch 状态"))
        print(f"autopilot: {source}失败：{summary}", file=sys.stderr)
        if not a.dry_run:
            recovery = f"{summary}。核对{recovery_check}后，重新运行 autopilot run。"
            key = hashlib.sha256(summary.encode("utf-8")).hexdigest()
            try:
                with state.lock(wait_seconds=flow.RUN_LOCK_WAIT):
                    state.pause_pr(a.repo, a.pr, "run-failed", recovery)
                    state.enqueue_notification("user", f"{a.repo}#{a.pr} 已暂停。{recovery}",
                                               f"{a.repo}:{a.pr}:run-failed:{key}")
            except (OSError, StateCorruptError, StateLockedError) as error:
                print("autopilot: 暂停记录或通知未写入：%s" % error, file=sys.stderr)
        return 2
    finally:
        tokens.revoke(token)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print("autopilot run %s#%d：%s（%s）%s" % (a.repo, a.pr, result["result"], result["step"], result["reason"]))
        if result.get("route"):
            print("  route=" + result["route"])
            for reason in result["routeReasons"]:
                print("  " + reason)
        for n in result["notes"]:
            print("  说明：" + n)
    return 0 if result["result"] in ("merged", "automerge-on", "handed-to-user", "dry-run") else 1


if __name__ == "__main__":
    sys.exit(main())
