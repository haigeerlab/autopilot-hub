"""Slice 18a: approved baselines for the compliance audit (audit/baselines.json; changing it is T4).

Format (autopilot.audit-baselines/v1):
  {"schema": "autopilot.audit-baselines/v1",
   "repos": {"owner/name": {"since": "YYYY-MM-DD", "approval": "<where the user's words are>",
                            "hub_pin": "<40 hex>" | null (no caller files), "takeover": {"project": <value>, "modules": {id: <value>}},
                            "checkup": {"C1": "ok" | "missing" | "unknown", ...}}}}
A takeover value is a spec-guard preset ("none", "plan", "merge") or an item object
({"spec", "plan", "merge", "release"} for spec-guard projects; {"merge", "release"} for takeover.yml repos).
"""
import json
import os
import re

from audit import config_check

DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baselines.json")
SCHEMA = "autopilot.audit-baselines/v1"
REPO = re.compile(r"[\w.-]+/[\w.-]+")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
SHA = re.compile(r"[0-9a-f]{40}")
CHECK = re.compile(r"C\d{1,2}")
STATES = ("ok", "missing", "unknown")


class BaselineError(ValueError):
    def __init__(self, problems):
        super().__init__("；".join(problems))
        self.problems = problems


def validate(data):
    """Return {repo: baseline}; raise BaselineError listing every problem."""
    problems = []
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("repos"), dict):
        raise BaselineError(["基线文件的 schema 必须是 %s，repos 必须是对象" % SCHEMA])
    for repo, item in data["repos"].items():
        where = "基线 %s" % repo
        if not REPO.fullmatch(repo):
            problems.append("%s：仓库名要写成 owner/name" % where)
        if not isinstance(item, dict):
            problems.append("%s：必须是对象" % where)
            continue
        if not isinstance(item.get("since"), str) or not DATE.fullmatch(item["since"]):
            problems.append("%s：since 必须是 YYYY-MM-DD" % where)
        if not isinstance(item.get("approval"), str) or not item["approval"].strip():
            problems.append("%s：approval 要写明批准原话的出处" % where)
        if "hub_pin" not in item or (item["hub_pin"] is not None and not (
                isinstance(item["hub_pin"], str) and SHA.fullmatch(item["hub_pin"]))):
            problems.append("%s：hub_pin 必须是 40 位提交号，没接入调用文件的写 null" % where)
        takeover = item.get("takeover")
        if not isinstance(takeover, dict) or not isinstance(takeover.get("modules", {}), dict):
            problems.append("%s：takeover 必须是 {project, modules}" % where)
        else:
            if config_check.items_of(takeover.get("project")) is None:
                problems.append("%s：takeover.project 不是合法的接管档位" % where)
            for module, value in takeover.get("modules", {}).items():
                if config_check.items_of(value) is None:
                    problems.append("%s：takeover.modules.%s 不是合法的接管档位" % (where, module))
        checkup = item.get("checkup", {})
        if not isinstance(checkup, dict):
            problems.append("%s：checkup 必须是对象" % where)
        else:
            for cid, state in checkup.items():
                if not CHECK.fullmatch(cid) or state not in STATES:
                    problems.append("%s：checkup.%s 只能是 ok / missing / unknown" % (where, cid))
    if problems:
        raise BaselineError(problems)
    return data["repos"]


def load(path=DEFAULT):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as error:
        raise BaselineError(["读不了基线文件 %s：%s" % (path, error)]) from error
    return validate(data)
