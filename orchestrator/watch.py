"""Reconcile GitHub auto-merge and CI with the local watch state."""

from datetime import datetime, timedelta
import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlencode

from degrade import run as degrade
from orchestrator import classify, flow, token
from orchestrator.state import REPAIR_LIMIT


REPO = re.compile(r"[\w.-]+/[\w.-]+")
PR_TARGET = re.compile(rf"({REPO.pattern})(?:#(\d+))?")
ACTIVE_PRS = ("automerge-requested", "automerge-on")
TIMEOUT = timedelta(minutes=60)
RECOVERY = "重新运行 autopilot run 并通过验证"
WATCH_ERRORS = (degrade.ApiError, flow.Stop, token.TokenError, OSError, subprocess.TimeoutExpired)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def clean_detail(value):
    value = re.sub(r"\b(?:gh[pousr]_[\w]+|github_pat_[\w]+|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b", "[redacted]", value)
    return flow.defang(" ".join(value.split())[:500])


def pause_intent(repo, number, record, reason, recovery, *, user=False, notify=None):
    return dict(action="watch-pause-pr", repo=repo, pr=number, head=record.get("validatedHeadOid"),
                since=record.get("since"), reason=reason, recovery=recovery, user=user, notify=notify)


def report_failure(state, repo, number, stage, detail, ledger):
    detail = clean_detail(detail)
    target = f"{repo}#{number}" if number else repo
    recovery = f"解决「{stage}」的问题后重试该步骤"
    record = state.get_pr(repo, number) or {}
    cycle = record.get("resumeCount", 0)
    head = record.get("validatedHeadOid") or "-"
    fingerprint = digest(json.dumps([stage, detail, record.get("since"), cycle]))
    key = f"{repo}:{number}:{head}:report-failure:{fingerprint}"
    if state.is_done(key):
        return
    if state.intend(key, dict(action="report-failure", repo=repo, pr=number)):
        ledger(dict(event="watch-intent", action="report-failure", repo=repo, pr=number, key=key,
                    at=state.now().isoformat()))
    if record.get("state") in ACTIVE_PRS:
        state.intend(key + ":close", pause_intent(repo, number, record, stage, recovery))
    state.enqueue_notification("user", f"{target} 已暂停。{stage} 失败：{detail}。{recovery}。",
                               key + ":user", repo=repo, number=number or None)
    state.pause_pr(repo, number, stage, recovery, detail=detail)
    state.mark_done(key)


def resume(state, repo, number):
    if number is None:
        state.resume(repo)
        return
    pr = state.get_pr(repo, number)
    if pr and pr.get("state") == "paused":
        # A resume does not restore a GitHub auto-merge request. Run must validate it again.
        state.set_pr(repo, number, state=pr.get("resumeState") or "handed-to-user", pause=None,
                     resumeState=None, resumeCount=pr.get("resumeCount", 0) + 1)


def pages(payload, field):
    """Flatten object pages returned by LocalIO.api(… --paginate)."""
    payload = payload if isinstance(payload, list) else [payload]
    if any(not isinstance(page, dict) or not isinstance(page.get(field), list) for page in payload):
        raise degrade.ApiError(f"GitHub 返回的 {field} 数据不完整")
    rows = [row for page in payload for row in page[field]]
    if any(not isinstance(row, dict) for row in rows):
        raise degrade.ApiError(f"GitHub 返回的 {field} 条目不完整")
    return rows


def checked_run(run):
    if (not isinstance(run, dict) or type(run.get("id")) is not int
            or type(run.get("run_attempt", 1)) is not int or run.get("run_attempt", 1) < 1
            or not run.get("head_sha") or not run.get("status")):
        raise degrade.ApiError("GitHub 返回的 CI 运行数据不完整")
    return run


def timestamp(stamp):
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if when.utcoffset() is not None:
            return when
    except (AttributeError, ValueError):
        pass
    raise degrade.ApiError("GitHub 或 watch 状态缺少有效时间")


