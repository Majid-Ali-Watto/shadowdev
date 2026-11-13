import dataclasses
import hashlib
import hmac
import html
import http.client
import json
import logging
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

from shadowdev.app import build_app, database
from shadowdev.automation import limits, runlog
from shadowdev.automation.agent import claude_config_dir
from shadowdev.clients.github import expiry as github_expiry
from shadowdev.clients.rest import HTTPError
from shadowdev.config import Config
from shadowdev.models.limits import Limits
from shadowdev.models.run import RunFilter
from shadowdev.models.settings import Settings
from shadowdev.models.tokens import Tokens
from shadowdev.models.user import GitHubAccount, Site
from shadowdev.repositories.projects import ProjectRepository
from shadowdev.repositories.runs import RunRepository
from shadowdev.repositories.sessions import SessionRepository
from shadowdev.repositories.users import UserRepository
from shadowdev.routes.ratelimit import RateLimiter
from shadowdev.server import Server, make_handler
from shadowdev.services import accounts, releases, run_queue, runs
from shadowdev.services import projects as project_service
from shadowdev.services.errors import UserError
from shadowdev.services.run_queue import RunQueue
from shadowdev.services.webhook import matching_issue, verify_signature
from shadowdev.views.home import home_page
from tests.helpers import HOME, OFFICE, add_developer, add_project, join, make_cfg, repo

ME = "acc-majid"
ME_HOME = "acc-majid-home"  # the same person's personal Atlassian account


ACTIVE_SPRINT = [{"id": 7, "name": "Sprint 7", "state": "active", "boardId": 1}]


def payload(event="jira:issue_created", key="MAR-12", type_="Bug", assignee=ME, site=OFFICE, status="To Do",
            sprint=ACTIVE_SPRINT):
    """A Jira event about an issue that starts by itself by default: To Do, in an active sprint."""
    issue = {"key": key, "fields": {"issuetype": {"name": type_}, "assignee": {"accountId": assignee},
                                    "status": {"name": status}, "customfield_10020": sprint}}
    if site:
        issue["self"] = f"{site.url}/rest/api/2/issue/10001"
    return {"webhookEvent": event, "issue": issue}


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_cfg(self.tmp)
        db = database(self.cfg)
        self.users, self.projects, self.runs = UserRepository(db), ProjectRepository(db), RunRepository(db)
        self.sessions = SessionRepository(db)

    def developer(self, account_id=ME, mode="bugs", project=None):
        """A developer in the office project (created on first use), with office accounts."""
        uid = add_developer(self.users, account_id)
        self.market = getattr(self, "market", None) or add_project(self.projects, uid, "marketplace", ["MAR", "MARS"])
        join(self.users, uid, project or self.market, account_id, mode)
        return uid


class WebhookTest(Env):
    def test_signature(self):
        body = b'{"a":1}'
        good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature("s3cret", body, good))
        self.assertFalse(verify_signature("other", body, good))
        self.assertFalse(verify_signature("s3cret", body, None))
        self.assertFalse(verify_signature("s3cret", body, "sha256=caf\u00e9"))  # refused, not an error
        self.assertFalse(verify_signature("s3cret", body, good.removeprefix("sha256=")))

    def test_routes_to_the_project_on_its_site_and_the_developer_of_that_jira_account(self):
        me = self.developer(ME, mode="all")
        billing = add_project(self.projects, me, "billing", ["BIL"], repos=[repo("billing")])
        join(self.users, me, billing, ME, "all")
        # The same person's personal Jira, GitHub and Claude, for a personal project that also uses MAR.
        add_developer(self.users, ME_HOME, sites=(HOME,), user_id=me)
        side = add_project(self.projects, me, "side", ["MAR"], site=HOME)
        join(self.users, me, side, ME_HOME)
        sara = self.developer("acc-sara")
        add_developer(self.users, "acc-new")  # signed in, but in no project
        match = lambda project=None, **kw: matching_issue(payload(**kw), self.projects, self.users, project)

        self.assertEqual(match(), ("MAR-12", self.market, me))
        self.assertEqual(match(key="MARS-3", assignee="acc-sara"), ("MARS-3", self.market, sara))
        self.assertEqual(match(key="BIL-7", type_="Story"), ("BIL-7", billing, me))
        self.assertEqual(match(event="jira:issue_updated", type_="bug")[0], "MAR-12")
        # Only an issue in a status that starts by itself, and in a sprint that has started.
        future = [{**ACTIVE_SPRINT[0], "state": "future"}]
        with self.assertLogs("shadowdev", "INFO") as logs:
            for kw in ({"status": "In Progress"}, {"sprint": None}, {"sprint": future},
                       {"sprint": [{**ACTIVE_SPRINT[0], "state": "closed"}]}):
                self.assertIsNone(match(**kw), kw)
        self.assertIn("MAR-12: not started by itself for", logs.output[0])
        self.assertIn("its status In Progress is not one that starts by itself", logs.output[0])
        self.assertIn("it is in no sprint", logs.output[1])
        self.assertIn("its sprint has not started, or has ended", logs.output[2])
        self.assertEqual(match(status="ready for pickup", sprint=[*future, *ACTIVE_SPRINT])[0], "MAR-12")
        uid = self.users.owner_of(ME)
        sub = self.users.get(uid).subscriptions[self.market.id]
        self.users.subscribe(uid, dataclasses.replace(sub, settings=Settings(auto_statuses="", auto_sprint=False)))
        self.assertEqual(match(status="In QA", sprint=None)[0], "MAR-12")  # conditions off: any status, no sprint
        # MAR-12 at home is the personal project's, for the personal Jira account only.
        self.assertEqual(match(site=HOME, assignee=ME_HOME), ("MAR-12", side, me))
        self.assertIsNone(match(site=HOME, assignee=ME))
        self.assertIsNone(match(site=HOME, assignee=ME_HOME, type_="Story"))  # bugs only there
        self.assertEqual(match(project=side, site=None, assignee=ME_HOME), ("MAR-12", side, me))  # per-project URL
        self.assertIsNone(match(project=side, key="BIL-1", site=None, assignee=ME_HOME))
        self.assertIsNone(match(site=Site("x", "https://elsewhere.atlassian.net", "x")))
        self.assertIsNone(match(site=None))  # shared URL: the site must be known
        self.assertIsNone(match(key="BIL-7", assignee="acc-sara"))  # not in billing
        self.assertIsNone(match(type_="Task", assignee="acc-sara"))  # bugs only
        self.assertIsNone(match(type_="Epic"))
        self.assertIsNone(match(assignee="acc-new"))
        self.assertIsNone(match(assignee="acc-unknown"))
        self.assertIsNone(match(key="HR-1"))  # no project
        self.assertIsNone(match(event="comment_created"))
        # Keys become branch and directory names, so anything odd is rejected.
        self.assertIsNone(match(key="MAR-1; rm -rf /"))
        self.assertIsNone(match(key="../MAR-1"))
        self.assertIsNone(match(key="MAR-1\n"))


