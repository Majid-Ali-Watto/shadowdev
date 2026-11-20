"""How a job ends in the run queue: what records its end (its `after`) runs while the job still holds
its issues, so the review watcher can't queue the same work again; and a job that was never started
(the server stopping) is released, and not recorded as ended."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from shadowdev.app import build_app
from tests.helpers import add_developer, add_project, join, make_cfg

ME = "acc-majid"
PR = "acme/fe#7"


class JobEndTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app(make_cfg(Path(tempfile.mkdtemp())), mock.Mock(), None)  # the queue is built, not started
        self.queue = self.app.queue
        self.me = add_developer(self.app.users, ME)
        self.market = add_project(self.app.projects, self.me, "marketplace", ["MAR"])
        join(self.app.users, self.me, self.market, ME)
        self.work = lambda *args, **kwargs: ("done", "approved")

    def test_the_end_is_recorded_while_the_issue_is_still_held(self):
        seen = []
        self.assertIsNone(self.queue.submit(self.market, [PR], self.me, work=self.work,
                                            after=lambda: seen.append(set(self.queue._pending))))
        job = self.queue._queue.get()  # as a worker takes it
        self.queue._end(job, time.monotonic(), ran=True)
        self.assertEqual(seen, [{(self.market.cloud_id, PR)}])  # held during `after`: no second review can be queued
        self.assertEqual(self.queue._pending, set())
        self.assertEqual(self.queue._jobs, {})
        self.assertIsNone(self.queue.submit(self.market, [PR], self.me, work=self.work))  # and it can be queued again

    def test_a_job_that_was_never_started_is_released_and_not_recorded_as_ended(self):
        ended = []  # an issue, which the next start queues again (a PR's review work is re-queued by its watcher)
        self.assertIsNone(self.queue.submit(self.market, ["MAR-1"], self.me, after=lambda: ended.append(True)))
        with self.queue._lock:
            self.queue._closing = True  # the server is stopping: the worker takes the job and lets it go
        worker = threading.Thread(target=self.queue.run_forever, daemon=True)
        worker.start()
        deadline = time.monotonic() + 5
        while (self.queue._pending or self.queue._jobs) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual((self.queue._pending, self.queue._jobs), (set(), {}))  # released
        self.assertEqual(ended, [])  # not recorded: it is queued again at the next start
        self.assertEqual(self.queue._working, 0)
        self.assertEqual([r["issue_key"] for r in self.app.runs.resume_after_restart()], ["MAR-1"])
        self.assertTrue(worker.is_alive())  # the worker waits for the next job, as it does


if __name__ == "__main__":
    unittest.main()
