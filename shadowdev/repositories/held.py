"""What waits for a developer's working hours in a project (services/outbox.py)."""
import json
import sqlite3
import time
from contextlib import closing

from ..models.publication import Publication
from .database import Database


class HeldRepository:
    def __init__(self, db: Database):
        self.db = db

    def hold_publication(self, user_id: int, project_id: int, run_id: int | None, batch: int | None,
                         publication: Publication) -> None:
        """A run's PRs to open. A later run of the same branch replaces what an earlier one left."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("DELETE FROM held WHERE user_id = ? AND project_id = ? AND kind = 'prs' AND branch = ?",
                         (user_id, project_id, publication.branch))
            conn.execute("INSERT INTO held (user_id, project_id, kind, branch, run_id, batch, data, at) "
                         "VALUES (?, ?, 'prs', ?, ?, ?, ?, ?)",
                         (user_id, project_id, publication.branch, run_id, batch, publication.to_json(), time.time()))

    def hold_slack(self, user_id: int, project_id: int, channel: str, text: str, thread_ts: str) -> None:
        """A reply in a Slack thread, to post as the developer."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("INSERT INTO held (user_id, project_id, kind, data, at) VALUES (?, ?, 'slack', ?, ?)",
                         (user_id, project_id, json.dumps({"channel": channel, "text": text, "ts": thread_ts}),
                          time.time()))

    def waiting(self) -> list[sqlite3.Row]:
        """Everything held, the oldest first."""
        with closing(self.db.connect()) as conn:
            return conn.execute("SELECT * FROM held ORDER BY id").fetchall()

    def done(self, held_ids: list[int]) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.executemany("DELETE FROM held WHERE id = ?", [(i,) for i in held_ids])
