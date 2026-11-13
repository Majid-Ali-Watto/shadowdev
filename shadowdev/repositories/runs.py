"""Runs ShadowDev did, shown to the developer in the UI, and the webhook events it accepted."""
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import replace

from ..models.run import PAGE_SIZE, STATUSES, RunFilter, RunPage
from .database import Database

KEPT_RUNS = 200  # finished runs kept per developer; older ones are dropped


class RunRepository:
    def __init__(self, db: Database):
        self.db = db

    def add(self, issue_key: str, user_id: int, project_id: int, project: str, note: str = "", repos: str = "",
            batch: int | None = None, follows: int | None = None, delay: float = 0) -> int:
        """`repos`: the repos the developer chose to change, separated by commas; `batch`: the runs
        whose PRs get one review request; `follows`: the run it starts `delay` seconds after."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("DELETE FROM runs WHERE user_id = ? AND status NOT IN ('queued', 'running', 'stopping') AND id NOT IN "
                         "(SELECT id FROM runs WHERE user_id = ? ORDER BY id DESC LIMIT ?)", (user_id, user_id, KEPT_RUNS - 1))
            return conn.execute(
                "INSERT INTO runs (issue_key, user_id, project_id, project, note, repos, batch, follows, delay, status, "
                "updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                (issue_key, user_id, project_id, project, note, repos, batch, follows, delay, time.time()),
            ).lastrowid

    def update(self, run_id: int, status: str, message: str = "") -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE runs SET status = ?, message = ?, updated = ? WHERE id = ?",
                         (status, message[:2000], time.time(), run_id))

    def set_record(self, run_id: int, seconds: float, cost: float, turns: int, log: str) -> None:
        """What a run that has ended took (see automation/runlog.py), and what was logged while it
        worked: kept with the run, and deleted with it."""
        with closing(self.db.connect()) as conn, conn:
            if conn.execute("UPDATE runs SET seconds = ?, cost = ?, turns = ? WHERE id = ?",
                            (seconds, cost, turns, run_id)).rowcount and log:
                conn.execute("INSERT OR REPLACE INTO run_logs (run_id, log) VALUES (?, ?)", (run_id, log))

    def log(self, user_id: int, run_id: int) -> str | None:
        """What was logged while one of the developer's runs worked; None for another's, or without one."""
        with closing(self.db.connect()) as conn:
            row = conn.execute("SELECT l.log FROM run_logs l JOIN runs r ON r.id = l.run_id WHERE r.id = ? AND r.user_id = ?",
                               (run_id, user_id)).fetchone()
        return row[0] if row else None

    def ended_at(self, run_id: int) -> float:
        """When the run ended; 0 when it is gone, or has not ended."""
        with closing(self.db.connect()) as conn:
            row = conn.execute("SELECT updated FROM runs WHERE id = ? AND status NOT IN ('queued', 'running', 'stopping')",
                               (run_id,)).fetchone()
        return row["updated"] if row else 0

    def set_prs(self, run_id: int, prs: list[dict]) -> None:
        """The PRs the run opened, kept for the one review request of its batch."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE runs SET prs = ? WHERE id = ?", (json.dumps(prs), run_id))

    def batches(self) -> list[int]:
        with closing(self.db.connect()) as conn:
            return [row[0] for row in conn.execute("SELECT DISTINCT batch FROM runs WHERE batch IS NOT NULL")]

    def take_batch(self, batch: int) -> list[sqlite3.Row]:
        """Once no run of the batch is queued or running: its runs that opened PRs, each returned
        once (the batch is then over). Before that, and after, nothing."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM runs WHERE batch = ? AND status IN ('queued', 'running', 'stopping')",
                            (batch,)).fetchone():
                return []
            rows = conn.execute("SELECT * FROM runs WHERE batch = ? AND prs != '' ORDER BY id", (batch,)).fetchall()
            conn.execute("UPDATE runs SET batch = NULL, prs = '' WHERE batch = ?", (batch,))
            return rows

    def set_message(self, run_id: int, message: str) -> None:
        """What a queued or running run waits for; "" when it no longer waits."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE runs SET message = ?, updated = ? WHERE id = ? AND status IN ('queued', 'running')",
                         (message[:2000], time.time(), run_id))

    def delete_finished(self, user_id: int, run_ids: list[int]) -> int:
        """Remove the developer's own finished runs from their history; how many were removed. Runs that are
        queued, running or being stopped are kept: they are stopped first."""
        with closing(self.db.connect()) as conn, conn:
            return conn.execute(
                f"DELETE FROM runs WHERE user_id = ? AND status NOT IN ('queued', 'running', 'stopping') "  # noqa: S608
                f"AND id IN ({','.join('?' * len(run_ids))})", (user_id, *run_ids)).rowcount

    def set_model(self, run_id: int, model: str) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE runs SET model = ? WHERE id = ?", (model[:200], run_id))

    def set_questions(self, run_id: int, questions: dict[str, list[str]]) -> None:
        """The questions the run asked its developer, by issue key, for their next run of it."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE runs SET questions = ? WHERE id = ?", (json.dumps(questions), run_id))

    def asked(self, user_id: int, project_id: int, keys: list[str], before: int | None = None) -> dict[str, list[str]]:
        """The questions the developer's latest run of each issue (before the run `before`, the one
        asking) asked them, when that run ended waiting for their answers (status "question")."""
        found = {}
        with closing(self.db.connect()) as conn:
            for key in keys:
                row = conn.execute(
                    "SELECT status, questions FROM runs WHERE user_id = ? AND project_id = ? AND id < ? AND "
                    "instr(', ' || issue_key || ', ', ?) > 0 ORDER BY id DESC LIMIT 1",
                    (user_id, project_id, before or 2**62, f", {key}, ")).fetchone()
                if row and row["status"] == "question" and row["questions"]:
                    questions = json.loads(row["questions"]).get(key)
                    if questions:
                        found[key] = questions
        return found

    def get(self, user_id: int, run_id: int) -> sqlite3.Row | None:
        """One of the developer's runs; None for another's."""
        with closing(self.db.connect()) as conn:
            return conn.execute("SELECT * FROM runs WHERE id = ? AND user_id = ?", (run_id, user_id)).fetchone()

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
            # Work on a PR (named acme/fe#12) is not queued again from here: what asked for it is still there.
            conn.execute("UPDATE runs SET status = 'failed', message = 'interrupted by a ShadowDev restart; it starts "
                         "again by itself', updated = ? WHERE status = 'queued' AND issue_key LIKE '%#%'", (time.time(),))
            return conn.execute("SELECT * FROM runs WHERE status = 'queued' ORDER BY id").fetchall()

    def recent(self, user_id: int) -> list[sqlite3.Row]:
        """The developer's latest runs: the first page, unfiltered."""
        return self.search(user_id, RunFilter()).rows

    def search(self, user_id: int, run_filter: RunFilter) -> RunPage:
        """The developer's runs the filter finds, newest first, one page of them. A page past the
        last (runs were deleted meanwhile) gives the last."""
        f = run_filter
        where, args = ["r.user_id = ?"], [user_id]
        if f.text:
            like = "%" + f.text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(r.issue_key LIKE ? ESCAPE '\\' OR r.message LIKE ? ESCAPE '\\' OR r.note LIKE ? ESCAPE '\\')")
            args += [like] * 3
        if f.status:
            statuses = STATUSES[f.status][1]
            where.append(f"r.status IN ({','.join('?' * len(statuses))})")
            args += statuses
        if f.project:
            where.append("r.project = ?")
            args.append(f.project)
        if f.kind:
            where.append("instr(r.issue_key, '#') " + ("> 0" if f.kind == "pr" else "= 0"))
        sql_where = " AND ".join(where)  # only the fixed conditions above; every value is a parameter
        with closing(self.db.connect()) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM runs r WHERE {sql_where}", args).fetchone()[0]  # noqa: S608
            f = replace(f, page=min(f.page, max(1, -(-total // PAGE_SIZE))))
            rows = conn.execute(
                "SELECT r.*, s.url AS site_url, EXISTS (SELECT 1 FROM run_logs l WHERE l.run_id = r.id) AS has_log "  # noqa: S608
                "FROM runs r LEFT JOIN projects p ON p.id = r.project_id "
                f"LEFT JOIN sites s ON s.cloud_id = p.cloud_id WHERE {sql_where} ORDER BY r.id DESC LIMIT ? OFFSET ?",
                (*args, PAGE_SIZE, (f.page - 1) * PAGE_SIZE)).fetchall()
            projects = [row[0] for row in conn.execute(
                "SELECT DISTINCT project FROM runs WHERE user_id = ? ORDER BY project COLLATE NOCASE", (user_id,))]
        return RunPage(rows, total, f, projects)

    def first_time(self, event_id: str, at: int, forget_before: int) -> bool:
        """Record a webhook event; False if it was seen before. Events older than `forget_before`
        are dropped: their timestamp is too old to be accepted again anyway."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("DELETE FROM webhook_events WHERE at < ?", (forget_before,))
            return conn.execute("INSERT OR IGNORE INTO webhook_events VALUES (?, ?)", (event_id, at)).rowcount == 1
