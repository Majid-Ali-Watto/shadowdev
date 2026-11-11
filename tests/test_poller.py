"""Polling Jira for new assignments (Limits.jira_polling)."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from shadowdev.app import build_app
from shadowdev.clients.jira import Jira
from shadowdev.config import Config
from shadowdev.models import limits
from shadowdev.models.limits import Limits
from shadowdev.services import poller
from tests.helpers import add_developer, add_project, join, make_cfg

ME, SARA = "acc-majid", "acc-sara"


def issue(key, type_="Bug", assignee=ME):
    return {"key": key, "fields": {"issuetype": {"name": type_}, "assignee": {"accountId": assignee}}}


class PollerTest(unittest.TestCase):
    def setUp(self):
        self.queued = []
        self.app = build_app(make_cfg(Path(tempfile.mkdtemp())), mock.Mock(),
                             mock.Mock(submit=lambda project, keys, uid, **kw: self.queued.append((project.name, keys, uid))))
        users, projects = self.app.users, self.app.projects
        self.me = add_developer(users, ME)
        self.market = add_project(projects, self.me, "marketplace", ["MAR", "MARS"])
        join(users, self.me, self.market, ME)  # bugs only
        self.sara = add_developer(users, SARA)
        join(users, self.sara, self.market, SARA, mode="all")
        add_developer(users, "acc-new")  # in no project: never polled
        for uid in (self.me, self.sara):
            users.set_limits(uid, Limits(jira_polling=True))
        self.found = {ME: [], SARA: []}
        self.jqls = []

        def client(oauth, users_, jira_account_id, cloud_id):
            account = next(a.account_id for u in users_.all() for a in u.jira_accounts if a.id == jira_account_id)
            jira = mock.Mock()
            jira.search.side_effect = lambda jql, fields, limit: self.jqls.append((account, jql)) or self.found[account]
            return jira

        patch = mock.patch.object(poller, "jira_client", client)
        patch.start()
        self.addCleanup(patch.stop)

    def test_queues_new_assignments_of_the_types_each_developer_automates(self):
        self.found[ME] = [issue("MAR-1"), issue("MAR-2", "Story"), issue("MAR-3", assignee="acc-other"),
                          issue("HR-1"), issue("../MAR-4")]
        self.found[SARA] = [issue("MARS-5", "Story", SARA)]
        now = time.time()
        self.assertEqual(self.app.poller.poll(now), 2)
        self.assertEqual(sorted(self.queued), [("marketplace", ["MAR-1"], self.me), ("marketplace", ["MARS-5"], self.sara)])
        # Asked as each developer's own Jira account, for its unresolved issues in the project.
        self.assertEqual(sorted(a for a, _ in self.jqls), [ME, SARA])
        self.assertEqual(self.jqls[0][1], 'project in (MAR, MARS) AND assignee = currentUser() AND statusCategory != Done '
                                          'AND updated >= "-15m" ORDER BY updated ASC')
        # The next poll looks back to the last one, with a little overlap.
        self.jqls.clear()
        self.app.poller.poll(now + 30 * 60)
        self.assertIn('updated >= "-32m"', self.jqls[0][1])

    def test_paused_developers_and_failing_projects(self):
        self.app.users.set_paused(self.me, True)
        self.found[SARA] = [issue("MAR-9", assignee=SARA)]
        self.assertEqual(self.app.poller.poll(), 1)
        self.assertEqual([a for a, _ in self.jqls], [SARA])
        # One developer's Jira failing doesn't stop the others.
        self.app.users.set_paused(self.me, False)
        self.found[ME] = None  # makes iterating fail
        self.queued.clear()
        with self.assertLogs("shadowdev", "WARNING"):
            self.assertEqual(self.app.poller.poll(time.time() + 16 * 60), 1)

    def test_each_developer_polls_at_their_own_interval_or_not_at_all(self):
        self.app.users.set_limits(self.me, Limits(jira_polling=True, jira_poll_minutes=5))
        self.app.users.set_limits(self.sara, Limits())  # polling off: the default
        now = time.time()
        self.app.poller.poll(now)
        self.assertEqual(self.jqls, [(ME, self.jqls[0][1])])
        self.assertIn('updated >= "-5m"', self.jqls[0][1])  # the first poll looks back one interval
        self.jqls.clear()
        self.app.poller.poll(now + 4 * 60)  # not due yet
        self.assertEqual(self.jqls, [])
        self.app.poller.poll(now + 5 * 60)
        self.assertEqual([a for a, _ in self.jqls], [ME])


class SearchTest(unittest.TestCase):
    def test_pages_by_token_and_stops_on_a_repeated_one(self):
        pages = iter([{"issues": [{"key": "A-1"}], "nextPageToken": "t1", "isLast": False},
                      {"issues": [{"key": "A-2"}], "nextPageToken": "t1", "isLast": False},  # the same token again
                      {"issues": [{"key": "A-3"}], "isLast": True}])
        calls = []
        jira = Jira("https://api.example/ex/jira/c", lambda: "tok")
        with mock.patch.object(jira, "_request", lambda method, path: calls.append(path) or next(pages)):
            self.assertEqual([i["key"] for i in jira.search("project = A", ["issuetype"])], ["A-1", "A-2"])
        query = parse_qs(urlsplit(calls[0]).query)
        self.assertEqual((urlsplit(calls[0]).path, query["jql"], query["fields"]),
                         ("/rest/api/3/search/jql", ["project = A"], ["issuetype"]))
        self.assertEqual(parse_qs(urlsplit(calls[1]).query)["nextPageToken"], ["t1"])


class LimitsTest(unittest.TestCase):
    def test_defaults_stored_values_and_the_form(self):
        self.assertEqual(Limits.from_json(None), Limits())
        self.assertEqual(Limits.from_json(Limits(github_poll_minutes=0.25, claude_timeout_minutes=45).to_json()),
                         Limits(github_poll_minutes=0.25, claude_timeout_minutes=45))
        self.assertEqual(Limits.from_json('{"github_poll_minutes": true, "max_fix_attempts": 2.5}'), Limits())
        self.assertEqual(Limits.from_json('{"jira_poll_minutes": 0, "max_parallel_runs": 7, "jira_polling": 1}'),
                         Limits(max_parallel_runs=7))  # out of range or the wrong type: the default
        form = {"jira_polling": "on", "jira_poll_minutes": "5", "github_poll_minutes": "0.5", "slack_poll_minutes": "1.5",
                "review_quiet_minutes": "0",
                "claude_timeout_minutes": "60", "command_timeout_minutes": "10", "max_parallel_runs": "2",
                "max_fix_attempts": "4"}
        self.assertEqual(limits.from_form(form), Limits(
            jira_polling=True, jira_poll_minutes=5, github_poll_minutes=0.5, slack_poll_minutes=1.5, review_quiet_minutes=0,
            claude_timeout_minutes=60, command_timeout_minutes=10, max_parallel_runs=2, max_fix_attempts=4))
        self.assertFalse(limits.from_form({**form, "jira_polling": ""}).jira_polling)
        for name, value in (("jira_poll_minutes", "0.5"), ("slack_poll_minutes", "0.2"), ("github_poll_minutes", "0.333"),
                            ("review_quiet_minutes", "1e1"), ("claude_timeout_minutes", "nan"), ("claude_timeout_minutes", "x"),
                            ("max_parallel_runs", "21"), ("max_fix_attempts", "-1"), ("command_timeout_minutes", "")):
            with self.assertRaises(ValueError):
                limits.from_form({**form, name: value})

    def test_a_run_gets_its_developers_limits(self):
        cfg = make_cfg(Path(tempfile.mkdtemp())).for_run(Limits(claude_timeout_minutes=60, command_timeout_minutes=10.5,
                                                                max_fix_attempts=4))
        self.assertEqual((cfg.claude_timeout_seconds, cfg.command_timeout_seconds, cfg.max_fix_attempts), (3600, 630, 4))

    def test_env_keys_moved_to_settings_are_ignored_with_a_warning(self):
        env = {"JIRA_SITES": "https://a.atlassian.net", "ATLASSIAN_CLIENT_ID": "a", "ATLASSIAN_CLIENT_SECRET": "b",
               "SHADOWDEV_SECRET_KEY": "d", "MAX_PARALLEL_RUNS": "9", "JIRA_POLLING": "true"}
        with self.assertLogs("shadowdev", "WARNING") as logs:
            cfg = Config.from_env(env)
        self.assertIn("JIRA_POLLING, MAX_PARALLEL_RUNS", logs.output[0])
        self.assertEqual(cfg.max_fix_attempts, 3)


if __name__ == "__main__":
    unittest.main()
