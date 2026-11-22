"""Load and validate declarative pipelines without executing their steps."""
import re
from pathlib import Path

from checkup import yamlsub
from risk.tiers import read_repo_config_at

SEGMENTS = ("pre-merge", "post-merge", "before-release", "release", "after-release", "after-live")
LEVELS = ("human", "prepare", "auto-confirm", "auto")
BY = ("ai", "human", "external")
EVIDENCE = ("stdout", "url", "file")
DEFAULT = Path(__file__).resolve().parent / "templates"
_STEP_FIELDS = ("id", "at", "run", "by", "auto_when", "stop_if", "evidence")
_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class PipelineError(ValueError):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


def _read(path):
    try:
        return yamlsub.load(path)
    except yamlsub.YamlSubsetError as error:
        raise PipelineError([f"{path} 第 {error.line} 行 YAML 格式错误，请检查语法"]) from error


def _unknown(data, allowed, label, problems):
    for key in data:
        if key not in allowed and key != "version" and not (isinstance(key, str) and key.startswith("$")):
            problems.append(f"{label} 不认识的键名 {key}")


def _items(data, key, problems):
    value = data.get(key, [])
    if not isinstance(value, list):
        problems.append(f"{key} 必须是列表")
        return []
    return value


def _step(data, label, problems, inherited_at=None, placement=False):
    if not isinstance(data, dict):
        problems.append(f"{label} 必须是步骤映射")
        return None
    _unknown(data, _STEP_FIELDS + (("before", "after") if placement else ()), label, problems)
    identifier = data.get("id")
    at = data.get("at", inherited_at)
    run, by = data.get("run"), data.get("by")
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
        problems.append(f"{label} 的 id 必须是 kebab-case（小写字母、数字及连接用的短横线）")
    if at not in SEGMENTS:
        problems.append(f"{label} 的 at 必须是有效段名：{', '.join(SEGMENTS)}")
    if not isinstance(run, str) or not run.strip():
        problems.append(f"{label} 的 run 必须是非空命令字符串")
    if by not in BY:
        problems.append(f"{label} 的 by 必须是 ai / human / external")
    if isinstance(run, str) and run.startswith("ask:") and by != "human":
        problems.append(f"{label} 的 run 以 ask: 开头时，by 必须是 human")
    if "auto_when" in data and data["auto_when"] not in tuple("release: " + x for x in LEVELS):
        problems.append(f"{label} 的 auto_when 必须为 release: <档位>，档位为 {', '.join(LEVELS)}")
    valid_stops = ("exit != 0",) + tuple("missing: " + x for x in EVIDENCE)
    if "stop_if" in data and data["stop_if"] not in valid_stops:
        problems.append(f"{label} 的 stop_if 必须是 exit != 0 或 missing: <stdout / url / file>")
    evidence = data.get("evidence", ["stdout"])
    if not isinstance(evidence, list) or any(x not in EVIDENCE for x in evidence):
        problems.append(f"{label} 的 evidence 必须是列表，元素只能是 stdout / url / file")
    if placement:
        if "before" in data and "after" in data:
            problems.append(f"{label} 不能同时指定 before 和 after")
        for key in ("before", "after"):
            if key in data and (not isinstance(data[key], str) or not _ID.fullmatch(data[key])):
                problems.append(f"{label} 的 {key} 必须是有效步骤 id")
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
        return None
    return {"id": identifier, "at": at, "run": run, "by": by,
            "auto_when": data.get("auto_when"), "stop_if": data.get("stop_if"),
            "evidence": list(evidence) if isinstance(evidence, list) else evidence}


def _template(name, templates_dir, problems):
    empty = {"name": name, "rollback": False, "steps": []}
    if (not isinstance(name, str) or not name or name in (".", "..")
            or "/" in name or "\\" in name):
        problems.append("extends 必须是模板文件名（不含路径和 .yml 后缀）")
        return empty
    path = Path(templates_dir) / (name + ".yml")
    try:
        data = _read(path)
    except FileNotFoundError:
        problems.append(f"模板 {name} 不存在：{path}")
        return empty
    except PipelineError as error:
        problems.extend(error.problems)
        return empty
    if not isinstance(data, dict):
        problems.append(f"模板 {name} 必须是映射")
        return empty
    _unknown(data, ("name", "rollback", "steps"), f"模板 {name}", problems)
    if data.get("name") != name:
        problems.append(f"模板 {name} 的 name 必须与文件名一致")
    if not isinstance(data.get("rollback"), bool):
        problems.append(f"模板 {name} 的 rollback 必须是布尔值")
    if "steps" not in data:
        problems.append(f"模板 {name} 缺少 steps 列表")
    steps, seen = [], set()
    for index, raw in enumerate(_items(data, "steps", problems)):
        label = f"模板 {name} 的 steps[{index}]"
        item = _step(raw, label, problems)
        if item is None:
            continue
        if item["id"] in seen:
            problems.append(f"{label} 的 id {item['id']} 重复")
        seen.add(item["id"])
        if item["at"] == "pre-merge":
            problems.append(f"{label} 不允许位于 pre-merge，合并前步骤只能由仓库 add")
        steps.append(item)
    return {"name": name, "rollback": data.get("rollback"), "steps": steps}