class WebTest(Env):
    """Drives the real HTTP server through Jira OAuth sign-in, accounts and projects. Atlassian,
    Jira, GitHub and Claude are faked."""

    def setUp(self):
        super().setUp()
        self.queued, self.options = [], []  # options: each queued run's other arguments (repos, batch)

        def submit(project, keys, uid, note="", **kw):
            self.queued.append((project.name, keys, uid, *([note] if note else [])))
            self.options.append(kw)

        def submit_sequence(items, uid, delay):
            self.sequences.append((delay, [keys[0] for _, keys, *_ in items]))
            return [submit(project, keys, uid, note, repos=repos, batch=batch) for project, keys, note, repos, batch in items]

        self.sequences = []  # (seconds between, the issues in order) of each sequence queued
        self.pool = mock.Mock(submit=submit, submit_sequence=submit_sequence)
        self.oauth = mock.Mock()
        self.oauth.authorize_url.side_effect = lambda state: f"https://auth.atlassian.com/authorize?state={state}"
        self.oauth.exchange.return_value = Tokens("access-1", "refresh-1", time.time() + 3600)
        self.oauth.sites.return_value = [OFFICE]
        self.app = build_app(self.cfg, self.oauth, self.pool)
        server = Server(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        self.cookies = {}

        self.me_jira = {"accountId": ME, "displayName": "Majid", "accountType": "atlassian", "emailAddress": "majid@example.com"}
        self.jira = mock.Mock()
        self.jira.myself.side_effect = lambda: self.me_jira
        self.jira.get_issue.side_effect = lambda key: {"fields": {"assignee": {"accountId": "acc-sara" if key.endswith("-9") else ME}}}
        self.github = mock.Mock()
        self.github.user.return_value = {"login": "majid", "id": 7, "name": "Majid Ali", "email": None}
        self.github.user_and_expiry.side_effect = lambda: (self.github.user(), 0)  # a token that never expires
        self.github.can_push.return_value = True
        self.github.repo.side_effect = lambda owner_repo: {"description": f"{owner_repo} on GitHub", "permissions": {"push": True}}
        patches = [mock.patch.object(accounts, "GitHub", return_value=self.github),
                   mock.patch.object(project_service, "GitHub", return_value=self.github),
                   mock.patch.object(accounts, "Jira", return_value=self.jira),
                   mock.patch.object(project_service, "jira_client", return_value=self.jira),
                   mock.patch.object(runs, "jira_client", return_value=self.jira),
                   mock.patch.object(accounts, "check_token"),
                   mock.patch.object(project_service.gitops, "remote_branch_exists", lambda owner_repo, token, b: b == "develop")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def request(self, method, path, form=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = urlencode(form, doseq=True) if form is not None else None
        cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        conn.request(method, path, body, {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded",
                                          **(headers or {})})
        resp = conn.getresponse()
        text = resp.read().decode()
        conn.close()
        self.set_cookies = resp.headers.get_all("Set-Cookie") or []
        for c in self.set_cookies:
            name, _, value = c.split(";")[0].partition("=")
            if "Max-Age=0" in c:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        self.headers = resp.headers
        return resp.status, resp.getheader("Location"), text

    def oauth_round_trip(self, location):
        state = parse_qs(urlsplit(location).query)["state"][0]
        return self.request("GET", f"/oauth/jira/callback?code=c0de&state={state}")

    def sign_in(self):
        status, location, _ = self.request("GET", "/login/jira")
        self.assertEqual(status, 303)
        return self.oauth_round_trip(location)

    def connect(self):
        """POST /jira/connect; returns the Atlassian URL its page opens."""
        status, location, page = self.request("POST", "/jira/connect", {"csrf": self.csrf()})
        self.assertEqual((status, location), (200, None))
        self.assertIn("location.replace", page)
        return html.unescape(re.search(r'id="continue" href="([^"]+)"', page).group(1))

    def csrf(self):
        return re.search(r'name="csrf" value="([^"]+)"', self.request("GET", "/")[2]).group(1)

    def me(self):
        return self.users.get(self.users.owner_of(ME))

    def test_sign_in_with_jira_oauth(self):
        page = self.request("GET", "/")[2]
        self.assertIn(f'href="{self.cfg.public_url}/login/jira"', page)

        # The button sends the browser to Atlassian with a one-time state, also kept in a cookie.
        status, location, _ = self.request("GET", "/login/jira")
        self.assertTrue(location.startswith("https://auth.atlassian.com/authorize?state="))
        state = parse_qs(urlsplit(location).query)["state"][0]
        self.assertEqual(self.cookies["shadowdev_oauth_state"], f"signin.{state}")

        # A callback with someone else's state, or a refusal, signs nobody in.
        self.assertIn("expired or was started elsewhere",
                      self.request("GET", "/oauth/jira/callback?code=c0de&state=forged")[2])
        self.assertIn("not granted", self.request("GET", "/oauth/jira/callback?error=access_denied&state=x")[2])
        self.oauth.exchange.assert_not_called()

        status, location, _ = self.sign_in()
        self.assertEqual((status, location), (303, self.cfg.public_url + "/"))
        self.oauth.exchange.assert_called_once_with("c0de")
        self.assertTrue(any("shadowdev_session=" in c and "HttpOnly; SameSite=Lax" in c for c in self.set_cookies))
        self.assertNotIn("shadowdev_oauth_state", self.cookies)  # one-time
        user = self.me()
        self.assertEqual((user.display_name, user.email, list(user.sites)), ("Majid", "majid@example.com", [OFFICE.cloud_id]))
        self.assertEqual(self.users.jira_tokens(user.jira_accounts[0].id).refresh_token, "refresh-1")
        self.assertIn("Signed in as Majid (majid@example.com)", self.request("GET", "/")[2])

    def test_sign_in_needs_a_site_this_server_allows(self):
        self.oauth.sites.return_value = [Site("x", "https://someone-else.atlassian.net", "x")]
        self.assertIn("None of the Jira sites you granted is enabled", self.sign_in()[2])
        self.assertIsNone(self.users.owner_of(ME))

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
        chosen = {"settings": "1", "set:auto_start": "on", "set:model": "sonnet", "set:publish": "draft"}
        self.assertEqual(self.request("POST", "/project/join", {**join_form(side, gh["personal"], cl["personal"]),
                                                                "mode": "bugs", **chosen})[1], "/?ok=joined")
        for form in (chosen, {}):
            self.request("POST", "/project/join", {**join_form(side, gh["personal"], cl["personal"]), "mode": "bugs", **form})
            mine = self.me().actor(side.id).settings
            self.assertEqual((mine.auto_start, mine.model, mine.publish, mine.fix_ci, mine.jira_comments),
                             (True, "sonnet", "draft", False, False))
        self.assertEqual(self.me().actor(market.id).settings, Settings())  # the other project keeps its own
        self.assertIn("is not a choice", self.request("POST", "/project/join", {
            **join_form(side, gh["personal"], cl["personal"]), **chosen, "set:publish": "force-push"})[2])
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
        self.assertIn("MAR-1 is in several of your projects", self.request("POST", "/run", {"csrf": csrf, "issue_keys": "mar-1"})[2])
        self.assertEqual(self.request("POST", "/run", {"csrf": csrf, "issue_keys": "mar-1", "project_id": market.id})[1], "/?ok=queued")
        self.assertEqual(self.queued, [("marketplace", ["MAR-1"], me.id)])
        self.queued.clear()
        self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1, MARS-2", "grouping": "together"})
        self.assertEqual(self.queued, [("marketplace", ["MARS-1", "MARS-2"], me.id)])
        # A note goes with every run it queues; one that is too long is refused.
        self.queued.clear()
        self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1 MARS-2", "note": "  Only the API.\r\nNot the UI. "})
        self.assertEqual(self.queued, [("marketplace", [k], me.id, "Only the API.\nNot the UI.") for k in ("MARS-1", "MARS-2")])
        # Lines starting with a key are only for that issue's run; the lines before them are for all.
        self.queued.clear()
        note = "Only the API.\nmars-1: the total is wrong,\nnot the tax.\nMARS-2:"
        self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1 MARS-2", "note": note})
        self.assertEqual(self.queued, [("marketplace", ["MARS-1"], me.id, "Only the API.\nthe total is wrong,\nnot the tax."),
                                       ("marketplace", ["MARS-2"], me.id, "Only the API.")])
        self.queued.clear()
        self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1 MARS-2", "grouping": "together", "note": note})
        self.assertEqual(self.queued, [("marketplace", ["MARS-1", "MARS-2"], me.id, note)])  # one run: all of it
        # What to change, and one review request for all: as chosen on the form.
        page = self.request("GET", "/")[2]
        self.assertIn('<option value="fe" selected>FE only</option>', page)
        self.assertIn('<option value="one" selected>One message for all issues</option>', page)
        self.options.clear()
        self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1 MARS-2", "change": "fe", "message": "one"})
        first, second = self.options
        self.assertEqual((first["repos"], second["repos"]), (("fe",), ("fe",)))
        self.assertTrue(first["batch"] and first["batch"] == second["batch"])  # one request for the two
        self.options.clear()
        self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1 MARS-2", "change": "be", "message": "each"})
        self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1", "change": "all", "message": "one"})
        self.assertEqual([(o["repos"], o["batch"]) for o in self.options],
                         [(("services",), None), (("services",), None), ((), None)])  # one issue: its own request
        self.assertIn("Enter one or more", self.request("POST", "/run", {"csrf": csrf, "issue_keys": "", "change": "fe"})[2])
        # One after another, with minutes between them; in parallel otherwise.
        self.assertEqual(self.sequences, [])
        self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-2 MARS-1", "grouping": "sequence", "delay": "10"})
        self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1", "grouping": "sequence", "delay": "10"})
        self.assertEqual(self.sequences, [(600.0, ["MARS-2", "MARS-1"])])  # in the order typed; one issue is no sequence
        for bad in ("-1", "241", "soon"):
            self.assertIn("minutes between issues must be a number from 0 to 240", self.request(
                "POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1 MARS-2", "grouping": "sequence", "delay": bad})[2])
        self.queued.clear()
        self.assertIn("a part for MARS-7, which is not among the issue keys",
                      self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1", "note": "MARS-7: x"})[2])
        self.queued.clear()
        self.assertIn("keep it under 2000", self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1", "note": "x" * 2001})[2])
        self.assertEqual(self.queued, [])
        self.assertIn("HR-1 is not in one of your projects", self.request("POST", "/run", {"csrf": csrf, "issue_keys": "HR-1"})[2])
        self.assertIn("Not an issue key: ../X", self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1 ../x"})[2])
        # Only issues assigned to you (to the Jira account you joined with) can be queued.
        self.queued.clear()
        self.assertIn("MARS-9 is not assigned to you",
                      self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MARS-1 MARS-9"})[2])
        self.assertEqual(self.queued, [])

        # Removing the personal GitHub account leaves that project waiting for another one.
        self.request("POST", "/github/remove", {"csrf": csrf, "github_account_id": gh["personal"].id})
        self.assertIsNone(self.me().actor(side.id))
        self.assertIn("Active in 1 project:", self.request("GET", "/")[2])
        self.assertEqual(self.request("POST", "/project/leave", {"csrf": csrf, "project_id": side.id})[1], "/?ok=left")
        self.assertNotIn(side.id, self.me().subscriptions)

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
        def jira_project(key):
            if key == "HID":
                raise project_service.HTTPError("not found", 404)
            return {}
        self.jira.project.side_effect = jira_project
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

    def test_connecting_another_developers_jira_account_is_refused(self):
        add_developer(self.users, "acc-sara")
        self.sign_in()
        location = self.connect()
        self.me_jira = {"accountId": "acc-sara", "displayName": "Sara", "accountType": "atlassian"}
        self.assertIn("already connected to another ShadowDev account", self.oauth_round_trip(location)[2])
        self.assertEqual(len(self.me().jira_accounts), 1)
        # Connecting needs a signed-in browser: a "connect" state without a session signs nobody in.
        self.cookies.pop("shadowdev_session")
        self.cookies["shadowdev_oauth_state"] = "connect.abc"
        self.assertIn("expired or was started elsewhere", self.request("GET", "/oauth/jira/callback?code=c&state=abc")[2])

    def test_pages_render_in_the_chosen_theme(self):
        self.assertIn('<html lang="en">', self.request("GET", "/")[2])  # no choice: the system's
        self.cookies["shadowdev_theme"] = "dark"
        self.assertIn('<html lang="en" data-theme="dark">', self.request("GET", "/")[2])
        self.sign_in()
        page = self.request("GET", "/")[2]
        self.assertIn('data-theme="dark"', page)
        self.assertIn("data-theme-toggle", page)
        self.cookies["shadowdev_theme"] = '"><script>'  # only light or dark is ever used
        self.assertIn('<html lang="en">', self.request("GET", "/")[2])

    def test_pages_let_browsers_send_their_origin(self):
        # With Referrer-Policy: no-referrer, browsers send "Origin: null" on form posts, which the
        # Origin check rejects: no form would work.
        self.sign_in()
        csrf = self.csrf()
        self.assertEqual(self.headers["Referrer-Policy"], "same-origin")
        self.assertEqual(self.request("POST", "/github", {"csrf": csrf, "github_token": "gh"}, {"Origin": "null"})[0], 403)
        self.assertEqual(self.request("POST", "/github", {"csrf": csrf, "github_token": "gh"},
                                      {"Origin": f"http://127.0.0.1:{self.port}", "Host": f"127.0.0.1:{self.port}"})[0], 303)
        # Behind a proxy that rewrites Host, the public URL is the origin.
        self.assertEqual(self.request("POST", "/github", {"csrf": csrf, "github_token": "gh"},
                                      {"Origin": self.cfg.public_url, "Host": "127.0.0.1:9"})[0], 303)

    def test_bad_requests_are_answered_not_crashed(self):
        self.sign_in()
        self.assertEqual(self.request("POST", "/github", headers={"Content-Length": "x"})[0], 413)
        with mock.patch.object(self.app.accounts, "set_paused", side_effect=RuntimeError("bug")), \
                self.assertLogs("shadowdev", "ERROR"):
            self.assertEqual(self.request("POST", "/pause", {"csrf": self.csrf()})[0], 500)
        self.assertEqual(self.request("GET", "/")[0], 200)

    def test_delete_account_stops_runs_and_removes_everything(self):
        self.sign_in()
        uid = self.me().id
        self.request("POST", "/delete-account", {"csrf": self.csrf()})
        self.pool.stop_runs.assert_called_once_with(uid)
        self.assertIsNone(self.users.get(uid))
        self.assertIn("Sign in with Jira", self.request("GET", "/")[2])

    def test_running_a_range_of_issues(self):
        self.sign_in()
        csrf = self.csrf()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        self.users.save_github(uid, ME, "gh", ME, ME, f"{ME}@x")
        self.users.save_claude(uid, ME, "sk-ant-oat01-x", f"key-{ME}")
        join(self.users, uid, market, ME)  # bugs only

        def issue(key):
            if key == "MAR-2":
                raise HTTPError("Jira GET … -> 404", 404)
            return {"fields": {"assignee": {"accountId": "acc-sara" if key in ("MAR-3", "MAR-9") else ME},
                               "issuetype": {"name": "Story" if key == "MAR-4" else "Bug"}}}
        self.jira.get_issue.side_effect = issue

        # Of a range, what isn't yours to run is skipped, and listed as a skipped run.
        self.assertEqual(self.request("POST", "/run", {"csrf": csrf, "issue_keys": "mar-1 to mar-5, MAR-7"})[1], "/?ok=queued")
        self.assertEqual(self.queued, [("marketplace", ["MAR-1"], uid), ("marketplace", ["MAR-5"], uid),
                                       ("marketplace", ["MAR-7"], uid)])
        skipped = self.runs.recent(uid)[0]
        self.assertEqual((skipped["issue_key"], skipped["status"]), ("MAR-2, MAR-3, MAR-4", "skipped"))
        self.assertEqual(skipped["message"], "Not run, from the range: MAR-2 was not found in Jira, or you cannot see it. "
                                             "MAR-3 is not assigned to you. MAR-4 is a Story you don't automate in marketplace.")
        # Left out of the range: not run, not looked up, not listed as skipped.
        self.queued.clear()
        self.jira.get_issue.reset_mock()
        self.assertEqual(self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MAR-5 to MAR-8 except MAR-6"})[1], "/?ok=queued")
        self.assertEqual([keys for _, keys, _ in self.queued], [["MAR-5"], ["MAR-7"], ["MAR-8"]])
        self.assertNotIn("MAR-6", [c.args[0] for c in self.jira.get_issue.call_args_list])
        self.assertIn("MAR-13 is left out, but not among the issues", self.request(
            "POST", "/run", {"csrf": csrf, "issue_keys": "MAR-5 to MAR-8, !MAR-13"})[2])
        # A key typed by itself must be yours.
        self.queued.clear()
        self.assertIn("MAR-9 is not assigned to you.",
                      self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MAR-5 to MAR-6, MAR-9"})[2])
        self.assertEqual(self.queued, [])
        self.assertIn("None of those issues is yours to run",
                      self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MAR-2 to MAR-4"})[2])
        self.assertIn("a range stays in one Jira project", self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MAR-1 to HR-4"})[2])
        # Together: one job, at most ten issues on one branch.
        self.assertEqual(self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MAR-5 to MAR-8", "grouping": "together"})[1],
                         "/?ok=queued")
        self.assertEqual(self.queued, [("marketplace", ["MAR-5", "MAR-6", "MAR-7", "MAR-8"], uid)])
        self.assertIn("At most 10 issues on one branch; 12 are yours", self.request(
            "POST", "/run", {"csrf": csrf, "issue_keys": "MAR-10 to MAR-21", "grouping": "together"})[2])

    def test_answering_the_questions_of_a_run(self):
        self.sign_in()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        run_id = self.runs.add("MAR-1, MAR-2", uid, market.id, "marketplace")
        self.runs.update(run_id, "question", "ShadowDev needs your decision before it works on MAR-1: …")
        self.runs.set_questions(run_id, {"MAR-1": ["Keep the old <API>?"]})
        page = self.request("GET", "/")[2]
        self.assertIn(f'<a class="button small" href="/?tab=runs&amp;answer={run_id}#run-form">Answer</a>', page)
        page = self.request("GET", f"/?answer={run_id}")[2]
        self.assertIn('name="issue_keys" value="MAR-1, MAR-2"', page)
        self.assertIn("<li>MAR-1: Keep the old &lt;API&gt;?</li>", page)
        self.assertIn(" autofocus>MAR-1: \n</textarea>", page)
        self.assertIn('value="together" checked', page)  # they were worked on together
        # Someone else's run, or one that asked nothing, fills in nothing.
        other = add_developer(self.users, "acc-sara")
        theirs = self.runs.add("MAR-3", other, market.id, "marketplace")
        self.runs.update(theirs, "question", "…")
        self.runs.set_questions(theirs, {"MAR-3": ["Secret question?"]})
        for answer in (theirs, "x"):
            page = self.request("GET", f"/?answer={answer}")[2]
            self.assertNotIn("Secret question", page)
            self.assertIn('name="issue_keys" value=""', page)

    def test_stop_button(self):
        self.sign_in()
        csrf = self.csrf()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        run_id = self.runs.add("MAR-1", uid, market.id, "marketplace", "Only the <API>.")
        self.runs.set_model(run_id, "haiku → sonnet (medium)")
        page = self.request("GET", "/")[2]
        self.assertIn(f'<input type="hidden" name="run_id" value="{run_id}">', page)  # queued: has a Stop button
        self.assertIn("<small>Model: haiku → sonnet (medium)</small><small>Your note: Only the &lt;API&gt;.</small>", page)
        # The page keeps Recent runs up to date by fetching its rows.
        self.assertIn("connect-src 'self'", self.headers["Content-Security-Policy"])
        status, _, rows = self.request("GET", "/runs")
        self.assertEqual(status, 200)
        self.assertTrue(rows.startswith('<div class="results-bar">') and f'name="run_id" value="{run_id}"' in rows
                        and csrf in rows)
        self.assertIn(f'href="{OFFICE.url}/browse/MAR-1"', page)
        self.assertEqual(self.request("POST", "/stop", {"csrf": csrf, "run_id": str(run_id)})[1], "/?ok=stopping")
        self.pool.stop_run.assert_called_once_with(run_id, uid)
        self.pool.stop_run.return_value = False
        self.assertIn("already finished, or is not yours", self.request("POST", "/stop", {"csrf": csrf, "run_id": "999"})[2])
        self.assertIn("already finished, or is not yours", self.request("POST", "/stop", {"csrf": csrf, "run_id": "x"})[2])
        self.runs.update(run_id, "done", "ok")
        finished = self.request("GET", "/")[2]
        self.assertNotIn(f'<input type="hidden" name="run_id" value="{run_id}">', finished)  # finished: no Stop button
        self.assertIn(f'name="run_id" value="{run_id}" form="delete-runs"', finished)  # but it can be selected for deletion

    def test_recent_runs_are_searched_filtered_and_paged_by_the_address(self):
        self.sign_in()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        for n in range(1, 26):
            self.runs.update(self.runs.add(f"MAR-{n}", uid, market.id, "marketplace"), "failed" if n == 7 else "done")
        page = self.request("GET", "/")[2]
        self.assertIn('id="run-filter"', page)
        self.assertIn("1–20 of 25 runs", page)
        self.assertIn('href="/?tab=runs&amp;page=2" rel="next" data-nav', page)
        self.assertIn('<option value="marketplace">marketplace</option>', page)
        # The same search on the page and in what the page fetches; values are escaped.
        for path in ("/?tab=runs&q=MAR-7", "/runs?q=MAR-7"):
            found = self.request("GET", path)[2]
            self.assertIn("1–1 of 1 run<", found)
            self.assertIn(">MAR-7<", found)
            self.assertNotIn(">MAR-8<", found)
        self.assertIn('value="&lt;b&gt;"', self.request("GET", "/?tab=runs&q=<b>")[2])
        self.assertIn("No runs match this search", self.request("GET", "/runs?q=<b>")[2])
        second = self.request("GET", "/runs?page=2")[2]
        self.assertIn("21–25 of 25 runs", second)
        self.assertIn('rel="prev" data-nav', second)
        self.assertIn(">MAR-1<", second)
        failed = self.request("GET", "/runs?status=failed&kind=issue&project=marketplace")[2]
        self.assertIn(">MAR-7<", failed)
        self.assertIn('data-query="tab=runs&amp;status=failed&amp;project=marketplace&amp;kind=issue"', failed)
        self.assertIn("1–1 of 1 run<", failed)
        self.assertIn("No runs match", self.request("GET", "/runs?kind=pr")[2])
        # The filters sit in a popup with Apply and Clear; the ones in use show as chips that remove them.
        self.assertIn('<details class="filter-menu" id="filter-menu">', page)
        self.assertIn("<button>Apply</button>", page)
        self.assertNotIn('class="chips"', page)
        filtered = self.request("GET", "/?tab=runs&q=MAR&status=failed&kind=issue")[2]
        self.assertIn('data-count>2</span>', filtered)
        self.assertIn('href="/?tab=runs&amp;q=MAR" data-clear>Clear</a>', filtered)  # Clear keeps the search
        self.assertIn('href="/?tab=runs&amp;q=MAR&amp;kind=issue" data-nav aria-label="Remove filter Status: Failed"',
                      filtered)

    def test_finished_runs_are_selected_and_deleted_but_not_active_or_other_peoples(self):
        self.sign_in()
        csrf = self.csrf()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        other = add_developer(self.users, "acc-sara")
        add = lambda key, status, owner=uid: (lambda run: (self.runs.update(run, status), run)[1])(
            self.runs.add(key, owner, market.id, "marketplace"))
        done, failed, running, queued, hers = (add("MAR-1", "done"), add("MAR-2", "failed"), add("MAR-3", "running"),
                                               add("MAR-4", "queued"), add("MAR-5", "done", other))
        page = self.request("GET", "/")[2]
        for run in (done, failed):  # finished: a checkbox, bound to the delete form
            self.assertIn(f'<input type="checkbox" name="run_id" value="{run}" form="delete-runs"', page)
        for run in (running, queued):  # active: stopped first, not deleted
            self.assertNotIn(f'name="run_id" value="{run}" form="delete-runs"', page)
        self.assertIn('id="pick-all"', page)
        self.assertIn('action="/runs/delete"', page)

        self.assertIn("Select the runs to delete", self.request("POST", "/runs/delete", {"csrf": csrf})[2])
        self.assertIn("still queued or running",
                      self.request("POST", "/runs/delete", {"csrf": csrf, "run_id": [str(running), str(queued)]})[2])
        # Selected finished ones go, with junk and other people's ids ignored; active ones in the same request stay.
        form = {"csrf": csrf, "run_id": [str(done), str(failed), str(running), str(hers), "x", "../1"]}
        self.assertEqual(self.request("POST", "/runs/delete", form)[1], "/?ok=runs-deleted")
        self.assertEqual({r["id"] for r in self.runs.recent(uid)}, {running, queued})
        self.assertEqual([r["id"] for r in self.runs.recent(other)], [hers])
        self.assertIn("Selected runs removed", self.request("GET", "/?ok=runs-deleted")[2])
        # Forms need the session's CSRF token.
        self.request("POST", "/runs/delete", {"csrf": "forged", "run_id": [str(hers)]})
        self.assertEqual([r["id"] for r in self.runs.recent(other)], [hers])

    def test_using_shadowdev_renews_the_session_but_the_pages_own_refresh_does_not(self):
        self.sign_in()
        token = self.cookies["shadowdev_session"]

        def age(days_left):
            with sqlite3.connect(self.cfg.data_dir / "shadowdev.db") as db:
                db.execute("UPDATE sessions SET expires = ?", (time.time() + days_left * 86400,))

        self.request("GET", "/")
        self.assertEqual(self.set_cookies, [])  # signed in just now: nothing to renew
        age(2)
        self.assertEqual(self.request("GET", "/runs")[0], 200)
        self.assertEqual(self.set_cookies, [])  # an open tab alone does not keep the session alive
        self.request("GET", "/")
        self.assertIn(f"shadowdev_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800", self.set_cookies[0])
        self.request("GET", "/")
        self.assertEqual(self.set_cookies, [])  # renewed: a full week again
        age(2)
        self.request("POST", "/logout", {"csrf": self.csrf()})  # (csrf() renewed it) signing out still wins
        self.assertNotIn("shadowdev_session", self.cookies)
        age(-1)
        self.cookies["shadowdev_session"] = token
        self.assertEqual(self.request("GET", "/runs")[0], 401)

    def test_timing_and_limits_are_the_developers_own(self):
        self.sign_in()
        csrf = self.csrf()
        page = self.request("GET", "/")[2]
        self.assertIn("Timing and limits", page)
        self.assertIn('name="max_parallel_runs" value="5"', page)  # the defaults until changed
        self.assertIn('name="github_poll_minutes" value="1"', page)
        self.assertNotIn("sec<", page)  # every time is in minutes
        form = {"csrf": csrf, "jira_polling": "on", "jira_poll_minutes": "5", "github_poll_minutes": "0.5", "slack_poll_minutes": "1.5",
                "review_quiet_minutes": "1", "claude_timeout_minutes": "45", "command_timeout_minutes": "20",
                "max_parallel_runs": "2", "max_fix_attempts": "4"}
        self.assertEqual(self.request("POST", "/limits", form)[1], "/?ok=limits")
        self.assertEqual(self.me().limits, Limits(
            jira_polling=True, jira_poll_minutes=5, github_poll_minutes=0.5, slack_poll_minutes=1.5, review_quiet_minutes=1,
            claude_timeout_minutes=45, command_timeout_minutes=20, max_parallel_runs=2, max_fix_attempts=4))
        status, _, body = self.request("POST", "/limits", {**form, "max_parallel_runs": "50"})
        self.assertEqual(status, 400)
        self.assertIn("Runs at the same time must be a whole number from 1 to 20.", html.unescape(body))
        self.assertEqual(self.me().limits.max_parallel_runs, 2)  # unchanged
        self.request("POST", "/limits", {**form, "csrf": "forged", "max_parallel_runs": "1"})  # needs the session's CSRF token
        self.assertEqual(self.me().limits.max_parallel_runs, 2)

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
        self.assertIn("Ready for Release -&gt; Done: root cause, fix version, parent, original estimate, logged time", page)
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

    def test_pause_and_resume(self):
        self.sign_in()
        csrf = self.csrf()
        uid = self.me().id
        add_developer(self.users, ME, user_id=uid)  # office GitHub and Claude accounts
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        join(self.users, uid, market, ME)
        self.assertIn("Pause automation", self.request("GET", "/")[2])

        self.assertEqual(self.request("POST", "/pause", {"csrf": csrf})[1], "/?ok=paused")
        page = self.request("GET", "/")[2]
        self.assertIn("Paused: nothing runs for you", page)
        self.assertIn("Resume automation", page)
        self.assertTrue(self.me().paused)
        self.assertIn(market.id, self.me().subscriptions)  # settings kept
        self.assertIsNone(matching_issue(payload(), self.projects, self.users))  # webhooks ignored
        self.assertIn("automation is paused", self.request("POST", "/run", {"csrf": csrf, "issue_keys": "MAR-1"})[2])
        self.assertEqual(self.queued, [])

        self.assertEqual(self.request("POST", "/resume", {"csrf": csrf})[1], "/?ok=resumed")
        self.assertFalse(self.me().paused)
        self.assertIsNotNone(matching_issue(payload(), self.projects, self.users))
        # With "Start by itself" off in the project, new assignments wait for Run issues now.
        sub = self.me().subscriptions[market.id]
        self.users.subscribe(self.me().id, dataclasses.replace(sub, settings=Settings(auto_start=False)))
        self.assertIsNone(matching_issue(payload(), self.projects, self.users))


def fake_process_factory(record=None, block: threading.Event | None = None):
    def fake_process(keys, project, actor, cfg, jira, github, cancel=None, note="", on_model=None, on_pr=None, **kw):
        if record is not None:
            record.append((actor.display_name, tuple(keys), *([note] if note else [])))
        if on_model:
            on_model("haiku")
        if block:
            while not cancel.cancelled and not block.is_set():
                time.sleep(0.01)
            cancel.check()
        return "done", "ok"
    return fake_process


class QueueEnv(Env):
    def pool(self, fake_process, workers=5, start=True):
        pool = RunQueue(self.cfg, self.users, self.projects, self.runs, mock.Mock(), workers=workers)
        for p in (mock.patch.object(run_queue, "process_issues", fake_process), mock.patch.object(run_queue, "jira_client")):
            p.start()
            self.addCleanup(p.stop)
        if start:
            pool.start()
        return pool

    def wait_for(self, condition):
        for _ in range(250):
            if condition():
                return
            time.sleep(0.02)
        self.fail("timed out")


class RunQueueTest(QueueEnv):
    def test_runs_up_to_the_limit_in_parallel_and_never_one_issue_twice(self):
        me = self.developer()
        running, peak, lock, release = set(), [0], threading.Lock(), threading.Event()

        def fake_process(keys, project, actor, cfg, jira, github, cancel=None, note="", on_model=None, on_pr=None, **kw):
            with lock:
                running.add(tuple(keys))
                peak[0] = max(peak[0], len(running))
            release.wait(5)
            with lock:
                running.discard(tuple(keys))
            return "done", "ok"

        pool = self.pool(fake_process)
        for n in range(1, 7):
            self.assertIsNone(pool.submit(self.market, [f"MAR-{n}"], me))
        self.assertEqual(pool.submit(self.market, ["MAR-1"], me), "already queued or running: MAR-1")
        self.assertIn("MAR-2", pool.submit(self.market, ["MAR-7", "MAR-2"], me))  # MAR-2 is in another job
        self.wait_for(lambda: len(running) == 5)  # the sixth waits for a free slot
        release.set()
        done = lambda: all(r["status"] == "done" for r in self.runs.recent(me))
        self.wait_for(done)
        self.assertEqual(peak[0], 5)
        self.assertEqual(sorted(r["issue_key"] for r in self.runs.recent(me)), [f"MAR-{n}" for n in range(1, 7)])
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))  # finished, so it can run again
        self.wait_for(done)

    def test_each_developer_runs_up_to_their_own_limit(self):
        me, sara = self.developer(), self.developer("acc-sara")
        self.users.set_limits(me, Limits(max_parallel_runs=2))
        running, release = [], threading.Event()

        def fake_process(keys, project, actor, cfg, jira, github, **kw):
            running.append((actor.jira_account_id, keys[0], cfg.max_fix_attempts))
            release.wait(5)
            return "done", "ok"

        pool = self.pool(fake_process, start=False)
        for n in range(1, 5):
            pool.submit(self.market, [f"MAR-{n}"], me)
        pool.submit(self.market, ["MAR-9"], sara)
        pool.start()
        self.wait_for(lambda: len(running) == 3)
        time.sleep(0.1)
        self.assertEqual(sorted(running), [(ME, "MAR-1", 3), (ME, "MAR-2", 3), ("acc-sara", "MAR-9", 3)])
        self.users.set_limits(me, Limits(max_parallel_runs=3, max_fix_attempts=5))
        pool.limits_changed()  # as AccountService.set_limits does: waiting runs see the new limit
        self.wait_for(lambda: len(running) == 4)
        self.assertEqual(running[-1], (ME, "MAR-3", 5))  # a run gets its developer's limits when it starts
        release.set()
        self.wait_for(lambda: all(r["status"] == "done" for r in self.runs.recent(me)))

    def test_a_finished_run_logs_its_end(self):
        me = self.developer()
        pool = self.pool(fake_process_factory(), start=False)
        with self.assertLogs("shadowdev", "INFO") as logs:
            pool.submit(self.market, ["MAR-1", "MAR-2"], me)
            pool.start()
            self.wait_for(lambda: any("===== END =====" in line for line in logs.output))
        self.assertRegex("\n".join(logs.output), r"===== END ===== run \d+ \(MAR-1, MAR-2\) finished after [\d.ms]+")

    def test_the_same_key_on_two_sites_runs_at_once(self):
        me = self.developer()
        add_developer(self.users, ME_HOME, sites=(HOME,), user_id=me)
        side = add_project(self.projects, me, "side", ["MAR"], site=HOME)
        join(self.users, me, side, ME_HOME)
        pool = self.pool(fake_process_factory(block=threading.Event()), start=False)
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.assertIsNone(pool.submit(side, ["MAR-1"], me))

    def test_developers_take_turns(self):
        me, sara = self.developer(), self.developer("acc-sara")
        order = []
        pool = self.pool(fake_process_factory(order), workers=1, start=False)
        for n in (1, 2, 3):
            pool.submit(self.market, [f"MAR-{n}"], me)
        pool.submit(self.market, ["MAR-9"], sara)
        pool.start()
        self.wait_for(lambda: len(order) == 4)
        self.assertEqual([keys[0] for _, keys in order], ["MAR-1", "MAR-9", "MAR-2", "MAR-3"])

    def test_queued_runs_survive_a_restart(self):
        me = self.developer()
        waiting = self.runs.add("MAR-1, MAR-2", me, self.market.id, "marketplace", "Only the API.")  # left by the previous server
        gone = self.runs.add("MAR-3", me, None, "deleted project")
        ran = []
        self.pool(fake_process_factory(ran))
        self.wait_for(lambda: ran)
        self.assertEqual(ran, [(ME, ("MAR-1", "MAR-2"), "Only the API.")])  # with its note
        status = {r["id"]: (r["status"], r["message"]) for r in self.runs.recent(me)}
        self.wait_for(lambda: self.runs.recent(me)[-1]["status"] == "done")
        self.assertEqual(self.runs.recent(me)[-1]["model"], "haiku")  # the run reports its model
        self.assertEqual(status[gone][0], "failed")
        self.assertIn("project was deleted", status[gone][1])
        self.assertIn(waiting, status)

    def test_work_on_a_pull_request_runs_in_the_queue_and_is_not_resumed_after_a_restart(self):
        me = self.developer()
        left = self.runs.add("acme/fe#3", me, self.market.id, "marketplace")  # queued when the server stopped
        pool = self.pool(fake_process_factory())
        self.assertIn("starts again by itself", next(r for r in self.runs.recent(me) if r["id"] == left)["message"])
        seen, ended = [], []

        def work(project, actor, cfg, github, cancel, on_model):
            seen.append((project.name, actor.display_name, cancel.cancelled))
            on_model("default")
            return "done", "approved https://github.com/acme/fe/pull/7"

        self.assertIsNone(pool.submit(self.market, ["acme/fe#7"], me, work=work, after=lambda: ended.append("review")))
        self.assertEqual(pool.submit(self.market, ["acme/fe#7"], me, work=work), "already queued or running: acme/fe#7")
        self.wait_for(lambda: ended)
        run = self.runs.recent(me)[0]
        self.assertEqual((seen, run["issue_key"], run["status"], run["model"]),
                         ([("marketplace", ME, False)], "acme/fe#7", "done", "default"))
        # One that never starts (its developer paused) still ends, so what asked for it is told.
        self.users.set_paused(me, True)
        self.assertIsNone(pool.submit(self.market, ["acme/fe#8"], me, work=work, after=lambda: ended.append("skipped")))
        self.wait_for(lambda: ended == ["review", "skipped"])
        self.assertEqual(len(seen), 1)

    def test_runs_use_the_project_and_accounts_as_they_are_when_they_start(self):
        me = self.developer()
        seen = []
        pool = self.pool(lambda keys, project, actor, *a, **k: seen.append((project.name, actor.github.login)) or ("done", ""),
                         start=False)
        pool.submit(self.market, ["MAR-1"], me)
        self.users.remove_github(me, self.users.get(me).github_accounts[0].id)  # before it starts
        pool.start()
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "skipped")
        self.assertEqual(seen, [])
        self.assertIn("GitHub or Claude account was removed", self.runs.recent(me)[0]["message"])


class ClaudeLimitTest(QueueEnv):
    def setUp(self):
        super().setUp()
        limits.USAGE.clear()
        self.addCleanup(limits.USAGE.clear)
        patcher = mock.patch.object(limits, "POLL", 0.02)
        patcher.start()
        self.addCleanup(patcher.stop)

    def nearly_used(self, me, resets_in: float, label: str | None = None):
        """The developer's Claude account `label` (default: the project's own) is at 98% of its 5-hour limit."""
        user = self.users.get(me)
        claude = next((c for c in user.claude_accounts if c.label == label), user.actor(self.market.id).claude)
        limits.USAGE.record(str(claude_config_dir(self.cfg, claude)), {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.98, "resetsAt": time.time() + resets_in}}})

    def second_account(self, me, switch: bool):
        """A personal Claude account besides the office one the project runs with, and the switch."""
        self.users.save_claude(me, "personal", "sk-ant-oat01-y", "key-personal")
        sub = self.users.get(me).subscriptions[self.market.id]
        self.users.subscribe(me, dataclasses.replace(sub, settings=Settings(switch_claude=switch)))

    def worked_with(self, me) -> list[str]:
        """The label of the Claude account each run worked with, once the developer's runs are done."""
        labels = []

        def fake_process(keys, project, actor, cfg, jira, github, **kw):
            labels.append(actor.claude.label)
            return "done", "ok"

        pool = self.pool(fake_process)
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        return labels

    def test_with_switching_on_a_run_starts_on_another_claude_account_and_the_page_says_which_is_active(self):
        me = self.developer()
        self.second_account(me, switch=True)
        self.nearly_used(me, 3600)
        labels = self.worked_with(me)
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done")
        self.assertEqual(labels, ["personal"])
        user = self.users.get(me)
        usage = accounts.AccountService(self.cfg, self.users, mock.Mock(), mock.Mock()).claude_usage(user)
        page = home_page(self.cfg, user, [self.market], {}, self.runs.search(me, RunFilter()), "csrf", "nonce",
                         claude_usage=usage)
        self.assertIn(f"Active Claude account: <b>personal</b>. {ME} is at its limit: 98% of the 5-hour Claude limit is used",
                      page)
        self.assertIn('<small class="usage"><span class="tag warn">at its limit</span> 98% of the 5-hour Claude limit is used',
                      page)  # on the Accounts tab

    def test_with_switching_off_the_run_waits_for_its_own_claude_account(self):
        me = self.developer()
        self.second_account(me, switch=False)
        self.nearly_used(me, 0.5)
        labels = self.worked_with(me)
        self.wait_for(lambda: "not started yet" in self.runs.recent(me)[0]["message"])
        self.assertEqual(labels, [])
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done")
        self.assertEqual(labels, [ME])  # its own account, after the reset

    def test_with_every_claude_account_at_its_limit_the_run_waits_and_takes_the_first_to_reset(self):
        me = self.developer()
        self.second_account(me, switch=True)
        self.nearly_used(me, 3600)
        self.nearly_used(me, 0.5, "personal")
        labels = self.worked_with(me)
        self.wait_for(lambda: "your other Claude accounts are at their limits too" in self.runs.recent(me)[0]["message"])
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done")
        self.assertEqual(labels, ["personal"])

    def test_a_run_waits_queued_while_the_claude_limit_is_nearly_used_and_starts_after_the_reset(self):
        me, started = self.developer(), []
        self.nearly_used(me, 0.5)
        pool = self.pool(fake_process_factory(started))
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.wait_for(lambda: "98% of the 5-hour Claude limit is used" in self.runs.recent(me)[0]["message"])
        self.assertEqual((self.runs.recent(me)[0]["status"], started), ("queued", []))  # nothing cloned, nothing asked
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done")
        self.assertEqual(len(started), 1)

    def test_work_that_asks_claude_nothing_is_not_held(self):
        me = self.developer()
        self.nearly_used(me, 3600)
        pool = self.pool(fake_process_factory())
        self.assertIsNone(pool.submit(self.market, ["acme/fe#1"], me, work=lambda *a, **kw: ("done", "approved"),
                                      claude=False))
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done")

    def test_a_run_held_by_the_claude_limit_can_be_stopped(self):
        me, started = self.developer(), []
        self.nearly_used(me, 3600)
        pool = self.pool(fake_process_factory(started))
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.wait_for(lambda: "not started yet" in self.runs.recent(me)[0]["message"])
        self.assertTrue(pool.stop_run(self.runs.recent(me)[0]["id"], me))
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "stopped")
        self.assertEqual(started, [])


class BatchTest(QueueEnv):
    def opening(self, prs: dict[str, list[str]], seen: list):
        """A pipeline that opens the given PRs (repo names) per issue key, and hands them over."""
        def fake_process(keys, project, actor, cfg, jira, github, only=(), announce=None, **kw):
            seen.append((keys[0], only, bool(announce)))
            opened = [(project.repo(name), f"https://github.com/{project.repo(name).github}/pull/{keys[0][-1]}")
                      for name in prs.get(keys[0], [])]
            if opened and announce:
                announce(f"{keys[0]}: a bug", opened, keys[0])
            return ("done", "ok") if opened else ("failed", "Claude made no changes")
        return fake_process

    def test_runs_started_together_get_one_review_request_once_the_last_has_ended(self):
        me, seen = self.developer(), []
        pool = self.pool(self.opening({"MAR-1": ["fe"], "MAR-2": ["fe", "services"]}, seen), start=False)
        for key in ("MAR-1", "MAR-2", "MAR-3"):
            self.assertIsNone(pool.submit(self.market, [key], me, repos=("fe", "gone"), batch=7))
        with mock.patch.object(run_queue, "ask_for_reviews", return_value=("C0REVIEWS1", "9.9")) as ask, \
             mock.patch.object(run_queue, "ask_for_review") as ask_one:
            pool.start()
            self.wait_for(lambda: ask.called)
        ask.assert_called_once()  # MAR-3 failed: it is left out
        ask_one.assert_not_called()
        self.assertEqual([(title, repo.name, url.rsplit("/", 3)[1:]) for title, repo, url in ask.call_args.args[2]],
                         [("MAR-1: a bug", "fe", ["fe", "pull", "1"]), ("MAR-2: a bug", "fe", ["fe", "pull", "2"]),
                          ("MAR-2: a bug", "services", ["services", "pull", "2"])])
        self.assertEqual(sorted(seen), [(f"MAR-{n}", ("fe",), True) for n in (1, 2, 3)])  # only the project's own repos
        self.assertEqual(self.runs.batches(), [])  # asked once

    def test_a_batch_with_one_run_that_opened_prs_asks_as_for_a_run_by_itself(self):
        me, seen = self.developer(), []
        pool = self.pool(self.opening({"MAR-2": ["fe"]}, seen), start=False)
        for key in ("MAR-1", "MAR-2"):
            self.assertIsNone(pool.submit(self.market, [key], me, batch=8))
        with mock.patch.object(run_queue, "ask_for_reviews") as ask, mock.patch.object(run_queue, "ask_for_review") as ask_one:
            pool.start()
            self.wait_for(lambda: ask_one.called)
        ask.assert_not_called()
        self.assertEqual(ask_one.call_args.args[2], "MAR-2: a bug")
        self.assertEqual(sorted(seen), [("MAR-1", (), True), ("MAR-2", (), True)])


class NeverRunTest(QueueEnv):
    def test_work_found_only_by_its_sprint_starts_only_if_never_run_before(self):
        me = self.developer()
        pool = self.pool(fake_process_factory([]))
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me, automatic=True, never_run=True))
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done")
        self.assertEqual(pool.submit(self.market, ["MAR-1"], me, automatic=True, never_run=True), "already run before")
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me, automatic=True))  # updated since: new work
        self.wait_for(lambda: all(r["status"] == "done" for r in self.runs.recent(me)))


