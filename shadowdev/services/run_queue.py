"""The pool that works on issues. Up to MAX_PARALLEL_RUNS issues (or groups of issues fixed
together) run at once, taken from the developers in turn. Every run acts as the developer the
issues are assigned to, with the Jira, GitHub and Claude accounts of their subscription."""
import logging
import threading
import time
from collections import deque

from ..automation.cancel import CancelToken, Cancelled
from ..automation.pipeline import process_issues
from ..automation.timing import END, duration
from ..clients.atlassian import AtlassianOAuth, jira_client
from ..clients.github import GitHub
from ..config import Config
from ..models.project import Project
from ..repositories.projects import ProjectRepository
from ..repositories.runs import RunRepository
from ..repositories.users import UserRepository

log = logging.getLogger("shadowdev")

MAX_JOBS_PER_DEVELOPER = 10  # queued or running


class FairQueue:
    """Jobs waiting for a worker, handed out one developer at a time in turn: a developer with ten
    queued jobs doesn't hold up one who just queued one."""

    def __init__(self):
        self._jobs: dict[int, deque] = {}  # developer -> their waiting jobs
        self._turn: deque[int] = deque()  # developers with waiting jobs, whose turn is next first
        self._ready = threading.Condition()

    def put(self, user_id: int, job) -> None:
        with self._ready:
            if user_id not in self._jobs:
                self._jobs[user_id] = deque()
                self._turn.append(user_id)
            self._jobs[user_id].append(job)
            self._ready.notify()

    def get(self):
        with self._ready:
            while not self._turn:
                self._ready.wait()
            user_id = self._turn.popleft()
            jobs = self._jobs[user_id]
            job = jobs.popleft()
            if jobs:
                self._turn.append(user_id)
            else:
                del self._jobs[user_id]
            return job


