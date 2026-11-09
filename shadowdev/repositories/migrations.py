"""Upgrading a v1 database (one Jira site, one Jira/GitHub/Claude account per developer, projects in
projects/*.toml) to the current layout. Everything is carried over: developers keep their accounts,
tokens, Claude logins, subscriptions and run history, and the project files become projects owned by
their first subscriber. A copy of the old database is kept next to it, and the upgrade runs in one
transaction, so a failure leaves the database as it was."""
import hashlib
import json
import logging
import secrets
import sqlite3
import tomllib
from collections import Counter
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from ..models.project import CUSTOM_FIELD_RE, GITHUB_REPO_RE, JIRA_PROJECT_RE, REPO_NAME_RE
from .database import SCHEMA, VERSION

log = logging.getLogger("shadowdev")


def statements(script: str):
    """The SQL statements of a script, one by one (executescript would commit midway)."""
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""


def legacy_project(path: Path) -> dict:
    """A v1 project file, checked as v1 checked it."""
    data = tomllib.loads(path.read_text())
    keys = data.get("jira_projects") or []
    if not keys or not all(isinstance(k, str) and JIRA_PROJECT_RE.match(k) for k in keys):
        raise SystemExit(f"{path}: jira_projects must list Jira project keys, e.g. [\"MAR\"]")
    field = data.get("root_cause_field") or None
    if field and not CUSTOM_FIELD_RE.match(field):
        raise SystemExit(f"{path}: root_cause_field must look like customfield_10201")
    repos = []
    for entry in data.get("repos") or []:
        name, github = entry.get("name", ""), entry.get("github", "")
        if not REPO_NAME_RE.match(name) or not GITHUB_REPO_RE.match(github) or not entry.get("test"):
            raise SystemExit(f"{path}: repo {name!r} needs a valid name, github owner/name and test")
        repos.append({"name": name, "github": github, "description": entry.get("description") or f"the {name} repo",
                      "release_prefix": entry.get("release_prefix", "release/"),
                      "setup": entry.get("setup") or None, "test": entry["test"]})
    if not repos:
        raise SystemExit(f"{path}: no [[repos]] defined")
    return {"name": path.stem, "keys": keys, "root_cause_field": field, "repos": repos,
            "start": data.get("start_statuses") or ["In Progress"], "review": data.get("review_statuses") or ["In Review"]}


def upgrade_from_v1(db, conn: sqlite3.Connection, projects_dir: Path | None, site_url: str | None) -> None:
    backup = db.path.with_name(db.path.name + ".v1-backup")
    if not backup.exists():
        with closing(sqlite3.connect(backup)) as copy:
            conn.backup(copy)
        backup.chmod(0o600)
    log.info("upgrading %s to schema %d (a copy of the old one is in %s)", db.path, VERSION, backup)
    files = sorted(projects_dir.glob("*.toml")) if projects_dir and projects_dir.is_dir() else []
    projects = [legacy_project(f) for f in files]  # parsed first: a bad file stops before any change

    conn.isolation_level = None  # explicit transaction: DDL included
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        users = conn.execute("SELECT * FROM users ORDER BY rowid").fetchall()
        subs = conn.execute("SELECT * FROM subscriptions ORDER BY rowid").fetchall()
        runs = conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
        for table in ("sessions", "subscriptions", "runs", "users"):  # sessions: developers sign in again
            conn.execute(f"DROP TABLE {table}")
        for statement in statements(SCHEMA):
            conn.execute(statement)

        host = urlsplit(site_url or "").netloc
        for cloud_id in {u["jira_cloud_id"] for u in users}:
            conn.execute("INSERT INTO sites (cloud_id, url, name) VALUES (?, ?, ?)",
                         (cloud_id, (site_url or "").rstrip("/"), host.split(".")[0] or cloud_id))
        user_ids, jira_ids, github_ids, claude_ids = {}, {}, {}, {}
        for u in users:
            acc = u["account_id"]
            user_ids[acc] = conn.execute("INSERT INTO users (display_name, email, paused) VALUES (?, ?, ?)",
                                         (u["display_name"], u["email"], u["paused"])).lastrowid
            jira_ids[acc] = conn.execute(
                "INSERT INTO jira_accounts (user_id, account_id, display_name, email, access_token, refresh_token, expires) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_ids[acc], acc, u["display_name"], u["email"], u["jira_access_token"], u["jira_refresh_token"],
                 u["jira_expires"])).lastrowid
            conn.execute("INSERT INTO jira_account_sites VALUES (?, ?)", (jira_ids[acc], u["jira_cloud_id"]))
            if u["github_token"]:
                github_ids[acc] = conn.execute(
                    "INSERT INTO github_accounts (user_id, label, login, token, git_name, git_email) VALUES (?, ?, ?, ?, ?, ?)",
                    (user_ids[acc], u["github_login"], u["github_login"], u["github_token"],
                     u["git_name"] or u["github_login"], u["git_email"] or "")).lastrowid
            if u["claude_token"]:
                # The config dir v1 used, so the developer's Claude login state is kept.
                key = hashlib.sha256(acc.encode()).hexdigest()[:16]
                claude_ids[acc] = conn.execute(
                    "INSERT INTO claude_accounts (user_id, label, token, config_key) VALUES (?, 'Claude', ?, ?)",
                    (user_ids[acc], u["claude_token"], key)).lastrowid

        project_ids = {}
        if users:
            cloud_id = Counter(u["jira_cloud_id"] for u in users).most_common(1)[0][0]
            for p in projects:
                first = next((s["account_id"] for s in subs if s["project"] == p["name"]), users[0]["account_id"])
                pid = conn.execute(
                    "INSERT INTO projects (name, cloud_id, owner_id, root_cause_field, start_statuses, review_statuses, "
                    "webhook_secret) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (p["name"], cloud_id, user_ids[first], p["root_cause_field"], json.dumps(p["start"]),
                     json.dumps(p["review"]), db.encrypt(secrets.token_urlsafe(32)))).lastrowid
                project_ids[p["name"]] = pid
                conn.executemany("INSERT INTO project_keys VALUES (?, ?, ?)", [(pid, cloud_id, k) for k in p["keys"]])
                conn.executemany(
                    "INSERT INTO repos VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(pid, i, r["name"], r["github"], r["description"], r["release_prefix"], r["setup"], r["test"])
                     for i, r in enumerate(p["repos"])])
        modes = {u["account_id"]: u["mode"] for u in users}
        for n, s in enumerate(subs):
            acc, pid = s["account_id"], project_ids.get(s["project"])
            if pid is None or acc not in user_ids:
                log.warning("dropping %s's subscription to %s: no such project file any more", acc, s["project"])
                continue
            conn.execute("INSERT INTO subscriptions (user_id, project_id, jira_account_id, github_account_id, "
                         "claude_account_id, mode, base_branches, joined) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (user_ids[acc], pid, jira_ids[acc], github_ids.get(acc), claude_ids.get(acc), modes[acc],
                          s["base_branches"], n))
        conn.executemany(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(r["id"], r["issue_key"], user_ids[r["account_id"]], project_ids.get(r["project"]), r["project"],
              r["status"], r["message"], r["updated"]) for r in runs if r["account_id"] in user_ids])
        conn.execute(f"PRAGMA user_version = {VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.isolation_level = ""
    log.info("upgraded: %d developer(s), %d project(s) from %s", len(users), len(project_ids), projects_dir)
