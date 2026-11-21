"""Shared helpers for the slice-5 checkup tests: throwaway git repos and a fake `gh` transport."""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from checkup.ghprobe import GhResponse  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def git(root, *args):
    subprocess.run(["git", "-C", root] + list(args), check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def write_files(root, files):
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)


def make_repo(test, files, remote=None):
    """A git repo on branch main with one commit of `files`; removed after the test."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = os.path.realpath(tmp.name)
    git(root, "init", "-q")
    git(root, "checkout", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    write_files(root, files)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    if remote:
        git(root, "remote", "add", "origin", "https://github.com/%s.git" % remote)
    return root


def commit_on_branch(root, files, branch="feature"):
    git(root, "checkout", "-q", "-b", branch)
    write_files(root, files)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "change")


class FakeTransport:
    """Answers from {path: GhResponse | data}; an unknown path is a 404."""

    def __init__(self, responses):
        self.responses = responses
        self.paths = []

    def fetch(self, path):
        self.paths.append(path)
        r = self.responses.get(path)
        if r is None:
            return GhResponse("not_found", 404, {"message": "Not Found"}, "Not Found")
        return r if isinstance(r, GhResponse) else GhResponse("ok", 200, r)


def repo_meta(**extra):
    d = {"default_branch": "main", "visibility": "public",
         "security_and_analysis": {"secret_scanning": {"status": "enabled"},
                                   "secret_scanning_push_protection": {"status": "enabled"}}}
    d.update(extra)
    return d


def good_github(name="o/n", branch="main"):
    """Responses that make C3, C4, C6 and C7 all ok."""
    return {
        "repos/%s" % name: repo_meta(),
        "repos/%s/branches/%s/protection" % (name, branch): GhResponse(
            "not_found", 404, {"message": "Branch not protected"}, "Branch not protected"),
        "repos/%s/rules/branches/%s" % (name, branch): [
            {"type": t, "ruleset_source_type": "Repository", "ruleset_source": name, "ruleset_id": 7}
            for t in ("deletion", "non_fast_forward", "pull_request")],
        "repos/%s/rulesets/7" % name: {"name": "gate", "enforcement": "active", "bypass_actors": []},
        "repos/%s/actions/runs?branch=%s&status=completed&per_page=30" % (name, branch): {"workflow_runs": [
            {"event": "push", "head_branch": branch, "conclusion": "success", "created_at": "2026-11-20T00:00:00Z",
             "name": "CI", "html_url": "u"}]},
        "repos/%s/actions/permissions/workflow" % name: {"default_workflow_permissions": "read",
                                                         "can_approve_pull_request_reviews": False},
    }


PR_CI = """name: CI
on:
  pull_request:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@%s
      - run: python3 -B -m unittest discover -s tests
""" % ("a" * 40)

INTERFACE = """version: 1
entries:
  check:
    run: "python3 -B -m unittest discover -s tests"
    report: wrapped
"""