class RunQueue:
    """Runs issues on up to `cfg.max_parallel_runs` worker threads. A job is one issue, or several
    issues of one project fixed together on one branch. Each run works in its own clones.

    Every job gets a cancel token when it is queued, so it can be stopped by its developer
    whether it is still queued (it never starts) or running (it stops at once: see cancel.py)."""

    def __init__(self, cfg: Config, users: UserRepository, projects: ProjectRepository, runs: RunRepository,
                 oauth: AtlassianOAuth):
        self.cfg = cfg
        self.users = users
        self.projects = projects
        self.runs = runs
        self.oauth = oauth
        self._queue = FairQueue()
        self._pending: set[tuple[str, str]] = set()  # (Jira site, issue key) queued or running
        self._jobs: dict[int, tuple[int, int, CancelToken]] = {}  # run id -> (developer, project, token)
        self._lock = threading.Lock()

    def start(self) -> None:
        """Queue again what was waiting when the server stopped, then start the workers."""
        for row in self.runs.resume_after_restart():
            if row["id"] in self._jobs:  # queued by this process, before the workers started
                continue
            project = self.projects.get(row["project_id"]) if row["project_id"] else None
            refusal = (self.submit(project, row["issue_key"].split(", "), row["user_id"], run_id=row["id"],
                                   note=row["note"])
                       if project else "its project was deleted")
            if refusal:
                self.runs.update(row["id"], "failed", f"not resumed after a ShadowDev restart: {refusal}")
        for n in range(1, self.cfg.max_parallel_runs + 1):
            threading.Thread(target=self.run_forever, daemon=True, name=f"run-{n}").start()

    def submit(self, project: Project, keys: list[str], user_id: int, run_id: int | None = None,
               note: str = "", automatic: bool = False) -> str | None:
        """Queue the issues as one job. Returns why not, or None when queued. `run_id`: an
        existing queued run, resumed after a restart. `note`: the developer's instructions for it.
        `automatic`: queued by a webhook or by polling, not by the developer. An issue whose last
        run failed or was stopped is then left alone: ShadowDev's own comment about the failure
        updates the issue, which would start it again, and again, each time it fails."""
        pending = {(project.cloud_id, k) for k in keys}
        if automatic:
            for key in keys:
                last = self.runs.last_status(user_id, project.id, key)
                if last in ("failed", "stopped"):
                    return f"the last run of {key} {'failed' if last == 'failed' else 'was stopped'}; run it again from the page"
        with self._lock:
            busy = sorted(k for _, k in self._pending & pending)
            if busy:
                return f"already queued or running: {', '.join(busy)}"
            if sum(1 for owner, _, _ in self._jobs.values() if owner == user_id) >= MAX_JOBS_PER_DEVELOPER:
                return f"you already have {MAX_JOBS_PER_DEVELOPER} runs queued or running"
            self._pending |= pending
            run_id = run_id or self.runs.add(", ".join(keys), user_id, project.id, project.name, note)
            self._jobs[run_id] = (user_id, project.id, CancelToken())
        self._queue.put(user_id, (project.id, tuple(keys), user_id, run_id, frozenset(pending), note))
        return None

    def run_forever(self) -> None:
        while True:
            project_id, keys, user_id, run_id, pending, note = self._queue.get()
            started = time.monotonic()
            try:
                self._run(project_id, list(keys), user_id, run_id, note)
            except Exception:
                log.exception("run %d: could not be run", run_id)
                self.runs.update(run_id, "failed", "ShadowDev could not run it; see the server log")
            finally:
                with self._lock:
                    self._pending -= pending
                    del self._jobs[run_id]
                log.info("%s run %d (%s) finished after %s", END, run_id, ", ".join(keys),
                         duration(time.monotonic() - started))

    def stop_run(self, run_id: int, user_id: int) -> bool:
        """Stop one of the developer's queued or running jobs. False if there is no such job of
        theirs (finished, or someone else's)."""
        with self._lock:
            job = self._jobs.get(run_id)
            if not job or job[0] != user_id:
                return False
            # Marked before cancelling, so the run's own final status ("stopped") comes after it.
            self.runs.mark_stopping(run_id)
            job[2].cancel()
        log.info("run %d: stop requested by its developer", run_id)
        return True

    def stop_runs(self, user_id: int | None = None, project_ids: set[int] | None = None) -> int:
        """Stop the queued and running jobs of a developer and/or of projects. Returns how many."""
        with self._lock:
            tokens = [token for owner, pid, token in self._jobs.values()
                      if (user_id is None or owner == user_id) and (project_ids is None or pid in project_ids)]
        for token in tokens:
            token.cancel()
        if tokens:
            log.info("stopped %d run(s) (developer %s, projects %s)", len(tokens), user_id, project_ids)
        return len(tokens)

    def _run(self, project_id: int, keys: list[str], user_id: int, run_id: int, note: str = "") -> None:
        label = ", ".join(keys)
        with self._lock:
            token = self._jobs[run_id][2]
        if token.cancelled:
            return self.runs.update(run_id, "stopped", "stopped before it started")
        # Read now, not when queued: the project or the developer's accounts may have changed.
        project, user = self.projects.get(project_id), self.users.get(user_id)
        if not project:
            return self.runs.update(run_id, "skipped", "the project was deleted")
        if not user:
            return self.runs.update(run_id, "skipped", "developer no longer registered")
        if user.paused:
            return self.runs.update(run_id, "skipped", "automation is paused")
        actor = user.actor(project.id)
        if not actor:
            return self.runs.update(run_id, "skipped", f"you left {project.name}, or its GitHub or Claude account "
                                                       "was removed")
        self.runs.update(run_id, "running")
        jira = jira_client(self.oauth, self.users, user.subscriptions[project.id].jira_account_id, project.cloud_id)
        try:
            status, message = process_issues(keys, project, actor, self.cfg, jira, GitHub(actor.github.token),
                                             cancel=token, note=note,
                                             on_model=lambda models: self.runs.set_model(run_id, models))
            log.info("%s: %s: %s", label, status, message)
            self.runs.update(run_id, status, message)
        except Cancelled:
            log.info("%s: stopped; nothing more was pushed or written to Jira", label)
            self.runs.update(run_id, "stopped", "stopped: nothing more was pushed or written to Jira; "
                                                "the ticket was left as it was")
        except Exception as e:
            log.exception("%s: pipeline failed", label)
            self.runs.update(run_id, "failed", str(e))
            for key in keys:
                try:
                    jira.comment(key, "ShadowDev could not finish working on this issue. The reason is under "
                                      "Recent runs on the ShadowDev page; it runs again when started from there.")
                except Exception:
                    log.exception("%s: could not report failure to Jira", key)
