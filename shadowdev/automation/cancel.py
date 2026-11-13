"""Stopping a run: when its developer deletes their account, a run in progress stops at once.
Its running processes (Claude, setup and test commands) are killed, and the pipeline checks
before every step that writes anywhere (Jira, git push, pull requests)."""
import subprocess
import threading
from contextlib import contextmanager


class Cancelled(Exception):
    """The run was stopped."""


class CancelToken:
    def __init__(self):
        self._event = threading.Event()
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        with self._lock:
            self._event.set()
            for proc in self._procs:
                proc.kill()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled("the run was stopped")

    def wait(self, seconds: float) -> None:
        """Sleep, unless the run is stopped meanwhile."""
        if self._event.wait(seconds):
            raise Cancelled("the run was stopped")

    @contextmanager
    def tracking(self, proc: subprocess.Popen):
        """Kill `proc` if the run is stopped while it runs (or already was)."""
        with self._lock:
            self._procs.add(proc)
            if self._event.is_set():
                proc.kill()
        try:
            yield
        finally:
            with self._lock:
                self._procs.discard(proc)


NEVER = CancelToken()  # for runs nobody can stop, e.g. a dry run
