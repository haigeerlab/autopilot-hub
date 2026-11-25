"""Release engine with hub-owned caps; remote and execution I/O use the caller."""
import contextlib
import hashlib
import json
import re
from urllib.parse import quote

from admission.decide import trusted
from checkup import yamlsub
from orchestrator.flow import SG_CAPABILITY_MAP, Stop, base_file, block, read_sg_config, rest_comment
from pipeline.limits import effective_level, load_limits
from pipeline.model import LEVELS

START = "autopilot-release"
STEP = "autopilot-release-step"
TAIL = 40


def read_release_level(io, repo, base_ref, notes):
    """Read only the project's raw release level; module overrides never apply."""
    if base_file(io, repo, SG_CAPABILITY_MAP, base_ref) is not None:
        config_notes = []
        config = read_sg_config(io, repo, base_ref, config_notes)
        if config_notes or not config:
            notes.append(".agent/config.json 缺省或读不懂，发布档位按 human")
        value = config.get("takeover", "none")
        if value in ("none", "plan", "design", "merge"):
            return "human"
        if isinstance(value, dict):
            value = value.get("release", "human")
            if value in LEVELS:
                return value
        notes.append(".agent/config.json 的项目级 takeover.release 读不懂，发布档位按 human")
        return "human"
    text = base_file(io, repo, ".github/autopr/takeover.yml", base_ref)
    if text is None:
        return "human"
    try:
        config = yamlsub.loads(text)
    except ValueError:
        config = None
    value = config.get("release") if isinstance(config, dict) else None
    if value in LEVELS:
        return value
    notes.append("takeover.yml 的 release 缺省、非法或读不懂，发布档位按 human")
    return "human"


def decision(step, level):
    """Return execute/waiting and the human-facing reason for one validated step."""
    command = step["run"]
    if command.startswith("ask:"):
        return "waiting", "等 %s 确认" % command[4:].strip()
    if step["by"] == "human":
        return "waiting", "这一步要你本人做：" + command
    if step["by"] == "external":
        return "waiting", "这一步由外部系统做：%s，做完告诉我" % command
    condition = step.get("auto_when")
    if condition is None:
        return "waiting", "这一步没有 auto_when，不写就永远等人：" + command
    needed = condition.split(": ", 1)[1]
    if LEVELS.index(level) >= LEVELS.index(needed):
        return "execute", None
    return "waiting", "当前发布档位 %s 低于这一步需要的 %s，要你确认后才做" % (level, needed)


def payload(body, name):
    """Decode a terminal block; JSON strings may themselves contain comment delimiters.

    Only the final block is authoritative, so command output cannot inject step records.
    """
    marker = "<!-- %s:v1 " % name
    for match in re.finditer(re.escape(marker), body):
        try:
            data, end = json.JSONDecoder().raw_decode(body[match.end():])
        except ValueError:
            continue
        if body[match.end() + end:].strip() == "-->" and isinstance(data, dict):
            return data
    return None


def pipeline_sha256(pipeline):
    return hashlib.sha256(json.dumps(pipeline, sort_keys=True).encode("utf-8")).hexdigest()


def _trusted_issue(issue, bot):
    """Posted by the configured bot and never edited; issue timestamps are normalized by release_issue."""
    user = issue.get("user") or {}
    created = issue.get("created_at")
    app = issue.get("performed_via_github_app")
    # GitHub does not return performed_via_github_app on App-created issues; comments still require it.
    return (bot is not None and user.get("type") == "Bot" and user.get("id") == bot["id"]
            and (app is None or app.get("slug") == bot["app_slug"])
            and bool(created) and issue.get("updated_at") == created)


def _start(issue, bot):
    if not isinstance(issue, dict) or issue.get("state") != "open" or "pull_request" in issue:
        raise Stop("issue", "跟踪 issue 不存在、不是 issue 或不是 open 状态")
    data = payload(issue.get("body") or "", START) if _trusted_issue(issue, bot) else None
    if (not data or any(not isinstance(data.get(k), str) or not data[k]
                        for k in ("version", "commit", "template", "level", "pipeline_sha256"))
            or data["level"] not in LEVELS or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", data["commit"])
            or not re.fullmatch(r"[0-9a-f]{64}", data["pipeline_sha256"])
            or not isinstance(data.get("steps"), list)
            or any(not isinstance(s, str) for s in data["steps"])
            or len(set(data["steps"])) != len(data["steps"])):
        raise Stop("issue", "不是可信的 autopilot 发布 issue（开始块缺失、非法、不是机器人发的或被编辑过）")
    return data