class Watch:
    """One round under the caller's WatchState lock; remote I/O is injected by the CLI."""

    def __init__(self, state, config, io_for, *, followups, narrow, ledger, ledger_events):
        self.state = state
        self.config = config
        self.io_for = io_for
        self.followups = followups
        self.narrow = narrow
        self.ledger = ledger
        self.logged = {row.get("key") for row in ledger_events() if isinstance(row, dict)}
        self.failed = False

    def event(self, key, event, **fields):
        if key not in self.logged:
            self.ledger(dict(key=key, event=event, at=self.state.now().isoformat(), **fields))
            self.logged.add(key)

    def intent(self, key, action, repo, **fields):
        self.state.intend(key, dict(action=action, repo=repo, **fields))
        self.event(key, "watch-intent", action=action, repo=repo, **fields)

    def effect(self, key, action, repo, perform, **fields):
        if not self.state.is_done(key):
            self.intent(key, action, repo, **fields)
            perform()
            self.state.mark_done(key)

    def notice(self, key, repo, number, target, body):
        if target:
            self.state.enqueue_notification(target, body, f"{key}:notice:{target}", repo=repo, number=number)

    def paused(self, reason, recovery):
        return dict(reason=reason, recovery=recovery, at=self.state.now().isoformat())

    def error(self, repo, stage, exc, *, io=None, number=None, subject=None):
        self.failed = True
        if isinstance(exc, subprocess.TimeoutExpired):
            detail = "本机命令超时"
        else:
            detail = str(exc)
            if io and getattr(io, "token", None):
                detail = detail.replace(io.token, "[redacted]")
        detail = clean_detail(detail)
        recovery = f"核对「{stage}」及其调用权限后重试"
        reason = f"{stage} 失败：{detail}"
        key = f"{repo}:{subject or number or 'repo'}:-:watch-error:{digest(reason)}"
        self.event(key, "watch-error", repo=repo, pr=number, detail=reason)
        label = f"{repo}#{number}" if number else repo
        self.notice(key, repo, number, "user", f"{label} 已暂停。{reason}。{recovery}。")
        return reason, recovery

    def prepare_pr_pause(self, repo, number, pr, key, reason, recovery, *, user=False, session=False):
        key = self.next_key(key)
        intent = pause_intent(repo, number, pr, reason, recovery, user=user, notify=pr.get("notify") if session else None)
        self.intent(key, **intent)
        return key, self.state.pending_intents()[key]

    def pr_pause(self, io, repo, number, pr, key, reason, recovery, *, user=False, session=False):
        self.finish_pr_pause(io, *self.prepare_pr_pause(repo, number, pr, key, reason, recovery,
                                                       user=user, session=session))

    def finish_pr_pause(self, io, key, intent):
        repo, number = intent["repo"], intent["pr"]
        pr = self.state.get_pr(repo, number) or {}
        if (pr.get("validatedHeadOid"), pr.get("since")) == (intent["head"], intent["since"]):
            self.event(key, "watch-intent", **intent)
            flow.disable_queued_automerge(io, repo, number)  # Checks the live queue before writing.
            if pr.get("state") not in ("handed-to-user", "merged", "post-merge-done"):
                body = f"{repo}#{number} 已暂停。{intent['reason']}。恢复条件：{intent['recovery']}。"
                if intent["user"]:
                    self.notice(key, repo, number, "user", body)
                self.notice(key, repo, number, intent["notify"], body)
                self.state.pause_pr(repo, number, intent["reason"], intent["recovery"], resumeState=None)
                self.finish_enable(repo, number)
        self.state.mark_done(key)

    def pause_pr_error(self, io, repo, number, reason, recovery):
        record = self.state.get_pr(repo, number) or {}
        merged = record.get("state") == "merged" or record.get("resumeState") == "merged"
        close = None
        pending = any((intent.get("action"), intent.get("repo"), intent.get("pr")) == (
            "watch-pause-pr", repo, number) for intent in self.state.pending_intents().values())
        if not pending and not merged and record.get("validatedHeadOid"):
            key = f"{repo}:{number}:{record['validatedHeadOid']}:{record.get('since')}:watch-error"
            close = self.prepare_pr_pause(repo, number, record, key, reason, recovery)
        self.state.pause_pr(repo, number, reason, recovery, resumeState="merged" if merged else None)
        if close and io is not None:
            try:
                self.finish_pr_pause(io, *close)
            except WATCH_ERRORS:
                pass  # The pause intent remains until the queue can be checked.

    def finish_enable(self, repo, number):
        for key in flow.pending_automerge_intents(self.state, repo, number):
            self.state.mark_done(key)

    def next_key(self, prefix):
        key = prefix
        generation = 0
        while self.state.is_done(key):
            generation += 1
            key = f"{prefix}:{generation}"
        return key

    def close_queued(self, io, repo, group, reason):
        queued = io.api(f"repos/{repo}/pulls?state=open&per_page=100")
        if not isinstance(queued, list) or any(not isinstance(pr, dict) or type(pr.get("number")) is not int
                                                for pr in queued):
            raise degrade.ApiError("GitHub 返回的开放 PR 列表不完整")
        pending = {detail["pr"]: key for key, detail in self.state.pending_intents().items()
                   if detail.get("action") == "watch-disable" and detail.get("group") == group}
        live = {pr["number"]: pr for pr in queued if pr.get("auto_merge") is not None}
        errors = []
        for number in sorted(live.keys() | pending.keys()):
            pr = live.get(number, {})
            head = (pr.get("head") or {}).get("sha", "-")
            key = pending.get(number)
            if key is None:
                request = digest(json.dumps(pr["auto_merge"], sort_keys=True))[:16]
                prefix = f"{repo}:{number}:{head}:disable:{group}:{request}"
                # A fresh remote queue may be opened while the hold remains active.
                key = self.next_key(prefix)

            def close():
                flow.disable_queued_automerge(io, repo, number)
                record = self.state.get_pr(repo, number) or {}
                if record.get("state") in ACTIVE_PRS:
                    self.state.pause_pr(repo, number, reason, RECOVERY, resumeState=None)
                    self.finish_enable(repo, number)
                self.event(key + ":closed", "watch-automerge-closed", repo=repo, pr=number, reason=reason)

            try:
                self.effect(key, "watch-disable", repo, close, pr=number, group=group)
            except WATCH_ERRORS as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def verified(self, io, repo, head):
        statuses = pages(io.api(f"repos/{repo}/commits/{head}/status?per_page=100"), "statuses")
        status = next((row for row in statuses if row.get("context") == flow.VERIFIED), {})
        creator = status.get("creator")
        bot = io.bot_identity()
        if creator and bot and creator.get("id") != bot["id"]:
            return {}
        return status

    def inspect_pr(self, io, repo, number, record):
        pr = io.api(f"repos/{repo}/pulls/{number}")
        if not isinstance(pr, dict):
            raise degrade.ApiError("GitHub 返回的 PR 数据不完整")
        head = record.get("validatedHeadOid")
        key = f"{repo}:{number}:{head or '-'}:{record.get('since', '-')}"
        if record["state"] != "merged":
            live_head = (pr.get("head") or {}).get("sha")
            status = self.verified(io, repo, head) if head and live_head == head else {}
            if live_head != head or status.get("state") != "success":
                reason = "PR 的 head 已变化" if live_head != head else f"{flow.VERIFIED} 未通过"
                self.pr_pause(io, repo, number, record, key + ":invalid", reason, RECOVERY, session=True)
                return
            if pr.get("merged"):
                self.state.set_pr(repo, number, state="merged", mergeCommitOid=pr.get("merge_commit_sha"),
                                  evidenceUrl=status.get("target_url"))
            else:
                queue = pr.get("auto_merge")
                since = record.get("since") or (queue or {}).get("enabled_at")
                if since and self.state.now() - timestamp(since) > TIMEOUT:
                    self.pr_pause(io, repo, number, record, key + ":queue-timeout", "自动合并已等待超过 60 分钟",
                                  RECOVERY, user=True)
                    return
                if queue is None:
                    if record["state"] == "automerge-requested":
                        # Run may still be between persisting its request and opening the queue.
                        return
                    self.pr_pause(io, repo, number, record, key + ":queue-missing", "GitHub 上已没有自动合并请求",
                                  RECOVERY, session=True)
                    return
                if record["state"] == "automerge-requested":
                    self.state.set_pr(repo, number, state="automerge-on")
                self.finish_enable(repo, number)
                self.inspect_pr_ci(io, repo, number, record)
                return

        self.finish_enable(repo, number)
        post_key = f"{repo}:{number}:{head}:post-merge"
        self.intent(post_key, "watch-post-merge", repo, pr=number)

        def label():
            if not any(label.get("name") == flow.AUTO_MERGED for label in pr.get("labels", [])):
                io.api(f"repos/{repo}/issues/{number}/labels", "POST", {"labels": [flow.AUTO_MERGED]})

        self.effect(post_key + ":label", "watch-label-merged", repo, label, pr=number)

        def follow():
            def issue_api(path, method="GET", body=None):
                if method == "GET":
                    return io.api(path)
                payload = digest(json.dumps([path, method, body], sort_keys=True))
                key = self.next_key(f"{post_key}:followup-write:{payload}")
                self.intent(key, "watch-write-followup", repo, pr=number, postMerge=post_key)
                result = io.api(path, method, body)
                self.state.mark_done(key)
                return result

            result = self.followups(io, repo, number, pr, head=head, issue_api=issue_api)
            if result["failed"]:
                raise degrade.ApiError("合并后的跟进 issue 有条目尚未写入")
            for key, intent in self.state.pending_intents().items():
                if intent.get("postMerge") == post_key:
                    self.state.mark_done(key)  # The followups reader found these writes on GitHub.

        self.effect(post_key + ":followups", "watch-open-followups", repo, follow, pr=number)
        self.event(post_key + ":done", "watch-post-merge-done", repo=repo, pr=number, head=head)
        self.state.set_pr(repo, number, state="post-merge-done", pause=None, resumeState=None)
        self.state.mark_done(post_key)

    def inspect_prs(self):
        # Reconcile a run killed between its intent write and the PR record write.
        for detail in self.state.pending_intents().values():
            if detail.get("action") == "enable-automerge":
                repo, number = detail["repo"], detail["pr"]
                record = self.state.get_pr(repo, number)
                if record is None or (detail.get("since") or "") > (record.get("since") or ""):
                    fields = {k: v for k, v in detail.items() if k not in ("action", "repo", "pr")}
                    self.state.set_pr(repo, number, state="automerge-requested", pause=None, **fields)

        pending = {(i["repo"], i["pr"]): (key, i) for key, i in self.state.pending_intents().items()
                   if i.get("action") == "watch-pause-pr"}
        for target, record in self.state.read()["prs"].items():
            repo, number = target.rsplit("#", 1)
            number = int(number)
            phase = record["state"]
            close = pending.get((repo, number))
            if not close and phase not in (*ACTIVE_PRS, "merged"):
                continue
            io = None
            try:
                io = self.io_for(repo)
                if close:
                    self.finish_pr_pause(io, *close)
                else:
                    self.inspect_pr(io, repo, number, record)
            except WATCH_ERRORS as exc:
                reason, recovery = self.error(repo, "PR 巡检", exc, io=io, number=number)
                self.pause_pr_error(io, repo, number, reason, recovery)

    def list_runs(self, io, repo, **filters):
        path = f"repos/{repo}/actions/runs"
        query = urlencode(dict(filters, per_page=100))
        result = io.api(f"{path}?{query}")
        first_page = result[0] if isinstance(result, list) and result else result
        if isinstance(first_page, dict) and first_page.get("total_count", 0) >= 1000:
            # GitHub caps filtered Actions searches at 1000 results.
            result = io.api(path + "?per_page=100")
        fields = {"branch": "head_branch"}
        runs = (checked_run(run) for run in pages(result, "workflow_runs")
                if all(run.get(fields.get(key, key)) == value for key, value in filters.items()))
        return sorted(runs,
                      key=lambda run: (run["id"], run.get("run_attempt", 1)))

    def reconcile_reruns(self, io, repo, event, number=None, head=None):
        for key, intent in self.state.pending_intents().items():
            if (intent.get("action"), intent.get("repo"), intent.get("trigger"), intent.get("pr")) != (
                    "watch-rerun", repo, event, number):
                continue
            if head is not None and head != intent["head"]:
                self.state.mark_done(key)
                continue
            run_id, attempt = intent["run_id"], intent["attempt"]
            live = checked_run(io.api(f"repos/{repo}/actions/runs/{run_id}"))
            if live.get("run_attempt", 1) > attempt or live["status"] != "completed":
                self.state.set_run(repo, run_id, attempt, **dict(intent["fields"], state="rerun"))
                self.state.mark_done(key)

    def inspect_pr_ci(self, io, repo, number, record):
        head = record["validatedHeadOid"]
        self.reconcile_reruns(io, repo, "pull_request", number, head)
        for run in self.list_runs(io, repo, event="pull_request", head_sha=head):
            linked = run.get("pull_requests") or []
            if linked and not any(item.get("number") == number for item in linked):
                continue
            if self.inspect_ci_run(io, repo, run, number=number, pr_record=record) == "paused":
                break

    def inspect_ci_run(self, io, repo, summary, *, number=None, pr_record=None, base=None):
        run_id, attempt = summary["id"], summary.get("run_attempt", 1)
        old = self.state.get_run(repo, run_id, attempt) or {}
        if old.get("state") not in (None, "pending") and not old.get("retry"):
            return old["state"]

        fields = dict(event=summary["event"], headSha=summary["head_sha"], pr=number, reruns=attempt - 1)
        try:
            run = checked_run(io.api(f"repos/{repo}/actions/runs/{run_id}"))
            if (run["id"], run.get("event"), run["head_sha"]) != (run_id, summary["event"], summary["head_sha"]):
                raise degrade.ApiError("CI 运行的标识、事件或提交已变化")
            attempt = run.get("run_attempt", 1)
            old = self.state.get_run(repo, run_id, attempt) or {}
            if old.get("state") not in (None, "pending") and not old.get("retry"):
                return old["state"]

            key = f"{repo}:{run_id}:{attempt}:{run['head_sha']}"
            fields.update(reruns=attempt - 1, retry=False, pause=None)
            result = self.classify_ci(io, repo, run, key, fields, number, pr_record, base)
            self.state.set_run(repo, run_id, attempt, **fields, state=result)
            return result
        except WATCH_ERRORS as exc:
            reason, recovery = self.pause_run_error(io, repo, run_id, attempt, fields, exc)
            # A PR pause requires an explicit resume or a new successful run. Main can safely catch up.
            if pr_record is not None:
                self.pause_pr_error(io, repo, number, reason, recovery)
                return "paused"
            return "retry"

    def pause_run_error(self, io, repo, run_id, attempt, fields, exc):
        record = self.state.get_run(repo, run_id, attempt) or {}
        thread = record.get("degradeIssue") or fields.get("pr")
        reason, recovery = self.error(repo, "CI 巡检", exc, io=io, number=thread, subject=f"{run_id}:{attempt}")
        self.state.set_run(repo, run_id, attempt, **dict(fields, state="paused", retry=True,
                                                         pause=self.paused(reason, recovery)))
        return reason, recovery

    def classify_ci(self, io, repo, run, key, fields, number, pr, base):
        run_id, head = run["id"], run["head_sha"]
        attempt = run.get("run_attempt", 1)
        conclusion = run.get("conclusion")
        notify = pr.get("notify") if pr else (self.repo_settings(repo).get("notify") if base else None)
        complete = run["status"] == "completed"
        started = run.get("run_started_at") or run.get("created_at")
        ended = run.get("updated_at") if complete else self.state.now().isoformat()
        if conclusion == "timed_out" or (ended and timestamp(ended) - timestamp(started) > TIMEOUT):
            if base:
                self.link_main_pr(io, repo, run["head_sha"], base, fields)
            return self.pause_ci(io, repo, fields["pr"], pr, key, fields, "CI 运行超过 60 分钟", RECOVERY if pr else
                                 "检查运行后重新触发主干 CI")
        if not complete:
            return "pending"
        if conclusion == "success":
            rerun = classify.classify_rerun(run)
            if rerun:
                fields.update(classification=rerun[0], reason=rerun[1])
            if base:
                fields.update(self.main_evidence(io, repo, run, base))
            return "green"
        if conclusion in ("cancelled", "skipped", "neutral"):
            fields["conclusion"] = conclusion
            return "red-classified"

        jobs = []
        if conclusion != "startup_failure":
            # The attempt-specific endpoint avoids reading a different job set if a rerun starts concurrently.
            jobs = pages(io.api(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"), "jobs")
        category, reason = classify.classify_failure(run, jobs)
        fields.update(classification=category, reason=reason)
        if category == "env" and attempt <= 2:
            rerun_key = key + ":rerun"
            self.intent(rerun_key, "watch-rerun", repo, trigger=run["event"], head=head,
                        pr=number, run_id=run_id, attempt=attempt, fields=dict(fields, reruns=attempt))
            if not self.state.is_done(rerun_key):
                live = checked_run(io.api(f"repos/{repo}/actions/runs/{run_id}"))
                if live.get("run_attempt", 1) == attempt and live["status"] == "completed":
                    io.api(f"repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs", "POST")
                self.state.set_run(repo, run_id, attempt, **dict(fields, state="rerun", reruns=attempt))
                self.state.mark_done(rerun_key)
            fields["reruns"] = attempt
            return "rerun"
        if category in ("config", "env"):
            if base:
                self.link_main_pr(io, repo, head, base, fields)
            if category == "config":
                reason = f"CI 配置错误：{reason}"
                recovery = "由用户批准并合并 CI 配置修复后重新验证"
                if pr is None:
                    notify = notify or (self.state.get_pr(repo, fields.get("pr")) or {}).get("notify")
            else:
                reason = f"CI 已重跑两次仍失败：{reason}"
                recovery = RECOVERY if pr else "处理 CI 环境问题后重新触发主干 CI"
            return self.pause_ci(io, repo, fields["pr"], pr, key, fields, reason, recovery,
                                 notify=notify if category == "config" else None)

        if pr:
            repair_key = f"check:{run.get('name') or run.get('workflow_id') or 'CI'}"
            commits = self.state.record_failure(repo, number, repair_key, head)
            if len(commits) >= REPAIR_LIMIT:
                reason = (f"同一原因修了两次仍失败：{repair_key}。已试过 {'、'.join(commits)}。"
                          "可继续修复并重新验证、修订方案后重新确认，或交用户手工处理")
                return self.pause_ci(io, repo, number, pr, key, fields, reason, "用户决定")
            self.notice(key, repo, number, notify,
                        f"{repo}#{number} 的 CI 测试失败。{reason}。请分析代码或用例。{run.get('html_url', '')}")
        else:
            incident = key + ":main-red"
            self.intent(incident, "watch-main-red", repo, run_id=run_id, attempt=attempt, base=base,
                        run_url=run.get("html_url"), fields=fields)
            fields.update(self.finish_main_red(io, incident, self.state.pending_intents()[incident]))
        return "red-classified"

    def pause_ci(self, io, repo, number, pr, key, fields, reason, recovery, *, notify=None):
        if pr:
            self.pr_pause(io, repo, number, pr, key + ":pause", reason, recovery, user=True, session=bool(notify))
        else:
            self.notice(key, repo, number, "user", f"{repo} 的主干 CI 已暂停。{reason}。{recovery}。")
            self.notice(key, repo, number, notify, f"{repo} 的主干 CI 已暂停。{reason}。{recovery}。")
        fields["pause"] = self.paused(reason, recovery)
        return "paused"

    def repo_settings(self, repo):
        repos = self.config.get("repos")
        settings = repos.get(repo) if isinstance(repos, dict) else None
        return settings if isinstance(settings, dict) else {}

    def main_pr(self, io, repo, head, base):
        candidates = io.api(f"repos/{repo}/commits/{head}/pulls?per_page=100") or []
        # Match the merge commit to avoid linking a PR that later picked up the same commit.
        return next((pr for pr in candidates if pr.get("merge_commit_sha") == head
                     and (pr.get("base") or {}).get("ref") == base), None)

    def link_main_pr(self, io, repo, head, base, fields):
        pr = self.main_pr(io, repo, head, base)
        if pr:
            fields.update(pr=pr["number"], prUrl=pr.get("html_url"))

    def main_evidence(self, io, repo, run, base):
        head = run["head_sha"]
        pr = self.main_pr(io, repo, head, base)
        if pr is None:
            return {}
        number = pr["number"]
        record = self.state.get_pr(repo, number) or {}
        pr_head = (pr.get("head") or {}).get("sha")
        validated_head = record.get("validatedHeadOid") or pr_head
        fields = dict(pr=number, prUrl=pr.get("html_url"))
        if not validated_head or validated_head != pr_head:
            return fields
        evidence = record.get("evidenceUrl") if record.get("mergeCommitOid") == head else None
        if not evidence:
            status = self.verified(io, repo, validated_head)
            if status.get("state") != "success":
                return fields
            evidence = status.get("target_url")
        fields.update(validatedHeadOid=validated_head, evidenceUrl=evidence)
        return fields

    def degrade_issue(self, io, repo, key, candidate, run_id, attempt):
        effect_key = key + ":issue"
        if self.state.is_done(effect_key):
            return self.state.get_run(repo, run_id, attempt)["degradeIssue"]
        pending = effect_key in self.state.pending_intents()
        self.intent(effect_key, "watch-open-degrade", repo, run_id=run_id, attempt=attempt)
        number = degrade.open_degrade_keys(io.api, repo).get(("main-red", candidate["ref"]))
        marker = flow.block("autopilot-watch-degrade", {"key": digest(effect_key)})
        if number is None and pending:
            closed = io.api(f"repos/{repo}/issues?labels={degrade.DEGRADE_LABEL}&state=all&per_page=100") or []
            bot = io.bot_identity()
            number = next((issue["number"] for issue in closed if flow.bot_issue(issue, bot)
                           and marker in (issue.get("body") or "")), None)
        if number is None:
            issue = io.api(f"repos/{repo}/issues", "POST", {
                "title": f"autopilot 降级：main-red（{candidate['ref'][:12]}）",
                "body": degrade.issue_body(repo, candidate) + "\n" + marker,
                "labels": [degrade.DEGRADE_LABEL],
            })
            number = (issue or {}).get("number")
            if type(number) is not int:
                raise degrade.ApiError("GitHub 未确认降级 issue 已创建")
        self.state.set_run(repo, run_id, attempt, degradeIssue=number)
        self.state.mark_done(effect_key)
        return number

    def finish_main_red(self, io, key, intent):
        repo, run_id, attempt, base = (intent[field] for field in ("repo", "run_id", "attempt", "base"))
        fields = dict(intent["fields"])
        head = fields["headSha"]
        source = f"{run_id}:{attempt}"
        self.effect(key + ":hold", "watch-narrow-repo", repo,
                    lambda: self.state.narrow_repo(repo, "main-red", source), run_id=run_id)
        self.close_queued(io, repo, key, "主干 CI 测试失败，仓库已收紧")
        self.link_main_pr(io, repo, head, base, fields)
        candidate = dict(reason="main-red", level="L2", ref=head, evidence=[fields.get("prUrl"), intent["run_url"]])
        number = self.degrade_issue(io, repo, key, candidate, run_id, attempt)
        self.effect(key + ":narrow", "watch-open-narrow-pr", repo,
                    lambda: self.narrow(io, repo, base, number, self.state), issue=number)
        self.notice(key, repo, number, "user",
                    f"{repo} 的主干 CI 测试失败，已收紧仓库并关闭排队的自动合并。"
                    f"请查看降级 issue https://github.com/{repo}/issues/{number}。")
        self.state.set_run(repo, run_id, attempt, **fields, state="red-classified")
        self.state.mark_done(key)
        return fields

    def inspect_main(self, io, repo):
        cursor = self.state.get_cursor(repo)
        for key, intent in self.state.pending_intents().items():
            if intent.get("action") == "watch-main-red" and intent.get("repo") == repo:
                try:
                    self.finish_main_red(io, key, intent)
                except WATCH_ERRORS as exc:
                    self.pause_run_error(io, repo, intent["run_id"], intent["attempt"], intent["fields"], exc)
                    return cursor
        self.reconcile_reruns(io, repo, "push")
        base = (io.api(f"repos/{repo}") or {}).get("default_branch")
        if not base:
            raise degrade.ApiError("GitHub 未返回默认分支")
        runs = self.list_runs(io, repo, event="push", branch=base)
        records = self.state.read()["runs"]
        known = {}
        for key, record in records.items():
            if key.startswith(repo + ":") and record.get("event") == "push":
                _, run_id, attempt = key.rsplit(":", 2)
                known[int(run_id)] = max(known.get(int(run_id), 0), int(attempt))
        # A run can still be in flight while newer IDs are observed.
        listed = {run["id"] for run in runs}
        for run_id, attempt in known.items():
            record = records[f"{repo}:{run_id}:{attempt}"]
            if run_id not in listed and (record.get("state") in ("pending", "rerun") or record.get("retry")):
                live = checked_run(io.api(f"repos/{repo}/actions/runs/{run_id}"))
                if live.get("event") == "push" and live.get("head_branch") == base:
                    runs.append(live)

        for run in sorted(runs, key=lambda item: item["id"]):
            run_id = run["id"]
            if cursor is not None and run_id <= cursor and run_id not in known:
                continue
            if self.inspect_ci_run(io, repo, run, base=base) == "retry":
                break
            cursor = run_id if cursor is None else max(cursor, run_id)
        return cursor

    def deliver(self, item):
        key, target, body = (item[field] for field in ("key", "target", "body"))
        if target != "user":
            relay = str(Path.home() / ".agent-relay" / "bin" / "agent-relay")
            repo = item.get("repo")
            self.effect(key + ":relay", "watch-notify-session", repo,
                        lambda: local_notice([relay, "send", "--from", "autopilot-watch", "--to", target,
                                              "--idempotency-key", digest(key)], body), target=target)
        else:
            repo, number = item.get("repo"), item.get("number")
            if repo is None:
                match = re.match(rf"({REPO.pattern})#(\d+)\b", body)
                if match:
                    repo, number = match[1], int(match[2])
            script = 'on run argv\n display notification (item 1 of argv) with title "Autopilot"\n end run'
            self.effect(key + ":desktop", "watch-notify-desktop", repo,
                        lambda: local_notice(["osascript", "-e", script, body]))
            if repo and number:
                self.effect(key + ":comment", "watch-notify-comment", repo,
                            lambda: self.comment_notice(repo, number, key, body), issue=number)
        self.state.dequeue_notification(key)

    def comment_notice(self, repo, number, key, body):
        io = self.io_for(repo)
        marker = flow.block("autopilot-watch-notice", {"key": digest(key)})
        path = f"repos/{repo}/issues/{number}/comments"
        comments = io.api(path + "?per_page=100") or []
        bot = io.bot_identity()
        if not any(flow.bot_issue(comment, bot) and marker in (comment.get("body") or "") for comment in comments):
            io.api(path, "POST", {"body": flow.defang(body) + "\n\n" + marker})

    def deliver_notifications(self):
        for item in self.state.pending_notifications():
            try:
                self.deliver(item)
            except WATCH_ERRORS as exc:
                self.failed = True
                # The original notice is already in the outbox. Do not generate another notice about its delivery.
                reason = "通知命令超时" if isinstance(exc, subprocess.TimeoutExpired) else clean_detail(str(exc))
                key = item["key"]
                self.state.pause_notification(key, reason, "下次 watch 重试通知")
                self.event(key + ":delivery-error", "watch-notification-error", detail=reason)

    def round(self):
        snapshot = self.state.read()
        repos = set(snapshot["repos"])
        repos.update(key.rsplit("#", 1)[0] for key in snapshot["prs"])
        repos.update(detail.get("repo") for detail in snapshot.get("intents", {}).values())
        for field in ("repos", "checkouts"):
            entries = self.config.get(field)
            if isinstance(entries, dict):
                repos.update(entries)
        repos = sorted(repo for repo in repos if isinstance(repo, str) and REPO.fullmatch(repo))

        cursors = {}
        if snapshot["frozen"]:
            for repo in repos:
                io = None
                try:
                    io = self.io_for(repo)
                    self.close_queued(io, repo, f"{repo}:freeze", "watch 已冻结")
                except WATCH_ERRORS as exc:
                    reason, recovery = self.error(repo, "关闭已排队的自动合并", exc, io=io)
                    self.state.set_repo(repo, pause=self.paused(reason, recovery))
        else:
            self.inspect_prs()
            for repo in repos:
                enabled = self.repo_settings(repo).get("watchMainRed") is True
                previous = snapshot["repos"].get(repo, {})
                if previous.get("watchMainRed") != enabled:
                    self.state.set_repo(repo, watchMainRed=enabled)
                if not enabled:
                    continue
                io = None
                try:
                    io = self.io_for(repo)
                    cursors[repo] = self.inspect_main(io, repo)
                except WATCH_ERRORS as exc:
                    reason, recovery = self.error(repo, "主干巡检", exc, io=io)
                    self.state.set_repo(repo, pause=self.paused(reason, recovery))
            self.deliver_notifications()

        for repo, cursor in cursors.items():
            if cursor is not None:
                self.state.set_cursor(repo, cursor)
        self.state.write_heartbeat()


def local_notice(args, body=None):
    result = subprocess.run(args, input=body, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise OSError(f"{Path(args[0]).name} 发送失败（退出码 {result.returncode}）")
