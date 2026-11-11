"""The SQLite database: schema, migrations, connections, and encryption of stored secrets.

Developers' Jira (OAuth), GitHub and Claude tokens and the projects' webhook secrets are
encrypted with SHADOWDEV_SECRET_KEY (Fernet), so a copy of the database alone does not reveal
them. A new connection per call keeps it safe to use from the web threads and the run workers.
"""
import sqlite3
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

VERSION = 10  # PRAGMA user_version. 0 with a `users.jira_cloud_id` column: the single-site layout (v1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    cloud_id TEXT PRIMARY KEY,  -- Jira Cloud site
    url      TEXT NOT NULL,     -- https://acme.atlassian.net
    name     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    email        TEXT NOT NULL DEFAULT '',
    paused       INTEGER NOT NULL DEFAULT 0,  -- 1: nothing runs for this developer
    limits       TEXT NOT NULL DEFAULT '{}'  -- JSON models.limits.Limits: polling, timeouts, runs; missing = default
);
CREATE TABLE IF NOT EXISTS jira_accounts (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
    account_id    TEXT NOT NULL UNIQUE,  -- Atlassian account: belongs to one developer
    display_name  TEXT NOT NULL,
    email         TEXT NOT NULL DEFAULT '',
    access_token  BLOB NOT NULL,
    refresh_token BLOB NOT NULL,
    expires       REAL NOT NULL,
    profile_at    REAL NOT NULL DEFAULT 0,  -- when display_name/email were fetched from Atlassian
    reported_at   REAL NOT NULL DEFAULT 0   -- last sent to Atlassian's personal data reporting
);
CREATE INDEX IF NOT EXISTS jira_accounts_user ON jira_accounts (user_id);
CREATE TABLE IF NOT EXISTS jira_account_sites (
    jira_account_id INTEGER NOT NULL REFERENCES jira_accounts ON DELETE CASCADE,
    cloud_id        TEXT NOT NULL REFERENCES sites,
    PRIMARY KEY (jira_account_id, cloud_id)
);
CREATE TABLE IF NOT EXISTS github_accounts (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
    label     TEXT NOT NULL,
    login     TEXT NOT NULL,
    token     BLOB NOT NULL,
    git_name  TEXT NOT NULL,
    git_email TEXT NOT NULL,
    UNIQUE (user_id, login)
);
CREATE TABLE IF NOT EXISTS claude_accounts (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
    label      TEXT NOT NULL,
    token      BLOB NOT NULL,
    config_key TEXT NOT NULL UNIQUE,  -- its Claude config dir: data/claude/<config_key>
    UNIQUE (user_id, label)
);
CREATE TABLE IF NOT EXISTS slack_accounts (
    user_id       INTEGER PRIMARY KEY REFERENCES users ON DELETE CASCADE,
    token         BLOB NOT NULL,  -- the developer's Slack user token
    slack_user_id TEXT NOT NULL,
    team          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    cloud_id         TEXT NOT NULL REFERENCES sites,
    owner_id         INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,  -- handed on before an owner leaves
    root_cause_field TEXT,
    start_statuses   TEXT NOT NULL,  -- JSON list
    review_statuses  TEXT NOT NULL,  -- JSON list
    webhook_secret   BLOB NOT NULL,
    slack_channel    TEXT,  -- channel ID for review requests
    UNIQUE (cloud_id, name)
);
CREATE TABLE IF NOT EXISTS project_keys (
    project_id INTEGER NOT NULL REFERENCES projects ON DELETE CASCADE,
    cloud_id   TEXT NOT NULL,
    jira_key   TEXT NOT NULL,
    PRIMARY KEY (cloud_id, jira_key)  -- a Jira project on a site belongs to one ShadowDev project
);
CREATE INDEX IF NOT EXISTS project_keys_project ON project_keys (project_id);
CREATE TABLE IF NOT EXISTS repos (
    project_id     INTEGER NOT NULL REFERENCES projects ON DELETE CASCADE,
    position       INTEGER NOT NULL,
    name           TEXT NOT NULL,
    github         TEXT NOT NULL,
    description    TEXT NOT NULL,
    release_prefix TEXT NOT NULL,
    setup          TEXT,
    test           TEXT,
    reviewers      TEXT NOT NULL DEFAULT '',  -- Slack member IDs, space-separated
    PRIMARY KEY (project_id, name)
);
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id           INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
    project_id        INTEGER NOT NULL REFERENCES projects ON DELETE CASCADE,
    jira_account_id   INTEGER NOT NULL REFERENCES jira_accounts ON DELETE CASCADE,
    github_account_id INTEGER REFERENCES github_accounts ON DELETE SET NULL,
    claude_account_id INTEGER REFERENCES claude_accounts ON DELETE SET NULL,
    mode              TEXT NOT NULL DEFAULT 'bugs',
    base_branches     TEXT NOT NULL DEFAULT '{}',  -- JSON {repo name: branch}; missing = default
    joined            INTEGER NOT NULL DEFAULT 0,  -- order of joining; the oldest member inherits a project
    polled_at         REAL NOT NULL DEFAULT 0,  -- last time Jira was asked for its new assignments (Limits.jira_polling)
    slack_seen        TEXT NOT NULL DEFAULT '',  -- timestamp of the last message read in the project's Slack channel
    settings          TEXT NOT NULL DEFAULT '{}',  -- JSON models.settings.Settings: what is automated; missing = default
    PRIMARY KEY (user_id, project_id)
);
CREATE INDEX IF NOT EXISTS subscriptions_project ON subscriptions (project_id);
CREATE INDEX IF NOT EXISTS subscriptions_jira ON subscriptions (jira_account_id);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
    csrf       TEXT NOT NULL,
    expires    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expires ON sessions (expires);
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY,
    issue_key  TEXT NOT NULL,  -- "MAR-1" or "MAR-1, MAR-2" for issues fixed together
    user_id    INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
    project_id INTEGER REFERENCES projects ON DELETE SET NULL,
    project    TEXT NOT NULL,  -- its name, kept for the history
    status     TEXT NOT NULL,  -- queued, running, stopping, stopped, done, skipped, failed
    message    TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',  -- the developer's instructions for a run started by hand
    model      TEXT NOT NULL DEFAULT '',  -- the Claude model(s) that worked on it, e.g. "haiku → sonnet (medium)"
    questions  TEXT NOT NULL DEFAULT '',  -- JSON {issue key: [questions]} it asked the developer (status "question")
    updated    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_user ON runs (user_id, id);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status);