def _records(comments, bot, identifiers):
    """Reconstruct a sequential journal, ignoring untrusted and malformed records."""
    records = {}
    for comment in comments:
        if not trusted(rest_comment(comment), bot):
            continue
        data = payload(comment.get("body") or "", STEP)
        if (not data or not {"id", "status", "exit", "evidence", "reason", "confirmed_words"}.issubset(data)
                or data.get("id") not in identifiers or data.get("status") not in ("done", "stopped", "waiting")
                or not isinstance(data.get("evidence"), dict)
                or (data.get("exit") is not None and type(data["exit"]) is not int)
                or (data.get("reason") is not None and not isinstance(data["reason"], str))
                or (data.get("status") != "done" and not data.get("reason"))
                or (data.get("confirmed_words") is not None and not isinstance(data["confirmed_words"], str))):
            continue
        pending = next((s for s in identifiers if records.get(s, {}).get("status") != "done"), None)
        if data["id"] == pending:
            records[pending] = data
    return records


def _execute(work, step, repo, version, issue):
    result = work.run_release(step["run"], version, repo, issue)
    output = result["output"]
    evidence = {"stdout": {"sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
                           "tail": output.splitlines()[-TAIL:]}}
    link = re.search(r"https?://[^\s<>\"']+", output)
    if link:
        evidence["url"] = link.group()
    file_line = re.search(r"^FILE: (.+)$", output, re.MULTILINE)
    if file_line:
        artifact = work.file_evidence(file_line.group(1).strip())
        if artifact:
            evidence["file"] = artifact
    required = set(step["evidence"])
    if (step.get("stop_if") or "").startswith("missing: "):
        required.add(step["stop_if"].split(": ", 1)[1])
    missing = sorted(required - set(evidence))
    reason = None
    if result["exit"] != 0:
        reason = "命令退出码 %d，先修好再续跑" % result["exit"]
    elif missing:
        reason = "缺少证据 %s，先修好再续跑" % "、".join(missing)
    return {"id": step["id"], "status": "stopped" if reason else "done", "exit": result["exit"],
            "evidence": evidence, "reason": reason, "confirmed_words": None}


def _comment(io, repo, issue, record):
    status = {"done": "已完成", "waiting": "等人", "stopped": "出问题了，已停下"}[record["status"]]
    text = "步骤 %s：%s。%s" % (record["id"], status, record["reason"] or "")
    if record["confirmed_words"] is not None:
        text += "已收到你的确认，原话记录在机器块中。"
    if record["status"] == "done" and "stdout" in record["evidence"]:
        tail = "\n".join(record["evidence"]["stdout"]["tail"])
        text += "\n\n<details><summary>输出最后 40 行</summary>\n\n````\n%s\n````\n</details>" % tail
    io.api(f"repos/{repo}/issues/{issue}/comments", "POST", {"body": text + "\n\n" + block(STEP, record)})


