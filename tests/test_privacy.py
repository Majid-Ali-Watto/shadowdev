"""Atlassian's personal data reporting: accounts are reported once per cycle, closed ones erased,
updated ones fetched again."""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from shadowdev.app import build_app
from shadowdev.clients import atlassian
from shadowdev.clients.rest import HTTPError
from shadowdev.repositories.database import Database
from shadowdev.services import accounts, privacy
from tests.helpers import HOME, add_developer, add_project, join, make_cfg

ME, ME_HOME, SARA = "acc-majid", "acc-majid-home", "acc-sara"


class ReportAccountsTest(unittest.TestCase):
    def test_request_and_answers(self):
        calls = []

        def fake(method, url, headers, body=None, label=""):
            calls.append((method, url, headers, body))
            return {"accounts": [{"accountId": "a", "status": "closed"}]}, {"Cycle-Period": "86400"}

        with mock.patch.object(atlassian, "request", fake):
            actions, cycle = atlassian.report_accounts("tok", [("a", 0), ("b", 1541000000.5)])
        self.assertEqual((actions, cycle), ([{"accountId": "a", "status": "closed"}], 86400))
        method, url, headers, body = calls[0]
        self.assertEqual((method, url, headers["Authorization"]),
                         ("POST", "https://api.atlassian.com/app/report-accounts/", "Bearer tok"))
        self.assertEqual(body, {"accounts": [{"accountId": "a", "updatedAt": "1970-01-01T00:00:00.000Z"},
                                             {"accountId": "b", "updatedAt": "2018-10-31T15:33:20.500Z"}]})
        with mock.patch.object(atlassian, "request", lambda *a, **k: (None, {})):  # 204: nothing to do
            self.assertEqual(atlassian.report_accounts("tok", [("a", 0)]), ([], None))


class PrivacyReporterTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(Path(tempfile.mkdtemp()))
        self.queue = mock.Mock()
        self.app = build_app(self.cfg, mock.Mock(), self.queue)
        self.users = self.app.users
        self.me = add_developer(self.users, ME)
        add_developer(self.users, ME_HOME, sites=(HOME,), user_id=self.me)
        self.sara = add_developer(self.users, SARA)
        self.market = add_project(self.app.projects, self.sara, "marketplace", ["MAR"])
        self.side = add_project(self.app.projects, self.me, "side", ["MAR"], site=HOME)
        join(self.users, self.me, self.market, ME)
        join(self.users, self.me, self.side, ME_HOME)
        self.reports = []
        self.answers = []  # what each report call returns or raises, in turn
        patches = [mock.patch.object(privacy, "report_accounts", self.fake_report),
                   mock.patch.object(privacy, "access_token", lambda oauth, users, jira_id: lambda: f"token-{jira_id}")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_report(self, token, accounts_):
        self.reports.append((token, [a for a, _ in accounts_]))
        answer = self.answers.pop(0) if self.answers else ([], None)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def test_closed_accounts_are_erased_and_updated_ones_fetched_again(self):
        self.answers = [([{"accountId": SARA, "status": "closed"}, {"accountId": ME_HOME, "status": "closed"},
                          {"accountId": ME, "status": "updated"}], None)]
        jira = mock.Mock()
        jira.myself.return_value = {"accountId": ME, "displayName": "Majid Ali", "emailAddress": "new@example.com"}
        with mock.patch.object(accounts, "Jira", return_value=jira):
            self.assertEqual(self.app.privacy.report_due(), privacy.PASS_SECONDS)
        self.assertEqual(sorted(self.reports[0][1]), sorted([ME, ME_HOME, SARA]))
        # Sara's only account was closed: she is gone, and her project passed to its other member.
        self.assertIsNone(self.users.get(self.sara))
        self.queue.stop_runs.assert_any_call(self.sara)
        self.assertEqual(self.app.projects.get(self.market.id).owner_id, self.me)
        # Majid's personal account was closed: it, and what he ran with it, are gone; the rest stays.
        me = self.users.get(self.me)
        self.assertEqual([a.account_id for a in me.jira_accounts], [ME])
        self.assertEqual(set(me.subscriptions), {self.market.id})
        self.queue.stop_runs.assert_any_call(self.me, {self.side.id})
        # His office account changed its name and email at Atlassian: fetched again, and he is renamed.
        self.assertEqual((me.jira_accounts[0].display_name, me.jira_accounts[0].email), ("Majid Ali", "new@example.com"))
        self.assertEqual((me.display_name, me.email), ("Majid Ali", "new@example.com"))

        # Within the cycle nothing is reported again; after it, everything is.
        self.app.privacy.report_due()
        self.assertEqual(len(self.reports), 1)
        self.app.privacy.report_due(now=time.time() + privacy.DEFAULT_CYCLE_SECONDS + 1)
        self.assertEqual(self.reports[1][1], [ME])

    def test_reports_in_batches_of_90_and_honours_the_cycle_period(self):
        for n in range(95):
            add_developer(self.users, f"acc-{n}", github=None, claude=None)
        self.answers = [([], 86400), ([], None)]
        self.app.privacy.report_due()
        self.assertEqual([len(accounts_) for _, accounts_ in self.reports], [90, 8])
        self.assertEqual(self.app.privacy.cycle, 86400)
        self.app.privacy.report_due(now=time.time() + 86401)  # Atlassian's shorter cycle
        self.assertEqual(len(self.reports), 4)

    def test_a_refused_token_is_replaced_by_the_next_accounts(self):
        self.answers = [HTTPError("401", 401), ([], None)]
        self.app.privacy.report_due()
        self.assertEqual([token for token, _ in self.reports], ["token-1", "token-2"])
        self.assertEqual(self.users.accounts_to_report(time.time() - 60, 90), [])  # reported

    def test_rate_limited_waits_and_reports_later(self):
        self.answers = [HTTPError("429", 429, retry_after="120")]
        self.assertEqual(self.app.privacy.report_due(), 120.0)
        self.assertEqual(len(self.users.accounts_to_report(time.time(), 90)), 3)  # not marked reported
        self.app.privacy.report_due()
        self.assertEqual(self.users.accounts_to_report(time.time() - 60, 90), [])

    def test_removing_the_account_a_developer_is_named_after_renames_them(self):
        me = self.users.get(self.me)
        self.assertEqual(me.display_name, ME)
        self.users.remove_jira_account(self.me, me.jira_accounts[0].id)
        me = self.users.get(self.me)
        self.assertEqual((me.display_name, me.email), (ME_HOME, f"{ME_HOME}@x"))

    def test_a_schema_2_database_gets_the_reporting_columns(self):
        path = self.cfg.data_dir / "shadowdev.db"
        with sqlite3.connect(path) as db:
            db.execute("ALTER TABLE jira_accounts DROP COLUMN profile_at")
            db.execute("ALTER TABLE jira_accounts DROP COLUMN reported_at")
            db.execute("ALTER TABLE runs DROP COLUMN note")
            db.execute("PRAGMA user_version = 2")
        Database(path, self.cfg.secret_key)
        with sqlite3.connect(path) as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(jira_accounts)")}
            self.assertTrue({"profile_at", "reported_at"} <= columns)
            self.assertIn("note", {r[1] for r in db.execute("PRAGMA table_info(runs)")})  # added in schema 4
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)


if __name__ == "__main__":
    unittest.main()
