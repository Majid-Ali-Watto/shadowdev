"""The pool that works on issues. Each developer's issues (or groups of issues fixed together) run up
to their own Limits.max_parallel_runs at once, and at most MAX_WORKERS in all, taken from the
developers in turn. Every run acts as the developer the
issues are assigned to, with the Jira, GitHub and Claude accounts of their subscription."""
import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable

from ..automation.cancel import Cancelled, CancelToken
from ..automation.pipeline import process_issues
from ..automation.timing import END, duration
from ..clients.atlassian import AtlassianOAuth, jira_client
from ..clients.github import GitHub
from ..config import Config
from ..models.issue import NOT_FINISHED
from ..models.project import Project
from ..repositories.projects import ProjectRepository
from ..repositories.reviews import ReviewRepository
from ..repositories.runs import RunRepository
from ..repositories.users import UserRepository

log = logging.getLogger("shadowdev")

MAX_JOBS_PER_DEVELOPER = 10  # queued or running
MAX_WORKERS = 20  # runs at the same time, of all developers: what one machine can take


class FairQueue:
    """Jobs waiting for a worker, handed out one developer at a time in turn: a developer with ten
    queued jobs doesn't hold up one who just queued one. A developer with `limit(developer)` jobs
    running gets no more until one is done(), or wake() says a limit was raised."""

    def __init__(self, limit: Callable[[int], int] = lambda user_id: MAX_WORKERS):
        self._jobs: dict[int, deque] = {}  # developer -> their waiting jobs
        self._turn: deque[int] = deque()  # developers with waiting jobs, whose turn is next first
        self._running: Counter[int] = Counter()  # developer -> their jobs handed out and not done
        self._limit = limit
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
            while True:
                user_id = next((u for u in self._turn if self._running[u] < self._limit(u)), None)
                if user_id is not None:
                    break
                self._ready.wait()
            self._turn.remove(user_id)
            jobs = self._jobs[user_id]
            job = jobs.popleft()
            if jobs:
                self._turn.append(user_id)
            else:
                del self._jobs[user_id]
            self._running[user_id] += 1
            return job

    def done(self, user_id: int) -> None:
        with self._ready:
            self._running[user_id] -= 1
            self._ready.notify_all()

    def wake(self) -> None:
        with self._ready:
            self._ready.notify_all()


class RunQueue:
    """Runs issues on `workers` threads, each developer's up to their own limit. A job is one issue, or several
    issues of one project fixed together on one branch. Each run works in its own clones.

    Every job gets a cancel token when it is queued, so it can be stopped by its developer
    whether it is still queued (it never starts) or running (it stops at once: see cancel.py)."""

    def __init__(self, cfg: Config, users: UserRepository, projects: ProjectRepository, runs: RunRepository,
                 oauth: AtlassianOAuth, reviews: ReviewRepository | None = None, workers: int = MAX_WORKERS):
        self.cfg = cfg
        self.workers = workers
        self.users = users
        self.projects = projects
        self.runs = runs
        self.oauth = oauth
        self.reviews = reviews  # set: the PRs runs open are tracked, to act on their review comments
        self._queue = FairQueue(lambda user_id: users.limits(user_id).max_parallel_runs)
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
        for n in range(1, self.workers + 1):
            threading.Thread(target=self.run_forever, daemon=True, name=f"run-{n}").start()

    def submit(self, project: Project, keys: list[str], user_id: int, run_id: int | None = None,
               note: str = "", automatic: bool = False, work=None, after=None) -> str | None:
        """Queue the issues as one job. Returns why not, or None when queued. `run_id`: an
        existing queued run, resumed after a restart. `note`: the developer's instructions for it.
        `automatic`: queued by a webhook or by polling, not by the developer. An issue whose last
        run failed or was stopped is then left alone: ShadowDev's own comment about the failure
        updates the issue, which would start it again, and again, each time it fails.

        `work`: a job that is not about issues (a PR's review comments, a review): `keys` is then
        its name (["acme/fe#12"]), and work(project, actor, cfg, github, cancel=, on_model=) does
        it, returning (status, message). `after()` is called when the job is over, however it
        ended, also when it never started."""
        pending = {(project.cloud_id, k) for k in keys}
        if automatic:
            for key in keys:
                last = self.runs.last_status(user_id, project.id, key)
                if last == "question":  # an update to the ticket is no answer: the developer's note is
                    return f"the last run of {key} asked you questions; answer them in a note when you run it from the page"
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
        self._queue.put(user_id, (project.id, tuple(keys), user_id, run_id, frozenset(pending), note, work, after))
        return None

    def run_forever(self) -> None:
        while True:
            project_id, keys, user_id, run_id, pending, note, work, after = self._queue.get()
            started = time.monotonic()
            try:
                self._run(project_id, list(keys), user_id, run_id, note, work)
            except Exception:
                log.exception("run %d: could not be run", run_id)
                self.runs.update(run_id, "failed", "ShadowDev could not run it; see the server log")
            finally:
                with self._lock:
                    self._pending -= pending
                    del self._jobs[run_id]
                self._queue.done(user_id)
                if after:
                    try:
                        after()
                    except Exception:
                        log.exception("run %d: could not record its end", run_id)
                log.info("%s run %d (%s) finished after %s", END, run_id, ", ".join(keys),
                         duration(time.monotonic() - started))

    def limits_changed(self) -> None:
        """A developer changed their limits: runs waiting for a free slot of theirs look again."""
        self._queue.wake()

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

    def _track(self, user_id: int, project_id: int, repo, number: int, branch: str, slack) -> None:
        if self.reviews:
            self.reviews.track_pr(user_id, project_id, repo.name, repo.github, number, branch, slack)

    def _run(self, project_id: int, keys: list[str], user_id: int, run_id: int, note: str = "", work=None) -> None:
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
        cfg = self.cfg.for_run(user.limits)
        on_model = lambda models: self.runs.set_model(run_id, models)
        # A note after a run that asked the developer answers its questions: Claude gets them together.
        asked = self.runs.asked(user_id, project_id, keys, before=run_id) if note and not work else {}
        try:
            if work:
                status, message = work(project, actor, cfg, GitHub(actor.github.token), cancel=token, on_model=on_model)
            else:
                status, message = process_issues(keys, project, actor, cfg, jira, GitHub(actor.github.token),
                                                 cancel=token, note=note, on_model=on_model,
                                                 on_pr=lambda repo, number, branch, slack: self._track(
                                                     user_id, project_id, repo, number, branch, slack),
                                                 asked=asked, on_questions=lambda q: self.runs.set_questions(run_id, q))
            log.info("%s: %s: %s", label, status, message)
            self.runs.update(run_id, status, message)
        except Cancelled:
            log.info("%s: stopped; nothing more was pushed or written to Jira", label)
            self.runs.update(run_id, "stopped", "stopped: nothing more was pushed or written to Jira; "
                                                "the ticket was left as it was")
        except Exception as e:
            log.exception("%s: pipeline failed", label)
            self.runs.update(run_id, "failed", str(e))
            for key in keys if not work and actor.settings.jira_comments else ():
                try:
                    jira.comment(key, NOT_FINISHED)  # the reason is under Recent runs, on the developer's page
                except Exception:
                    log.exception("%s: could not report failure to Jira", key)