class SequenceTest(QueueEnv):
    def test_issues_run_one_after_another_each_after_the_delay(self):
        me, events, release = self.developer(), [], threading.Event()

        def fake_process(keys, project, actor, cfg, jira, github, **kw):
            events.append(("start", keys[0], time.monotonic()))
            if keys[0] == "MAR-1":
                release.wait(5)
            events.append(("end", keys[0], time.monotonic()))
            if keys[0] == "MAR-2":
                raise RuntimeError("tests still failing")  # a failed one does not stop the sequence
            return "done", "ok"

        pool = self.pool(fake_process)
        items = [(self.market, [f"MAR-{n}"], "", (), None) for n in (1, 2, 3)]
        self.assertEqual(pool.submit_sequence(items, me, 0.3), [None, None, None])
        self.wait_for(lambda: events)
        waiting = {r["issue_key"]: (r["status"], r["message"]) for r in self.runs.recent(me)}
        self.assertEqual(waiting["MAR-2"], ("queued", "starts after the issue before it, 0.3s later"))
        self.assertEqual([e[:2] for e in events], [("start", "MAR-1")])  # the others wait for it
        release.set()
        self.wait_for(lambda: all(r["status"] in ("done", "failed") for r in self.runs.recent(me)))
        self.assertEqual([e[:2] for e in events], [(what, f"MAR-{n}") for n in (1, 2, 3) for what in ("start", "end")])
        times = {e[:2]: e[2] for e in events}
        for before, after in (("MAR-1", "MAR-2"), ("MAR-2", "MAR-3")):
            self.assertGreaterEqual(times["start", after] - times["end", before], 0.3)
        self.assertEqual(sorted(r["status"] for r in self.runs.recent(me)), ["done", "done", "failed"])

    def test_an_issue_waiting_for_its_turn_can_be_stopped_and_the_next_still_runs(self):
        me, started, release = self.developer(), [], threading.Event()
        pool = self.pool(fake_process_factory(started, block=release))
        items = [(self.market, [f"MAR-{n}"], "", (), None) for n in (1, 2, 3)]
        self.assertEqual(pool.submit_sequence(items, me, 0), [None, None, None])
        self.wait_for(lambda: started)
        second = next(r["id"] for r in self.runs.recent(me) if r["issue_key"] == "MAR-2")
        self.assertTrue(pool.stop_run(second, me))
        self.wait_for(lambda: next(r for r in self.runs.recent(me) if r["id"] == second)["status"] == "stopped")
        release.set()
        self.wait_for(lambda: all(r["status"] in ("done", "stopped") for r in self.runs.recent(me)))
        self.assertEqual([keys for _, keys in started], [("MAR-1",), ("MAR-3",)])

    def test_a_sequence_goes_on_after_a_restart(self):
        me = self.developer()
        first = self.runs.add("MAR-1", me, self.market.id, "marketplace")
        self.runs.add("MAR-2", me, self.market.id, "marketplace", follows=first, delay=0.2)
        started = []
        pool = self.pool(fake_process_factory(started))  # start() queues again what was waiting
        self.wait_for(lambda: all(r["status"] == "done" for r in self.runs.recent(me)))
        self.assertEqual([keys for _, keys in started], [("MAR-1",), ("MAR-2",)])
        self.assertIsNotNone(pool)


