"""The pool that works on issues. Each developer's issues (or groups of issues fixed together) run up
to their own Limits.max_parallel_runs at once, and at most MAX_WORKERS in all, taken from the
developers in turn. Every run acts as the developer the
issues are assigned to, with the Jira, GitHub and Claude accounts of their subscription."""
import json
import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable

from ..automation import limits, runlog
from ..automation.agent import claude_config_dir
from ..automation.cancel import Cancelled, CancelToken
from ..automation.pipeline import ask_for_review, ask_for_reviews, process_issues
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
STOP_SECONDS = 10  # what a run being stopped at shutdown gets to end and record that


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
        # Runs of a sequence not handed to the workers yet: run id -> (the run it follows, seconds
        # after it, developer, job), and, once that run has ended, run id -> (timer, developer, job).
        self._held: dict[int, tuple[int, float, int, tuple]] = {}
        self._timers: dict[int, tuple[threading.Timer, int, tuple]] = {}
        self._lock = threading.Lock()
        self._closing = False  # the server is stopping: no run starts any more
        self._working = 0  # runs a worker is on

    def start(self) -> None:
        """Queue again what was waiting when the server stopped, then start the workers."""
        for row in self.runs.resume_after_restart():
            if row["id"] in self._jobs:  # queued by this process, before the workers started
                continue
            project = self.projects.get(row["project_id"]) if row["project_id"] else None
            refusal = (self.submit(project, row["issue_key"].split(", "), row["user_id"], run_id=row["id"],
                                   note=row["note"], repos=tuple(filter(None, row["repos"].split(","))), batch=row["batch"],
                                   follows=row["follows"], delay=row["delay"])
                       if project else "its project was deleted")
            if refusal:
                self.runs.update(row["id"], "failed", f"not resumed after a ShadowDev restart: {refusal}")
        for batch in self.runs.batches():  # whose last run ended as the server stopped
            self._announce(batch)
        for n in range(1, self.workers + 1):
            threading.Thread(target=self.run_forever, daemon=True, name=f"run-{n}").start()

    def submit(self, project: Project, keys: list[str], user_id: int, run_id: int | None = None,
               note: str = "", automatic: bool = False, work=None, after=None, claude: bool = True,
               repos: tuple[str, ...] = (), batch: int | None = None, follows: int | None = None,
               delay: float = 0, never_run: bool = False) -> str | None:
        """Queue the issues as one job. Returns why not, or None when queued; see queue().
        `never_run`: only when the developer never ran these issues before."""
        if never_run and any(self.runs.last_status(user_id, project.id, k) for k in keys):
            return "already run before"
        return self.queue(project, keys, user_id, run_id, note, automatic, work, after, claude, repos, batch, follows,
                          delay)[0]

    def submit_sequence(self, items: list[tuple[Project, list[str], str, tuple[str, ...], int | None]], user_id: int,
                        delay: float) -> list[str | None]:
        """Queue jobs that run one after another, each `delay` seconds after the one before has
        ended (however it ended). `items`: (project, keys, note, repos, batch) per job. Returns,
        per job, why it was not queued, or None; one that is not queued is left out of the sequence."""
        refusals, before = [], None
        for project, keys, note, repos, batch in items:
            refusal, run_id = self.queue(project, keys, user_id, note=note, repos=repos, batch=batch, follows=before,
                                         delay=delay)
            refusals.append(refusal)
            before = run_id or before
        return refusals

    def queue(self, project: Project, keys: list[str], user_id: int, run_id: int | None = None,
              note: str = "", automatic: bool = False, work=None, after=None, claude: bool = True,
              repos: tuple[str, ...] = (), batch: int | None = None, follows: int | None = None,
              delay: float = 0) -> tuple[str | None, int | None]:
        """Queue the issues as one job. Returns (why not, None), or (None, its run). `run_id`: an
        existing queued run, resumed after a restart. `note`: the developer's instructions for it.
        `automatic`: queued by a webhook or by polling, not by the developer. An issue whose last
        run failed or was stopped is then left alone: ShadowDev's own comment about the failure
        updates the issue, which would start it again, and again, each time it fails.

        `work`: a job that is not about issues (a PR's review comments, a review): `keys` is then
        its name (["acme/fe#12"]), and work(project, actor, cfg, github, cancel=, on_model=) does
        it, returning (status, message). `after()` is called when the job is over, however it
        ended, also when it never started. `claude`: False for work that asks Claude nothing (a PR
        approved without a review), which the Claude account's usage limits then don't hold.
        `repos`: the repos the developer chose to change (none: Claude decides). `batch`: the runs
        started with this one, whose PRs get one review request once the last of them has ended.
        `follows`: the run this one starts after, `delay` seconds after it has ended."""
        pending = {(project.cloud_id, k) for k in keys}
        if automatic:
            for key in keys:
                last = self.runs.last_status(user_id, project.id, key)
                if last == "question":  # an update to the ticket is no answer: the developer's note is
                    return f"the last run of {key} asked you questions; answer them in a note when you run it from the page", None
                if last in ("failed", "stopped"):
                    return (f"the last run of {key} {'failed' if last == 'failed' else 'was stopped'}; run it again from "
                            "the page"), None
        with self._lock:
            busy = sorted(k for _, k in self._pending & pending)
            if busy:
                return f"already queued or running: {', '.join(busy)}", None
            if sum(1 for owner, _, _ in self._jobs.values() if owner == user_id) >= MAX_JOBS_PER_DEVELOPER:
                return f"you already have {MAX_JOBS_PER_DEVELOPER} runs queued or running", None
            self._pending |= pending
            run_id = run_id or self.runs.add(", ".join(keys), user_id, project.id, project.name, note, ",".join(repos), batch,
                                             follows, delay)
            self._jobs[run_id] = (user_id, project.id, CancelToken())
            job = (project.id, tuple(keys), user_id, run_id, frozenset(pending), note, work, after, claude, repos, batch)
            waits = follows is not None and follows in self._jobs  # the run before it is still queued or running
            if waits:
                self._held[run_id] = (follows, delay, user_id, job)
        if waits:
            self.runs.set_message(run_id, "starts after the issue before it"
                                  + (f", {duration(delay)} later" if delay else ""))
        else:  # by itself, or the run before it has ended (as a restart finds it): what is left of the delay
            ended = self.runs.ended_at(follows) if follows is not None and delay else 0
            self._start_later(run_id, user_id, job, ended + delay - time.time() if ended else 0)
        return None, run_id

    def _start_later(self, run_id: int, user_id: int, job: tuple, seconds: float) -> None:
        """Hand the job to the workers, now or in `seconds`."""
        if seconds <= 0:
            return self._queue.put(user_id, job)

        def start() -> None:
            with self._lock:
                waiting = self._timers.pop(run_id, None)
            if waiting:
                self.runs.set_message(run_id, "")
                self._queue.put(user_id, job)

        timer = threading.Timer(seconds, start)
        timer.daemon = True
        with self._lock:
            self._timers[run_id] = (timer, user_id, job)
        self.runs.set_message(run_id, f"starts at {limits.when(time.time() + seconds)}, {duration(seconds)} after the issue "
                                      "before it ended")
        timer.start()

    def _release(self, ended: int) -> None:
        """The run `ended` is over: the runs that follow it start, after their delay."""
        with self._lock:
            following = [(run_id, self._held.pop(run_id)) for run_id in [r for r, held in self._held.items() if held[0] == ended]]
        for run_id, (_, delay, user_id, job) in following:
            self._start_later(run_id, user_id, job, delay)

    def _start_now(self, run_ids: list[int]) -> None:
        """Runs being stopped while they wait for their turn go to the workers at once, which end them."""
        for run_id in run_ids:
            with self._lock:
                held, timed = self._held.pop(run_id, None), self._timers.pop(run_id, None)
            if timed:
                timed[0].cancel()
            if held or timed:
                self._queue.put(*(held[2:] if held else timed[1:]))

    def run_forever(self) -> None:
        while True:
            project_id, keys, user_id, run_id, pending, note, work, after, claude, repos, batch = self._queue.get()
            with self._lock:
                if self._closing:  # it stays queued in the database, and is queued again at the next start
                    continue
                self._working += 1
            started = time.monotonic()
            try:
                with runlog.recording() as record:
                    try:
                        self._run(project_id, list(keys), user_id, run_id, note, work, claude, repos, batch)
                    except Exception:
                        log.exception("run %d: could not be run", run_id)
                        self.runs.update(run_id, "failed", "ShadowDev could not run it; see the server log")
                self.runs.set_record(run_id, time.monotonic() - started, record.cost, record.turns, record.log)
            except Exception:
                log.exception("run %d: could not keep its log", run_id)
            finally:
                with self._lock:
                    self._pending -= pending
                    del self._jobs[run_id]
                    self._working -= 1
                self._release(run_id)
                self._queue.done(user_id)
                if after:
                    try:
                        after()
                    except Exception:
                        log.exception("run %d: could not record its end", run_id)
                if batch:
                    try:
                        self._announce(batch)
                    except Exception:
                        log.exception("run %d: could not ask for review of its batch", run_id)
                log.info("%s run %d (%s) finished after %s", END, run_id, ", ".join(keys),
                         duration(time.monotonic() - started))

    def drain(self, seconds: float, hurry: threading.Event) -> int:
        """The server is stopping. No run starts any more: the queued ones stay queued in the
        database and start again with the server. The runs in progress get `seconds` to finish
        (less when `hurry` is set); those still working then are stopped, as by their developer,
        so nothing of them is left running. Returns how many were stopped."""
        with self._lock:
            self._closing = True
            working = self._working
        if working:
            log.info("stopping: waiting up to %s for %d run(s) in progress to finish (again to stop them now)",
                     duration(seconds), working)
        deadline = time.monotonic() + seconds
        while self._working and time.monotonic() < deadline and not hurry.is_set():
            hurry.wait(0.2)
        with self._lock:
            # A queued run has a token too; only those a worker is on have processes to stop.
            left = self._working
            tokens = [token for _, _, token in self._jobs.values()] if left else []
        for token in tokens:
            token.cancel()
        deadline = time.monotonic() + STOP_SECONDS
        while self._working and time.monotonic() < deadline:
            time.sleep(0.1)
        return left

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
        self._start_now([run_id])
        log.info("run %d: stop requested by its developer", run_id)
        return True

    def stop_runs(self, user_id: int | None = None, project_ids: set[int] | None = None) -> int:
        """Stop the queued and running jobs of a developer and/or of projects. Returns how many."""
        with self._lock:
            stopped = {run_id: token for run_id, (owner, pid, token) in self._jobs.items()
                       if (user_id is None or owner == user_id) and (project_ids is None or pid in project_ids)}
        tokens = list(stopped.values())
        for token in tokens:
            token.cancel()
        self._start_now(list(stopped))
        if tokens:
            log.info("stopped %d run(s) (developer %s, projects %s)", len(tokens), user_id, project_ids)
        return len(tokens)

    def _track(self, user_id: int, project_id: int, repo, number: int, branch: str, slack) -> None:
        if self.reviews:
            self.reviews.track_pr(user_id, project_id, repo.name, repo.github, number, branch, slack)

    def _announce(self, batch: int) -> None:
        """Once the last run of a batch has ended: one review request in Slack for all the PRs its
        runs opened, which are then watched in that message's thread."""
        with self._lock:
            rows = self.runs.take_batch(batch)
        if not rows:
            return
        project, user = self.projects.get(rows[0]["project_id"]), self.users.get(rows[0]["user_id"])
        actor = user.actor(project.id) if project and user else None
        if not actor:
            return
        prs = [(pr["title"], project.repo(pr["repo"]), pr["url"]) for row in rows for pr in json.loads(row["prs"])
               if project.repo(pr["repo"])]
        label = ", ".join(row["issue_key"] for row in rows)
        if len({title for title, _, _ in prs}) == 1:  # only one of them opened PRs: as for a run by itself
            slack = ask_for_review(actor, project, prs[0][0], [(repo, url) for _, repo, url in prs], label)
        else:
            slack = ask_for_reviews(actor, project, prs, label)
        if slack and self.reviews:
            for _, repo, url in prs:
                self.reviews.set_pr_slack(user.id, repo.github, int(url.rstrip("/").rsplit("/", 1)[1]), slack)

    def _run(self, project_id: int, keys: list[str], user_id: int, run_id: int, note: str = "", work=None,
             claude: bool = True, repos: tuple[str, ...] = (), batch: int | None = None) -> None:
        label = ", ".join(keys)
        with self._lock:
            token = self._jobs[run_id][2]
        if token.cancelled:
            return self.runs.update(run_id, "stopped", "stopped before it started")
        found = self._actor(project_id, user_id)
        if isinstance(found, str):
            return self.runs.update(run_id, "skipped", found)
        # A run the Claude account's usage limit would cut off stays queued until it can finish,
        # or starts on another of the developer's Claude accounts when they switched that on.
        actor = found[2]
        if not claude:
            return self._work(project_id, keys, user_id, run_id, note, work, token, label, actor.claude.config_key,
                              repos, batch)
        accounts = {str(claude_config_dir(self.cfg, a)): a for a in (actor.claude, *actor.spare_claudes)}
        with limits.telling(lambda message: self.runs.set_message(run_id, message)):
            try:
                account, started = limits.admit(list(accounts), token, label)
            except Cancelled:
                return self.runs.update(run_id, "stopped", "stopped before it started")
            try:
                self._work(project_id, keys, user_id, run_id, note, work, token, label, accounts[account].config_key,
                           repos, batch)
            finally:
                limits.USAGE.done(account, started)

    def _actor(self, project_id: int, user_id: int):
        """(project, developer, the Actor of their subscription), or why the run is skipped."""
        project, user = self.projects.get(project_id), self.users.get(user_id)
        if not project:
            return "the project was deleted"
        if not user:
            return "developer no longer registered"
        if user.paused:
            return "automation is paused"
        actor = user.actor(project.id)
        if not actor:
            return f"you left {project.name}, or its GitHub or Claude account was removed"
        return project, user, actor

    def _work(self, project_id: int, keys: list[str], user_id: int, run_id: int, note: str, work, token: CancelToken,
              label: str, claude_key: str, repos: tuple[str, ...] = (), batch: int | None = None) -> None:
        """`claude_key`: the config key of the Claude account the run was admitted on."""
        # Read now, not when queued: the project or the developer's accounts may have changed.
        found = self._actor(project_id, user_id)
        if isinstance(found, str):
            return self.runs.update(run_id, "skipped", found)
        project, user, actor = found
        if actor.claude.config_key != claude_key:
            actor = actor.with_claude(claude_key)
            log.info("%s: its Claude account is at its limit; working with %s", label, actor.claude.label)
        self.runs.update(run_id, "running")
        jira = jira_client(self.oauth, self.users, user.subscriptions[project.id].jira_account_id, project.cloud_id)
        cfg = self.cfg.for_run(user.limits)
        on_model = lambda models: self.runs.set_model(run_id, models)
        # A note after a run that asked the developer answers its questions: Claude gets them together.
        # What the run before this one asked the developer: a note answers it, and so does running it again.
        asked = self.runs.asked(user_id, project_id, keys, before=run_id) if not work else {}
        announce = (lambda title, opened, branch: self.runs.set_prs(run_id, [
            {"title": title, "repo": repo.name, "url": url, "branch": branch} for repo, url in opened])) if batch else None
        try:
            if work:
                status, message = work(project, actor, cfg, GitHub(actor.github.token), cancel=token, on_model=on_model)
            else:
                status, message = process_issues(keys, project, actor, cfg, jira, GitHub(actor.github.token),
                                                 cancel=token, note=note, on_model=on_model,
                                                 on_pr=lambda repo, number, branch, slack: self._track(
                                                     user_id, project_id, repo, number, branch, slack),
                                                 asked=asked, on_questions=lambda q: self.runs.set_questions(run_id, q),
                                                 only=tuple(r for r in repos if project.repo(r)), announce=announce)
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
