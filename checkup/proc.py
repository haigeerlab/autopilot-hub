"""Start child processes with stdin closed and a hard timeout.

Ported from repo-readiness src/repo_readiness/proc.py at commit ac13f17 (trimmed).
A timeout or a missing executable gives exit code 3 ("environment not ready"), never a verdict
about the code under test.
"""
import subprocess
from dataclasses import dataclass

EXIT_ENV_NOT_READY = 3


@dataclass(frozen=True)
class RunResult:
    code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run(argv, *, cwd, timeout_s, env=None):
    """Run argv without stdin. env, when given, replaces the environment entirely."""
    try:
        p = subprocess.run(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return RunResult(EXIT_ENV_NOT_READY, "", "timeout after %ds" % timeout_s, timed_out=True)
    except (FileNotFoundError, NotADirectoryError):
        return RunResult(EXIT_ENV_NOT_READY, "", "executable not found: %s" % argv[0])
    return RunResult(p.returncode, p.stdout, p.stderr)
