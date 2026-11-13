"""A developer's automation settings per project, and restarting the server on changes (--reload)."""
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from shadowdev import reload
from shadowdev.models import settings
from shadowdev.models.settings import Settings


class SettingsTest(unittest.TestCase):
    def test_stored_settings_fall_back_to_the_defaults(self):
        self.assertEqual(Settings.from_json(None), Settings())
        self.assertEqual(Settings.from_json("not json"), Settings())
        stored = Settings.from_json('{"fix_ci": false, "publish": "draft", "model": "gpt", "auto_start": "yes", "old": 1}')
        self.assertEqual(stored, Settings(fix_ci=False, publish="draft"))  # bad values and unknown keys dropped
        self.assertEqual(Settings.from_json(stored.to_json()), stored)

    def test_from_a_form_an_unticked_switch_is_off(self):
        picked = settings.from_form({"auto_start": "on", "model": "haiku", "publish": "branch"})
        self.assertEqual((picked.auto_start, picked.model, picked.publish, picked.fix_ci, picked.jira_comments),
                         (True, "haiku", "branch", False, False))
        self.assertEqual(settings.from_form({}).model, "auto")
        picked = settings.from_form({"review_for_me": "on", "review:fe": "on"}, ("fe", "services"))
        self.assertEqual(picked.no_review_repos, ("services",))
        self.assertTrue(picked.reviews("fe"))
        self.assertFalse(picked.reviews("services"))
        self.assertEqual(Settings.from_json(picked.to_json()), picked)  # stored as a JSON list, read as a tuple
        self.assertEqual(Settings.from_json('{"no_review_repos": [1]}').no_review_repos, ())
        with self.assertRaisesRegex(ValueError, "not a choice"):
            settings.from_form({"publish": "force-push"})

    def test_the_conditions_for_starting_by_itself(self):
        self.assertEqual((Settings().auto_statuses, Settings().auto_sprint), ("To Do, Backlog, Ready for Pickup, Reopened", True))
        picked = settings.from_form({"auto_start": "on", "auto_statuses": " To Do ,  QA (ready) "})
        self.assertEqual((picked.auto_statuses, picked.auto_sprint), ("To Do , QA (ready)", False))
        for bad in ("To Do; DROP", "<b>", "x" * 201):
            with self.assertRaisesRegex(ValueError, "not a list of Jira statuses"):
                settings.from_form({"auto_statuses": bad})
        active = {"customfield_10020": [{"state": "active", "boardId": 1}], "status": {"name": "to do"}}
        self.assertIsNone(Settings().start_problem(active))
        self.assertIsNone(picked.start_problem({**active, "status": {"name": "QA (ready)"}, "customfield_10020": None}))
        self.assertEqual(Settings().start_problem({"status": {"name": "Done"}}), "its status Done is not one that starts by itself")

    def test_branches_approved_without_a_review(self):
        self.assertFalse(Settings().approves_unreviewed("release/15", "main"))  # none chosen: off
        picked = settings.from_form({"review_approve": "on", "approve_from": " release/*,  hotfix/* ", "approve_into": "main"})
        self.assertEqual((picked.approve_from, picked.approve_into), ("release/*, hotfix/*", "main"))
        self.assertTrue(picked.approves_unreviewed("release/15", "main"))
        self.assertTrue(picked.approves_unreviewed("hotfix/login", "main"))
        self.assertFalse(picked.approves_unreviewed("feature/x", "main"))
        self.assertFalse(picked.approves_unreviewed("release/15", "Main"))  # branch names are case-sensitive
        self.assertEqual(Settings.from_json(picked.to_json()), picked)
        self.assertEqual(Settings.from_json('{"approve_from": "a b; rm"}').approve_from, "")
        for bad in ("release/* main", "<script>", "x" * 201):
            with self.assertRaisesRegex(ValueError, "branch names"):
                settings.from_form({"approve_from": bad})


class ReloadTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "shadowdev").mkdir()
        self.code = self.root / "shadowdev" / "app.py"
        self.code.write_text("x = 1\n")
        (self.root / "shadowdev" / "notes.txt").write_text("not watched\n")

    def test_a_change_restarts_the_server_once_and_ctrl_c_stops_it(self):
        started, stopped = [], []
        steps = iter([
            lambda: None,  # nothing changed
            lambda: (self.root / "shadowdev" / "notes.txt").write_text("other\n"),  # not code: no restart
            lambda: os.utime(self.code, ns=(1, 2_000_000_000)),  # saved: restart
            lambda: None,  # settling
            lambda: None,
        ])

        def sleep(_):
            try:
                next(steps)()
            except StopIteration:
                raise KeyboardInterrupt from None

        child = lambda argv: started.append(argv) or mock.Mock(poll=lambda: None)
        with mock.patch.object(reload, "WATCHED", (self.root / "shadowdev", self.root / ".env")), \
             mock.patch.object(reload, "PROJECT_ROOT", self.root), mock.patch.object(reload, "start", child), \
             mock.patch.object(reload, "stop", stopped.append), mock.patch.object(reload.time, "sleep", sleep), \
             self.assertLogs("shadowdev", "INFO") as logs:
            reload.watch([])
        self.assertEqual(len(started), 2)  # the first start, and one restart
        self.assertEqual(len(stopped), 2)  # for the restart, and at Ctrl+C
        self.assertIn("changed: shadowdev/app.py; restarting the server", "\n".join(logs.output))

    def test_stopping_the_server_stops_what_it_started(self):
        child = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 60"], start_new_session=True)
        time.sleep(0.2)
        reload.stop(child)
        self.assertIsNotNone(child.poll())
        for _ in range(50):  # the background sleep, in the same process group, is gone too
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.killpg(child.pid, signal.SIGKILL)
            self.fail("a process of the server outlived it")


if __name__ == "__main__":
    unittest.main()
