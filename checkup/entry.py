"""C2 (--run-check): run C1's command in a throwaway clone and report the exit code and output tail.

Ported from repo-readiness src/repo_readiness/probes/entry.py (`run_entry`) at commit ac13f17,
reduced to what the checkup needs: committed HEAD in a `git clone --shared` temp dir (the source
checkout is never written; uncommitted changes are not part of the run), a scrubbed environment
(no GH_TOKEN etc.), stdin closed, hard timeout, only the tail of the output kept.
"""
import os
import tempfile

from checkup.proc import run

_SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME", "SHELL", "TERM")
_GIT_TIMEOUT = 60
TIMEOUT_S = 15 * 60
TAIL_LINES = 40


def _git(repo, *args):
    r = run(["git", "-C", repo] + list(args), cwd=repo, timeout_s=_GIT_TIMEOUT)
    return r.stdout.strip() if r.code == 0 else None


def tail(text, n=TAIL_LINES):
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-n:]


def run_check(root, command, timeout_s=TIMEOUT_S):
    """(status, detail, output tail lines)."""
    head = _git(root, "rev-parse", "--verify", "HEAD")
    if not head:
        return "unknown", "%s 没有可运行的提交" % root, []
    with tempfile.TemporaryDirectory(prefix="autopilot-checkup-") as tmp:
        work = os.path.join(tmp, "work")
        clone = run(["git", "clone", "-q", "--shared", "--no-checkout", root, work], cwd=tmp, timeout_s=_GIT_TIMEOUT)
        if clone.code != 0 or _git(work, "checkout", "-q", "--detach", head) is None:
            return "unknown", "建不出一次性克隆：%s" % clone.stderr.strip()[:300], []
        env = {k: os.environ[k] for k in _SAFE_ENV if k in os.environ}
        r = run(["/bin/sh", "-c", command], cwd=work, timeout_s=timeout_s, env=env)
        lines = tail(r.stdout + "\n" + r.stderr)
        where = "在 %s 的一次性克隆里运行「%s」" % (head[:7], command)
        if r.timed_out:
            return "unknown", "%s：超过 %d 分钟仍未结束" % (where, timeout_s // 60), lines
        if r.code != 0:
            return "missing", "%s：退出码 %d（只测了已提交的内容，没带未提交的改动）" % (where, r.code), lines
        return "ok", "%s：退出码 0" % where, lines