class StopAndPauseTest(QueueEnv):
    def test_stop_runs_stops_a_run_in_progress(self):
        me = self.developer()
        started = []
        pool = self.pool(fake_process_factory(started, block=threading.Event()))
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.wait_for(lambda: started)
        self.assertEqual(pool.stop_runs(me), 1)
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "stopped")
        self.assertEqual(pool.stop_runs(me), 0)

    def test_a_failed_or_stopped_issue_is_not_started_again_automatically(self):
        # Its failure comment updates the issue, which a webhook or the poller would take for new work.
        me, sara = self.developer(), self.developer("acc-sara")

        def failing(keys, project, actor, cfg, jira, github, cancel=None, note="", on_model=None, on_pr=None, **kw):
            raise RuntimeError("tests still failing")

        pool = self.pool(failing)
        self.assertIsNone(pool.submit(self.market, ["MAR-1", "MAR-2"], me, automatic=True))
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "failed")
        self.wait_for(lambda: pool.submit(self.market, ["MAR-3"], me) is None)  # the worker is free again
        self.assertIn("the last run of MAR-2 failed", pool.submit(self.market, ["MAR-2"], me, automatic=True))
        self.assertIsNone(pool.submit(self.market, ["MAR-22"], me, automatic=True))  # another issue
        self.assertIsNone(pool.submit(self.market, ["MAR-2"], me))  # by hand
        # Reassigned: it is new work for the other developer (once this developer's run is over).
        self.wait_for(lambda: pool.submit(self.market, ["MAR-2"], sara, automatic=True) is None)
        self.wait_for(lambda: all(r["status"] == "failed" for u in (me, sara) for r in self.runs.recent(u)))
        # One that asked the developer waits for their note, not for the ticket to change.
        run = self.runs.add("MAR-7", me, self.market.id, self.market.name)
        self.runs.update(run, "question", "ShadowDev needs your decision…")
        self.assertIn("the last run of MAR-7 asked you questions", pool.submit(self.market, ["MAR-7"], me, automatic=True))

    def test_questions_for_the_developer_come_back_with_their_answer(self):
        me = self.developer()
        seen = []

        def asking(keys, project, actor, cfg, jira, github, cancel=None, note="", on_model=None, on_pr=None, asked=None,
                   on_questions=None, **kw):
            seen.append((note, asked))
            if not note:
                on_questions({"MAR-1": ["Keep the old API?"]})
                return "question", "ShadowDev needs your decision…"
            return "done", "ok"

        pool = self.pool(asking)
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "question")
        self.assertEqual(self.runs.asked(me, self.market.id, ["MAR-1", "MAR-2"]), {"MAR-1": ["Keep the old API?"]})
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me, note="MAR-1: yes, keep it."))
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done")
        self.assertEqual(seen, [("", {}), ("MAR-1: yes, keep it.", {"MAR-1": ["Keep the old API?"]})])
        self.assertEqual(self.runs.asked(me, self.market.id, ["MAR-1"]), {})  # answered: the latest run is done

    def test_a_developer_stops_one_of_their_runs(self):
        me, sara = self.developer(), self.developer("acc-sara")
        started = []
        pool = self.pool(fake_process_factory(started, block=threading.Event()), workers=1)
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.assertIsNone(pool.submit(self.market, ["MAR-2"], me))  # waits behind MAR-1
        self.wait_for(lambda: started)
        running, queued = sorted(r["id"] for r in self.runs.recent(me))
        self.assertFalse(pool.stop_run(running, sara))  # not hers
        self.assertTrue(pool.stop_run(queued, me))  # never starts
        self.assertTrue(pool.stop_run(running, me))
        status = lambda run_id: next(r for r in self.runs.recent(me) if r["id"] == run_id)
        self.wait_for(lambda: status(running)["status"] == "stopped" and status(queued)["status"] == "stopped")
        self.assertEqual(status(queued)["message"], "stopped before it started")
        self.assertIn("the ticket was left as it was", status(running)["message"])
        self.assertFalse(pool.stop_run(running, me))  # finished
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))  # can run again
        again = max(r["id"] for r in self.runs.recent(me))
        self.assertTrue(pool.stop_run(again, me))
        self.wait_for(lambda: status(again)["status"] == "stopped")

    def test_stopping_a_projects_runs(self):
        me = self.developer()
        pool = self.pool(fake_process_factory(), start=False)
        pool.submit(self.market, ["MAR-1"], me)
        self.assertEqual(pool.stop_runs(project_ids={self.market.id + 1}), 0)
        self.assertEqual(pool.stop_runs(project_ids={self.market.id}), 1)

    def test_a_paused_developers_queued_runs_are_skipped(self):
        me = self.developer()
        self.users.set_paused(me, True)
        ran = []
        pool = self.pool(fake_process_factory(ran))
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "skipped")
        self.assertEqual((ran, self.runs.recent(me)[0]["message"]), ([], "automation is paused"))


