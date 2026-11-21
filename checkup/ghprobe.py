"""Read-only GitHub probes for the repository-level checks (C3, C4, C6, C7).

Ported from repo-readiness at commit ac13f17:
  src/repo_readiness/platform/github.py  (GhResponse, LiveTransport, GitHub, ReplayTransport)
  src/repo_readiness/probes/github.py    (effective_branch_rules, main_ci_green, secret_scanning,
                                          workflow_permissions)
Every request is `gh api -i --method GET <path>`; nothing else is ever run. Messages are Chinese.
Each probe returns (status, detail) with status in ok / missing / unknown; a response that means
"environment not ready" (not logged in, network, rate limit, 5xx) raises EnvNotReady instead.
"""
import hashlib
import json
import os
import re
from dataclasses import dataclass, field

from checkup.proc import run

_PATH_OK = re.compile(r"[A-Za-z0-9_][A-Za-z0-9/_.,%=&?+~-]*\Z")
_STATUS = re.compile(r"HTTP/\S+ (\d{3})")
ENV_STATUSES = {"unauthenticated", "network", "rate_limited", "server_error", "gh_missing"}


class EnvNotReady(Exception):
    """gh is not logged in / not installed, or the network is down: exit code 3."""


@dataclass
class GhResponse:
    status: str
    http_status: int = None
    data: object = None
    message: str = ""
    headers: dict = field(default_factory=dict)

    @property
    def env_error(self):
        return self.status in ENV_STATUSES

    @property
    def ok(self):
        return self.status == "ok"


def _check_path(path):
    if not isinstance(path, str) or not _PATH_OK.match(path) or ".." in path.split("/"):
        raise ValueError("refusing GitHub API path %r (only plain read-only REST paths are allowed)" % (path,))


class LiveTransport:
    """`runner(argv, timeout_s)` -> proc.RunResult; injectable so tests never start gh."""

    def __init__(self, gh="gh", timeout_s=30, runner=None):
        self.gh = gh
        self.timeout_s = timeout_s
        self.runner = runner or (lambda argv, t: run(argv, cwd="/", timeout_s=t))

    def fetch(self, path):
        r = self.runner([self.gh, "api", "-i", "--method", "GET", path], self.timeout_s)
        if r.timed_out:
            return GhResponse("network", message="gh api 超时（%d 秒）" % self.timeout_s)
        if r.code == 3 and r.stderr.startswith("executable not found"):
            return GhResponse("gh_missing", message="没有找到 gh 命令行工具")
        if r.code == 4:
            return GhResponse("unauthenticated", message=r.stderr.strip() or "gh 没有登录（先运行 gh auth login）")
        text = r.stdout.replace("\r\n", "\n")
        head, _, body = text.partition("\n\n")
        lines = head.split("\n")
        m = _STATUS.match(lines[0]) if lines else None
        if not m:
            return GhResponse("network", message=(r.stderr.strip() or "gh 没有返回 HTTP 响应")[:500])
        code = int(m.group(1))
        headers = {}
        for line in lines[1:]:
            k, sep, v = line.partition(":")
            if sep:
                headers[k.strip().lower()] = v.strip()
        try:
            data = json.loads(body) if body.strip() else None
        except ValueError:
            data = None
        msg = data.get("message", "") if isinstance(data, dict) else ""
        if 200 <= code < 300:
            status = "ok"
        elif code == 404:
            status = "not_found"
        elif code == 401:
            status = "unauthenticated"
        elif code in (403, 429) and (headers.get("x-ratelimit-remaining") == "0" or "rate limit" in msg.lower()):
            status = "rate_limited"
        elif code == 403:
            status = "forbidden"
        elif code >= 500:
            status = "server_error"
        else:
            status = "forbidden" if code == 422 else "server_error"
        return GhResponse(status, code, data, msg or r.stderr.strip()[:500], headers)


def _record_name(path):
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16] + ".json"


class ReplayTransport:
    """Answers only from recorded responses (tests); an unrecorded path is a test bug."""

    def __init__(self, dir_):
        self.dir = dir_

    def fetch(self, path):
        f = os.path.join(str(self.dir), _record_name(path))
        if not os.path.isfile(f):
            raise KeyError("no recording for %r in %s" % (path, self.dir))
        with open(f, encoding="utf-8") as fh:
            rec = json.load(fh)
        return GhResponse(rec["status"], rec.get("http_status"), rec.get("data"), rec.get("message", ""),
                          rec.get("headers", {}))


