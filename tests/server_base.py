"""Base classes for the server tests: the real HTTP server, a signed-in developer, and the run queue
with the pipeline faked. The tests themselves are in tests/test_server_*.py."""
import html
import http.client
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

from shadowdev.app import build_app, database
from shadowdev.models.tokens import Tokens
from shadowdev.repositories.projects import ProjectRepository
from shadowdev.repositories.runs import RunRepository
from shadowdev.repositories.sessions import SessionRepository
from shadowdev.repositories.users import UserRepository
from shadowdev.server import Server, make_handler
from shadowdev.services import accounts, run_queue, runs
from shadowdev.services import projects as project_service
from shadowdev.services.run_queue import RunQueue
from tests.helpers import HOME, OFFICE, add_developer, add_project, join, make_cfg

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

    def every_repo(self) -> list[str]:
        """The run form's value ticking every repo of every project: Claude decides what to change."""
        return [f"{p.id}:{r.name}" for p in self.projects.on_sites([OFFICE.cloud_id, HOME.cloud_id]) for r in p.repos]

    def run_form(self, csrf: str, form: dict):
        """POST /run with every repo ticked, unless `form` says which."""
        return self.request("POST", "/run", {"csrf": csrf, "repos": self.every_repo(), **form})

    def developer(self, account_id=ME, mode="bugs", project=None):
        """A developer in the office project (created on first use), with office accounts."""
        uid = add_developer(self.users, account_id)
        self.market = getattr(self, "market", None) or add_project(self.projects, uid, "marketplace", ["MAR", "MARS"])
        join(self.users, uid, project or self.market, account_id, mode)
        return uid


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
        self.pool = mock.Mock(submit=submit, submit_sequence=submit_sequence, log=self.runs.log)
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
        self.jira.project_statuses.return_value = ["To Do", "In Progress", "In Review", "Merged", "In QA",
                                                   "Ready for Release", "Done"]
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