class LimitsTest(Env):
    def test_a_developer_can_have_at_most_ten_runs_queued(self):
        me = self.developer()
        pool = RunQueue(self.cfg, self.users, self.projects, self.runs, mock.Mock())  # not started: all stay queued
        for n in range(1, 11):
            self.assertIsNone(pool.submit(self.market, [f"MAR-{n}"], me))
        self.assertEqual(pool.submit(self.market, ["MAR-11"], me), "you already have 10 runs queued or running")
        self.assertIsNone(pool.submit(self.market, ["MAR-11"], self.developer("acc-sara")))  # others are not affected

    def test_webhooks_are_signed_fresh_and_accepted_once_also_across_restarts(self):
        self.developer()
        queued = []
        pool = mock.Mock(submit=lambda project, keys, uid, **kw: queued.append((project.name, keys[0])))

        def serve():
            server = Server(("127.0.0.1", 0), make_handler(build_app(self.cfg, mock.Mock(), pool)))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            return server.server_address[1]

        def send(port, body: dict, path="/webhook", secret="hook-secret"):
            raw = json.dumps(body).encode()
            sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("POST", path, raw, {"X-Hub-Signature": sig, "Content-Type": "application/json"})
            resp = conn.getresponse()
            result = resp.status, json.loads(resp.read())["status"]
            conn.close()
            return result

        port = serve()
        now = int(time.time() * 1000)
        event = {**payload(), "timestamp": now}
        self.assertEqual(send(port, {**payload()}), (400, "stale or missing timestamp"))
        self.assertEqual(send(port, {**payload(), "timestamp": now - 2 * 3600 * 1000}), (400, "stale or missing timestamp"))
        self.assertEqual(send(port, event), (202, "queued"))
        self.assertEqual(send(port, event), (200, "duplicate event"))  # a replay of the same signed event
        self.assertEqual(send(serve(), event), (200, "duplicate event"))  # also after a restart

        # Per project: its own URL and secret; the shared secret, or another project's, doesn't work there.
        secret = self.projects.webhook_secret(self.market.id)
        hook = f"/webhook?project={self.market.id}"
        event2 = {**payload(key="MAR-13", site=None), "timestamp": now}
        self.assertEqual(send(port, event2, hook), (401, "bad signature"))
        self.assertEqual(send(port, event2, "/webhook?project=999", secret), (401, "bad signature"))
        self.assertEqual(send(port, event2, hook, secret), (202, "queued"))
        self.assertEqual(queued, [("marketplace", "MAR-12"), ("marketplace", "MAR-13")])

    def test_without_a_shared_secret_only_project_webhooks_work(self):
        cfg = Config(**{**self.cfg.__dict__, "webhook_secret": None})
        status, _ = build_app(cfg, mock.Mock(), mock.Mock()).webhooks.handle(b"{}", "sha256=x")
        self.assertEqual(status, 401)

    def test_config(self):
        env = {"JIRA_SITES": "https://office.atlassian.net, https://home.atlassian.net/", "ATLASSIAN_CLIENT_ID": "a",
               "ATLASSIAN_CLIENT_SECRET": "b", "SHADOWDEV_SECRET_KEY": "d"}
        cfg = Config.from_env(env)
        self.assertEqual((cfg.jira_sites, cfg.webhook_secret), ((OFFICE.url, HOME.url), None))
        self.assertTrue(cfg.allows_site(HOME.url + "/"))
        self.assertFalse(cfg.allows_site("https://other.atlassian.net"))
        legacy = {**env, "JIRA_SITES": "", "JIRA_BASE_URL": "https://acme.atlassian.net"}
        self.assertEqual(Config.from_env(legacy).jira_sites, ("https://acme.atlassian.net",))
        self.assertTrue(Config.from_env({**env, "JIRA_SITES": "*"}).allows_site("https://anything.atlassian.net"))
        with self.assertRaisesRegex(SystemExit, "must look like"):
            Config.from_env({**env, "JIRA_SITES": "http://insecure.example"})
        with self.assertRaisesRegex(SystemExit, "must be https"):
            Config.from_env({**env, "PUBLIC_URL": "http://shadowdev.internal:8080"})
        self.assertEqual(Config.from_env({**env, "PUBLIC_URL": "http://localhost:8080"}).public_url, "http://localhost:8080")
        self.assertEqual(Config.from_env({**env, "PUBLIC_URL": "https://shadowdev.example.com"}).public_url, "https://shadowdev.example.com")