def run(io, repo, version=None, *, resume=None, confirm=None, dry_run=False):
    """Run or resume a release, returning a printable result with its CLI exit code."""
    notes, issue, url, level = [], resume, None, None
    level_raw, level_cap_reason = None, None

    def outcome(code, result, reason, **extra):
        return dict({"code": code, "result": result, "reason": reason, "notes": notes,
                     "issue": issue, "url": url, "level": level,
                     "level_raw": level_raw, "level_cap_reason": level_cap_reason,
                     "resume_command": f"python3 -B orchestrator/cli.py release --repo {repo} --resume {issue}"}, **extra)

    try:
        if ((resume is None) == (version is None) or (confirm is not None and resume is None)
                or (dry_run and resume is not None) or (resume is not None and (type(resume) is not int or resume < 1))
                or (version is not None and (not isinstance(version, str) or not version.strip()))
                or (confirm is not None and (not isinstance(confirm, str) or not confirm.strip()))):
            raise Stop("arguments", "请提供 --version 或 --resume；--confirm 只用于续跑且不能为空，--dry-run 只用于新发布")
        bot = io.bot_identity()
        if bot is None:
            raise Stop("identity", "hub 没配机器人身份（bot-identity.json），不能发布")
        start = _start(io.release_issue(repo, resume), bot) if resume is not None else None
        if start:
            issue = resume
            url = f"https://github.com/{repo}/issues/{issue}"
            version = start["version"]
        info = io.api(f"repos/{repo}")
        base_ref = info["default_branch"]
        current_commit = io.api(f"repos/{repo}/commits/{quote(base_ref, safe='')}")["sha"]
        pipeline = io.resolve_pipeline(repo, current_commit, notes)
        digest = pipeline_sha256(pipeline)
        steps = [step for step in pipeline["steps"] if step["at"] != "pre-merge"]
        identifiers = [step["id"] for step in steps]
        if start and (start["pipeline_sha256"] != digest or start["steps"] != identifiers
                      or start["template"] != pipeline["template"]):
            reason = "发布进行中流程文件变了，请重新发起"
            io.api(f"repos/{repo}/issues/{issue}/comments", "POST", {"body": reason})
            return outcome(1, "stopped", reason)
        level_raw = read_release_level(io, repo, current_commit, notes)
        level, level_cap_reason = effective_level(repo, level_raw, pipeline, load_limits())
        if level_cap_reason:
            notes.append(level_cap_reason)
        if dry_run:
            plan = [{"id": step["id"], "at": step["at"], "status": decision(step, level)[0],
                     "reason": decision(step, level)[1]} for step in steps]
            return outcome(0, "dry-run", "发布预演完成", plan=plan)
        confirmed_step = None
        if start:
            commit = start["commit"]
            comments = io.api(f"repos/{repo}/issues/{issue}/comments?per_page=100") or []
            records = _records(comments, bot, identifiers)
            pending = next((s for s in identifiers if records.get(s, {}).get("status") != "done"), None)
            last = records.get(pending)
            if last and last["status"] == "stopped" and confirm is not None:
                raise Stop("confirm", "stopped 不能靠确认跳过；请修好后不带 --confirm 续跑")
            if last and last["status"] == "waiting":
                if confirm is None:
                    return outcome(1, "waiting", "还在等：" + last["reason"])
                step = next(s for s in steps if s["id"] == pending)
                if step["by"] == "ai" and not step["run"].startswith("ask:"):
                    confirmed_step = pending
                else:
                    confirmed = dict(last, status="done", reason=None, confirmed_words=confirm)
                    _comment(io, repo, issue, confirmed)
                    records[pending] = confirmed
            elif confirm is not None:
                raise Stop("confirm", "当前没有 waiting 步骤，不能使用 --confirm")
        else:
            commit, records = current_commit, {}
            start = {"version": version, "commit": commit, "template": pipeline["template"], "level": level,
                     "level_raw": level_raw, "level_cap_reason": level_cap_reason,
                     "pipeline_sha256": digest, "steps": identifiers}
            if io.api_optional(f"repos/{repo}/labels/autopilot%3Arelease") is None:
                io.api(f"repos/{repo}/labels", "POST", {"name": "autopilot:release", "color": "0e8a16",
                                                       "description": "autopilot 发布过程跟踪"})
            created = io.api(f"repos/{repo}/issues", "POST", {
                "title": f"autopilot 发布 {repo} v{version}", "labels": ["autopilot:release"],
                "body": (f"跟踪 {repo} 版本 {version} 的发布：合并后 → 发布前 → 发布 → 发布后 → 上线后。\n"
                         f"开始提交：`{commit}`，模板：{pipeline['template']}，发布档位：{level}。\n"
                         + (f"档位说明：{level_cap_reason}。\n" if level_cap_reason else "") +
                         "每步在评论里留证据；等人时确认后续跑，出问题时先修好再重试。\n\n" + block(START, start))})
            issue = created["number"]
            url = created.get("html_url") or f"https://github.com/{repo}/issues/{issue}"
        with contextlib.ExitStack() as stack:
            work = None
            for step in steps:
                if records.get(step["id"], {}).get("status") == "done":
                    continue
                action, reason = decision(step, level)
                if step["id"] == confirmed_step:
                    action, reason = "execute", None
                record = {"id": step["id"], "status": "waiting", "exit": None, "evidence": {},
                          "reason": reason, "confirmed_words": None}
                if action == "execute":
                    try:
                        if work is None:
                            work = stack.enter_context(io.release_checkout(repo, base_ref, commit))
                        record = _execute(work, step, repo, version, issue)
                    except Stop as error:
                        record.update(status="stopped", reason=error.reason)
                if step["id"] == confirmed_step:
                    record["confirmed_words"] = confirm
                _comment(io, repo, issue, record)
                records[step["id"]] = record
                if record["status"] != "done":
                    return outcome(1, record["status"], record["reason"])
        summary = "发布全部完成。\n\n" + "\n".join(f"{s}：{records[s]['status']}" for s in identifiers)
        io.api(f"repos/{repo}/issues/{issue}/comments", "POST", {"body": summary})
        io.api(f"repos/{repo}/issues/{issue}", "PATCH", {"state": "closed"})
        io.ledger({"event": "release_done", "repo": repo, "version": version, "issue": issue, "at": io.now()})
        return outcome(0, "done", "发布全部完成")
    except Stop as error:
        return outcome(2, "error", error.reason)
