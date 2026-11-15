"""What waits for a developer's working hours (services/outbox.py): sent once they begin."""
import dataclasses
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from shadowdev.app import build_app
from shadowdev.automation import pipeline
from shadowdev.models.publication import Change, Publication
from shadowdev.models.settings import DAYS, Settings
from shadowdev.services import outbox
from tests.helpers import add_developer, add_project, join, make_cfg

ME = "acc-majid"


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app(make_cfg(Path(tempfile.mkdtemp())), mock.Mock(), mock.Mock())
        self.me = add_developer(self.app.users, ME)
        self.market = add_project(self.app.projects, self.me, "marketplace", ["MAR"])
        now = datetime.now(UTC)
        self.closed = Settings(work_from=(now + timedelta(hours=2)).strftime("%H:%M"),
                               work_until=(now + timedelta(hours=3)).strftime("%H:%M"), work_days=DAYS, work_timezone="UTC")
        join(self.app.users, self.me, self.market, ME, settings=self.closed)
        self.github, self.jira, self.slack = mock.Mock(), mock.Mock(), mock.Mock()
        self.github.create_pr.side_effect = lambda repo, **kw: f"https://github.com/{repo}/pull/7"
        self.slack.post.return_value = "111.222"
        self.jira.move_along.return_value = ("In Review", ["In Review"])
        for target, name, value in ((outbox, "GitHub", self.github), (outbox, "jira_client", self.jira),
                                    (outbox, "Slack", self.slack), (pipeline, "Slack", self.slack)):
            patch = mock.patch.object(target, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        self.run_id = self.app.runs.add("MAR-1", self.me, self.market.id, self.market.name)
        self.app.runs.update(self.run_id, "done", "pushed; its PRs wait")
        publication = Publication(("MAR-1",), "MAR-1", "MAR-1: Totals", (Change("fe", "main", "new", "Resolves MAR-1"),))
        self.held = self.app.outbox.held
        self.held.hold_publication(self.me, self.market.id, self.run_id, None, publication)
        self.held.hold_slack(self.me, self.market.id, "C0REVIEWS1", "Kindly review this PR again", "9.9")

    def settings(self, chosen: Settings) -> None:
        sub = self.app.users.get(self.me).subscriptions[self.market.id]
        self.app.users.subscribe(self.me, dataclasses.replace(sub, settings=chosen))

    def test_sent_once_working_hours_begin(self):
        self.assertEqual(self.app.outbox.flush(), 0)  # outside them: nothing
        self.github.create_pr.assert_not_called()
        self.assertEqual(len(self.held.waiting()), 2)
        self.settings(Settings())  # within them (none set: any time)
        self.app.users.save_slack(self.me, "xoxp-1", "U0000MEME0", "acme")
        self.assertEqual(self.app.outbox.flush(), 2)
        self.github.create_pr.assert_called_once_with("acme/fe", base="main", head="MAR-1", title="MAR-1: Totals",
                                                      body="Resolves MAR-1", draft=False)
        self.assertIn(mock.call("C0REVIEWS1", "Kindly review this PR again", "9.9"), self.slack.post.call_args_list)
        run = next(r for r in self.app.runs.recent(self.me) if r["id"] == self.run_id)
        self.assertEqual((run["status"], run["message"]), ("done", "fe into main: https://github.com/acme/fe/pull/7"))
        self.assertEqual([r["number"] for r in self.app.reviews.tracked_prs()], [7])  # watched for review comments
        self.assertEqual(self.held.waiting(), [])
        self.assertEqual(self.app.outbox.flush(), 0)  # sent once

    def test_dropped_when_the_developer_leaves_the_project(self):
        self.app.users.unsubscribe(self.me, self.market.id)
        self.app.outbox.flush()
        self.github.create_pr.assert_not_called()
        self.assertEqual(self.held.waiting(), [])
        run = next(r for r in self.app.runs.recent(self.me) if r["id"] == self.run_id)
        self.assertIn("its PRs were not opened: you left the project", run["message"])


if __name__ == "__main__":
    unittest.main()
