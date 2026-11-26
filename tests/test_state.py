"""Tests for the local state shared by run and watch (R13)."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from orchestrator.state import StateCorruptError, StateLockedError, WatchState


REPO = "owner/repo"
PR = 7
HEAD = "a" * 40
BASE = "b" * 40
NOW = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)
STAMP = NOW.isoformat()


class WatchStateTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "watch-state.json"
        self.now = NOW
        self.state = self.open_state()

    def open_state(self):
        return WatchState(self.directory, clock=lambda: self.now)

    def test_missing_state_and_default_paths(self):
        with mock.patch("orchestrator.state.Path.home", return_value=self.directory):
            default = WatchState()
        self.assertEqual(default.path, self.directory / ".autopilot" / "watch-state.json")
        self.assertEqual(default.lock_path, self.directory / ".autopilot" / "watch.lock")
        self.assertEqual(self.state.read(), {
            "heartbeat": None, "frozen": False, "prs": {}, "runs": {}, "repos": {},
            "repairs": {}, "done": [], "outbox": [],
        })
        self.assertFalse(self.path.exists())

    def test_lock_is_exclusive_and_released_on_exception(self):
        other = self.open_state()
        with self.assertRaisesRegex(RuntimeError, "stop"):
            with self.state.lock():
                self.state.set_frozen(True)
                with self.assertRaises(StateLockedError):
                    with other.lock():
                        self.fail("The second holder acquired the lock")
                with self.assertRaises(StateLockedError):
                    other.set_frozen(False)
                self.assertTrue(other.is_frozen())
                raise RuntimeError("stop")
        with other.lock():
            other.set_frozen(False)
        self.assertFalse(self.state.is_frozen())

    def test_atomic_replace_writes_a_complete_file_in_the_same_directory(self):
        self.state.set_frozen(True)
        real_replace = os.replace
        temporary_paths = []

        def replace(source, destination):
            source = Path(source)
            self.assertEqual(source.parent, self.path.parent)
            self.assertNotEqual(source, self.path)
            self.assertEqual(Path(destination), self.path)
            self.assertTrue(json.loads(self.path.read_text(encoding="utf-8"))["frozen"])
            self.assertFalse(json.loads(source.read_text(encoding="utf-8"))["frozen"])
            temporary_paths.append(source)
            real_replace(source, destination)

        with mock.patch("orchestrator.state.os.replace", side_effect=replace) as replace_mock:
            self.state.set_frozen(False)
        replace_mock.assert_called_once()
        self.assertFalse(self.state.is_frozen())
        self.assertFalse(temporary_paths[0].exists())

    def test_failed_replace_keeps_previous_state_and_cleans_up_temp_file(self):
        self.state.set_frozen(True)
        before = self.path.read_bytes()
        with mock.patch("orchestrator.state.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                self.state.set_frozen(False)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertCountEqual(self.directory.iterdir(), [self.path, self.state.lock_path])

    def test_corrupt_state_is_reported_and_preserved(self):
        for bad in (b"{broken", b"\xff", b"[]", b'{"outbox": {}}'):
            with self.subTest(bad=bad):
                self.path.write_bytes(bad)
                with self.assertRaisesRegex(StateCorruptError, "watch-state.json"):
                    self.state.read()
                with self.assertRaises(StateCorruptError):
                    self.state.set_frozen(True)
                self.assertEqual(self.path.read_bytes(), bad)

    def test_heartbeat_is_fresh_through_the_requested_limit(self):
        self.assertFalse(self.state.heartbeat_is_fresh(5))
        self.now = NOW.astimezone(timezone(timedelta(hours=8)))
        self.state.write_heartbeat()
        self.assertEqual(self.state.read()["heartbeat"], STAMP)
        self.now = NOW + timedelta(minutes=5)
        self.assertTrue(self.open_state().heartbeat_is_fresh(5))
        self.now += timedelta(microseconds=1)
        self.assertFalse(self.state.heartbeat_is_fresh(5))
        self.now = NOW - timedelta(seconds=1)
        self.assertFalse(self.state.heartbeat_is_fresh(5))

    def test_freeze_and_clear(self):
        self.assertFalse(self.state.is_frozen())
        self.state.set_frozen(True)
        self.assertTrue(self.open_state().is_frozen())
        self.state.set_frozen(False)
        self.assertFalse(self.open_state().is_frozen())

    def test_pr_record_updates_preserve_validation_fields(self):
        self.assertIsNone(self.state.get_pr(REPO, PR))
        self.state.set_pr(REPO, PR, state="automerge-requested", validatedHeadOid=HEAD,
                          validatedBaseOid=BASE, since=STAMP, notify="author")
        self.state.pause_pr(REPO, PR, "Head changed", "Run verification again")
        expected = {
            "state": "paused", "validatedHeadOid": HEAD, "validatedBaseOid": BASE,
            "since": STAMP, "notify": "author",
            "pause": {"reason": "Head changed", "at": STAMP, "recovery": "Run verification again"},
        }
        self.assertEqual(self.open_state().get_pr(REPO, PR), expected)
        self.assertEqual(self.state.read()["prs"], {f"{REPO}#{PR}": expected})
        self.state.set_pr(REPO, PR, state="automerge-requested", pause=None)
        self.assertIsNone(self.state.get_pr(REPO, PR)["pause"])

    def test_runs_use_run_id_and_attempt_even_when_sha_is_the_same(self):
        self.assertIsNone(self.state.get_run(REPO, 10, 1))
        self.state.set_run(REPO, 10, 1, event="push", headSha=HEAD, state="red-classified")
        self.state.set_run(REPO, 10, 2, event="push", headSha=HEAD, state="pending")
        self.state.set_run(REPO, 11, 1, event="push", headSha=HEAD, state="green")
        self.state.set_run(REPO, 10, 2, state="green")
        self.assertEqual(set(self.state.read()["runs"]), {f"{REPO}:10:1", f"{REPO}:10:2", f"{REPO}:11:1"})
        self.assertEqual(self.open_state().get_run(REPO, 10, 1)["state"], "red-classified")
        self.assertEqual(self.state.get_run(REPO, 10, 2), {"event": "push", "headSha": HEAD, "state": "green"})

    def test_narrowing_and_resume_preserve_repository_cursor(self):
        self.assertIsNone(self.state.get_cursor(REPO))
        self.assertIsNone(self.state.get_narrowed(REPO))
        self.state.set_cursor(REPO, 10)
        source = f"{REPO}:11:2"
        self.state.narrow_repo(REPO, "Main tests failed", source)
        self.state.set_cursor(REPO, 11)
        self.assertEqual(self.open_state().get_narrowed(REPO), {
            "reason": "Main tests failed", "at": STAMP, "sourceRun": source,
        })
        self.state.resume(REPO)
        self.state.resume(REPO)
        self.assertIsNone(self.open_state().get_narrowed(REPO))
        self.assertEqual(self.open_state().get_cursor(REPO), 11)
        self.assertEqual(self.state.read()["repos"], {REPO: {"cursor": 11}})

    def test_repairs_count_unique_commits_per_pr_and_reason(self):
        self.assertEqual(self.state.repair_count(REPO, PR, "tests"), 0)
        self.assertEqual(self.state.record_repair(REPO, PR, "tests", HEAD), 1)
        self.assertEqual(self.open_state().record_repair(REPO, PR, "tests", HEAD), 1)
        self.assertEqual(self.state.record_repair(REPO, PR, "tests", BASE), 2)
        self.state.record_repair(REPO, PR, "lint", HEAD)
        self.assertEqual(self.open_state().repair_count(REPO, PR, "tests"), 2)
        self.assertEqual(self.state.repair_count(REPO, PR, "lint"), 1)
        self.assertEqual(self.state.repair_count(REPO, PR + 1, "tests"), 0)
        self.assertEqual(self.state.read()["repairs"], {f"{REPO}#{PR}": {"tests": [HEAD, BASE], "lint": [HEAD]}})

    def test_pending_intent_survives_a_process_exit_and_is_completed_once(self):
        key = f"{REPO}:{PR}:{HEAD}:enable-automerge"
        detail = {"pr": PR, "head": HEAD}
        child = subprocess.run(
            [sys.executable, "-B", "-c", """\
