"""Pull requests in review: the ones ShadowDev opened, watched for review comments, and the ones a
developer was asked in Slack to review."""
import sqlite3
import time
from contextlib import closing

from .database import Database

KEPT_SECONDS = 30 * 24 * 3600  # a review request is forgotten after this long


class ReviewRepository:
    def __init__(self, db: Database):
        self.db = db

    # PRs ShadowDev opened

    def track_pr(self, user_id: int, project_id: int, repo: str, github: str, number: int, branch: str,
                 slack: tuple[str, str] | None) -> None:
        """`slack`: (channel, timestamp) of the message that asked for its review."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO pull_requests (user_id, project_id, repo, github, number, branch, "
                         "slack_channel, slack_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (user_id, project_id, repo, github, number, branch, *(slack or (None, None))))

    def tracked_prs(self) -> list[sqlite3.Row]:
        with closing(self.db.connect()) as conn:
            return conn.execute("SELECT * FROM pull_requests ORDER BY id").fetchall()

    def pr_handled(self, pr_id: int, newest_comment: str) -> None:
        """Its review comments up to `newest_comment` (their time) were acted on, in one more round."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE pull_requests SET handled_at = ?, rounds = rounds + 1 WHERE id = ?", (newest_comment, pr_id))

    def ci_handled(self, pr_id: int, sha: str) -> None:
        """The failing CI of head commit `sha` was acted on, in one more round."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE pull_requests SET ci_sha = ?, ci_rounds = ci_rounds + 1 WHERE id = ?", (sha, pr_id))

    def untrack_pr(self, pr_id: int) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute("DELETE FROM pull_requests WHERE id = ?", (pr_id,))

    # PRs a developer was asked to review

    def request(self, user_id: int, project_id: int, github: str, number: int, channel: str, ts: str, last_ts: str) -> None:
        """The developer was asked (again) to review the PR, in the Slack thread `ts`; `last_ts` is
        the message that asked."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("DELETE FROM reviews WHERE created < ?", (time.time() - KEPT_SECONDS,))
            conn.execute("INSERT INTO reviews (user_id, project_id, github, number, channel, ts, last_ts, state, created) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, 'requested', ?) ON CONFLICT (user_id, github, number) DO UPDATE SET "
                         "project_id = excluded.project_id, channel = excluded.channel, ts = excluded.ts, "
                         "last_ts = excluded.last_ts, state = 'requested'",
                         (user_id, project_id, github, number, channel, ts, last_ts, time.time()))

    def in_state(self, *states: str) -> list[sqlite3.Row]:
        with closing(self.db.connect()) as conn:
            return conn.execute(f"SELECT * FROM reviews WHERE state IN ({','.join('?' * len(states))}) ORDER BY id",  # noqa: S608
                                states).fetchall()

    def finish(self, review_id: int, state: str) -> None:
        """A review round ended: commented, approved, failed, or closed (nothing more to do)."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE reviews SET state = ?, rounds = rounds + 1 WHERE id = ?", (state, review_id))
