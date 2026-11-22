"""Release clones use real local Git repositories without network access."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import cli


REPO = "haigeerlab/autopilot-sandbox"
URL = "https://github.com/" + REPO


class ReleaseCheckoutTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = {"PATH": os.environ["PATH"], "HOME": str(self.root),
                       "GH_TOKEN": "parent-secret", "GITHUB_TOKEN": "parent-secret",
                       "GH_ENTERPRISE_TOKEN": "parent-secret", "CUSTOM": "kept"}
        patch = mock.patch.dict(os.environ, environment, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        self.git(self.remote, "init", "-q")
        self.git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        self.git(self.remote, "-c", "user.name=Test", "-c", "user.email=test@example.com",
                 "commit", "-q", "--allow-empty", "-m", "Pinned release commit")
        self.commit = self.git(self.remote, "rev-parse", "HEAD")
        self.git(self.remote, "tag", "v0.1.0")
        self.git(self.remote, "-c", "user.name=Test", "-c", "user.email=test@example.com",
                 "commit", "-q", "--allow-empty", "-m", "New base tip")
        self.tip = self.git(self.remote, "rev-parse", "HEAD")
        self.git(self.remote, "tag", "v0.2.0")
        # Redirect only this repository's HTTPS URL to a local remote for real fetches.
        self.git(self.remote, "config", "--global", "url.%s.insteadOf" % self.remote.as_uri(), URL)
        self.io = cli.LocalIO("hub-secret", None)

    @staticmethod
    def git(root, *args):
        return subprocess.run(["git", "-C", str(root)] + list(args), check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_origin_tracks_base_and_checkout_stays_pinned_without_local_branches(self):
        for base_ref in ("main", "release/stable"):
            with self.subTest(base_ref=base_ref):
                if base_ref != "main":
                    self.git(self.remote, "branch", base_ref)
                with self.io.release_checkout(REPO, base_ref, self.commit) as work:
                    self.assertEqual(work.git("rev-parse", "origin/" + base_ref).strip(), self.tip)
                    self.assertEqual(work.git("rev-parse", "refs/remotes/base").strip(), self.tip)
                    self.assertEqual(work.git("rev-parse", "HEAD").strip(), self.commit)
                    self.assertEqual(work.git("config", "remote.origin.url").strip(), URL)
                    self.assertEqual(work.git("config", "--get-all", "remote.origin.fetch").strip(),
                                     "+refs/heads/%s:refs/remotes/origin/%s" % (base_ref, base_ref))
                    self.assertEqual(work.git("branch", "--show-current").strip(), "")
                    self.assertEqual(work.git("for-each-ref", "refs/heads").strip(), "")

    def test_release_clone_fetches_remote_tags(self):
        with self.io.release_checkout(REPO, "main", self.commit) as work:
            self.assertEqual(work.git("tag").splitlines(), ["v0.1.0", "v0.2.0"])
            self.assertEqual(work.git("rev-parse", "v0.1.0").strip(), self.commit)
            self.assertEqual(work.git("rev-parse", "v0.2.0").strip(), self.tip)

    def test_step_environment_has_no_hub_git_authentication(self):
        with self.io.release_checkout(REPO, "main", self.commit) as work:
            self.assertIn("AUTHORIZATION:", work.git_env["GIT_CONFIG_VALUE_0"])
            self.assertNotIn("extraheader", work.git("config", "--local", "--list").lower())
            command = shlex.quote(sys.executable) + ' -c "import json, os; print(json.dumps(dict(os.environ)))"'
            result = work.run_release(command, "1.0.0", REPO, 9)
            self.assertEqual(result["exit"], 0, result["output"])
            environment = json.loads(result["output"])
            self.assertFalse(any(key.startswith("GIT_CONFIG_") for key in environment))
            for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
                self.assertNotIn(key, environment)
            self.assertEqual(environment["CUSTOM"], "kept")
            self.assertEqual(environment["AUTOPILOT_VERSION"], "1.0.0")

    def test_pr_work_still_has_no_origin_or_tags(self):
        root = self.root / "pr-clone"
        root.mkdir()
        pr = {"head": {"sha": self.commit}, "base": {"ref": "main"}}
        work = cli.Work(str(root), REPO, pr, "hub-secret", None)
        self.assertEqual(work.git("remote").strip(), "")
        self.assertEqual(work.git("tag").strip(), "")
        self.assertEqual(work.git("rev-parse", "refs/remotes/base").strip(), self.tip)


if __name__ == "__main__":
    unittest.main()
