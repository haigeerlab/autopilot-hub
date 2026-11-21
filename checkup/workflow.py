"""Static analysis of .github/workflows (nothing is executed): C1 (find the check command), C5, C8, C9.

Ported from repo-readiness src/repo_readiness/probes/workflow.py (workflow loading, triggers,
`_check_command`, `pr_runs_check`, `actions_pinned`, `no_dangerous_triggers`) and
src/repo_readiness/remedy/__init__.py (`_check_command` inference), commit ac13f17.
A workflow that cannot be read with the YAML subset makes the verdict unknown: a verdict about
"all workflows" must not skip the ones we could not read.
"""
import os
import re

from checkup.yamlsub import YamlSubsetError, load

INTERFACE = ".github/autopr/interface.yml"
WRAPPER = "autopr-entry run --entry check"
_SHA = re.compile(r"@[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}\Z")
_PRIVILEGED = {"pull_request_target", "workflow_run"}
_PR_CODE = ("github.event.pull_request.head", "github.head_ref", "github.event.workflow_run.head", "refs/pull/")
# A run line that looks like a test / build / validation step, best kind first (tests before lint).
_CMD_RANKS = [re.compile(p, re.I) for p in (
    r"(?<![a-z])(tests?|pytest|unittest)(?![a-z])", r"(?<![a-z])build(?![a-z])",
    r"(?<![a-z])(validate|verify|check)(?![a-z])", r"(?<![a-z])lint(?![a-z])")]
_NOT_A_CHECK = re.compile(
    r"^(?:echo|printf|cd|export|set|mkdir|cp|mv|ln|ls|cat|tee|grep|egrep|sed|awk|\[|test\b|apt|apt-get|brew|sudo|"
    r"(?:python3?|node) +(?:-c|-e)|pip3? install|npm (?:ci|install|i)|yarn(?: install)?|pnpm (?:install|i))(?![\w./-])")


def workflow_paths(root):
    wf_dir = os.path.join(root, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        return []
    return sorted(f for f in os.listdir(wf_dir) if f.endswith((".yml", ".yaml")))


def load_workflows(root, names=None):
    """([(file name, mapping)], [error strings]); names limits the files that are read."""
    loaded, errors = [], []
    for f in workflow_paths(root):
        if names is not None and f not in names:
            continue
        try:
            data = load(os.path.join(root, ".github", "workflows", f))
        except (YamlSubsetError, OSError, UnicodeDecodeError) as e:
            errors.append("%s: %s" % (f, e))
            continue
        if not isinstance(data, dict):
            errors.append("%s: 不是 YAML 映射" % f)
            continue
        loaded.append((f, data))
    return loaded, errors


def triggers(wf):
    on = wf.get("on")
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {x for x in on if isinstance(x, str)}
    if isinstance(on, dict):
        return set(on)
    return set()


def jobs(wf):
    j = wf.get("jobs")
    return [x for x in j.values() if isinstance(x, dict)] if isinstance(j, dict) else []


def steps(job):
    s = job.get("steps")
    return [x for x in s if isinstance(x, dict)] if isinstance(s, list) else []


def norm(s):
    return " ".join(s.split())


# --------------------------------------------------------------------------- C1 / C5

def interface_check(root):
    """(command, problem). Both None = interface.yml absent or registers no check entry."""
    path = os.path.join(root, INTERFACE)
    if not os.path.isfile(path):
        return None, None
    try:
        data = load(path)
    except (YamlSubsetError, OSError, UnicodeDecodeError) as e:
        return None, "%s 读不了：%s" % (INTERFACE, e)
    run = (((data or {}).get("entries") or {}).get("check") or {}).get("run") if isinstance(data, dict) else None
    if not isinstance(run, str) or not run.strip():
        return None, None
    return run.strip(), None


def inferred_check(wfs):
    """Best test/build-like `run` line of a pull_request workflow: (command, workflow file) or (None, None)."""
    lines = []
    for name, wf in wfs:
        if "pull_request" not in triggers(wf):
            continue
        for job in jobs(wf):
            for s in steps(job):
                for line in str(s.get("run", "")).splitlines():
                    line = line.split(" | ")[0].strip()
                    if line and not line.startswith("#") and not _NOT_A_CHECK.match(line):
                        lines.append((line, name))
    for rank in _CMD_RANKS:
        for line, name in lines:
            if rank.search(line):
                return line, name
    return None, None


def find_check(root):
    """C1. Returns dict(command, source, problem); source is 'interface.yml' or a workflow file name."""
    cmd, problem = interface_check(root)
    if problem:
        return {"command": None, "source": None, "problem": problem}
    if cmd:
        return {"command": cmd, "source": "interface.yml", "problem": None}
    wfs, errors = load_workflows(root)
    cmd, name = inferred_check(wfs)
    if cmd:
        return {"command": cmd, "source": name, "problem": None}
    if errors:
        return {"command": None, "source": None, "problem": "有 workflow 读不了：%s" % "；".join(errors)}
    return {"command": None, "source": None, "problem": None}


def pr_runs_check(root, command):
    """C5: (status, detail). A pull_request workflow step runs `command` (or the autopr wrapper)."""
    wfs, errors = load_workflows(root)
    if errors:
        return "unknown", "有 workflow 读不了：%s" % "；".join(errors)
    if not wfs:
        return "missing", "仓库里没有 workflow 文件"
    pr = [(n, wf) for n, wf in wfs if "pull_request" in triggers(wf)]
    if not pr:
        return "missing", "没有 workflow 在 pull_request 上触发"
    if not command:
        return "unknown", "没找到检查命令（见 C1），无法比对 CI 有没有跑它"
    want = norm(command)
    for name, wf in pr:
        for job in jobs(wf):
            for s in steps(job):
                run = norm(str(s.get("run", "")))
                if run and (want in run or WRAPPER in run):
                    return "ok", "%s 在 pull_request 上运行了%s" % (name, "autopr 包装入口" if WRAPPER in run else "「%s」" % command)
    return "missing", "没有 pull_request 的 workflow 步骤运行了「%s」（也没有 %s）" % (command, WRAPPER)


# --------------------------------------------------------------------------- C8 / C9

def unpinned_actions(wfs):
    """['file: uses-ref', ...] for references not pinned to a 40-hex commit (or sha256 digest)."""
    out = []
    for name, wf in wfs:
        for job in jobs(wf):
            for u in [job.get("uses")] + [s.get("uses") for s in steps(job)]:
                if not isinstance(u, str) or u.startswith("./"):
                    continue
                ok = bool(_DIGEST.search(u)) if u.startswith("docker://") else bool(_SHA.search(u))
                item = "%s: %s" % (name, u)
                if not ok and item not in out:
                    out.append(item)
    return out


def dangerous_triggers(wfs):
    """Places where a privileged trigger checks out or runs pull-request code."""
    bad = []
    for name, wf in wfs:
        trig = triggers(wf) & _PRIVILEGED
        if not trig:
            continue
        for job in jobs(wf):
            for s in steps(job):
                with_ = s.get("with") if isinstance(s.get("with"), dict) else {}
                texts = [str(s.get("run", "")), str(with_.get("ref", "")), str(with_.get("repository", ""))]
                hit = next((p for p in _PR_CODE for t in texts if p in t), None)
                if hit:
                    bad.append("%s：%s 触发的步骤用到了 PR 的代码（%s）" % (name, "/".join(sorted(trig)), hit))
    return bad