CREATE TABLE IF NOT EXISTS pull_requests (  -- PRs ShadowDev opened, watched for review comments and failing CI until they close
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
    project_id    INTEGER NOT NULL REFERENCES projects ON DELETE CASCADE,
    repo          TEXT NOT NULL,  -- the repo's name in the project
    github        TEXT NOT NULL,
    number        INTEGER NOT NULL,
    branch        TEXT NOT NULL,
    slack_channel TEXT,  -- the message that asked for its review
    slack_ts      TEXT,
    handled_at    TEXT NOT NULL DEFAULT '',  -- time (as GitHub gives it) of the newest review comment acted on
    rounds        INTEGER NOT NULL DEFAULT 0,
    ci_sha        TEXT NOT NULL DEFAULT '',  -- the newest head commit whose failing CI was acted on
    ci_rounds     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (github, number)
);
CREATE TABLE IF NOT EXISTS reviews (  -- PRs a developer was asked in Slack to review
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects ON DELETE CASCADE,
    github     TEXT NOT NULL,
    number     INTEGER NOT NULL,
    channel    TEXT NOT NULL,
    ts         TEXT NOT NULL,  -- the Slack message (thread) it was asked in, where ShadowDev answers
    last_ts    TEXT NOT NULL,  -- the newest message of that thread acted on
    state      TEXT NOT NULL,  -- requested, commented, approved, failed, closed
    rounds     INTEGER NOT NULL DEFAULT 0,
    created    REAL NOT NULL,
    UNIQUE (user_id, github, number)
);
CREATE TABLE IF NOT EXISTS webhook_events (
    id TEXT PRIMARY KEY,  -- hash of the signed event: each is accepted once, also across restarts
    at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS webhook_events_at ON webhook_events (at);
"""

# Columns added since schema 2, which older databases get on upgrade.
ADDED_COLUMNS = (
    ("jira_accounts", "profile_at", "REAL NOT NULL DEFAULT 0"), ("jira_accounts", "reported_at", "REAL NOT NULL DEFAULT 0"),
    ("subscriptions", "polled_at", "REAL NOT NULL DEFAULT 0"),  # schema 3
    ("runs", "note", "TEXT NOT NULL DEFAULT ''"),  # 4
    ("runs", "model", "TEXT NOT NULL DEFAULT ''"),  # 5
    ("projects", "slack_channel", "TEXT"), ("repos", "reviewers", "TEXT NOT NULL DEFAULT ''"),  # 6
    ("subscriptions", "slack_seen", "TEXT NOT NULL DEFAULT ''"),
    ("pull_requests", "ci_sha", "TEXT NOT NULL DEFAULT ''"), ("pull_requests", "ci_rounds", "INTEGER NOT NULL DEFAULT 0"),  # 7
    ("subscriptions", "settings", "TEXT NOT NULL DEFAULT '{}'"),  # 8
    ("runs", "questions", "TEXT NOT NULL DEFAULT ''"),  # 9
    ("users", "limits", "TEXT NOT NULL DEFAULT '{}'"),  # 10
)


class Database:
    def __init__(self, path: Path, secret_key: str, legacy_projects_dir: Path | None = None,
                 legacy_site_url: str | None = None):
        """`legacy_*`: where a v1 database's project files and Jira site were, to carry them over."""
        try:
            self._fernet = Fernet(secret_key)
        except ValueError:
            raise SystemExit("SHADOWDEV_SECRET_KEY is not a valid key; see .env.example to generate one") from None
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)  # also covers the WAL files next to the database
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
            if version == 0 and columns and "jira_cloud_id" not in columns:
                raise SystemExit(f"{path} is from an older ShadowDev that used Jira API tokens; "
                                 "move it away and start again (developers sign up again)")
            if version == 0 and "jira_cloud_id" in columns:
                from .migrations import upgrade_from_v1
                upgrade_from_v1(self, db, legacy_projects_dir, legacy_site_url)
            elif version > VERSION:
                raise SystemExit(f"{path} is from a newer ShadowDev (schema {version}); upgrade ShadowDev")
            else:
                with db:
                    db.executescript(SCHEMA)
                    for table, column, definition in ADDED_COLUMNS:
                        if column not in {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}:
                            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    db.execute(f"PRAGMA user_version = {VERSION}")
        path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        """A new connection; use as `with closing(db.connect()) as conn, conn:` to commit."""
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; a crash loses at most the last commits
        return conn

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode())

    def decrypt(self, value: bytes | None) -> str | None:
        if value is None:
            return None
        try:
            return self._fernet.decrypt(value).decode()
        except InvalidToken:
            raise RuntimeError("cannot decrypt stored token: SHADOWDEV_SECRET_KEY changed?") from None