if __name__ == "__main__":
    unittest.main()


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


class OperationsWebTest(WebTest):
    """The run log, health check, rate limits, token warnings and activity, through the real server."""

    def test_a_run_keeps_its_log_and_cost_for_its_developer_only(self):
        self.sign_in()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        run_id = self.runs.add("MAR-1", uid, market.id, "marketplace")
        self.runs.update(run_id, "done", "ok")
        self.runs.set_record(run_id, 125, 0.4321, 12, "10:00:00 MAR-1: Claude analysing\n10:01:00 <b>tests</b> pass")
        page = self.request("GET", "/")[2]
        self.assertIn(f"Took: 2m05s · 12 Claude turns · $0.43 as Claude counts it · "
                      f'<a href="/runs/log?run={run_id}"', page)
        status, _, text = self.request("GET", f"/runs/log?run={run_id}")
        self.assertEqual((status, self.headers["Content-Type"], self.headers["X-Content-Type-Options"]),
                         (200, "text/plain; charset=utf-8", "nosniff"))
        self.assertEqual(text, "10:00:00 MAR-1: Claude analysing\n10:01:00 <b>tests</b> pass")
        sara = add_developer(self.users, "acc-sara")
        theirs = self.runs.add("MAR-2", sara, market.id, "marketplace")
        self.runs.set_record(theirs, 1, 0, 0, "secret of sara")
        for path in (f"/runs/log?run={theirs}", "/runs/log?run=x", "/runs/log"):
            self.assertEqual(self.request("GET", path)[0], 404)
        self.runs.delete_finished(uid, [run_id])  # its log goes with it
        self.assertEqual(self.request("GET", f"/runs/log?run={run_id}")[0], 404)
        self.cookies.clear()
        self.assertEqual(self.request("GET", f"/runs/log?run={theirs}")[0], 404)  # signed out

    def test_health_says_only_ok(self):
        self.assertEqual(self.request("GET", "/health"), (200, None, '{"status": "ok"}'))

    def test_sign_in_and_webhook_are_rate_limited_per_client(self):
        for _ in range(30):
            self.assertEqual(self.request("GET", "/login/jira")[0], 303)
        status, _, body = self.request("GET", "/login/jira")
        self.assertEqual((status, body), (429, '{"status": "too many requests"}'))
        self.assertTrue(1 <= int(self.headers["Retry-After"]) <= 60)
        # Behind a proxy on this machine, each client is counted by the address the proxy gives.
        self.assertEqual(self.request("GET", "/login/jira", headers={"X-Forwarded-For": "203.0.113.9"})[0], 303)
        self.assertEqual(self.request("GET", "/health")[0], 200)  # other routes are not limited

    def test_lost_jira_access_and_expiring_github_tokens_are_pointed_out(self):
        self.sign_in()
        me = self.me()
        self.users.mark_jira_lost(me.jira_accounts[0].id)
        self.github.user_and_expiry.side_effect = lambda: (self.github.user(), time.time() + 3 * 86400 + 60)
        self.request("POST", "/github", {"csrf": self.csrf(), "github_token": "ghp_x"})
        page = self.request("GET", "/")[2]
        self.assertIn("Jira access of Majid was lost (expired or revoked): connect that account again under Accounts.", page)
        self.assertIn("The GitHub token expires in 3 days (majid): save a new token under Accounts.", page)
        self.assertIn('<span class="tag warn">access lost: connect it again</span>', page)
        self.oauth_round_trip(self.connect())  # connecting it again mends it
        self.assertFalse(self.me().jira_accounts[0].lost)

    def test_what_is_done_to_the_account_is_listed_without_secrets(self):
        self.sign_in()
        csrf = self.csrf()
        self.request("POST", "/github", {"csrf": csrf, "github_token": "ghp_secret", "github_label": "office"})
        self.request("POST", "/pause", {"csrf": csrf})
        activity = [what for _, what, _ in self.users.activity(self.me().id)]
        self.assertEqual(activity, ["Paused automation", "Saved a GitHub token", "Signed in"])
        page = self.request("GET", "/?tab=settings")[2]
        self.assertIn("Saved a GitHub token <span class=muted>majid</span>", page)
        self.assertNotIn("ghp_secret", page)