class GitHub:
    def __init__(self, transport):
        self.transport = transport
        self._cache = {}

    def get(self, path):
        _check_path(path)
        if path not in self._cache:
            r = self.transport.fetch(path)
            if r.env_error:
                raise EnvNotReady("%s：%s" % (r.status, r.message))
            self._cache[path] = r
        return self._cache[path]


# --------------------------------------------------------------------------- C3

_RULE_TYPES = {"deletion": "no_deletion", "non_fast_forward": "no_force_push", "pull_request": "pull_request_required"}
_RULE_ZH = {"no_deletion": "禁止删除分支", "no_force_push": "禁止强推", "pull_request_required": "只能经 PR 合入"}
_ADMIN_ROLE_ID = 5  # built-in repository role id of "admin" in ruleset bypass lists
_HUMAN_ADMIN_TYPES = {"OrganizationAdmin", "EnterpriseOwner", "User"}


class _Source:
    def __init__(self, label):
        self.label = label
        self.blocked = []  # bypassers that could be (or act for) an agent
        self.flagged = []  # admin / individual bypassers: allowed, reported


def _classify_bypass(actor, src):
    kind = actor.get("actor_type")
    who = "%s(%s)" % (kind, actor.get("actor_id"))
    if kind == "RepositoryRole" and actor.get("actor_id") == _ADMIN_ROLE_ID:
        src.flagged.append("管理员角色")
    elif kind in _HUMAN_ADMIN_TYPES:
        src.flagged.append(who)
    else:
        src.blocked.append(who)


def _legacy(gh, name, branch, provided, unreadable, notes):
    r = gh.get("repos/%s/branches/%s/protection" % (name, branch))
    if r.status == "not_found" and r.message == "Branch not protected":
        notes.append("旧版分支保护：未设置")
        return
    if not r.ok:
        unreadable.append("旧版分支保护（%s）" % (r.message or r.status))
        return
    d = r.data
    src = _Source("旧版保护")
    if not d.get("enforce_admins", {}).get("enabled", False):
        src.flagged.append("管理员不受旧版保护约束")
    rpr = d.get("required_pull_request_reviews")
    if rpr:
        allow = rpr.get("bypass_pull_request_allowances") or {}
        for a in allow.get("apps", []):
            src.blocked.append("app %s" % a.get("slug"))
        for t in allow.get("teams", []):
            src.blocked.append("team %s" % t.get("slug"))
        for u in allow.get("users", []):
            src.flagged.append("user %s" % u.get("login"))
        provided["pull_request_required"].append(src)
    if not d.get("allow_force_pushes", {}).get("enabled", True):
        provided["no_force_push"].append(src)
    if not d.get("allow_deletions", {}).get("enabled", True):
        provided["no_deletion"].append(src)
    gives = [_RULE_ZH[k] for k, v in provided.items() if src in v]
    notes.append("旧版分支保护：%s" % ("、".join(gives) if gives else "没有相关规则"))


def _rulesets(gh, name, branch, provided, unreadable, notes):
    r = gh.get("repos/%s/rules/branches/%s" % (name, branch))
    if not r.ok or not isinstance(r.data, list):
        unreadable.append("生效规则（%s）" % (r.message or r.status))
        return
    by_id = {}
    for rule in r.data:
        key = (rule.get("ruleset_source_type"), rule.get("ruleset_source"), rule.get("ruleset_id"))
        by_id.setdefault(key, set()).add(rule.get("type"))
    if not by_id:
        notes.append("规则集：无")
    for (stype, source, rid), types in sorted(by_id.items(), key=lambda kv: str(kv[0])):
        prefix = "orgs" if stype == "Organization" else "repos"
        d = gh.get("%s/%s/rulesets/%s" % (prefix, source, rid))
        if not d.ok:
            unreadable.append("规则集 %s（%s）" % (rid, d.message or d.status))
            continue
        rs_name = d.data.get("name", rid)
        if d.data.get("enforcement") != "active":
            notes.append("规则集「%s」处于 %s，不计" % (rs_name, d.data.get("enforcement")))
            continue
        if "bypass_actors" not in d.data:
            unreadable.append("规则集「%s」的绕过名单（需要管理员权限）" % rs_name)
            continue
        src = _Source("规则集「%s」" % rs_name)
        for actor in d.data["bypass_actors"]:
            _classify_bypass(actor, src)
        gives = sorted(_RULE_TYPES[t] for t in types if t in _RULE_TYPES)
        for req in gives:
            provided[req].append(src)
        notes.append("%s：%s" % (src.label, "、".join(_RULE_ZH[g] for g in gives) or "没有相关规则"))


