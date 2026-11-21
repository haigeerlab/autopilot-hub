"""C10: dependency manifests have a lockfile and CI installs from it.

Ported from repo-readiness src/repo_readiness/probes/ecosystem.py (`lockfile_installed`) and
adapters/node.json, adapters/python.json, commit ac13f17. Only the manifests passed in (the ones
changed against the base) are judged.
"""
import json
import os
import re
from posixpath import dirname, basename

from checkup.workflow import jobs, load_workflows, norm, steps

_PIN = re.compile(r"^[A-Za-z0-9_.\-\[\],]+\s*==\s*[^\s;]+")

ADAPTERS = [
    {
        "id": "node", "manifests": ["package.json"], "parser": "package_json",
        "lockfiles": ["package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"],
        "frozen_commands": ["npm ci", "pnpm install --frozen-lockfile", "pnpm i --frozen-lockfile",
                            "yarn install --immutable", "yarn install --frozen-lockfile", "yarn --frozen-lockfile",
                            "bun install --frozen-lockfile"],
        "frozen_actions": [{"uses": "pnpm/setup", "with": {"require-lockfile": True}}],
        "install_commands": ["npm install", "npm i ", "pnpm install", "pnpm i ", "pnpm add", "yarn install",
                             "yarn add", "bun install"],
        "install_actions": ["pnpm/setup"],
    },
    {
        "id": "python", "manifests": ["pyproject.toml", "requirements.txt", "requirements-dev.txt", "Pipfile"],
        "parser": "python",
        "lockfiles": ["uv.lock", "poetry.lock", "Pipfile.lock", "pdm.lock", "pylock.toml"],
        "pinned_requirements": True,
        "frozen_commands": ["uv sync --frozen", "uv sync --locked", "poetry install", "pipenv install --deploy",
                            "pipenv sync", "pdm sync", "pip install -r", "pip install --require-hashes"],
        "frozen_actions": [],
        "install_commands": ["pip install", "uv sync", "uv pip install", "poetry install", "pipenv install",
                             "pdm install"],
        "install_actions": [],
    },
]


def manifest_names():
    return {n for ad in ADAPTERS for n in ad["manifests"]}


def _requirement_lines(text):
    lines = []
    for raw in text.splitlines():
        line = raw.split(" #")[0].strip()
        if line and not line.startswith("#") and not line.startswith("-"):
            lines.append(line)
    return lines


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _python_deps(path):
    text = _read(path)
    name = os.path.basename(path)
    if name.startswith("requirements"):
        return bool(_requirement_lines(text)), None
    if name == "pyproject.toml":
        m = re.search(r"^\s*dependencies\s*=\s*\[(.*?)\]", text, re.M | re.S)
        return bool(m and re.search(r"[\"']", m.group(1))), None
    if name == "Pipfile":
        m = re.search(r"^\[(?:dev-)?packages\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
        return bool(m and any(ln.strip() and not ln.strip().startswith("#") for ln in m.group(1).splitlines())), None
    return False, None


def _package_json_deps(path):
    try:
        data = json.loads(_read(path))
    except ValueError as e:
        return False, "%s 不是合法 JSON：%s" % (os.path.basename(path), e)
    keys = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
    return isinstance(data, dict) and any(isinstance(data.get(k), dict) and data.get(k) for k in keys), None


_PARSERS = {"package_json": _package_json_deps, "python": _python_deps}


def _unpinned(path):
    return [ln for ln in _requirement_lines(_read(path)) if not (_PIN.match(ln) or "--hash" in ln)]


def _parents(rel):
    out, d = [], dirname(rel)
    while True:
        out.append(d)
        if not d:
            break
        d = dirname(d)
    return out


def _lock_for(ad, manifest, files, root):
    if ad.get("pinned_requirements") and basename(manifest).startswith("requirements"):
        loose = _unpinned(os.path.join(root, manifest))
        if loose:
            return None, "%s 没有固定版本：%s" % (manifest, ", ".join(loose[:5]))
        return manifest, None
    for d in _parents(manifest):
        for lock in ad["lockfiles"]:
            cand = "%s/%s" % (d, lock) if d else lock
            if cand in files:
                return cand, None
        if ad.get("pinned_requirements"):
            for f in files:
                if dirname(f) == d and basename(f).startswith("requirements") and f.endswith(".txt") \
                        and not _unpinned(os.path.join(root, f)):
                    return f, None
    return None, "%s 有第三方依赖，但它所在目录或上层目录没有锁文件（%s）" % (manifest, "、".join(ad["lockfiles"]))


def _ci_installs_any(ad, wfs):
    commands = [norm(c) for c in ad["install_commands"]]
    actions = set(ad["install_actions"])
    for _, wf in wfs:
        for job in jobs(wf):
            for s in steps(job):
                text = norm(str(s.get("run", ""))) + " "
                if any(c and c in text for c in commands):
                    return True
                uses = s.get("uses")
                if isinstance(uses, str) and uses.split("@")[0] in actions:
                    return True
    return False


def _ci_installs_frozen(ad, wfs):
    commands = [norm(c) for c in ad["frozen_commands"]]
    for name, wf in wfs:
        for job in jobs(wf):
            for s in steps(job):
                run_text = norm(str(s.get("run", "")))
                for c in commands:
                    if c and c in run_text:
                        return "%s：`%s`" % (name, c)
                uses = s.get("uses")
                if isinstance(uses, str):
                    with_ = s.get("with") if isinstance(s.get("with"), dict) else {}
                    for act in ad["frozen_actions"]:
                        if uses.split("@")[0] == act["uses"] and all(with_.get(k) == v for k, v in act["with"].items()):
                            return "%s：%s" % (name, act["uses"])
    return None


def lockfile_installed(root, changed_manifests, tracked_files):
    """(status, detail) for the changed manifests; tracked_files is the set of repo-relative paths."""
    found, errors = {}, []
    for ad in ADAPTERS:
        for rel in changed_manifests:
            if basename(rel) not in ad["manifests"]:
                continue
            has, err = _PARSERS[ad["parser"]](os.path.join(root, rel))
            if err:
                errors.append(err)
            elif has:
                found.setdefault(ad["id"], []).append(rel)
    if errors:
        return "unknown", "依赖清单读不了：" + "；".join(errors)
    if not found:
        return "ok", "改过的依赖清单里没有第三方依赖，不需要锁文件"
    wfs, wf_errors = load_workflows(root)
    if wf_errors:
        return "unknown", "有 workflow 读不了：" + "；".join(wf_errors)
    by_id = {ad["id"]: ad for ad in ADAPTERS}
    evidence, missing = [], []
    for ad_id, manifests in sorted(found.items()):
        ad = by_id[ad_id]
        for m in manifests:
            lock, problem = _lock_for(ad, m, tracked_files, root)
            if problem:
                missing.append(problem)
            else:
                evidence.append("%s 由 %s 锁定" % (m, lock))
        how = _ci_installs_frozen(ad, wfs)
        if how:
            evidence.append("CI 按锁文件安装 %s 依赖（%s）" % (ad_id, how))
        elif _ci_installs_any(ad, wfs):
            missing.append("CI 安装 %s 依赖时没有按锁文件（改用：%s）" % (ad_id, "；".join(ad["frozen_commands"])))
        else:
            evidence.append("CI 不安装 %s 依赖，锁文件本身已固定版本" % ad_id)
    if missing:
        return "missing", "；".join(missing)
    return "ok", "；".join(evidence)
