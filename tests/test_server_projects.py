"""Projects: adding, editing, joining and leaving them; their Jira options, working hours and notifications."""
import dataclasses
import json
import re
import unittest
from unittest import mock

from shadowdev.automation import pipeline
from shadowdev.clients import mail
from shadowdev.clients.rest import HTTPError
from shadowdev.models.settings import Settings
from shadowdev.routes import LIMITS
from shadowdev.services import accounts, notifications, releases
from shadowdev.services import projects as project_service
from shadowdev.services.errors import UserError
from tests.helpers import HOME, OFFICE, add_developer, add_project, join
from tests.server_base import ME, ME_HOME, Env, WebTest


class EmailAddressTest(Env):
    def test_the_address_typed_first_then_the_projects_accounts_then_the_sign_in(self):
        me = self.developer()  # Jira acc-majid@x, GitHub acc-majid@x
        user = self.users.get(me)
        sub = user.subscriptions[self.market.id]
        jira = dataclasses.replace(user.jira_accounts[0], email="")
        github = dataclasses.replace(user.github_accounts[0], git_email="9+me@users.noreply.github.com")
        cases = (
            (user, Settings(notify_address="me@mine.example"), ("me@mine.example", "you chose")),
            (user, Settings(), (f"{ME}@x", "of your Jira account")),
            (dataclasses.replace(user, jira_accounts=(jira,)), Settings(), (f"{ME}@x", "of your GitHub account")),
            (dataclasses.replace(user, jira_accounts=(jira,), github_accounts=(github,), email="me@signin.example"),
             Settings(), ("me@signin.example", "you signed in with")),  # never GitHub's noreply address
            (dataclasses.replace(user, jira_accounts=(jira,), github_accounts=(github,), email=""), Settings(), ("", "")),
        )
        for who, chosen, expected in cases:
            with self.subTest(expected=expected):
                mine = dataclasses.replace(sub, settings=chosen)
                who = dataclasses.replace(who, subscriptions={self.market.id: mine})
                self.assertEqual(who.email_for(mine), expected)
                self.assertEqual(who.actor(self.market.id).email, expected[0])


class NotificationTestsTest(Env):
    def setUp(self):
        super().setUp()
        self.me_id = self.developer()  # marketplace: MAR, MARS
        self.users.save_slack(self.me_id, "xoxp-1", "U0000MEME0", "acme")
        self.slack = mock.Mock()
        for patch in (mock.patch.object(notifications, "Slack", return_value=self.slack),
                      mock.patch.object(pipeline, "send_mail")):
            patch.start()
            self.addCleanup(patch.stop)
        self.tests = notifications.NotificationTests(
            dataclasses.replace(self.cfg, smtp=mail.Smtp("mail.x", 587, "dev@x")), self.users, self.projects)

    def send(self, way, at):
        self.tests.send(self.users.get(self.me_id), str(self.market.id), way, now=at)

    def test_three_different_slack_messages_with_the_projects_keys_then_no_more(self):
        for at in (1000, 1030, 1060):
            self.send("slack", at)
        texts = [c.args[1] for c in self.slack.post.call_args_list]
        self.assertEqual([c.args[0] for c in self.slack.post.call_args_list], ["U0000MEME0"] * 3)  # direct messages
        self.assertEqual(len(set(texts)), 3)  # each a different sample
        self.assertTrue(texts[0].startswith("Test 1 of 3: sample tickets, nothing to do.\nYour decision is needed"))
        self.assertIn("<https://office.atlassian.net/browse/MAR-101|MAR-101>", texts[0])
        self.assertIn("MAR-103|MAR-103> stays in Ready for Release, missing fix version, logged time", texts[1])
        self.assertIn("MAR-105|MAR-105>: Keep the old endpoint", texts[2])
        for text in texts:
            self.assertIsNone(re.search(r"shadow\s*dev|claude|\bAI\b|\bbot\b|automat|assistant", text, re.I))
        with self.assertRaisesRegex(UserError, "sent the 3 test Slack messages of marketplace already"):
            self.send("slack", 2000)
        self.assertEqual(self.slack.post.call_count, 3)

    def test_test_messages_wait_between_and_need_what_they_go_through(self):
        self.send("slack", 1000)
        with self.assertRaisesRegex(UserError, "Wait 21 seconds"):
            self.send("email", 1010)  # one wait for both ways
        self.users.remove_slack(self.me_id)
        with self.assertRaisesRegex(UserError, "Connect Slack on the Accounts tab first"):
            self.send("slack", 2000)
        self.tests.cfg = self.cfg  # no SMTP
        with self.assertRaisesRegex(UserError, "needs SMTP_HOST"):
            self.send("email", 2000)
        with self.assertRaisesRegex(UserError, "Join the project first"):
            self.send("pigeon", 2000)
        self.slack.post.side_effect = HTTPError("Slack chat.postMessage: token_revoked", 400)
        self.users.save_slack(self.me_id, "xoxp-1", "U0000MEME0", "acme")
        with self.assertRaisesRegex(UserError, "Slack refused the message"):
            self.send("slack", 3000)
        self.assertEqual(self.users.get(self.me_id).subscriptions[self.market.id].tests_left("slack"), 2)  # not counted

    def test_three_emails_per_address_and_only_a_hash_of_it_is_kept(self):
        for at in (1000, 1030, 1060):
            self.send("email", at)
        subjects = [c.args[2] for c in pipeline.send_mail.call_args_list]
        self.assertEqual(subjects, ["Test 1 of 3: MAR-101: your decision is needed",
                                    "Test 2 of 3: marketplace: tickets not complete yet",
                                    "Test 3 of 3: MAR-104, MAR-105: your decision is needed"])
        self.assertEqual({c.args[1] for c in pipeline.send_mail.call_args_list}, {f"{ME}@x"})
        self.assertIn("Test 1 of 3: sample tickets, nothing to do.</p>", pipeline.send_mail.call_args_list[0].args[4])
        with self.assertRaisesRegex(UserError, "sent the 3 test emails"):
            self.send("email", 2000)
        sub = self.users.get(self.me_id).subscriptions[self.market.id]
        self.assertNotIn("@", json.dumps(sub.tests_sent))  # the address itself is not stored
        join(self.users, self.me_id, self.market, ME, settings=Settings(notify_address="me@home.example"))
        self.send("email", 3000)  # another address: three more
        self.assertEqual(pipeline.send_mail.call_args.args[1:3], ("me@home.example", "Test 1 of 3: MAR-101: your decision is needed"))


