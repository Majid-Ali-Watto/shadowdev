"""Web sessions. Only a hash of the cookie value is stored."""
import hashlib
import secrets
import time
from contextlib import closing

from .database import Database

SESSION_SECONDS = 12 * 3600


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, user_id: int) -> str:
        """A new session; returns the cookie value."""
        token = secrets.token_urlsafe(32)
        with closing(self.db.connect()) as conn, conn:
            conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, csrf, expires) VALUES (?, ?, ?, ?)",
                (_hash(token), user_id, secrets.token_urlsafe(32), time.time() + SESSION_SECONDS),
            )
        return token

    def get(self, token: str) -> tuple[int, str] | None:
        """(user ID, csrf token) of a live session."""
        with closing(self.db.connect()) as conn:
            row = conn.execute(
                "SELECT user_id, csrf FROM sessions WHERE token_hash = ? AND expires > ?", (_hash(token), time.time())
            ).fetchone()
        return (row["user_id"], row["csrf"]) if row else None

    def end(self, token: str) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))
