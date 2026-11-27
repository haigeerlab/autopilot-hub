"""Local state shared by run and watch."""

from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import tempfile
from threading import get_ident
import time


REPAIR_LIMIT = 2


class StateLockedError(RuntimeError):
    """Another holder owns the watch lock."""


class StateCorruptError(ValueError):
    """The state file cannot be decoded or has an invalid structure."""


class WatchState:
    """Read snapshots and persist changes under an exclusive file lock.

    Mutations take the lock for a single update unless the caller already holds
    it with lock(). Each mutation is persisted before it returns. An injected
    clock must provide timezone-aware datetime values.
    """

    def __init__(self, directory=None, *, clock=None):
        directory = Path(directory) if directory is not None else Path.home() / ".autopilot"
        self.path = directory / "watch-state.json"
        self.lock_path = directory / "watch.lock"
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._lock_owner = None

    @contextmanager
    def lock(self, wait_seconds=0):
        """Hold the shared lock across updates, optionally retrying for up to wait_seconds."""
        self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        deadline = time.monotonic() + wait_seconds if wait_seconds > 0 else None
        with self.lock_path.open("a") as lock_file:
            while True:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                        raise
                    remaining = deadline - time.monotonic() if deadline is not None else 0
                    if remaining <= 0:
                        raise StateLockedError(f"Watch lock is held: {self.lock_path}") from exc
                    time.sleep(min(0.5, remaining))
            self._lock_owner = (os.getpid(), get_ident())
            try:
                yield self
            finally:
                self._lock_owner = None
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def read(self):
        """Read a complete snapshot, with defaults for a missing file or fields."""
        defaults = {
            "heartbeat": None, "frozen": False, "prs": {}, "runs": {}, "repos": {},
            "repairs": {}, "done": [], "outbox": [],
        }
        try:
            with self.path.open(encoding="utf-8") as state_file:
                data = json.load(state_file)
        except FileNotFoundError:
            return defaults
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StateCorruptError(f"Invalid JSON in {self.path}: {exc}") from exc

        if not isinstance(data, dict):
            raise StateCorruptError(f"Expected a JSON object in {self.path}")
        fields = {key: type(value) for key, value in defaults.items()}
        fields.update(heartbeat=(str, type(None)), intents=dict)
        for key, expected in fields.items():
            if key in data and not isinstance(data[key], expected):
                raise StateCorruptError(f"Invalid {key} field in {self.path}")
        defaults.update(data)
        return defaults

    @contextmanager
    def _locked_snapshot(self):
        held = self._lock_owner == (os.getpid(), get_ident())
        with nullcontext() if held else self.lock():
            yield self.read()

    def _write(self, data):
        temporary = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, prefix=".watch-state-", delete=False,
        )
        try:
            with temporary:
                json.dump(data, temporary, ensure_ascii=False, allow_nan=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary.name, self.path)
        finally:
            Path(temporary.name).unlink(missing_ok=True)

    def now(self):
        now = self._clock()
        if now.utcoffset() is None:
            raise ValueError("The watch clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)

    def write_heartbeat(self):
        with self._locked_snapshot() as data:
            data["heartbeat"] = self.now().isoformat()
            self._write(data)

    def heartbeat_is_fresh(self, minutes):
        stamp = self.read()["heartbeat"]
        if stamp is None:
            return False
        try:
            at = datetime.fromisoformat(stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp)
            if at.utcoffset() is None:
                raise ValueError("missing timezone")
        except ValueError as exc:
            raise StateCorruptError(f"Invalid heartbeat in {self.path}: {exc}") from exc
        return timedelta(0) <= self.now() - at <= timedelta(minutes=minutes)

    def is_frozen(self):
        return self.read()["frozen"]

    def set_frozen(self, frozen: bool):
        with self._locked_snapshot() as data:
            data["frozen"] = frozen
            self._write(data)

    def _set_fields(self, section, key, fields):
        with self._locked_snapshot() as data:
            data[section].setdefault(key, {}).update(fields)
            self._write(data)

    def get_pr(self, repo, number):
        return self.read()["prs"].get(f"{repo}#{number}")

    def set_pr(self, repo, number, **fields):
        """Update a PR record while retaining fields omitted by the caller."""
        self._set_fields("prs", f"{repo}#{number}", fields)

    def pause_pr(self, repo, number, reason, recovery, **fields):
        self.set_pr(repo, number, state="paused", pause={
            "reason": reason, "at": self.now().isoformat(), "recovery": recovery,
        }, **fields)

    def get_run(self, repo, run_id, attempt):
        return self.read()["runs"].get(f"{repo}:{run_id}:{attempt}")

    def set_run(self, repo, run_id, attempt, **fields):
        """Update a single CI attempt while retaining fields omitted by the caller."""
        self._set_fields("runs", f"{repo}:{run_id}:{attempt}", fields)

    def get_cursor(self, repo):
        return self.read()["repos"].get(repo, {}).get("cursor")

    def set_cursor(self, repo, run_id):
        self.set_repo(repo, cursor=run_id)

    def set_repo(self, repo, **fields):
        """Update repository watch fields without changing the narrowing hold."""
        self._set_fields("repos", repo, fields)

    def get_narrowed(self, repo):
        return self.read()["repos"].get(repo, {}).get("narrowed")

    def narrow_repo(self, repo, reason, source_run):
        self._set_fields("repos", repo, {"narrowed": {
            "reason": reason, "at": self.now().isoformat(), "sourceRun": source_run,
        }})

    def resume(self, repo):
        """Clear repository narrowing and retain its cursor and other fields."""
        with self._locked_snapshot() as data:
            record = data["repos"].get(repo, {})
            if "narrowed" in record:
                del record["narrowed"]
                self._write(data)

    def record_repair(self, repo, number, reason_key, commit):
        """Record a distinct repair commit and return the resulting count."""
        with self._locked_snapshot() as data:
            repairs = data["repairs"].setdefault(f"{repo}#{number}", {}).setdefault(reason_key, [])
            if commit not in repairs:
                repairs.append(commit)
                self._write(data)
            return len(repairs)

    def repair_count(self, repo, number, reason_key):
        return len(self.read()["repairs"].get(f"{repo}#{number}", {}).get(reason_key, []))

    def has_failure_on_other_head(self, repo, number, head):
        """Return whether this PR has a recorded failure on a different head."""
        pr_key = f"{repo}#{number}"
        data = self.read()
        first_failures = data["prs"].get(pr_key, {}).get("firstFailures", {})
        if any(commit != head for commit in first_failures.values()):
            return True
        repairs = data["repairs"].get(pr_key, {})
        return any(commit != head for commits in repairs.values() for commit in commits)

    def record_failure(self, repo, number, reason_key, commit):
        """Record the first failing head separately and return the counted repair commits."""
        pr_key = f"{repo}#{number}"
        with self._locked_snapshot() as data:
            repairs = data["repairs"].get(pr_key, {}).get(reason_key, [])
            first = data["prs"].get(pr_key, {}).get("firstFailures", {}).get(reason_key)
            if first is None and not repairs:
                data["prs"].setdefault(pr_key, {}).setdefault("firstFailures", {})[reason_key] = commit
                self._write(data)
            elif commit != first and commit not in repairs and len(repairs) < REPAIR_LIMIT:
                self.record_repair(repo, number, reason_key, commit)
                repairs.append(commit)
            return repairs[:REPAIR_LIMIT]

    def intend(self, key, detail):
        """Persist a new intent and return whether it was recorded."""
        with self._locked_snapshot() as data:
            intents = data.setdefault("intents", {})
            if key in data["done"] or key in intents:
                return False
            intents[key] = detail
            self._write(data)
            return True

    def pending_intents(self):
        return self.read().get("intents", {})

    def _complete(self, data, key):
        if key not in data["done"]:
            data["done"].append(key)
        intents = data.get("intents", {})
        intents.pop(key, None)
        if not intents:
            data.pop("intents", None)

    def mark_done(self, key):
        with self._locked_snapshot() as data:
            if key not in data["done"]:
                self._complete(data, key)
                self._write(data)

    def is_done(self, key):
        return key in self.read()["done"]

    def enqueue_notification(self, target, body, key, *, repo=None, number=None):
        """Queue a notice once per key, including across successful delivery."""
        with self._locked_snapshot() as data:
            if key in data["done"] or any(item["key"] == key for item in data["outbox"]):
                return False
            item = {"target": target, "body": body, "key": key}
            if repo is not None:
                item.update(repo=repo, number=number)
            data["outbox"].append(item)
            self._write(data)
            return True

    def pending_notifications(self):
        return self.read()["outbox"]

    def pause_notification(self, key, reason, recovery):
        with self._locked_snapshot() as data:
            for item in data["outbox"]:
                if item["key"] == key:
                    item["pause"] = dict(reason=reason, recovery=recovery, at=self.now().isoformat())
                    self._write(data)
                    break

    def dequeue_notification(self, key):
        """After delivery succeeds, remove the notice and record completion together."""
        with self._locked_snapshot() as data:
            remaining = [item for item in data["outbox"] if item["key"] != key]
            if len(remaining) != len(data["outbox"]):
                data["outbox"] = remaining
                self._complete(data, key)
                self._write(data)
