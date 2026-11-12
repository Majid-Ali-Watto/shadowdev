"""Moving complete tickets by a project's status rules (Settings.release_moves)."""
import dataclasses
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from shadowdev.app import build_app
from shadowdev.clients import slack
from shadowdev.clients.rest import HTTPError
from shadowdev.models import release
from shadowdev.models.release import DEFAULT_RULES, ReleaseReport, StatusRule
from shadowdev.models.settings import Settings
from shadowdev.services import releases
from shadowdev.services.errors import UserError
from tests.helpers import add_developer, add_project, join, make_cfg

ME, SARA = "acc-majid", "acc-sara"
ROOT_CAUSE = "customfield_10201"
COMPLETE = {"fixVersions": [{"name": "1.4"}], "parent": {"key": "MAR-100"}, "timeoriginalestimate": 3600,
            "timespent": 1800}


def ticket(key, type_="Bug", status="Ready for Release", assignee=ME, **fields):
    return {"key": key, "fields": {"issuetype": {"name": type_}, "status": {"name": status},
                                   "assignee": {"accountId": assignee}, **COMPLETE, **fields}}


class RulesTest(unittest.TestCase):
    def test_typed_rules(self):
        self.assertEqual(release.parse_rules(""), ())
        self.assertEqual(release.parse_rules(release.rules_text(DEFAULT_RULES)), DEFAULT_RULES)
        self.assertEqual(release.parse_rules("Merged  →  Ready for QA\n\n In QA -> Ready for Release: Parent ticket, time tracking"),
                         (StatusRule("Merged", "Ready for QA"),
                          StatusRule("In QA", "Ready for Release", ("parent", "time_spent"))))
        for bad, message in (("Done", "From status -> To status"), ("A -> B: luck", "not a check"),
                             ('A" OR x = "y -> B', "not a Jira status"), ("A -> B\na -> C", "one rule per status")):
            with self.assertRaisesRegex(ValueError, message):
                release.parse_rules(bad)
        self.assertEqual(release.rules_from_json(None), DEFAULT_RULES)  # never set: the defaults
        self.assertEqual(release.rules_from_json(release.rules_to_json(())), ())

    def test_checks_root_cause_only_on_bugs(self):
        rule = DEFAULT_RULES[0]
        story = ticket("MAR-1", "Story")["fields"]
        self.assertEqual(release.missing(rule, story, ROOT_CAUSE), [])
        bug = ticket("MAR-2")["fields"]
        self.assertEqual(release.missing(rule, bug, ROOT_CAUSE), ["root cause"])
        # Text, an Atlassian document or an option: filled unless blank.
        for value, ok in (("  ", False), ("The cache", True), ({"type": "doc", "content": []}, False),
                          ({"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "x"}]}]}, True),
                          ({"value": "Config"}, True)):
            self.assertEqual(release.missing(rule, {**bug, ROOT_CAUSE: value}, ROOT_CAUSE) == [], ok, value)
        # Without a root cause field: a comment giving it.
        self.assertEqual(release.missing(rule, bug, None), ["root cause"])
        self.assertEqual(release.missing(rule, {**bug, "comment": {"comments": [{"body": "*Root cause:* x"}]}}, None), [])
        empty = {"issuetype": {"name": "Task"}, "fixVersions": [], "parent": None, "timeoriginalestimate": None, "timespent": 0}
        self.assertEqual(release.missing(rule, empty, ROOT_CAUSE),
                         ["fix version", "parent", "original estimate", "logged time"])
        self.assertEqual(release.missing(StatusRule("A", "B"), empty, ROOT_CAUSE), [])


class SlackLinkTest(unittest.TestCase):
    def test_issue_keys_and_urls_become_links(self):
        site = "https://acme.atlassian.net/"
        self.assertEqual(slack.issue_link(site, "MAR-1"), "<https://acme.atlassian.net/browse/MAR-1|MAR-1>")
        # Whole keys only, each once, also next to punctuation; other text untouched.
        self.assertEqual(slack.link_issues("MAR-1, MAR-12: MAR-1 fails (MAR-1)", site, ["MAR-1", "MAR-12"]),
                         "<https://acme.atlassian.net/browse/MAR-1|MAR-1>, <https://acme.atlassian.net/browse/MAR-12|MAR-12>: "
                         "<https://acme.atlassian.net/browse/MAR-1|MAR-1> fails (<https://acme.atlassian.net/browse/MAR-1|MAR-1>)")
        self.assertEqual(slack.link("https://github.com/o/r/pull/1", "PR 1"), "<https://github.com/o/r/pull/1|PR 1>")
        self.assertEqual(slack.link("https://github.com/o/r/pull/1"), "<https://github.com/o/r/pull/1>")
        # Anything that would break Slack's link syntax stays plain text.
        self.assertEqual(slack.link("javascript:alert(1)", "PR 1"), "PR 1")
        self.assertEqual(slack.link("https://x/a|b>", "<PR>"), "&lt;PR&gt;")


class ReleaseMoverTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app(make_cfg(Path(tempfile.mkdtemp())), mock.Mock(), mock.Mock())
        users, projects = self.app.users, self.app.projects
        self.me = add_developer(users, ME)
        market = add_project(projects, self.me, "marketplace", ["MAR"])
        self.market = projects.get(projects.save(dataclasses.replace(market, root_cause_field=ROOT_CAUSE)))
        join(users, self.me, self.market, ME, settings=Settings(release_moves=True))
        self.sara = add_developer(users, SARA)
        join(users, self.sara, self.market, SARA)  # the default: off
        self.found = []
        self.jqls = []
        self.jira = mock.Mock()
        self.jira.search.side_effect = lambda jql, fields, limit: self.jqls.append(jql) or self.found
        self.statuses = {}
        self.jira.status.side_effect = lambda key: self.statuses.get(key, "Ready for Release")
        self.jira.transitions.return_value = [{"id": "31", "to": {"name": "Done"}}, {"id": "11", "to": {"name": "In QA"}}]
        patch = mock.patch.object(releases, "jira_client", return_value=self.jira)
        patch.start()
        self.addCleanup(patch.stop)

    def test_off_by_default(self):
        self.assertEqual(Settings().release_moves, False)
        self.assertEqual(self.app.projects.get(self.market.id).status_rules, DEFAULT_RULES)  # the project's defaults
        self.app.users.save_release_report(self.me, self.market.id, ReleaseReport(0))
        join(self.app.users, self.me, self.market, ME)  # switched off
        self.found = [ticket("MAR-1", **{ROOT_CAUSE: "x"})]
        self.assertEqual(self.app.releases.poll(), 0)
        self.jira.search.assert_not_called()

    def test_moves_complete_tickets_and_reports_what_the_others_miss(self):
        self.found = [ticket("MAR-1", **{ROOT_CAUSE: "The cache"}), ticket("MAR-2", "Story"),
                      ticket("MAR-3"), ticket("MAR-4", "Task", fixVersions=[], timespent=None),
                      ticket("MAR-5", assignee="acc-other", **{ROOT_CAUSE: "x"}), ticket("HR-1", **{ROOT_CAUSE: "x"}),
                      ticket("MAR-6", "Story")]
        self.statuses["MAR-6"] = "Done"  # moved by someone since the search
        self.assertEqual(self.app.releases.poll(), 2)
        self.assertEqual(self.jqls, ['project in (MAR) AND assignee = currentUser() AND status = "Ready for Release" '
                                     'ORDER BY key ASC'])  # only for the developer who switched it on
        self.assertEqual([c.args for c in self.jira.transition.call_args_list], [("MAR-1", "31"), ("MAR-2", "31")])
        report = self.app.users.get(self.me).subscriptions[self.market.id].release_report
        self.assertEqual(report.moved, ("MAR-1 to Done", "MAR-2 to Done"))
        self.assertEqual(report.held, ("MAR-3 (Ready for Release): no root cause",
                                       "MAR-4 (Ready for Release): no fix version, logged time"))
        self.assertEqual(report.failed, ("MAR-6: now in Done, left alone",))

    def test_incomplete_tickets_stay_and_the_developer_is_told_in_slack(self):
        slack = mock.Mock()
        patch = mock.patch.object(releases, "Slack", return_value=slack)
        patch.start()
        self.addCleanup(patch.stop)
        self.found = [ticket("MAR-3"), ticket("MAR-4", "Task", fixVersions=[]), ticket("MAR-1", **{ROOT_CAUSE: "x"})]
        now = time.time()
        self.app.users.save_slack(self.me, "xoxp-1", "U0000MEME0", "acme")
        self.app.releases.poll(now)
        slack.post.assert_called_once()
        channel, text = slack.post.call_args.args
        self.assertEqual(channel, "U0000MEME0")  # a direct message to the developer
        self.assertIn("<https://office.atlassian.net/browse/MAR-3|MAR-3> stays in Ready for Release, missing root cause "
                      "(needed for Done)", text)
        self.assertIn("MAR-4|MAR-4> stays in Ready for Release, missing fix version", text)
        self.assertNotIn("MAR-1", text)
        self.assertEqual([c.args[0] for c in self.jira.transition.call_args_list], ["MAR-1"])  # never MAR-3, MAR-4
        # The same gaps again: not repeated every few hours; something new missing, or Check now: told.
        slack.post.reset_mock()
        self.app.releases.poll(now + 16 * 3600)
        slack.post.assert_not_called()
        self.found.append(ticket("MAR-5", "Story", parent=None))
        self.app.releases.poll(now + 24 * 3600)
        self.assertIn("MAR-5|MAR-5> stays in Ready for Release, missing parent", slack.post.call_args.args[1])
        slack.post.reset_mock()
        self.app.releases.check_now(self.app.users.get(self.me), str(self.market.id))
        slack.post.assert_called_once()
        # Slack failing is reported, and changes nothing else.
        slack.post.side_effect = HTTPError("Slack chat.postMessage: token_revoked", 400)
        report = self.app.releases.check_now(self.app.users.get(self.me), str(self.market.id))
        self.assertIn("Could not send you the missing fields in Slack", report.failed[0])
        self.assertEqual(len(report.held), 3)
        # Without Slack connected: only the report.
        slack.post.reset_mock()
        self.found = [ticket("MAR-6", assignee=SARA)]
        self.assertEqual(len(self.app.releases.check_now(self.app.users.get(self.sara), str(self.market.id)).held), 1)
        slack.post.assert_not_called()

    def test_every_few_hours_as_chosen(self):
        now = time.time()
        self.app.releases.poll(now)
        self.app.releases.poll(now + 7 * 3600)  # the default: every 8 hours
        self.assertEqual(len(self.jqls), 1)
        self.app.releases.poll(now + 8 * 3600)
        self.assertEqual(len(self.jqls), 2)
        join(self.app.users, self.me, self.market, ME, settings=Settings(release_moves=True, release_hours="4"))
        self.app.releases.poll(now + 12 * 3600)
        self.assertEqual(len(self.jqls), 3)
        self.app.users.set_paused(self.me, True)
        self.app.releases.poll(now + 48 * 3600)
        self.assertEqual(len(self.jqls), 3)

    def test_check_now_and_the_project_rules(self):
        # By hand also while switched off, with the project's own rules.
        self.app.projects.save(dataclasses.replace(self.market, status_rules=(
            StatusRule("Merged", "In QA"), StatusRule("Ready for Release", "Released", ("parent",)))))
        self.found = [ticket("MAR-7", "Story", parent=None)]
        report = self.app.releases.check_now(self.app.users.get(self.sara), str(self.market.id))
        self.assertEqual(self.jqls[0], 'project in (MAR) AND assignee = currentUser() AND status = "Merged" ORDER BY key ASC')
        self.assertEqual(report.moved, ())  # SARA's tickets only: MAR-7 is assigned to someone else
        self.found = [ticket("MAR-8", "Story", assignee=SARA, parent=None)]
        self.statuses.update({"MAR-8": "Merged", "MAR-9": "Merged"})
        report = self.app.releases.check_now(self.app.users.get(self.sara), str(self.market.id))
        self.assertEqual(report.moved, ("MAR-8 to In QA",))
        self.assertEqual(report.held, ("MAR-8 (Ready for Release): no parent",))
        # A missing transition, or one Jira refuses, is reported; the rest still move.
        self.jira.transitions.return_value = []
        self.found = [ticket("MAR-9", "Story", assignee=SARA)]
        self.assertIn("MAR-9: the workflow has no transition from Merged to In QA",
                      self.app.releases.check_now(self.app.users.get(self.sara), str(self.market.id)).failed)
        self.jira.search.side_effect = HTTPError("Jira -> 400", 400)
        self.assertIn("is it a status of the project?",
                      self.app.releases.check_now(self.app.users.get(self.sara), str(self.market.id)).failed[0])
        stranger = add_developer(self.app.users, "acc-new")
        with self.assertRaisesRegex(UserError, "Join the project"):
            self.app.releases.check_now(self.app.users.get(stranger), str(self.market.id))


if __name__ == "__main__":
    unittest.main()
