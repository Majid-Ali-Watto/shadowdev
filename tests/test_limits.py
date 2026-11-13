import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from shadowdev.automation import limits
from shadowdev.automation.agent import CONTINUE, ClaudeSession
from shadowdev.automation.cancel import Cancelled, CancelToken

HOUR = 3600


def info(five=0.2, week=0.3, five_in=2 * HOUR, week_in=3 * 24 * HOUR, **more):
    now = time.time()
    return {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": five, "resetsAt": now + five_in},
                                                    "seven_day": {"utilization": week, "resetsAt": now + week_in}}, **more}


class LimitsTest(unittest.TestCase):
    def setUp(self):
        limits.USAGE.clear()
        self.addCleanup(limits.USAGE.clear)
        for name, value in (("POLL", 0.02), ("AFTER_RESET", 0)):
            patcher = mock.patch.object(limits, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def admit(self, **windows):
        limits.USAGE.record("acc", info(**windows))
        return limits.USAGE.admit("acc", time.time())

    def test_an_account_that_reports_nothing_or_has_room_starts_its_runs(self):
        self.assertIsNone(limits.USAGE.admit("api-key", time.time()))
        self.assertEqual(self.admit(five=0.96, week=0.89), 0.89)
        limits.USAGE.record("acc", {"status": "allowed", "unifiedWindows": "garbled"})  # ignored
        self.assertEqual(limits.USAGE.admit("acc", time.time()), 0.89)

    def test_nothing_starts_once_the_five_hour_window_is_nearly_used_until_it_resets(self):
        until, why = self.admit(five=0.97, five_in=HOUR)
        self.assertAlmostEqual(until, time.time() + HOUR, delta=5)
        self.assertEqual(why, "97% of the 5-hour Claude limit is used")
        self.assertEqual(self.admit(five=0.99, five_in=-1), 0.3)  # it has reset since

    def test_a_nearly_used_week_starts_only_what_fits_and_only_when_it_resets_soon(self):
        self.assertEqual(self.admit(week=0.95, week_in=6 * HOUR)[1], "95% of the weekly Claude limit is used")  # not soon
        self.assertEqual(self.admit(week=0.95, week_in=4 * HOUR), 0.95)  # 5% left: room for two runs of 2%
        self.assertEqual(limits.USAGE.admit("acc", time.time()), 0.95)
        self.assertIsInstance(limits.USAGE.admit("acc", time.time()), tuple)  # a third would not fit
        limits.USAGE.done("acc", 0.95)
        self.assertEqual(limits.USAGE.admit("acc", time.time()), 0.95)
        self.assertIsInstance(self.admit(week=0.99, week_in=4 * HOUR), tuple)  # 1% left: less than a run needs

    def test_what_a_run_used_is_what_the_next_one_is_taken_to_need(self):
        started = self.admit(week=0.80, week_in=4 * HOUR)
        limits.USAGE.record("acc", info(week=0.90, week_in=4 * HOUR))  # it used 10% of the week
        limits.USAGE.done("acc", started)
        self.assertIsInstance(self.admit(week=0.92, week_in=4 * HOUR), tuple)  # 8% left is not enough for the next
        self.assertEqual(self.admit(week=0.90, week_in=4 * HOUR), 0.90)

    def test_a_refused_request_holds_new_runs_until_the_limit_resets(self):
        limits.USAGE.record("acc", info(status="rejected", resetsAt=time.time() + HOUR))
        self.assertEqual(limits.USAGE.admit("acc", time.time())[1], "the Claude limit is reached")
        self.assertAlmostEqual(limits.USAGE.refused("acc", time.time()), time.time() + HOUR, delta=5)
        self.assertIsNone(limits.USAGE.refused("acc", time.time() + 2 * HOUR))

    def test_a_held_run_stays_queued_says_why_and_starts_after_the_reset(self):
        limits.USAGE.record("acc", info(five=0.98, five_in=0.2))
        told = []
        with limits.telling(told.append):
            started = time.monotonic()
            self.assertEqual(limits.admit(["acc"], CancelToken(), "MAR-1"), ("acc", 0.3))
        self.assertGreater(time.monotonic() - started, 0.15)
        self.assertRegex(told[0], r"^not started yet: 98% of the 5-hour Claude limit is used; it resets at \d\d:\d\d$")
        self.assertEqual(told[-1], "")

    def test_a_held_run_can_be_stopped(self):
        limits.USAGE.record("acc", info(five=0.98))
        token = CancelToken()
        threading.Timer(0.05, token.cancel).start()
        with self.assertRaises(Cancelled):
            limits.admit(["acc"], token, "MAR-1")

    # A run in progress

    def fake_claude(self, first: list[dict], session: str = "s-2"):
        """A claude whose first call prints `first`, and whose later calls succeed; each call's
        arguments and prompt are kept in calls/."""
        bin_dir = Path(tempfile.mkdtemp())
        (bin_dir / "calls").mkdir()
        (bin_dir / "first").write_text("\n".join(json.dumps(e) for e in first) + "\n")
        done = json.dumps({"type": "result", "is_error": False, "session_id": session, "structured_output": {"ok": 1}})
        script = bin_dir / "claude"
        script.write_text(f"""#!/bin/sh
n=$(ls {bin_dir}/calls | grep -c args)
echo "$@" > {bin_dir}/calls/$n.args
echo "$CLAUDE_CONFIG_DIR" > {bin_dir}/calls/$n.dir
cat > {bin_dir}/calls/$n.prompt
if [ "$n" = 0 ]; then cat {bin_dir}/first; else echo '{done}'; fi
""")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return bin_dir, {"PATH": f"{bin_dir}:{os.environ['PATH']}", "CLAUDE_CONFIG_DIR": "acc"}

    def refused(self, in_seconds: float) -> dict:
        return {"type": "rate_limit_event", "rate_limit_info": info(five=1.0, status="rejected", rateLimitType="five_hour",
                                                                    resetsAt=time.time() + in_seconds)}

    LIMIT = {"type": "result", "is_error": True, "session_id": "s-1", "result": "You've hit your session limit"}

    def test_a_run_cut_off_by_the_limit_waits_and_goes_on_in_the_same_conversation(self):
        calls, env = self.fake_claude([self.refused(0.2), {"type": "assistant", "session_id": "s-1", "message": None},
                                       self.LIMIT])
        told = []
        with limits.telling(told.append):
            self.assertEqual(ClaudeSession(Path(tempfile.mkdtemp()), env).ask("the work", "Read", {}), {"ok": 1})
        self.assertIn("waiting for the Claude limit to reset at", told[0])
        self.assertEqual(told[-1], "")
        self.assertIn("--resume s-1", (calls / "calls" / "1.args").read_text())  # the conversation it had begun
        self.assertEqual((calls / "calls" / "1.prompt").read_text(), CONTINUE)  # not the whole request again

    def test_with_a_spare_account_the_run_goes_on_there_at_once_with_its_conversation(self):
        calls, env = self.fake_claude([self.refused(6 * 3600), {"type": "assistant", "session_id": "s-1", "message": None},
                                       self.LIMIT])
        cwd, own, spare = (Path(tempfile.mkdtemp()) for _ in range(3))
        transcript = own / "projects" / "ws" / "s-1.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"cwd": str(cwd)}) + "\n")
        other = own / "projects" / "elsewhere" / "s-7.jsonl"  # another run's: stays where it is
        other.parent.mkdir()
        other.write_text(json.dumps({"cwd": "/somewhere/else"}) + "\n")
        env["CLAUDE_CONFIG_DIR"] = str(own)
        spares = [{**env, "CLAUDE_CONFIG_DIR": str(spare)}]
        told = []
        with limits.telling(told.append):
            session = ClaudeSession(cwd, env, spares=spares)
            started = time.monotonic()
            self.assertEqual(session.ask("the work", "Read", {}), {"ok": 1})
        self.assertLess(time.monotonic() - started, 5)  # no waiting, though its own limit resets in 6 hours
        self.assertEqual(told, ["its Claude account reached its limit; going on with another of your Claude accounts"])
        self.assertEqual((calls / "calls" / "1.dir").read_text().strip(), str(spare))
        self.assertIn("--resume s-1", (calls / "calls" / "1.args").read_text())
        self.assertEqual((calls / "calls" / "1.prompt").read_text(), CONTINUE)
        self.assertTrue((spare / "projects" / "ws" / "s-1.jsonl").exists())  # the conversation went along
        self.assertFalse((spare / "projects" / "elsewhere").exists())
        self.assertEqual((session.env["CLAUDE_CONFIG_DIR"], spares[0]["CLAUDE_CONFIG_DIR"]), (str(spare), str(own)))

    def test_a_spare_account_at_its_limit_too_is_not_switched_to(self):
        calls, env = self.fake_claude([self.refused(0.2), self.LIMIT])
        limits.USAGE.record("spare", info(five=0.99))
        session = ClaudeSession(Path(tempfile.mkdtemp()), env, spares=[{**env, "CLAUDE_CONFIG_DIR": "spare"}])
        self.assertEqual(session.ask("the work", "Read", {}), {"ok": 1})  # waited for its own reset
        self.assertEqual((calls / "calls" / "1.dir").read_text().strip(), "acc")

    def test_a_request_refused_before_claude_began_is_asked_again(self):
        calls, env = self.fake_claude([self.refused(0.1), self.LIMIT])
        session = ClaudeSession(Path(tempfile.mkdtemp()), env)
        session.session_id = "s-0"  # an earlier answer of this conversation
        self.assertEqual(session.ask("the work", "Read", {}), {"ok": 1})
        self.assertIn("--resume s-0", (calls / "calls" / "1.args").read_text())
        self.assertEqual((calls / "calls" / "1.prompt").read_text(), "the work")
        self.assertEqual(session.session_id, "s-2")

    def test_a_limit_that_resets_too_late_or_another_error_fails_the_run_as_before(self):
        _, env = self.fake_claude([self.refused(6 * HOUR), self.LIMIT])
        with self.assertRaisesRegex(RuntimeError, "hit your session limit"):
            ClaudeSession(Path(tempfile.mkdtemp()), env).ask("the work", "Read", {})
        limits.USAGE.clear()
        _, env = self.fake_claude([{"type": "result", "is_error": True, "result": "Not logged in"}])
        with self.assertRaisesRegex(RuntimeError, "Not logged in"):
            ClaudeSession(Path(tempfile.mkdtemp()), env).ask("the work", "Read", {})

    def test_a_waiting_run_can_be_stopped(self):
        _, env = self.fake_claude([self.refused(HOUR), self.LIMIT])
        token = CancelToken()
        threading.Timer(0.3, token.cancel).start()
        with self.assertRaises(Cancelled):
            ClaudeSession(Path(tempfile.mkdtemp()), env, cancel=token).ask("the work", "Read", {})


if __name__ == "__main__":
    unittest.main()
