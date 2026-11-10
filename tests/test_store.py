import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from cryptography.fernet import Fernet

from shadowdev.models.tokens import Tokens
from shadowdev.models.user import Subscription
from shadowdev.repositories.database import Database
from shadowdev.repositories.projects import ProjectRepository, Taken
from shadowdev.repositories import runs as runs_repository
from shadowdev.repositories.runs import RunRepository
from shadowdev.repositories.sessions import SessionRepository
from shadowdev.repositories.users import AccountInUse, UserRepository
from tests.helpers import HOME, OFFICE, add_developer, add_project, join, repo


class RepositoriesTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "data" / "shadowdev.db"
        self.key = Fernet.generate_key().decode()
        db = Database(self.path, self.key)
        self.users, self.sessions, self.runs = UserRepository(db), SessionRepository(db), RunRepository(db)
        self.projects = ProjectRepository(db)
        self.me = self.users.save_jira_account(None, "acc-1", "Majid", "m@x", [OFFICE],
                                               Tokens("jira-access-token", "jira-refresh-token", 123.0))

    def test_tokens_are_encrypted_at_rest(self):
        self.users.save_github(self.me, "office", "github-secret-token", "majid", "Majid", "m@x")
        self.users.save_claude(self.me, "office", "sk-ant-oat01-secret", "k1")
        project = add_project(self.projects, self.me, "market", ["MAR"])
        raw = b"".join(p.read_bytes() for p in self.path.parent.iterdir())
        for secret in (b"jira-access-token", b"jira-refresh-token", b"github-secret-token", b"sk-ant-oat01-secret",
                       self.projects.webhook_secret(project.id).encode()):
            self.assertNotIn(secret, raw)
        user = self.users.get(self.me)
        self.assertEqual((user.github_accounts[0].token, user.claude_accounts[0].token),
                         ("github-secret-token", "sk-ant-oat01-secret"))
        jira_id = user.jira_accounts[0].id
        self.assertEqual(self.users.jira_tokens(jira_id), Tokens("jira-access-token", "jira-refresh-token", 123.0))
        self.users.save_jira_tokens(jira_id, Tokens("a2", "r2", 456.0))
        self.assertEqual(self.users.jira_tokens(jira_id), Tokens("a2", "r2", 456.0))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(RuntimeError, "SECRET_KEY changed"):
            UserRepository(Database(self.path, Fernet.generate_key().decode())).get(self.me)

    def test_one_developer_with_personal_and_office_accounts(self):
        # Office Jira (signed in), personal Jira connected to the same developer; one of each GitHub and Claude.
        self.assertEqual(self.users.save_jira_account(self.me, "acc-home", "Majid (home)", "", [HOME], Tokens("a", "r", 0)),
                         self.me)
        self.users.save_github(self.me, "office", "t1", "majid-office", "Majid", "m@office")
        self.users.save_github(self.me, "personal", "t2", "majid", "Majid", "m@home")
        self.users.save_claude(self.me, "office", "sk-ant-oat01-a", "k-office")
        self.users.save_claude(self.me, "personal", "sk-ant-api03-b", "k-home")
        user = self.users.get(self.me)
        self.assertEqual(set(user.sites), {OFFICE.cloud_id, HOME.cloud_id})
        self.assertEqual([g.label for g in user.github_accounts], ["office", "personal"])
        # Signing in with either Jira account is the same developer; another developer can't take one.
        self.assertEqual(self.users.save_jira_account(None, "acc-home", "Majid", "", [HOME], Tokens("a", "r", 0)), self.me)
        other = add_developer(self.users, "acc-sara")
        with self.assertRaises(AccountInUse):
            self.users.save_jira_account(other, "acc-home", "x", "", [HOME], Tokens("a", "r", 0))
        # A token for a login (GitHub) or a label (Claude) already there replaces it, keeping the config dir.
        self.users.save_github(self.me, "office", "t3", "majid-office", "Majid", "m@office")
        self.users.save_claude(self.me, "office", "sk-ant-oat01-c", "ignored")
        user = self.users.get(self.me)
        self.assertEqual([g.token for g in user.github_accounts], ["t3", "t2"])
        self.assertEqual([(c.token, c.config_key) for c in user.claude_accounts][0], ("sk-ant-oat01-c", "k-office"))

        # Office project with office accounts, personal project with personal ones: both runnable.
        work = add_project(self.projects, self.me, "market", ["MAR"])
        home = add_project(self.projects, self.me, "side", ["MAR"], site=HOME)  # same key, other site
        jira = {a.account_id: a.id for a in user.jira_accounts}
        gh = {g.label: g.id for g in user.github_accounts}
        cl = {c.label: c.id for c in user.claude_accounts}
        self.users.subscribe(self.me, Subscription(work.id, jira["acc-1"], gh["office"], cl["office"], "bugs", {"fe": "develop"}))
        self.users.subscribe(self.me, Subscription(home.id, jira["acc-home"], gh["personal"], cl["personal"], "all"))
        user = self.users.get(self.me)
        office, personal = user.actor(work.id), user.actor(home.id)
        self.assertEqual((office.jira_account_id, office.github.login, office.claude.label, office.mode, office.base_branches),
                         ("acc-1", "majid-office", "office", "bugs", {"fe": "develop"}))
        self.assertEqual((personal.jira_account_id, personal.github.login, personal.claude.label, personal.mode),
                         ("acc-home", "majid", "personal", "all"))
        self.assertEqual(self.projects.for_issue(HOME.cloud_id, "MAR-3").name, "side")
        self.assertEqual(self.projects.for_issue(OFFICE.cloud_id, "MAR-3").name, "market")

        # Removing an account: subscriptions lose it (and stop being runnable) but are kept.
        self.users.remove_github(self.me, gh["personal"])
        self.assertIsNone(self.users.get(self.me).actor(home.id))
        self.assertIn(home.id, self.users.get(self.me).subscriptions)
        # Removing a Jira account removes what ran with it; losing a site removes its subscriptions too.
        self.users.save_jira_account(None, "acc-1", "Majid", "m@x", [], Tokens("a", "r", 0))  # office no longer granted
        self.assertNotIn(work.id, self.users.get(self.me).subscriptions)
        self.users.remove_jira_account(self.me, jira["acc-home"])
        self.assertEqual(self.users.get(self.me).subscriptions, {})

    def test_projects_keys_are_unique_per_site_and_ownership_passes_on(self):
        project = add_project(self.projects, self.me, "market", ["MAR", "MARS"], repos=[repo("fe"), repo("be", test=None)])
        self.assertEqual((project.jira_projects, [r.name for r in project.repos], project.repos[1].test),
                         ({"MAR", "MARS"}, ["fe", "be"], None))
        with self.assertRaises(Taken) as taken:
            add_project(self.projects, self.me, "other", ["MARS"])
        self.assertEqual(taken.exception.what, "key")
        with self.assertRaises(Taken) as taken:
            add_project(self.projects, self.me, "market", ["NEW"])
        self.assertEqual(taken.exception.what, "name")
        self.users.save_jira_account(self.me, "acc-home", "Majid", "", [HOME], Tokens("a", "r", 0))
        self.assertEqual(add_project(self.projects, self.me, "market", ["MAR"], site=HOME).site_url, HOME.url)

        sara, omar = add_developer(self.users, "acc-sara"), add_developer(self.users, "acc-omar")
        join(self.users, omar, project, "acc-omar")
        join(self.users, sara, project, "acc-sara")
        lonely = add_project(self.projects, self.me, "lonely", ["LON"])
        self.users.delete(self.me)
        self.assertEqual(self.projects.get(project.id).owner_id, omar)  # joined first
        self.assertIsNone(self.projects.get(lonely.id))  # nobody else in it

    def test_sessions_runs_and_account_deletion(self):
        token = self.sessions.create(self.me)
        user_id, csrf = self.sessions.get(token)
        self.assertEqual(user_id, self.me)
        self.assertTrue(csrf)
        self.assertIsNone(self.sessions.get("forged"))
        # A session used a day or more after it was made gets a full week again; an expired one does not.
        expire_in = lambda days: sqlite3.connect(self.path).execute(
            "UPDATE sessions SET expires = ?", (time.time() + days * 86400,)).connection.commit()
        self.assertFalse(self.sessions.renew(token))
        expire_in(5)
        self.assertTrue(self.sessions.renew(token))
        self.assertFalse(self.sessions.renew(token))
        expire_in(-1)
        self.assertFalse(self.sessions.renew(token))
        self.assertIsNone(self.sessions.get(token))
        token = self.sessions.create(self.me)
        project = add_project(self.projects, self.me, "market", ["MAR"])
        running = self.runs.add("MAR-1", self.me, project.id, "market")
        self.runs.update(running, "running")
        queued = self.runs.add("MAR-2, MAR-3", self.me, project.id, "market")
        # After a restart: running ones failed, queued ones handed back to be queued again.
        self.assertEqual([r["id"] for r in self.runs.resume_after_restart()], [queued])
        self.assertEqual({r["id"]: r["status"] for r in self.runs.recent(self.me)}, {running: "failed", queued: "queued"})
        self.assertEqual(self.runs.recent(self.me)[0]["site_url"], OFFICE.url)
        # Old finished runs are dropped as new ones are added; unfinished ones never are.
        with mock.patch.object(runs_repository, "KEPT_RUNS", 3):
            for n in range(4, 8):
                self.runs.update(self.runs.add(f"MAR-{n}", self.me, project.id, "market"), "done")
        self.assertEqual([r["issue_key"] for r in self.runs.recent(self.me)], ["MAR-7", "MAR-6", "MAR-5", "MAR-2, MAR-3"])
        self.assertEqual(self.users.delete(self.me), [])
        self.assertIsNone(self.users.get(self.me))
        self.assertIsNone(self.sessions.get(token))
        self.assertEqual(self.runs.recent(self.me), [])

    def test_webhook_events_are_accepted_once(self):
        self.assertTrue(self.runs.first_time("e1", 1000, 0))
        self.assertFalse(self.runs.first_time("e1", 1000, 0))
        self.assertTrue(self.runs.first_time("e2", 5000, 2000))  # e1 is too old to be accepted again: forgotten
        self.assertTrue(self.runs.first_time("e1", 1000, 0))


