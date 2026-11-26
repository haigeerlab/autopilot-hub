#!/bin/bash
# Usage: scripts/validate.sh [--quick] [--list]
# PYTHON selects the interpreter (default: python3).
# VALIDATE_CHANGED, when set, overrides Git changes with newline-separated paths.
# Keep the shell wrapper compatible with macOS Bash 3.2; use only Python stdlib.
set -eu
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PYTHON=${PYTHON:-python3}
exec "$PYTHON" -B - "$@" <<'PY'
import argparse
import ast
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest

parser = argparse.ArgumentParser(description="Run repository tests, optionally limited to changed files.")
parser.add_argument("--quick", action="store_true")
parser.add_argument("--list", action="store_true", help="list test files without running them")
args = parser.parse_args()


def discovered_files(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from discovered_files(test)
        else:
            module = test.__class__.__module__
            # Failed imports must still be run and reported as failures.
            if module == "unittest.loader":
                module = test._testMethodName
            yield "tests/" + module.replace(".", "/") + ".py"


tests = sorted(set(discovered_files(unittest.defaultTestLoader.discover("tests"))))


def explain(message):
    print(message, file=sys.stderr, flush=True)


def git(*arguments):
    return subprocess.check_output(["git", *arguments], stderr=subprocess.PIPE)


def changed_files():
    if "VALIDATE_CHANGED" in os.environ:
        return set(filter(None, os.environ["VALIDATE_CHANGED"].splitlines()))
    try:
        git("rev-parse", "--verify", "origin/main")
        target = "origin/main"
    except subprocess.CalledProcessError:
        target = "main"
    base = os.fsdecode(git("merge-base", "HEAD", target)).strip()
    changes = git("diff", "--name-only", "--no-renames", "-z", base, "HEAD")
    changes += git("diff", "--name-only", "--no-renames", "-z", "HEAD")
    changes += git("ls-files", "--others", "--exclude-standard", "-z")
    return {os.fsdecode(path) for path in changes.split(b"\0") if path}


def references(text, source):
    module = source[:-3].replace("/", ".")
    return bool(re.search(r"(?<![\w.])" + re.escape(module) + r"(?![\w.])", text) or source in text)


def dependencies(source, text):
    modules = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            modules.update(name.name for name in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                package = source.split("/")[:-1]
                base = ".".join(package[:len(package) - node.level + 1] + ([base] if base else []))
            modules.add(base)
            modules.update(base + "." + name.name for name in node.names if name.name != "*")
    paths = set()
    for module in modules:
        parts = module.split(".")
        for length in range(1, len(parts) + 1):
            path = "/".join(parts[:length])
            paths.update((path + ".py", path + "/__init__.py"))
            # unittest discover adds tests/ to sys.path for helper imports.
            paths.update(("tests/" + path + ".py", "tests/" + path + "/__init__.py"))
    return paths


def quick_tests():
    try:
        changed = changed_files()
    except subprocess.CalledProcessError:
        explain("无法计算 Git 改动，退回全量")
        return tests
    if not changed:
        explain("没有任何改动，退回全量")
        return tests
    selected = set()
    unmatched = []
    texts = {test: Path(test).read_text(encoding="utf-8") for test in tests}
    try:
        imports = {path.as_posix(): dependencies(path.as_posix(), path.read_text(encoding="utf-8"))
                   for path in Path(".").rglob("*.py") if ".git" not in path.parts}
    except (SyntaxError, UnicodeError, OSError):
        explain("无法分析 Python 依赖，退回全量")
        return tests
    # A test or module may put a subdirectory on sys.path and import a file by its bare name;
    # also count any repository file whose path ends with the imported name (over-selects, never misses).
    by_name = {}
    for path in imports:
        by_name.setdefault(path.rsplit("/", 1)[-1], []).append(path)
    for deps in imports.values():
        deps.update(path for dep in list(deps) for path in by_name.get(dep.rsplit("/", 1)[-1], ())
                    if path.endswith("/" + dep))
    if any(source.startswith("docs/") or source.lower().endswith(".md") for source in changed):
        for test, text in texts.items():
            if "ls-files" in text:
                selected.add(test)
                explain("选中 %s：文档改动，加跑全仓文本检查" % test)
    for source in sorted(changed):
        if source.startswith("docs/") or source.lower().endswith(".md"):
            continue
        if source.endswith(".py"):
            affected = {source}
            while True:
                dependents = {path for path, deps in imports.items() if deps & affected}
                if dependents <= affected:
                    break
                affected.update(dependents)
            matches = [test for test, text in texts.items()
                       if test in affected or any(references(text, path) for path in affected
                                                  if not path.startswith("tests/"))]
        else:
            matches = []
        if not matches:
            unmatched.append(source)
            explain("对应不上，退回全量：" + source)
        for test in matches:
            selected.add(test)
            explain("选中 %s：改动 %s" % (test, source))
    if unmatched:
        return tests
    if not selected:
        explain("本次文档改动没有匹配的全仓文本检查")
    return sorted(selected)


selected = quick_tests() if args.quick else tests
if args.quick and selected:
    explain("将运行的测试文件：\n" + "\n".join(selected))
if args.list:
    for test in selected:
        print(test)
    sys.exit(0)

failed = False
timings = []
test_env = dict(os.environ)
test_env["PYTHONPATH"] = os.pathsep.join((str(Path("tests").resolve()), os.environ.get("PYTHONPATH", "")))
for test in selected:
    print("运行 " + test, flush=True)
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "-v", test[6:-3].replace("/", ".")],
        stdin=subprocess.DEVNULL, env=test_env)
    timings.append((time.monotonic() - start, test))
    failed = failed or result.returncode != 0
if timings:
    print("用时最长的 5 个测试文件：", flush=True)
    for seconds, test in sorted(timings, reverse=True)[:5]:
        print("%s %.3f 秒" % (test, seconds), flush=True)
sys.exit(1 if failed else 0)
PY
