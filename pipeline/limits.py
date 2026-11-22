"""Apply hub-owned release caps independently of repository pipeline settings."""
import json
from pathlib import Path

from pipeline.model import LEVELS

DEFAULT = Path(__file__).resolve().parents[1] / "release-limits.json"


def load_limits(path=DEFAULT):
    """Return a validated policy, or None to select the conservative cap."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or data.get("schema") != "autopilot.release-limits/v1"
            or not isinstance(data.get("pipeline_own"), list)
            or any(not isinstance(repo, str) or not repo.strip() for repo in data["pipeline_own"])
            or not isinstance(data.get("repo_caps"), dict)
            or any(not isinstance(repo, str) or not repo.strip() or cap not in LEVELS
                   for repo, cap in data["repo_caps"].items())
            or not isinstance(data.get("sandbox_repos"), list)
            or any(not isinstance(repo, str) or not repo.strip() for repo in data["sandbox_repos"])):
        return None
    return data


def effective_level(repo, raw_level, pipeline, limits):
    """Return min(raw level, kernel cap) and a Chinese explanation if needed."""
    if limits is None:
        cap = "prepare"
        reason = "release-limits.json 不存在或读不懂，发布最高到 prepare"
    elif repo.casefold() in [name.casefold() for name in limits["pipeline_own"]]:
        cap = "prepare"
        reason = "%s 是流水线自用插件，发布最高到 prepare" % repo.split("/")[-1]
    else:
        cap = "auto-confirm"
        reason = "模板类别不允许 auto、未声明 rollback: true 或缺少 rollback 开头的步骤，发布最高到 auto-confirm"
        category = pipeline.get("class")
        sandbox = repo.casefold() in [name.casefold() for name in limits["sandbox_repos"]]
        if category == "sandbox" and not sandbox:
            reason = "sandbox 模板只给沙盒仓库，发布最高到 auto-confirm"
        elif ((category == "site" or (category == "sandbox" and sandbox))
              and pipeline.get("rollback") is True
              and any(step["id"].startswith("rollback") for step in pipeline["steps"])):
            cap = "auto"
        repo_caps = [value for name, value in limits["repo_caps"].items()
                     if name.casefold() == repo.casefold()]
        if repo_caps:
            repo_cap = min(repo_caps, key=LEVELS.index)
            if LEVELS.index(repo_cap) <= LEVELS.index(cap):
                repo_reason = "%s 的仓库上限为 %s" % (repo, repo_cap)
                reason = repo_reason + "；" + reason if repo_cap == cap else repo_reason
                cap = repo_cap
    if LEVELS.index(raw_level) > LEVELS.index(cap):
        return cap, reason + "（你设的是 %s）" % raw_level
    return raw_level, None
