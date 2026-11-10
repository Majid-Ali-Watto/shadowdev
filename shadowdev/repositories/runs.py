"""Runs ShadowDev did, shown to the developer in the UI, and the webhook events it accepted."""
import sqlite3
import time
from contextlib import closing

from .database import Database

KEPT_RUNS = 200  # finished runs kept per developer; older ones are dropped


class RunRepository:
    def __init__(self, db: Database):
        self.db = db

    def add(self, issue_key: str, user_id: int, project_id: int, project: str, note: str = "") -> int:
        with closing(self.db.connect()) as conn, conn:
            conn.execute("DELETE FROM runs WHERE user_id = ? AND status NOT IN ('queued', 'running', 'stopping') AND id NOT IN "
                         "(SELECT id FROM runs WHERE user_id = ? ORDER BY id DESC LIMIT ?)", (user_id, user_id, KEPT_RUNS - 1))
            return conn.execute(
                "INSERT INTO runs (issue_key, user_id, project_id, project, note, status, updated) "
                "VALUES (?, ?, ?, ?, ?, 'queued', ?)",
                (issue_key, user_id, project_id, project, note, time.time()),
            ).lastrowid

    def update(self, run_id: int, status: str, message: str = "") -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE runs SET status = ?, message = ?, updated = ? WHERE id = ?",
                         (status, message[:2000], time.time(), run_id))

    def set_model(self, run_id: int, model: str) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE runs SET model = ? WHERE id = ?", (model[:200], run_id))

    def mark_stopping(self, run_id: int) -> None:
        """Only while it is queued or running: a run that already ended keeps its status."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE runs SET status = 'stopping', message = 'stop requested', updated = ? "
                         "WHERE id = ? AND status IN ('queued', 'running')", (time.time(), run_id))

    def last_status(self, user_id: int, project_id: int, issue_key: str) -> str | None:
        """How the developer's latest run of the issue (alone or in a group) in the project ended."""
        with closing(self.db.connect()) as conn:
            row = conn.execute(
                "SELECT status FROM runs WHERE user_id = ? AND project_id = ? AND instr(', ' || issue_key || ', ', ?) > 0 "
                "ORDER BY id DESC LIMIT 1", (user_id, project_id, f", {issue_key}, ")).fetchone()
        return row[0] if row else None

    def resume_after_restart(self) -> list[sqlite3.Row]:
        """At startup: runs that were running are lost (failed); queued ones are returned, oldest first,
        to be queued again."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE runs SET status = 'failed', message = 'interrupted by a ShadowDev restart', updated = ? "
                         "WHERE status IN ('running', 'stopping')", (time.time(),))
            return conn.execute("SELECT * FROM runs WHERE status = 'queued' ORDER BY id").fetchall()

    def recent(self, user_id: int, limit: int = 20) -> list[sqlite3.Row]:
        with closing(self.db.connect()) as conn:
            return conn.execute(
                "SELECT r.*, s.url AS site_url FROM runs r LEFT JOIN projects p ON p.id = r.project_id "
                "LEFT JOIN sites s ON s.cloud_id = p.cloud_id WHERE r.user_id = ? ORDER BY r.id DESC LIMIT ?",
                (user_id, limit)).fetchall()

    def first_time(self, event_id: str, at: int, forget_before: int) -> bool:
        """Record a webhook event; False if it was seen before. Events older than `forget_before`
        are dropped: their timestamp is too old to be accepted again anyway."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("DELETE FROM webhook_events WHERE at < ?", (forget_before,))
            return conn.execute("INSERT OR IGNORE INTO webhook_events VALUES (?, ?)", (event_id, at)).rowcount == 1