V1_SCHEMA = """
CREATE TABLE users (account_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
    jira_cloud_id TEXT NOT NULL, jira_access_token BLOB NOT NULL, jira_refresh_token BLOB NOT NULL, jira_expires REAL NOT NULL,
    github_login TEXT, github_token BLOB, git_name TEXT, git_email TEXT, claude_token BLOB,
    mode TEXT NOT NULL DEFAULT 'bugs', paused INTEGER NOT NULL DEFAULT 0);
CREATE TABLE subscriptions (account_id TEXT NOT NULL REFERENCES users ON DELETE CASCADE, project TEXT NOT NULL,
    base_branches TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (account_id, project));
CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES users ON DELETE CASCADE,
    csrf TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE runs (id INTEGER PRIMARY KEY, issue_key TEXT NOT NULL, account_id TEXT NOT NULL REFERENCES users ON DELETE CASCADE,
    project TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', updated REAL NOT NULL);
"""

PROJECT_FILE = '''jira_projects = ["MAR"]
root_cause_field = "customfield_10201"
start_statuses = ["To Do", "In Progress"]
[[repos]]
name = "fe"
github = "acme/market-place-fe"
description = "frontend"
setup = "npm ci"
test = "npm test"
'''


class MigrationTest(unittest.TestCase):
    """A v1 database (one site, one account of each per developer, project files) is carried over."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "data" / "shadowdev.db"
        self.path.parent.mkdir()
        self.key = Fernet.generate_key().decode()
        enc = Fernet(self.key).encrypt
        with sqlite3.connect(self.path) as db:
            db.executescript(V1_SCHEMA)
            db.execute("INSERT INTO users VALUES ('acc-majid', 'Majid', 'm@x', 'cloud-1', ?, ?, 99.0, 'majid', ?, "
                       "'Majid Ali', 'm@users.noreply', ?, 'all', 0)",
                       (enc(b"jira-a"), enc(b"jira-r"), enc(b"gh-tok"), enc(b"sk-ant-oat01-x")))
            db.execute("INSERT INTO users VALUES ('acc-sara', 'Sara', '', 'cloud-1', ?, ?, 0, NULL, NULL, NULL, NULL, NULL, "
                       "'bugs', 1)", (enc(b"a"), enc(b"r")))
            db.execute("INSERT INTO subscriptions VALUES ('acc-sara', 'marketplace', '{}')")
            db.execute("INSERT INTO subscriptions VALUES ('acc-majid', 'marketplace', ?)", (json.dumps({"fe": "develop"}),))
            db.execute("INSERT INTO subscriptions VALUES ('acc-majid', 'gone', '{}')")
            db.execute("INSERT INTO sessions VALUES ('h', 'acc-majid', 'c', 1e12)")
            db.execute("INSERT INTO runs VALUES (7, 'MAR-1', 'acc-majid', 'marketplace', 'done', 'ok', 1.0)")
        (self.dir / "projects").mkdir()
        (self.dir / "projects" / "marketplace.toml").write_text(PROJECT_FILE)

    def open(self) -> Database:
        return Database(self.path, self.key, self.dir / "projects", "https://acme.atlassian.net")

    def test_upgrade_keeps_developers_accounts_projects_and_history(self):
        db = self.open()
        users, projects, runs = UserRepository(db), ProjectRepository(db), RunRepository(db)
        majid_id = users.owner_of("acc-majid")
        majid = users.get(majid_id)
        self.assertEqual((majid.display_name, majid.paused), ("Majid", False))
        self.assertEqual(users.jira_tokens(majid.jira_accounts[0].id), Tokens("jira-a", "jira-r", 99.0))
        self.assertEqual(majid.jira_accounts[0].sites[0].url, "https://acme.atlassian.net")
        self.assertEqual((majid.github_accounts[0].login, majid.github_accounts[0].token, majid.github_accounts[0].git_name),
                         ("majid", "gh-tok", "Majid Ali"))
        # The Claude config dir v1 used for this developer, so its state is kept.
        self.assertEqual(majid.claude_accounts[0].config_key, hashlib.sha256(b"acc-majid").hexdigest()[:16])

        (market,) = projects.on_sites(["cloud-1"])
        self.assertEqual((market.name, market.jira_projects, market.root_cause_field, market.start_statuses),
                         ("marketplace", {"MAR"}, "customfield_10201", ("To Do", "In Progress")))
        self.assertEqual((market.repos[0].github, market.repos[0].setup), ("acme/market-place-fe", "npm ci"))
        sara_id = users.owner_of("acc-sara")
        self.assertEqual(market.owner_id, sara_id)  # its first subscriber
        self.assertTrue(projects.webhook_secret(market.id))

        actor = majid.actor(market.id)
        self.assertEqual((actor.mode, actor.base_branches, actor.github.login), ("all", {"fe": "develop"}, "majid"))
        sara = users.get(sara_id)
        self.assertTrue(sara.paused)
        self.assertIsNone(sara.actor(market.id))  # had not connected GitHub/Claude
        self.assertEqual(set(majid.subscriptions), {market.id})  # "gone" had no project file
        (run,) = runs.recent(majid_id)
        self.assertEqual((run["id"], run["issue_key"], run["project_id"], run["status"]), (7, "MAR-1", market.id, "done"))

        self.assertTrue(self.path.with_name("shadowdev.db.v1-backup").exists())
        self.open()  # opening again changes nothing
        self.assertEqual(len(UserRepository(self.open()).all()), 2)

    def test_a_bad_project_file_leaves_the_database_untouched(self):
        (self.dir / "projects" / "broken.toml").write_text('jira_projects = ["../x"]\n')
        with self.assertRaisesRegex(SystemExit, "jira_projects"):
            self.open()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
            self.assertIn("jira_cloud_id", {r[1] for r in db.execute("PRAGMA table_info(users)")})

    def test_refuses_a_database_from_the_api_token_version(self):
        old = self.dir / "old.db"
        with sqlite3.connect(old) as db:
            db.execute("CREATE TABLE users (account_id TEXT PRIMARY KEY, jira_email TEXT, jira_token BLOB)")
        with self.assertRaisesRegex(SystemExit, "older ShadowDev"):
            Database(old, self.key)


if __name__ == "__main__":
    unittest.main()