def load_template(name, templates_dir=DEFAULT):
    problems = []
    template = _template(name, templates_dir, problems)
    if problems:
        raise PipelineError(problems)
    return template


def resolve(repo_config, templates_dir=DEFAULT):
    """Apply remove, replace and ordered add, then sort stably by segment."""
    if not isinstance(repo_config, dict):
        raise PipelineError(["pipeline.yml 必须是映射"])
    problems = []
    _unknown(repo_config, ("extends", "add", "remove", "replace"), "pipeline.yml", problems)
    name = repo_config.get("extends")
    template = _template(name, templates_dir, problems)
    steps = template["steps"][:]
    originals = {item["id"]: item for item in steps}
    removed = set()
    for index, identifier in enumerate(_items(repo_config, "remove", problems)):
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            problems.append(f"remove[{index}] 必须是有效步骤 id")
            continue
        removed.add(identifier)
        if identifier not in originals:
            problems.append(f"remove 引用的 id {identifier} 不存在")
        elif originals[identifier]["at"] == "pre-merge":
            problems.append(f"remove 不允许操作 pre-merge 步骤 {identifier}")
    steps = [item for item in steps if item["id"] not in removed]
    replaced = set()
    for index, raw in enumerate(_items(repo_config, "replace", problems)):
        identifier = raw.get("id") if isinstance(raw, dict) else None
        original = originals.get(identifier) if isinstance(identifier, str) else None
        label = f"replace[{index}]"
        item = _step(raw, label, problems, original["at"] if original else None)
        if item is None:
            continue
        identifier = item["id"]
        if identifier in replaced:
            problems.append(f"replace 的 id {identifier} 重复")
        replaced.add(identifier)
        if identifier in removed:
            problems.append(f"id {identifier} 不能同时 remove 和 replace")
        if original is None:
            problems.append(f"replace 引用的 id {identifier} 不存在")
        if item["at"] == "pre-merge" or (original and original["at"] == "pre-merge"):
            problems.append(f"replace 不允许操作或移入 pre-merge 步骤 {identifier}")
        steps = [item if current["id"] == identifier else current for current in steps]
    for index, raw in enumerate(_items(repo_config, "add", problems)):
        label = f"add[{index}]"
        item = _step(raw, label, problems, placement=True)
        if item is None:
            continue
        duplicate = any(current["id"] == item["id"] for current in steps)
        if duplicate:
            problems.append(f"{label} 的 id {item['id']} 重复")
        position = len(steps)
        for key in ("before", "after"):
            if key not in raw:
                continue
            target = next((i for i, current in enumerate(steps) if current["id"] == raw[key]), None)
            if target is None:
                problems.append(f"{label} 的 {key} 引用的 id {raw[key]} 不存在")
            elif steps[target]["at"] != item["at"]:
                problems.append(f"{label} 的 {key} 必须指向同一段的步骤 {raw[key]}")
            else:
                position = target + (1 if key == "after" else 0)
        if not duplicate:
            steps.insert(position, item)
    if problems:
        raise PipelineError(problems)
    return {"template": name, "rollback": template["rollback"],
            "steps": [item for segment in SEGMENTS for item in steps if item["at"] == segment]}


def read_repo_pipeline(checkout):
    path = Path(checkout) / ".github" / "autopr" / "pipeline.yml"
    try:
        data = _read(path)
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        raise PipelineError([f"{path} 必须是流程配置映射"])
    return data


def default_template_for(checkout):
    config = read_repo_config_at(checkout)
    category = config.get("extends") if isinstance(config, dict) else None
    return "plugin" if category == "plugin" else "site" if category == "frontend" else None