import json
import os
import sys
from orchestrator.state import WatchState

state = WatchState(sys.argv[1])
with state.lock():
    assert state.intend(sys.argv[2], json.loads(sys.argv[3]))
    os._exit(17)
""", str(self.directory), key, json.dumps(detail)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(child.returncode, 17, child.stderr)

        reopened = self.open_state()
        self.assertEqual(reopened.pending_intents(), {key: detail})
        self.assertFalse(reopened.is_done(key))
        self.assertFalse(reopened.intend(key, {"pr": PR, "head": BASE}))
        self.assertEqual(reopened.pending_intents(), {key: detail})
        reopened.mark_done(key)
        reopened.mark_done(key)
        self.assertTrue(self.open_state().is_done(key))
        self.assertFalse(reopened.intend(key, detail))
        self.assertEqual(reopened.pending_intents(), {})
        self.assertEqual(reopened.read()["done"], [key])

    def test_notification_deduplicates_and_only_acknowledged_entry_is_removed(self):
        self.assertTrue(self.state.enqueue_notification("author", "Paused", "notice-1"))
        self.assertFalse(self.open_state().enqueue_notification("other", "Changed body", "notice-1"))
        self.assertTrue(self.state.enqueue_notification("user", "Resumed", "notice-2"))
        first = {"target": "author", "body": "Paused", "key": "notice-1"}
        second = {"target": "user", "body": "Resumed", "key": "notice-2"}
        self.assertEqual(self.open_state().pending_notifications(), [first, second])
        self.state.dequeue_notification("unknown")
        self.state.dequeue_notification("notice-1")
        self.state.dequeue_notification("notice-1")
        self.assertEqual(self.open_state().pending_notifications(), [second])
        self.assertTrue(self.open_state().is_done("notice-1"))
        self.assertFalse(self.state.enqueue_notification("author", "Paused", "notice-1"))
        self.state.dequeue_notification("notice-2")
        self.assertEqual(self.open_state().pending_notifications(), [])