class SlackMembersTest(unittest.TestCase):
    def test_reviewers_as_pasted_from_slack_and_what_is_wrong_is_named(self):
        from shadowdev.services.projects import slack_members
        self.assertEqual(slack_members("U012ABC3DE, <@U045FGH6IJ>; @w0789klm0n <@U0111AAAA|majid>", "fe: reviewers"),
                         ("U012ABC3DE", "U045FGH6IJ", "W0789KLM0N", "U0111AAAA"))
        self.assertEqual(slack_members("", "fe: reviewers"), ())
        with self.assertRaisesRegex(UserError, r"fe: reviewers: '@majid\.ali' is not a member ID\. Give up to 5"):
            slack_members("U012ABC3DE @majid.ali", "fe: reviewers")
        with self.assertRaisesRegex(UserError, "6 people; at most 5"):
            slack_members(" ".join(f"U00000000{n}" for n in range(6)), "fe: reviewers")


class ProjectsWebTest(WebTest):
    """Projects: adding, editing, joining and leaving them; their Jira options, working hours and notifications."""

    def test_personal_and_office_accounts_and_projects(self):
        self.sign_in()
        csrf = self.csrf()
        self.assertIn("Not active yet", self.request("GET", "/")[2])

        # Connect the personal Jira account: a CSRF-checked POST, then Atlassian, then back as the same developer.
        # The POST answers with a page opening Atlassian: a redirect would break the page's form-action CSP.
        location = self.connect()
        self.assertTrue(self.cookies["shadowdev_oauth_state"].startswith("connect."))
        self.me_jira = {"accountId": ME_HOME, "displayName": "Majid", "accountType": "atlassian"}
        self.oauth.sites.return_value = [HOME]
        self.assertEqual(self.oauth_round_trip(location)[1], self.cfg.public_url + "/?ok=jira")
        me = self.me()
        self.assertEqual(([a.account_id for a in me.jira_accounts], set(me.sites)), ([ME, ME_HOME], {OFFICE.cloud_id, HOME.cloud_id}))
        self.assertEqual(self.users.owner_of(ME_HOME), me.id)

        # Forms without the session's CSRF token, or from another origin, are refused.
        self.request("POST", "/github", {"csrf": "forged", "github_token": "gh"})
        self.assertEqual(self.me().github_accounts, ())
        self.assertEqual(self.request("POST", "/github", {"csrf": csrf, "github_token": "gh"},
                                      {"Origin": "https://evil.example"})[0], 403)

        # Office and personal GitHub and Claude accounts.
        self.github.user.return_value = {"login": "majid-office", "id": 8, "name": "Majid Ali", "email": "majid@example.com"}
        self.assertEqual(self.request("POST", "/github", {"csrf": csrf, "github_token": "gh-office", "github_label": "office"})[1],
                         "/?ok=github")
        self.github.user.return_value = {"login": "majid", "id": 7, "name": "Majid", "email": None}
        self.request("POST", "/github", {"csrf": csrf, "github_token": "gh-home", "github_label": "personal"})
        error_page = self.request("POST", "/claude", {"csrf": csrf, "claude_token": "abc"})[2]
        self.assertIn("not a Claude token", error_page)
        # The page opens on the tab of the form that failed, and after a form on the tab of its notice.
        selected = lambda page: re.search(r'data-tab="(\w+)" aria-selected="true"', page).group(1)
        self.assertEqual(selected(error_page), "accounts")
        self.assertEqual(selected(self.request("GET", "/?ok=queued")[2]), "runs")
        self.assertEqual(selected(self.request("GET", "/?ok=runs-deleted")[2]), "recent")
        self.assertEqual(selected(self.request("GET", "/?tab=recent")[2]), "recent")
        self.assertEqual(selected(self.request("GET", "/?tab=settings")[2]), "settings")
        self.assertEqual(selected(self.request("GET", "/?tab=bogus")[2]), "accounts")  # no Claude account yet: the next step
        # A token copied from a wrapped terminal line, with a space and a line break in it.
        self.assertEqual(self.request("POST", "/claude", {"csrf": csrf, "claude_token": " sk-ant-oat01-ab cd\r\nef ",
                                                          "claude_label": "office"})[1], "/?ok=claude")
        self.assertEqual(accounts.check_token.call_args.args[0], "sk-ant-oat01-abcdef")
        self.request("POST", "/claude", {"csrf": csrf, "claude_token": "sk-ant-api03-home", "claude_label": "personal"})
        me = self.me()
        gh = {g.label: g for g in me.github_accounts}
        cl = {c.label: c for c in me.claude_accounts}
        self.assertEqual((gh["office"].login, gh["office"].git_email, gh["personal"].git_email),
                         ("majid-office", "majid@example.com", "7+majid@users.noreply.github.com"))
        self.assertEqual((cl["office"].token, cl["personal"].token), ("sk-ant-oat01-abcdef", "sk-ant-api03-home"))
        self.assertNotEqual(cl["office"].config_key, cl["personal"].config_key)

        # Slack: the developer's own user token, checked with Slack.
        self.assertIn("starts with xoxp-", self.request("POST", "/slack", {"csrf": csrf, "slack_token": "xoxb-bot"})[2])
        with mock.patch.object(accounts, "Slack") as slack:
            slack.return_value.whoami.return_value = {"user_id": "U0000MEME0", "team": "acme"}
            self.assertEqual(self.request("POST", "/slack", {"csrf": csrf, "slack_token": "xoxp-secret"})[1], "/?ok=slack")
        self.assertEqual((self.me().slack.token, self.me().slack.user_id), ("xoxp-secret", "U0000MEME0"))
        page = self.request("GET", "/")[2]
        self.assertIn("member U0000MEME0", page)
        self.assertNotIn("xoxp-secret", page)

        # Add the office project from the page: repo URLs pasted, names and descriptions filled in.
        self.assertIn("Add a project", self.request("GET", "/project")[2])
        form = {"csrf": csrf, "name": "marketplace", "cloud_id": OFFICE.cloud_id, "jira_keys": "mar, mars",
                "github_account_id": gh["office"].id,
                "repo_github": ["https://github.com/acme/market-place-fe.git", "acme/services", ""],
                "repo_name": ["fe", "", ""], "repo_description": ["", "Python services", ""],
                "repo_release_prefix": ["", "", ""], "repo_setup": ["npm ci", "", ""], "repo_test": ["npm test", "", ""],
                "slack_channel": "C0REVIEWS1", "repo_reviewers": ["<@U0000AAA1>, U0000BBB2", "", ""]}
        self.assertIn("Slack channel: the channel", self.request("POST", "/project/save", {**form, "slack_channel": "#reviews"})[2])
        self.assertIn("reviewers: &#x27;@sara&#x27; is not a member ID",
                      self.request("POST", "/project/save", {**form, "repo_reviewers": ["@sara", "", ""]})[2])
        self.assertEqual(self.request("POST", "/project/save", form)[1], "/?ok=project")
        (market,) = self.app.project_service.visible(self.me())
        self.assertEqual((market.slack_channel, [r.reviewers for r in market.repos]), ("C0REVIEWS1", [("U0000AAA1", "U0000BBB2"), ()]))
        editor = self.request("GET", f"/project?id={market.id}")[2]
        self.assertIn('value="C0REVIEWS1"', editor)
        self.assertIn('value="U0000AAA1, U0000BBB2"', editor)
        self.assertEqual((market.name, market.jira_projects, market.owner_id, market.cloud_id),
                         ("marketplace", {"MAR", "MARS"}, me.id, OFFICE.cloud_id))
        self.assertEqual([(r.name, r.github, r.description, r.setup, r.test) for r in market.repos],
                         [("fe", "acme/market-place-fe", "acme/market-place-fe on GitHub", "npm ci", "npm test"),
                          ("services", "acme/services", "Python services", None, None)])
        self.assertEqual(self.github.can_push.call_count, 2)
        # The owner joined it, with the GitHub account the repos were checked with.
        self.assertEqual(self.me().subscriptions[market.id].github_account_id, gh["office"].id)
        # The personal project uses MAR too, on the personal site; a second office one can't.
        self.assertEqual(self.request("POST", "/project/save", {**form, "name": "side", "cloud_id": HOME.cloud_id,
                                                                "jira_keys": "MAR", "github_account_id": gh["personal"].id})[1],
                         "/?ok=project")
        page = self.request("POST", "/project/save", {**form, "name": "copy", "jira_keys": "MAR"})[2]
        self.assertIn("Jira project MAR is already handled by the project marketplace on office", page)
        self.assertIn('value="copy"', page)  # the form keeps what was typed
        side = next(p for p in self.app.project_service.visible(self.me()) if p.name == "side")

        # Join each with its own accounts; base branches must exist.
        join_form = lambda project, g, c, **kw: {"csrf": csrf, "project_id": project.id, "github_account_id": g.id,
                                                 "claude_account_id": c.id, "mode": "all", **kw}
        self.assertIn("does not exist", self.request("POST", "/project/join", join_form(
            market, gh["office"], cl["office"], **{"base:fe": "no-such-branch"}))[2])
        self.assertEqual(self.request("POST", "/project/join", join_form(
            market, gh["office"], cl["office"], **{"base:fe": "develop"}))[1], "/?ok=joined")
        self.request("POST", "/project/join", {**join_form(side, gh["personal"], cl["personal"]), "mode": "bugs"})
        me = self.me()
        office, personal = me.actor(market.id), me.actor(side.id)
        self.assertEqual((office.jira_account_id, office.github.login, office.claude.label, office.mode, office.base_branches),
                         (ME, "majid-office", "office", "all", {"fe": "develop"}))
        self.assertEqual((personal.jira_account_id, personal.github.login, personal.claude.label, personal.mode),
                         (ME_HOME, "majid", "personal", "bugs"))
        page = self.request("GET", "/")[2]
        self.assertIn("Active in 2 projects", page)
        # What is automated is chosen per project: an unticked switch is off; saving without the
        # settings fields keeps them.
        self.assertIn('<input type="checkbox" name="set:fix_ci" checked>', page)
        self.assertIn('<option value="5" selected>5 at a time</option>', page)  # each project's runs at the same time
        chosen = {"settings": "1", "set:auto_start": "on", "set:model": "sonnet", "set:publish": "draft",
                  "set:parallel_runs": "2", "set:priority": "high"}
        self.assertEqual(self.request("POST", "/project/join", {**join_form(side, gh["personal"], cl["personal"]),
                                                                "mode": "bugs", **chosen})[1], "/?ok=joined")
        for form in (chosen, {}):
            self.request("POST", "/project/join", {**join_form(side, gh["personal"], cl["personal"]), "mode": "bugs", **form})
            mine = self.me().actor(side.id).settings
            self.assertEqual((mine.auto_start, mine.model, mine.publish, mine.fix_ci, mine.jira_comments, mine.max_runs,
                              mine.priority), (True, "sonnet", "draft", False, False, 2, "high"))
        self.assertEqual(self.me().actor(market.id).settings, Settings())  # the other project keeps its own
        self.assertIn("is not a choice", self.request("POST", "/project/join", {
            **join_form(side, gh["personal"], cl["personal"]), **chosen, "set:publish": "force-push"})[2])
        self.assertIn("is not a choice", self.request("POST", "/project/join", {
            **join_form(side, gh["personal"], cl["personal"]), **chosen, "set:parallel_runs": "50"})[2])
        # Each kind of message is told its own way, and the page says what is not set up for it.
        page = self.request("GET", "/")[2]
        for kind in ("notify_decisions", "notify_missing"):
            self.assertIn(f'name="set:{kind}"', page)
        self.assertIn('<option value="both">Slack and email</option>', page)
        self.assertIn("Everything is on this page too. This server sends no email: its .env needs SMTP_HOST.", page)
        # Emails about a project go to the address typed for it, else its accounts'.
        self.assertIn("Now: majid@example.com, the address of your Jira account.", self.request("GET", "/")[2])
        email_me = lambda address: self.request("POST", "/project/join", {
            **join_form(side, gh["personal"], cl["personal"]), **chosen, "set:notify_decisions": "email",
            "set:notify_address": address})
        self.assertIn("is not an email address", email_me("me@home.example, x@evil.example")[2])
        self.assertEqual(email_me("me@home.example")[1], "/?ok=joined")
        self.assertEqual(self.me().actor(side.id).email, "me@home.example")
        self.assertIn("Now: me@home.example, the address you chose.", self.request("GET", "/")[2])
        # The branches approved without a review must exist on GitHub: a typo would never match.
        unreviewed = lambda source, into: self.request("POST", "/project/join", {
            **join_form(side, gh["personal"], cl["personal"]), **chosen, "set:review_approve": "on",
            "set:approve_from": source, "set:approve_into": into})
        with mock.patch.object(project_service.gitops, "remote_branches", return_value=["main", "release/14", "release/15"]):
            page = unreviewed("release/*, hotfix/*", "mian")[2]
            self.assertIn("Approve without review: no branch of ", page)
            self.assertIn(" matches hotfix/*, mian.", page)
            self.assertEqual(self.me().actor(side.id).settings.approve_from, "")  # nothing saved
            self.assertEqual(unreviewed("release/*", "main")[1], "/?ok=joined")
        mine = self.me().actor(side.id).settings
        self.assertEqual((mine.approve_from, mine.approve_into), ("release/*", "main"))
        self.assertIn('name="set:approve_from" value="release/*"', self.request("GET", "/")[2])
        self.assertIn("branch names", unreviewed("release/* main", "main")[2])
        page = self.request("GET", "/")[2]
        for secret in ("gh-office", "gh-home", "sk-ant-oat01-abcdef"):
            self.assertNotIn(secret, page)  # tokens are never shown back
        self.assertIn(f"/webhook?project={market.id}", page)  # the owner sees each project's webhook
        self.assertIn(self.projects.webhook_secret(market.id), page)

        # MAR-1 exists on both sites: the project decides.
        self.assertIn("MAR-1 is in several of your projects", self.run_form(csrf, {"issue_keys": "mar-1"})[2])
        self.assertEqual(self.run_form(csrf, {"issue_keys": "mar-1", "project_id": market.id})[1], "/?ok=queued")
        self.assertEqual(self.queued, [("marketplace", ["MAR-1"], me.id)])
        self.queued.clear()
        self.run_form(csrf, {"issue_keys": "MARS-1, MARS-2", "grouping": "together"})
        self.assertEqual(self.queued, [("marketplace", ["MARS-1", "MARS-2"], me.id)])
        # A note goes with every run it queues; one that is too long is refused.
        self.queued.clear()
        self.run_form(csrf, {"issue_keys": "MARS-1 MARS-2", "note": "  Only the API.\r\nNot the UI. "})
        self.assertEqual(self.queued, [("marketplace", [k], me.id, "Only the API.\nNot the UI.") for k in ("MARS-1", "MARS-2")])
        # Lines starting with a key are only for that issue's run; the lines before them are for all.
        self.queued.clear()
        note = "Only the API.\nmars-1: the total is wrong,\nnot the tax.\nMARS-2:"
        self.run_form(csrf, {"issue_keys": "MARS-1 MARS-2", "note": note})
        self.assertEqual(self.queued, [("marketplace", ["MARS-1"], me.id, "Only the API.\nthe total is wrong,\nnot the tax."),
                                       ("marketplace", ["MARS-2"], me.id, "Only the API.")])
        self.queued.clear()
        self.run_form(csrf, {"issue_keys": "MARS-1 MARS-2", "grouping": "together", "note": note})
        self.assertEqual(self.queued, [("marketplace", ["MARS-1", "MARS-2"], me.id, note)])  # one run: all of it
        # What to change, and one review request for all: as chosen on the form.
        page = self.request("GET", "/")[2]
        self.assertIn(f'<input type="checkbox" name="repos" value="{market.id}:services">', page)
        self.assertIn("marketplace: choose the repos to change: fe, services.",
                      self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1"})[2])
        self.assertIn('<option value="one" selected>One message for all issues</option>', page)
        self.options.clear()
        self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1 MARS-2", "repos": [f"{market.id}:fe"], "message": "one"})
        first, second = self.options
        self.assertEqual((first["repos"], second["repos"]), (("fe",), ("fe",)))
        self.assertTrue(first["batch"] and first["batch"] == second["batch"])  # one request for the two
        self.options.clear()
        self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1 MARS-2", "repos": [f"{market.id}:services"], "message": "each"})
        self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1", "repos": self.every_repo(), "message": "one"})
        self.assertEqual([(o["repos"], o["batch"]) for o in self.options],
                         [(("services",), None), (("services",), None), ((), None)])  # one issue: its own request
        self.assertIn("Enter one or more", self.request("POST", "/run", {"csrf": csrf, "issue_keys": ""})[2])
        # One after another, with minutes between them; in parallel otherwise.
        self.assertEqual(self.sequences, [])
        self.run_form(csrf, {"issue_keys": "MARS-2 MARS-1", "grouping": "sequence", "delay": "10"})
        self.run_form(csrf, {"issue_keys": "MARS-1", "grouping": "sequence", "delay": "10"})
        self.assertEqual(self.sequences, [(600.0, ["MARS-2", "MARS-1"])])  # in the order typed; one issue is no sequence
        for bad in ("-1", "241", "soon"):
            self.assertIn("minutes between issues must be a number from 0 to 240",
                          self.run_form(csrf, {"issue_keys": "MARS-1 MARS-2", "grouping": "sequence", "delay": bad})[2])
        self.queued.clear()
        self.assertIn("a part for MARS-7, which is not among the issue keys",
                      self.run_form(csrf, {"issue_keys": "MARS-1", "note": "MARS-7: x"})[2])
        self.queued.clear()
        self.assertIn("keep it under 2000", self.run_form(csrf, {"issue_keys": "MARS-1", "note": "x" * 2001})[2])
        self.assertEqual(self.queued, [])
        self.assertIn("HR-1 is not in one of your projects", self.run_form(csrf, {"issue_keys": "HR-1"})[2])
        self.assertIn("Not an issue key: ../X", self.run_form(csrf, {"issue_keys": "MARS-1 ../x"})[2])
        # Only issues assigned to you (to the Jira account you joined with) can be queued.
        self.queued.clear()
        self.assertIn("MARS-9 is not assigned to you",
                      self.run_form(csrf, {"issue_keys": "MARS-1 MARS-9"})[2])
        self.assertEqual(self.queued, [])

        # Removing the personal GitHub account leaves that project waiting for another one.
        self.request("POST", "/github/remove", {"csrf": csrf, "github_account_id": gh["personal"].id})
        self.assertIsNone(self.me().actor(side.id))
        page = self.request("GET", "/")[2]
        self.assertIn("Active in 1 project:", page)  # under Overview's title
        self.assertIn('<a class="side-status ok" href="/?tab=projects" title="Active in 1 project:', page)
        self.assertIn(">Active · 1 project</a>", page)
        # Its owner is its only member: they can't leave it (the project would be left to nobody); they delete it.
        self.assertIn("delete the project instead", self.request("POST", "/project/leave", {"csrf": csrf, "project_id": side.id})[2])
        self.assertIn(side.id, self.me().subscriptions)
        self.request("POST", "/project/delete", {"csrf": csrf, "project_id": side.id})
        self.assertIsNone(self.app.projects.get(side.id))

        self.request("POST", "/logout", {"csrf": csrf})
        self.assertIn("Sign in with Jira", self.request("GET", "/")[2])

    def test_only_the_owner_edits_and_only_site_members_see_a_project(self):
        sara = add_developer(self.users, "acc-sara")
        market = add_project(self.projects, sara, "marketplace", ["MAR"])
        stranger = add_developer(self.users, "acc-home-only", sites=(HOME,))
        self.sign_in()
        csrf = self.csrf()
        self.assertIn("marketplace", self.request("GET", "/")[2])  # same site: visible, joinable
        self.assertNotIn(f"/project?id={market.id}", self.request("GET", "/")[2])
        self.assertEqual(self.request("GET", f"/project?id={market.id}")[1], "/")
        page = self.request("POST", "/project/save", {"csrf": csrf, "project_id": market.id, "name": "mine",
                                                      "jira_keys": "MAR", "repo_github": ["acme/x"], "repo_test": ["true"]})[2]
        self.assertIn("Only the project&#x27;s owner can change it", page)
        self.assertIn("Only the project&#x27;s owner", self.request("POST", "/project/delete", {"csrf": csrf, "project_id": market.id})[2])
        self.assertEqual(self.projects.get(market.id).name, "marketplace")
        # A developer without access to the office site can't see or join it.
        self.cookies = {"shadowdev_session": self.sessions.create(stranger)}
        csrf = self.csrf()
        self.assertNotIn("marketplace", self.request("GET", "/")[2])
        self.assertIn("No such project", self.request("POST", "/project/join", {"csrf": csrf, "project_id": market.id})[2])

    def test_no_access_to_project_repos_or_jira_is_reported(self):
        self.sign_in()
        csrf = self.csrf()
        self.request("POST", "/github", {"csrf": csrf, "github_token": "gh-tok"})
        github_id = self.me().github_accounts[0].id
        self.github.can_push.side_effect = lambda owner_repo: owner_repo != "acme/billing"
        def jira_statuses(key):
            if key == "HID":
                raise project_service.HTTPError("not found", 404)
            return ["In Progress", "In Review"]
        self.jira.project_statuses.side_effect = jira_statuses
        page = self.request("POST", "/project/save", {
            "csrf": csrf, "name": "billing", "cloud_id": OFFICE.cloud_id, "jira_keys": "BIL, HID",
            "github_account_id": github_id, "repo_github": ["acme/billing", "acme/ok"], "repo_test": ["true", "true"]})[2]
        self.assertIn("GitHub account majid cannot push to acme/billing", page)
        self.assertIn("cannot see Jira project HID on office", page)
        self.assertEqual(self.app.project_service.visible(self.me()), [])
        for bad, message in (("not a repo", "give the GitHub repo as owner/name"), ("../..", "give the GitHub repo")):
            page = self.request("POST", "/project/save", {"csrf": csrf, "name": "x", "cloud_id": OFFICE.cloud_id,
                                                          "jira_keys": "X1", "github_account_id": github_id,
                                                          "repo_github": [bad], "repo_test": [""]})[2]
            self.assertIn(message, page)

    def test_statuses_must_be_the_jira_projects_own(self):
        self.sign_in()
        csrf = self.csrf()
        self.request("POST", "/github", {"csrf": csrf, "github_token": "gh-tok"})
        workflows = {"MAR": ["Backlog", "Doing", "Code Review", "Done"], "MARS": ["Shipped"]}
        self.jira.project_statuses.side_effect = lambda key: workflows[key]
        form = {"csrf": csrf, "name": "market", "cloud_id": OFFICE.cloud_id, "jira_keys": "MAR, MARS",
                "github_account_id": self.me().github_accounts[0].id, "repo_github": ["acme/fe"], "repo_test": ["true"]}
        # The defaults (In Progress, In Review) are not in this workflow; unused default status rules are not checked.
        page = self.request("POST", "/project/save", form)[2]
        self.assertIn("Start statuses: In Progress is not a status of MAR, MARS.", page)
        self.assertIn("Review statuses: In Review is not a status of MAR, MARS.", page)
        self.assertNotIn("Status rules:", page)
        self.assertIn("Their statuses: Backlog, Doing, Code Review, Done, Shipped.", page)
        # The editor shown again suggests them, in the status fields.
        self.assertIn('<option value="Code Review">', page)
        self.assertEqual(page.count('list="jira-statuses"'), 2)
        page = self.request("POST", "/project/save", {**form, "start_statuses": "backlog, doing", "review_statuses":
                                                      "Code Review", "status_rules": "Code Review -> Released"})[2]
        self.assertIn("Status rules: Released is not a status of MAR, MARS.", page)
        self.assertEqual(self.request("POST", "/project/save", {**form, "start_statuses": "backlog, doing",
                                                                "review_statuses": "Code Review",
                                                                "status_rules": "Code Review -> Shipped"})[1], "/?ok=project")
        (market,) = self.app.project_service.visible(self.me())
        self.assertEqual(market.start_statuses, ("backlog", "doing"))  # Jira matches status names in any case
        self.assertIn('<option value="Shipped">', self.request("GET", f"/project?id={market.id}")[2])
        # Suggestions are best effort: a Jira project that can't be read leaves them out, the page still shows.
        self.jira.project_statuses.side_effect = project_service.HTTPError("down", 0)
        page = self.request("GET", f"/project?id={market.id}")[2]
        self.assertIn('<datalist id="jira-statuses"></datalist>', page)
        self.assertIn("saving checks them", page)

    def test_start_statuses_are_suggested_and_checked_against_jira(self):
        self.sign_in()
        csrf = self.csrf()
        uid = self.me().id
        self.request("POST", "/github", {"csrf": csrf, "github_token": "gh-tok"})
        self.request("POST", "/claude", {"csrf": csrf, "claude_token": "sk-ant-oat01-abc"})
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        self.jira.project_statuses.side_effect = lambda key: ["Backlog", "Selected", "Done"]
        page = self.request("GET", "/?tab=projects")[2]
        self.assertIn(f'list="set-{market.id}-auto_statuses-list" data-statuses>', page)
        self.assertIn(f'data-src="/project/statuses?id={market.id}"', page)
        self.assertEqual(self.request("GET", f"/project/statuses?id={market.id}")[2], "Backlog\nDone\nSelected")
        form = {"csrf": csrf, "project_id": market.id, "github_account_id": self.me().github_accounts[0].id,
                "claude_account_id": self.me().claude_accounts[0].id, "mode": "all", "settings": "1", "set:auto_start": "on"}
        # The default lists statuses this workflow lacks: not checked. A status typed must be one of its own.
        self.assertEqual(self.request("POST", "/project/join", {
            **form, "set:auto_statuses": Settings.auto_statuses})[1], "/?ok=joined")
        page = self.request("POST", "/project/join", {**form, "set:auto_statuses": "Backlog, Selectd"})[2]
        self.assertIn("In one of these statuses: Selectd is not a status of MAR.", page)
        self.assertEqual(self.me().subscriptions[market.id].settings.auto_statuses, Settings.auto_statuses)
        self.assertEqual(self.request("POST", "/project/join", {**form, "set:auto_statuses": "backlog, Selected"})[1],
                         "/?ok=joined")
        self.assertEqual(self.me().subscriptions[market.id].settings.auto_statuses, "backlog, Selected")
        # What was saved before (an older default, kept by an upgrade) is not checked again.
        sub = self.me().subscriptions[market.id]
        self.users.subscriptions.subscribe(uid, dataclasses.replace(sub, settings=Settings(auto_statuses="To Do, Ready for Pickup")))
        self.assertEqual(self.request("POST", "/project/join", {**form, "set:auto_statuses": "To Do, Ready for Pickup"})[1],
                         "/?ok=joined")
        # Only for a signed-in developer, and a project on a Jira site of theirs.
        stranger = add_developer(self.users, "acc-home", sites=(HOME,))
        other = add_project(self.projects, stranger, "elsewhere", ["ELS"], site=HOME)
        self.assertEqual(self.request("GET", f"/project/statuses?id={other.id}")[0], 404)
        self.assertEqual(self.request("GET", "/project/statuses?id=x")[0], 404)
        self.cookies.pop("shadowdev_session")
        self.assertEqual(self.request("GET", f"/project/statuses?id={market.id}")[0], 404)

    def test_test_messages_from_the_project_page_three_of_each_then_the_button_is_disabled(self):
        self.sign_in()
        uid = self.me().id
        add_developer(self.users, ME, user_id=uid)
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        join(self.users, uid, market, ME)
        page = self.request("GET", "/")[2]
        self.assertIn('value="slack" disabled>Send a test Slack message</button>', page)  # Slack not connected
        self.assertIn('value="email" disabled>Send a test email</button>', page)  # no SMTP on this server
        self.users.save_slack(uid, "xoxp-1", "U0000MEME0", "acme")
        slack = mock.Mock()
        send = lambda: self.request("POST", "/project/test-message", {"csrf": self.csrf(), "project_id": market.id,
                                                                      "way": "slack"})
        with mock.patch.object(notifications, "Slack", return_value=slack), \
             mock.patch.object(notifications, "COOLDOWN_SECONDS", 0):  # the wait is tested with the service
            self.assertEqual(send()[1], "/?ok=test-sent")
            self.assertIn("Send a test Slack message (2 left)", self.request("GET", "/")[2])
            send()
            send()
            self.assertIn("You have sent the 3 test Slack messages of marketplace already.", send()[2])
        self.assertEqual(slack.post.call_count, 3)
        self.assertIn('value="slack" disabled>3 test Slack messages sent</button>', self.request("GET", "/")[2])
        # Sending messages out is rate limited, like the routes anyone can call.
        self.assertEqual(LIMITS["/project/test-message"], 6)

    def test_status_rules_switch_and_check_now(self):
        self.sign_in()
        csrf = self.csrf()
        uid = self.me().id
        add_developer(self.users, ME, user_id=uid)
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        join(self.users, uid, market, ME)
        page = self.request("GET", "/")[2]
        self.assertIn('<input type="checkbox" name="set:release_moves">', page)  # off by default
        self.assertIn("Status rules · Off: only when you check now", page)
        self.assertIn("<li>None: its owner sets them</li>", page)  # no rules until the owner writes them
        self.assertIn("Not checked yet.", page)

        # The owner sets the project's rules; a bad one is named.
        form = {"csrf": csrf, "project_id": market.id, "name": "marketplace", "jira_keys": "MAR", "github_account_id":
                self.me().github_accounts[0].id, "repo_github": ["acme/fe"], "repo_test": ["true"]}
        self.assertIn("Status rules: &#x27;luck&#x27; is not a check",
                      self.request("POST", "/project/save", {**form, "status_rules": "A -> B: luck"})[2])
        self.assertEqual(self.request("POST", "/project/save", {**form, "status_rules": "Merged -> In QA\nIn QA -> Done: parent"})[1],
                         "/?ok=project")
        self.assertIn("Merged -&gt; In QA\nIn QA -&gt; Done: parent", self.request("GET", f"/project?id={market.id}")[2])

        # Switched on, every 4 hours.
        join_form = {"csrf": csrf, "project_id": market.id, "jira_account_id": self.me().subscriptions[market.id].jira_account_id,
                     "github_account_id": self.me().github_accounts[0].id, "claude_account_id": self.me().claude_accounts[0].id,
                     "mode": "bugs", "settings": "1", "set:release_moves": "on", "set:release_hours": "4"}
        self.assertEqual(self.request("POST", "/project/join", join_form)[1], "/?ok=joined")
        self.assertEqual((self.me().subscriptions[market.id].settings.release_moves,
                          self.me().subscriptions[market.id].settings.release_hours), (True, "4"))

        jira = mock.Mock()
        jira.search.return_value = [{"key": "MAR-3", "fields": {"issuetype": {"name": "Story"}, "assignee": {"accountId": ME}}}]
        jira.status.return_value = "Merged"
        jira.transitions.return_value = [{"id": "5", "to": {"name": "In QA"}}]
        with mock.patch.object(releases, "jira_client", return_value=jira):
            self.assertEqual(self.request("POST", "/project/release", {"csrf": csrf, "project_id": market.id})[1], "/?ok=released")
        page = self.request("GET", "/")[2]
        self.assertIn("Status rules · On, every 4 hours", page)
        self.assertIn("<b>Moved:</b> MAR-3 to In QA", page)
        self.assertIn("<b>Not moved:</b> MAR-3 (In QA): no parent", page)

    def test_project_jira_options_and_working_hours(self):
        self.sign_in()
        csrf = self.csrf()
        uid = self.me().id
        add_developer(self.users, ME, user_id=uid)
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        join(self.users, uid, market, ME)
        page = self.request("GET", "/")[2]
        self.assertIn("In an active sprint", page)
        self.assertIn('type="checkbox" name="set:jira_fix_version"', page)
        self.assertNotIn('type="checkbox" name="set:jira_root_cause"', page)  # the project has no root cause field

        # An operations team: no sprints, no fix versions, issue types of its own.
        form = {"csrf": csrf, "project_id": market.id, "name": "marketplace", "jira_keys": "MAR", "github_account_id":
                self.me().github_accounts[0].id, "repo_github": ["acme/fe"], "repo_test": ["true"], "jira_options": "1",
                "fix_version_pick": "branch", "bug_types": "Bug, Incident", "task_types": "Service request"}
        self.assertIn("either worked on as a bug or as a task",
                      self.request("POST", "/project/save", {**form, "task_types": "incident"})[2])
        self.assertIn("Choose which fix version", self.request("POST", "/project/save", {**form, "fix_version_pick": "x"})[2])
        self.assertEqual(self.request("POST", "/project/save", form)[1], "/?ok=project")
        saved = self.projects.get(market.id)
        self.assertEqual((saved.uses_sprints, saved.uses_fix_versions, saved.fix_version_pick, saved.bug_types,
                          saved.task_types), (False, False, "branch", ("Bug", "Incident"), ("Service request",)))
        editor = self.request("GET", f"/project?id={market.id}")[2]
        self.assertIn('<input type="checkbox" name="uses_sprints">', editor)  # unticked
        self.assertIn('value="Bug, Incident"', editor)
        page = self.request("GET", "/")[2]
        self.assertNotIn("In an active sprint", page)
        self.assertNotIn('type="checkbox" name="set:jira_fix_version"', page)
        # Kept as they were, for when the owner switches sprints and fix versions back on.
        self.assertIn('<input type="hidden" name="set:auto_sprint" value="on">', page)
        self.assertIn('<input type="hidden" name="set:jira_fix_version" value="on">', page)

        join_form = {"csrf": csrf, "project_id": market.id, "jira_account_id": self.me().subscriptions[market.id].jira_account_id,
                     "github_account_id": self.me().github_accounts[0].id, "claude_account_id": self.me().claude_accounts[0].id,
                     "mode": "bugs", "settings": "1", "set:work_from": "09:00", "set:work_until": "17:30",
                     "set:day:mon": "on", "set:day:fri": "on", "set:work_timezone": "Asia/Karachi"}
        self.assertIn("is not a time zone", self.request("POST", "/project/join", {**join_form, "set:work_timezone": "Mars/X"})[2])
        self.assertEqual(self.request("POST", "/project/join", join_form)[1], "/?ok=joined")
        chosen = self.me().subscriptions[market.id].settings
        self.assertEqual((chosen.work_from, chosen.work_until, chosen.work_days, chosen.work_timezone),
                         ("09:00", "17:30", ("mon", "fri"), "Asia/Karachi"))
        page = self.request("GET", "/")[2]
        self.assertIn('name="set:work_from" value="09:00"', page)
        self.assertIn('name="set:day:fri" aria-label="Friday" checked', page)
        self.assertNotIn('aria-label="Tuesday" checked', page)
