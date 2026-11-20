"""A developer's subscriptions: the projects they joined, with the accounts each runs with, what it
automates, and the bookkeeping of each (when it was last polled or checked, its Slack cursor).
UserRepository owns one as `users.subscriptions`."""
import json
import sqlite3
from contextlib import closing

from ..models.release import ReleaseReport
from ..models.settings import Settings
from ..models.user import MODES, Subscription
from .database import Database


class SoleMember(Exception):
    """The developer owns the project and no other member is left to take it over."""


class SubscriptionRepository:
    def __init__(self, db: Database):
        self.db = db

    def subscribe(self, user_id: int, sub: Subscription) -> None:
        """Join the project, or change the accounts and settings of a membership already there."""
        if sub.mode not in MODES:
            raise ValueError(f"unknown mode {sub.mode!r}")
        with closing(self.db.connect()) as conn, conn:
            conn.execute(
                "INSERT INTO subscriptions (user_id, project_id, jira_account_id, github_account_id, claude_account_id, mode, "
                "base_branches, settings, joined) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                "(SELECT COALESCE(MAX(joined), 0) + 1 FROM subscriptions)) "
                "ON CONFLICT (user_id, project_id) DO UPDATE SET jira_account_id = excluded.jira_account_id, "
                "github_account_id = excluded.github_account_id, claude_account_id = excluded.claude_account_id, "
                "mode = excluded.mode, base_branches = excluded.base_branches, settings = excluded.settings",
                (user_id, sub.project_id, sub.jira_account_id, sub.github_account_id, sub.claude_account_id, sub.mode,
                 json.dumps(sub.base_branches), sub.settings.to_json()))

    def leave(self, user_id: int, project_id: int) -> int | None:
        """The developer leaves the project. If they own it, it passes to its longest-standing other
        member, in the same transaction; returns that member's ID, or None when it was not theirs.
        Raises SoleMember, and leaves everything as it was, when they own it and nobody else is a member."""
        with closing(self.db.connect()) as conn, conn:
            owner = conn.execute("SELECT owner_id FROM projects WHERE id = ?", (project_id,)).fetchone()
            heir = None
            if owner and owner[0] == user_id:
                pass_ownership(conn, user_id, project_id)
                heir = conn.execute("SELECT owner_id FROM projects WHERE id = ?", (project_id,)).fetchone()[0]
                if heir == user_id:
                    raise SoleMember(project_id)
            conn.execute("DELETE FROM subscriptions WHERE user_id = ? AND project_id = ?", (user_id, project_id))
        return heir if heir != user_id else None

    def unsubscribe(self, user_id: int, project_id: int) -> None:
        """Remove the membership, without handing on what the developer owns (see leave)."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("DELETE FROM subscriptions WHERE user_id = ? AND project_id = ?", (user_id, project_id))

    # Single values of a subscription, read and written by name (the names are fixed in this module)

    def _read(self, user_id: int, project_id: int, column: str):
        with closing(self.db.connect()) as conn:
            row = conn.execute(f"SELECT {column} FROM subscriptions WHERE user_id = ? AND project_id = ?",  # noqa: S608
                               (user_id, project_id)).fetchone()
        return row[0] if row else None

    def _write(self, user_id: int, project_id: int, **values) -> None:
        assignments = ", ".join(f"{column} = ?" for column in values)  # noqa: S608 (keyword names in this module only)
        with closing(self.db.connect()) as conn, conn:
            conn.execute(f"UPDATE subscriptions SET {assignments} WHERE user_id = ? AND project_id = ?",  # noqa: S608
                         (*values.values(), user_id, project_id))

    def settings(self, user_id: int, project_id: int) -> Settings:
        """A subscription's settings alone, without loading (and decrypting) the developer's accounts;
        the defaults when they left the project."""
        return Settings.from_json(self._read(user_id, project_id, "settings"))

    def save_tests_sent(self, user_id: int, project_id: int, sent: dict) -> None:
        self._write(user_id, project_id, tests_sent=json.dumps(sent))

    def polled_at(self, user_id: int, project_id: int) -> float:
        return self._read(user_id, project_id, "polled_at") or 0.0

    def mark_polled(self, user_id: int, project_id: int, at: float) -> None:
        self._write(user_id, project_id, polled_at=at)

    def released_at(self, user_id: int, project_id: int) -> float:
        return self._read(user_id, project_id, "released_at") or 0.0

    def save_release_report(self, user_id: int, project_id: int, report: ReleaseReport) -> None:
        self._write(user_id, project_id, released_at=report.at, release_report=report.to_json())

    def slack_seen(self, user_id: int, project_id: int) -> str:
        """Timestamp of the last message read, as the developer, in the project's Slack channel."""
        return self._read(user_id, project_id, "slack_seen") or ""

    def mark_slack_seen(self, user_id: int, project_id: int, ts: str) -> None:
        self._write(user_id, project_id, slack_seen=ts)

    def releasing(self) -> list[tuple[int, int, float, float]]:
        """(developer, project, hours between its status checks, when it was last checked) of the
        subscriptions that move released tickets, of developers not paused: read without decrypting
        anything, so the releases loop can see what is due before it loads anyone."""
        with closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT s.user_id, s.project_id, s.settings, s.released_at FROM subscriptions s "
                                "JOIN users u ON u.id = s.user_id WHERE u.paused = 0").fetchall()
        result = []
        for user_id, project_id, settings, released_at in rows:
            chosen = Settings.from_json(settings)
            if chosen.release_moves:
                result.append((user_id, project_id, float(chosen.release_hours), released_at or 0.0))
        return result


def pass_ownership(conn: sqlite3.Connection, user_id: int, project_id: int | None = None) -> None:
    """Each project the developer owns (only `project_id`, when given) passes to its longest-standing
    other member. A project without other members keeps them as its owner."""
    sql = ("UPDATE projects SET owner_id = (SELECT s.user_id FROM subscriptions s WHERE s.project_id = projects.id "
           "AND s.user_id != ? ORDER BY s.joined LIMIT 1) WHERE owner_id = ? AND EXISTS (SELECT 1 FROM subscriptions s "
           "WHERE s.project_id = projects.id AND s.user_id != ?)")
    args: list = [user_id, user_id, user_id]
    if project_id is not None:
        sql += " AND id = ?"
        args.append(project_id)
    conn.execute(sql, args)


def subscription_of(row: sqlite3.Row) -> Subscription:
    return Subscription(row["project_id"], row["jira_account_id"], row["github_account_id"], row["claude_account_id"],
                        row["mode"], json.loads(row["base_branches"]), Settings.from_json(row["settings"]),
                        ReleaseReport.from_json(row["release_report"]), json_object(row["tests_sent"]))


def json_object(text: str | None) -> dict:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