class ShutdownTest(QueueEnv):
    def test_stopping_lets_runs_finish_and_leaves_the_queued_ones_for_the_next_start(self):
        me = self.developer()
        started, release = threading.Event(), threading.Event()

        def fake_process(keys, project, actor, cfg, jira, github, cancel=None, **kw):
            started.set()
            release.wait(5)
            return "done", "ok"

        pool = self.pool(fake_process, workers=1)
        pool.submit(self.market, ["MAR-1"], me)
        started.wait(5)
        pool.submit(self.market, ["MAR-2"], me)
        threading.Timer(0.3, release.set).start()
        self.assertEqual(pool.drain(30, threading.Event()), 0)  # MAR-1 finished by itself
        statuses = {r["issue_key"]: r["status"] for r in self.runs.recent(me)}
        self.assertEqual(statuses, {"MAR-1": "done", "MAR-2": "queued"})  # resumed at the next start
        self.assertEqual([r["issue_key"] for r in self.runs.resume_after_restart()], ["MAR-2"])

    def test_runs_still_working_when_time_is_up_are_stopped(self):
        me = self.developer()
        started = threading.Event()

        def fake_process(keys, project, actor, cfg, jira, github, cancel=None, **kw):
            started.set()
            while True:
                cancel.wait(0.05)

        pool = self.pool(fake_process, workers=1)
        pool.submit(self.market, ["MAR-1"], me)
        started.wait(5)
        hurry = threading.Event()
        hurry.set()  # stopped a second time: no waiting
        self.assertEqual(pool.drain(600, hurry), 1)
        self.assertEqual(self.runs.recent(me)[0]["status"], "stopped")

    def test_a_run_records_what_it_logged_and_what_claude_reported(self):
        me = self.developer()
        logger = logging.getLogger("shadowdev")  # at INFO, as the server sets it up
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.INFO)

        def fake_process(keys, project, actor, cfg, jira, github, cancel=None, **kw):
            logging.getLogger("shadowdev").info("%s: Claude analysing", keys[0])
            runlog.add_usage(0.25, 4)
            runlog.add_usage(0.5, 6)
            return "done", "ok"

        self.pool(fake_process).submit(self.market, ["MAR-1"], me)
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done" and self.runs.recent(me)[0]["has_log"])
        run = self.runs.recent(me)[0]
        self.assertEqual((run["cost"], run["turns"]), (0.75, 10))
        self.assertGreater(run["seconds"], 0)
        self.assertRegex(self.runs.log(me, run["id"]), r"\d\d:\d\d:\d\d MAR-1: Claude analysing")
        self.assertIsNone(self.runs.log(me + 1, run["id"]))


