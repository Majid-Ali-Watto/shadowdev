"""Projects: their Jira site and project keys, repos, Jira workflow, and webhook secret."""
import json
import secrets
import sqlite3
from contextlib import closing

from ..models.project import Project, Repo
from .database import Database


class Taken(Exception):
    """The name, or a Jira project key, is already another project's on the same site."""

    def __init__(self, what: str):
        super().__init__(what)
        self.what = what  # "name" or "key"


class ProjectRepository:
    def __init__(self, db: Database):
        self.db = db

    def _query(self, where: str, args: tuple) -> list[Project]:
        with closing(self.db.connect()) as conn:
            rows = conn.execute(f"SELECT p.*, s.url FROM projects p JOIN sites s ON s.cloud_id = p.cloud_id "  # noqa: S608
                                f"WHERE {where} ORDER BY p.name", args).fetchall()
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            marks = ",".join("?" * len(ids))
            keys: dict[int, set[str]] = {}
            for k in conn.execute(f"SELECT project_id, jira_key FROM project_keys WHERE project_id IN ({marks})", ids):  # noqa: S608
                keys.setdefault(k[0], set()).add(k[1])
            repos: dict[int, list[Repo]] = {}
            for r in conn.execute(f"SELECT * FROM repos WHERE project_id IN ({marks}) ORDER BY position", ids):  # noqa: S608
                repos.setdefault(r["project_id"], []).append(
                    Repo(r["name"], r["github"], r["description"], r["release_prefix"], r["setup"], r["test"],
                         tuple(r["reviewers"].split())))
        return [Project(r["id"], r["name"], r["owner_id"], r["cloud_id"], r["url"], frozenset(keys.get(r["id"], ())),
                        r["root_cause_field"], tuple(repos.get(r["id"], ())), tuple(json.loads(r["start_statuses"])),
                        tuple(json.loads(r["review_statuses"])), r["slack_channel"]) for r in rows]

    def get(self, project_id: int) -> Project | None:
        found = self._query("p.id = ?", (project_id,))
        return found[0] if found else None

    def on_sites(self, cloud_ids: list[str]) -> list[Project]:
        """The projects a developer with Jira access to these sites can see and join."""
        if not cloud_ids:
            return []
        return self._query(f"p.cloud_id IN ({','.join('?' * len(cloud_ids))})", tuple(cloud_ids))

    def for_issue(self, cloud_id: str, issue_key: str) -> Project | None:
        """The project handling an issue of a site, by its Jira project key (MAR-123 -> MAR)."""
        found = self._query("p.id = (SELECT project_id FROM project_keys WHERE cloud_id = ? AND jira_key = ?)",
                            (cloud_id, issue_key.rpartition("-")[0]))
        return found[0] if found else None

    def site_for_url(self, url: str) -> str | None:
        with closing(self.db.connect()) as conn:
            row = conn.execute("SELECT cloud_id FROM sites WHERE url = ?", (url.rstrip("/"),)).fetchone()
        return row[0] if row else None

    def save(self, project: Project) -> int:
        """Create (id 0) or update a project; returns its ID. Raises Taken."""
        with closing(self.db.connect()) as conn, conn:
            values = (project.name, project.root_cause_field, json.dumps(project.start_statuses),
                      json.dumps(project.review_statuses), project.slack_channel)
            try:
                if project.id:
                    pid = project.id
                    conn.execute("UPDATE projects SET name = ?, root_cause_field = ?, start_statuses = ?, review_statuses = ?, "
                                 "slack_channel = ? WHERE id = ?", (*values, pid))
                    conn.execute("DELETE FROM project_keys WHERE project_id = ?", (pid,))
                    conn.execute("DELETE FROM repos WHERE project_id = ?", (pid,))
                else:
                    pid = conn.execute(
                        "INSERT INTO projects (name, root_cause_field, start_statuses, review_statuses, slack_channel, cloud_id, "
                        "owner_id, webhook_secret) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (*values, project.cloud_id, project.owner_id, self.db.encrypt(secrets.token_urlsafe(32)))).lastrowid
                conn.executemany("INSERT INTO project_keys VALUES (?, ?, ?)",
                                 [(pid, project.cloud_id, k) for k in sorted(project.jira_projects)])
            except sqlite3.IntegrityError as e:
                if "UNIQUE" not in str(e):
                    raise
                raise Taken("name" if "projects.name" in str(e) else "key") from None
            conn.executemany("INSERT INTO repos (project_id, position, name, github, description, release_prefix, setup, "
                             "test, reviewers) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             [(pid, i, r.name, r.github, r.description, r.release_prefix, r.setup, r.test, " ".join(r.reviewers))
                              for i, r in enumerate(project.repos)])
        return pid

    def delete(self, project_id: int) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    def webhook_secret(self, project_id: int) -> str | None:
        with closing(self.db.connect()) as conn:
            row = conn.execute("SELECT webhook_secret FROM projects WHERE id = ?", (project_id,)).fetchone()
        return self.db.decrypt(row[0]) if row else None

    def members(self, project_id: int) -> int:
        with closing(self.db.connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM subscriptions WHERE project_id = ?", (project_id,)).fetchone()[0]