def effective_branch_rules(gh, name, branch):
    """C3: rulesets plus classic protection together give no force push, no deletion, PR required."""
    provided = {req: [] for req in _RULE_TYPES.values()}
    unreadable, notes = [], []
    _legacy(gh, name, branch, provided, unreadable, notes)
    _rulesets(gh, name, branch, provided, unreadable, notes)
    missing, flags = [], []
    for req in _RULE_TYPES.values():
        sources = provided[req]
        clean = [s for s in sources if not s.blocked]
        if clean:
            if all(s.flagged for s in clean):
                flags.extend(f for s in clean for f in s.flagged)
            continue
        if sources:
            missing.append("「%s」可被绕过：%s" % (_RULE_ZH[req], "；".join(
                "%s 的绕过名单含 %s" % (s.label, ", ".join(s.blocked)) for s in sources)))
        else:
            missing.append("「%s」没有生效" % _RULE_ZH[req])
    evidence = list(notes)
    if flags:
        evidence.append("存在绕过（只有管理员或个人账号）：%s" % "、".join(sorted(set(flags))))
    if missing:
        if unreadable:
            return "unknown", "读不到 %s，已读到的部分不足以判定：%s" % ("、".join(unreadable), "；".join(missing)), evidence
        return "missing", "；".join(missing), evidence
    return "ok", "禁止强推、禁止删除、只能经 PR 合入都已生效", evidence


# --------------------------------------------------------------------------- C4, C6, C7

_NOT_A_VERDICT = {"cancelled", "skipped"}
_BRANCH_EVENTS = {"push", "schedule", "workflow_dispatch", "merge_group"}


def main_ci_green(gh, name, branch):
    """C4: the latest completed push / schedule run on the branch concluded success (no age limit)."""
    r = gh.get("repos/%s/actions/runs?branch=%s&status=completed&per_page=30" % (name, branch))
    if not r.ok:
        return "unknown", "读不到运行记录：%s %s" % (r.status, r.message), []
    runs = [x for x in r.data.get("workflow_runs", []) if x.get("event") in _BRANCH_EVENTS
            and x.get("head_branch") == branch and x.get("conclusion") not in _NOT_A_VERDICT]
    if not runs:
        return "missing", "最近 30 次运行里没有 %s 上已完成的推送或定时运行" % branch, []
    latest = max(runs, key=lambda x: x["created_at"])
    ev = ["%s 上最近一次 %s 运行：%s「%s」%s（%s）" % (branch, latest["event"], latest.get("conclusion"),
                                               latest.get("name"), latest.get("html_url", ""), latest["created_at"])]
    if latest.get("conclusion") != "success":
        return "missing", "最近一次运行的结论是 %s" % latest.get("conclusion"), ev
    return "ok", "最近一次运行成功", ev


def secret_scanning(gh, name, has_workflows):
    """C6: secret scanning and push protection are enabled."""
    r = gh.get("repos/%s" % name)
    if not r.ok:
        return "unknown", "读不到仓库信息：%s %s" % (r.status, r.message), []
    ssa = r.data.get("security_and_analysis") or {}
    scan = (ssa.get("secret_scanning") or {}).get("status")
    push = (ssa.get("secret_scanning_push_protection") or {}).get("status")
    if scan is None or push is None:
        return "unknown", ("GitHub 没有报告本仓库的密钥扫描状态（token 没有管理员权限，或这类仓库不提供）；"
                           "请到仓库 Settings → Code security 手动确认"), []
    ev = ["密钥扫描 %s，推送保护 %s" % (scan, push)]
    missing = []
    if scan != "enabled":
        missing.append("密钥扫描是 %s" % scan)
    if push != "enabled":
        missing.append("推送保护是 %s" % push)
    if missing:
        return "missing", "；".join(missing), ev
    return "ok", "密钥扫描和推送保护都已开启", ev


def workflow_permissions(gh, name):
    """C7: the default GITHUB_TOKEN permission is read-only."""
    r = gh.get("repos/%s/actions/permissions/workflow" % name)
    if not r.ok:
        return "unknown", "读不到 workflow 默认权限：%s %s（需要管理员权限）" % (r.status, r.message), []
    default = r.data.get("default_workflow_permissions")
    ev = ["默认 GITHUB_TOKEN 权限 %s；workflow 可批准 PR：%s" % (default, r.data.get("can_approve_pull_request_reviews"))]
    if default != "read":
        return "missing", "默认 token 权限是 %s，应为 read" % default, ev
    return "ok", "默认 token 只读", ev