class RateLimiterTest(unittest.TestCase):
    def test_counts_per_client_route_and_minute(self):
        limiter = RateLimiter({"/x": 2})
        self.assertEqual([limiter.retry_after("a", "/x", 120.0) for _ in range(3)], [0, 0, 60])
        self.assertEqual(limiter.retry_after("b", "/x", 130.0), 0)  # another client
        self.assertEqual(limiter.retry_after("a", "/y", 130.0), 0)  # a route without a limit
        self.assertEqual(limiter.retry_after("a", "/x", 175.0), 5)  # still that minute
        self.assertEqual(limiter.retry_after("a", "/x", 180.0), 0)  # the next one


class GitHubExpiryTest(unittest.TestCase):
    def test_reads_githubs_expiration_header(self):
        self.assertEqual(github_expiry("2030-01-02 03:04:05 UTC"), 1893553445.0)
        self.assertEqual(github_expiry("2030-01-01 20:04:05 -0700"), 1893553445.0)
        self.assertEqual((github_expiry(None), github_expiry("soon")), (0, 0))

    def test_warns_a_week_ahead(self):
        account = lambda expires: GitHubAccount(1, "office", "majid", "t", "M", "m@x", expires)
        now = 1_000_000.0
        self.assertEqual(account(0).expiry_warning(now), "")  # never expires
        self.assertEqual(account(now + 30 * 86400).expiry_warning(now), "")
        self.assertEqual(account(now + 86400 + 5).expiry_warning(now), "token expires in 1 day")
        self.assertEqual(account(now + 3600).expiry_warning(now), "token expires today")
        self.assertEqual(account(now - 1).expiry_warning(now), "token expired")
        self.assertNotIn("'t'", repr(account(0)))  # the token is never in its repr
