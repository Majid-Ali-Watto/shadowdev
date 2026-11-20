"""The pool that works on issues. Each developer's issues (or groups of issues fixed together) run up
to their own Limits.max_parallel_runs at once, each project's up to its own Settings.parallel_runs
(its Settings.priority first, then the most urgent issue by its Jira priority), and at most
MAX_WORKERS in all, taken from the developers in turn (services/fair_queue.py). Every run acts as the
developer the issues are assigned to, with the Jira, GitHub and Claude accounts of their subscription.
Runs work at any time; outside the developer's working hours a run's PRs are held for them
(services/outbox.py)."""
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

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
from ..repositories.held import HeldRepository
from ..repositories.projects import ProjectRepository
from ..repositories.reviews import ReviewRepository
from ..repositories.runs import RunRepository
from ..repositories.users import UserRepository
from .fair_queue import FairQueue
from .outbox import Holding

log = logging.getLogger("shadowdev")

MAX_JOBS_PER_PROJECT = 10  # of one developer in one project, queued or running
MAX_WORKERS = 20  # runs at the same time, of all developers: what one machine can take
STOP_SECONDS = 10  # what a run being stopped at shutdown gets to end and record that


@dataclass(frozen=True)
class Job:
    """One job: an issue, or several issues of one project fixed together on one branch (`keys`), or
    other work (`work`: a PR's review comments, a review, where `keys` is its name, ["acme/fe#12"]).

    `note`: the developer's instructions. `automatic`: queued by a webhook or by polling, not by the
    developer. `after()` is called when the job is over, however it ended, also when it never
    started. `claude`: False for work that asks Claude nothing (a PR approved without a review), which
    the Claude account's usage limits then don't hold. `repos`: the repos the developer chose to
    change (none: Claude decides). `batch`: the runs started with this one, whose PRs get one review
    request once the last of them has ended. `follows`: the run this one starts after, `delay` seconds
    after it has ended. `urgency`: by the issue's Jira priority (rules.urgency). `run_id`: an existing
    queued run, resumed after a restart."""
    project: Project
    keys: tuple[str, ...]
    user_id: int
    note: str = ""
    automatic: bool = False
    work: Callable | None = None
    after: Callable | None = None
    claude: bool = True
    repos: tuple[str, ...] = ()
    batch: int | None = None
    follows: int | None = None
    delay: float = 0
    urgency: int = 2
    run_id: int | None = None


def _pending_of(job: Job) -> set[tuple[str, str]]:
    """(Jira site, issue key) of each issue the job is about: none of them may be queued twice."""
    return {(job.project.cloud_id, key) for key in job.keys}


class RunQueue:
    """Runs issues on `workers` threads, each developer's up to their own limit. Each run works in its
    own clones. Every job gets a cancel token when it is queued, so it can be stopped by its developer
    whether it is still queued (it never starts) or running (it stops at once: see cancel.py)."""

    def __init__(self, cfg: Config, users: UserRepository, projects: ProjectRepository, runs: RunRepository,
                 oauth: AtlassianOAuth, reviews: ReviewRepository | None = None, workers: int = MAX_WORKERS,
                 held: HeldRepository | None = None):
        self.cfg = cfg
        self.held = held  # set: runs outside their developer's working hours leave their PRs for them
        self.workers = workers
        self.users = users
        self.projects = projects
        self.runs = runs
        self.oauth = oauth
        self.reviews = reviews  # set: the PRs runs open are tracked, to act on their review comments
        self._queue = FairQueue(lambda user_id: users.limits(user_id).max_parallel_runs, self._lane)
        self._pending: set[tuple[str, str]] = set()  # (Jira site, issue key) queued or running
        self._jobs: dict[int, tuple[int, int, CancelToken]] = {}  # run id -> (developer, project, token)
        self._records: dict[int, runlog.Record] = {}  # run id -> what it logged so far, while a worker is on it
        # Runs of a sequence not handed to the workers yet: run id -> (the run it follows, its job), and,
        # while a timer is to start it, run id -> (timer, its job).
        self._held: dict[int, tuple[int, Job]] = {}
        self._timers: dict[int, tuple[threading.Timer, Job]] = {}
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
                                   follows=row["follows"], delay=row["delay"], urgency=row["urgency"])
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
               delay: float = 0, never_run: bool = False, urgency: int = 2) -> str | None:
        """Queue the issues as one job (see Job). Returns why not, or None when queued; see queue().
        `never_run`: only when the developer never ran these issues before."""
        if never_run and any(self.runs.last_status(user_id, project.id, k) for k in keys):
            return "already run before"
        job = Job(project, tuple(keys), user_id, note, automatic, work, after, claude, tuple(repos), batch, follows,
                  delay, urgency, run_id)
        return self.queue(job)[0]

    def submit_sequence(self, items: list[tuple[Project, list[str], str, tuple[str, ...], int | None]], user_id: int,
                        delay: float) -> list[str | None]:
        """Queue jobs that run one after another, each `delay` seconds after the one before has
        ended (however it ended). `items`: (project, keys, note, repos, batch) per job. Returns,
        per job, why it was not queued, or None; one that is not queued is left out of the sequence."""
        refusals, before = [], None
        for project, keys, note, repos, batch in items:
            refusal, run_id = self.queue(Job(project, tuple(keys), user_id, note=note, repos=repos, batch=batch,
                                             follows=before, delay=delay))
            refusals.append(refusal)
            before = run_id or before
        return refusals

    def queue(self, job: Job) -> tuple[str | None, int | None]:
        """Queue a job. Returns (why not, None), or (None, its run). The developer's runs of a project
        are limited (MAX_JOBS_PER_PROJECT); an issue already queued or running is refused. `automatic`
        jobs leave alone an issue whose last run failed or was stopped, or asked questions: ShadowDev's
        own comment about the failure updates the issue, which would start it again, and again."""
        pending = _pending_of(job)
        if job.automatic:
            for key in job.keys:
                last = self.runs.last_status(job.user_id, job.project.id, key)
                if last == "question":  # an update to the ticket is no answer: the developer's note is
                    return f"the last run of {key} asked you questions; answer them in a note when you run it from the page", None
                if last in ("failed", "stopped"):
                    return (f"the last run of {key} {'failed' if last == 'failed' else 'was stopped'}; run it again from "
                            "the page"), None
        with self._lock:
            busy = sorted(k for _, k in self._pending & pending)
            if busy:
                return f"already queued or running: {', '.join(busy)}", None
            if sum(1 for owner, p, _ in self._jobs.values() if (owner, p) == (job.user_id, job.project.id)) >= MAX_JOBS_PER_PROJECT:
                return f"you already have {MAX_JOBS_PER_PROJECT} runs of {job.project.name} queued or running", None
            self._pending |= pending
            run_id = job.run_id or self.runs.add(", ".join(job.keys), job.user_id, job.project.id, job.project.name, job.note,
                                                 ",".join(job.repos), job.batch, job.follows, job.delay, job.urgency)
            job = replace(job, run_id=run_id)
            self._jobs[run_id] = (job.user_id, job.project.id, CancelToken())
            waits = job.follows is not None and job.follows in self._jobs  # the run before it is still queued or running
            if waits:
                self._held[run_id] = (job.follows, job)
        if waits:
            self.runs.set_message(run_id, "starts after the issue before it"
                                  + (f", {duration(job.delay)} later" if job.delay else ""))
        else:  # by itself, or the run before it has ended (as a restart finds it): what is left of the delay
            ended = self.runs.ended_at(job.follows) if job.follows is not None and job.delay else 0
            self._start_later(job, ended + job.delay - time.time() if ended else 0)
        return None, run_id

    def _put(self, job: Job) -> None:
        self._queue.put(job.user_id, job.project.id, job, job.urgency)

    def _start_later(self, job: Job, seconds: float) -> None:
        """Hand the job to the workers, now or in `seconds`."""
        if seconds <= 0:
            return self._put(job)
        run_id = job.run_id

        def start() -> None:
            with self._lock:
                waiting = self._timers.pop(run_id, None)
            if waiting:
                self.runs.set_message(run_id, "")
                self._put(job)

        timer = threading.Timer(seconds, start)
        timer.daemon = True
        with self._lock:
            self._timers[run_id] = (timer, job)
        self.runs.set_message(run_id, f"starts at {limits.when(time.time() + seconds)}, {duration(seconds)} after the issue "
                                      "before it ended")
        timer.start()

    def _release(self, ended: int) -> None:
        """The run `ended` is over: the runs that follow it start, after their delay."""
        with self._lock:
            following = [self._held.pop(run_id)[1] for run_id in [r for r, (before, _) in self._held.items() if before == ended]]
        for job in following:
            self._start_later(job, job.delay)

    def _start_now(self, run_ids: list[int]) -> None:
        """Runs being stopped while they wait for their turn go to the workers at once, which end them."""
        for run_id in run_ids:
            with self._lock:
                held, timed = self._held.pop(run_id, None), self._timers.pop(run_id, None)
            if timed:
                timed[0].cancel()
            if held or timed:
                self._put(held[1] if held else timed[1])

    def run_forever(self) -> None:
        while True:
            job = self._queue.get()
            with self._lock:
                stopping = self._closing  # the job stays queued in the database, and is queued again at the next start
                if not stopping:
                    self._working += 1
            started = time.monotonic()
            try:
                if not stopping:
                    self._execute(job, started)
            finally:
                self._end(job, started, ran=not stopping)

    def _execute(self, job: Job, started: float) -> None:
        """Run the job on this worker, recording its log and cost with the run."""
        run_id = job.run_id
        try:
            with runlog.recording(on_step=lambda name: self.runs.set_step(run_id, name)) as record:
                with self._lock:
                    self._records[run_id] = record
                try:
                    self._run(job)
                except Exception:
                    log.exception("run %d: could not be run", run_id)
                    self.runs.update(run_id, "failed", "ShadowDev could not run it; see the server log")
            self.runs.set_record(run_id, time.monotonic() - started, record.cost, record.turns, record.log)
        except Exception:
            log.exception("run %d: could not keep its log", run_id)

    def _end(self, job: Job, started: float, ran: bool) -> None:
        """The job is over, however it ended. What records its end runs first (a review's or a fix's
        `after`, and the review request of a batch), while the job still holds its issues, so the
        watcher can't queue the same work again meanwhile; then the issues, the worker and the
        developer's slot are released, and the runs that follow it start. A job that never ran (`ran`
        False: the server is stopping) records nothing: it is queued again at the next start."""
        run_id = job.run_id
        if ran:
            if job.after:
                try:
                    job.after()
                except Exception:
                    log.exception("run %d: could not record its end", run_id)
            if job.batch:
                try:
                    self._announce(job.batch)
                except Exception:
                    log.exception("run %d: could not ask for review of its batch", run_id)
        with self._lock:
            self._pending -= _pending_of(job)
            del self._jobs[run_id]
            self._records.pop(run_id, None)
            if ran:
                self._working -= 1
        self._release(run_id)
        self._queue.done(job.user_id, job.project.id)
        if ran:
            log.info("%s run %d (%s) finished after %s", END, run_id, ", ".join(job.keys),
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

    def _lane(self, user_id: int, project_id: int) -> tuple[int, int]:
        settings = self.users.subscriptions.settings(user_id, project_id)
        return settings.max_runs, settings.rank

    def limits_changed(self) -> None:
        """A developer changed their limits, or a project's runs at the same time or priority:
        runs waiting for a free slot of theirs look again."""
        self._queue.wake()

    def log(self, user_id: int, run_id: int) -> str | None:
        """What one of the developer's runs logged: so far while it works, else what was kept when it
        ended. None for another developer's run, or one without a log."""
        with self._lock:
            job, record = self._jobs.get(run_id), self._records.get(run_id)
        if job and record and job[0] == user_id:
            return record.log
        return self.runs.log(user_id, run_id)

    def live(self, user_id: int, seen: dict[int, int]) -> list[tuple[int, int, list[str]]]:
        """The developer's runs that are working now, each as (run id, lines logged so far, the lines
        after the `seen[run id]` first ones): what Live logs shows next. Never another developer's."""
        with self._lock:
            records = [(run_id, record) for run_id, record in self._records.items()
                       if (job := self._jobs.get(run_id)) and job[0] == user_id]
        return [(run_id, *record.since(seen.get(run_id, 0))) for run_id, record in records]

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

    def _run(self, job: Job) -> None:
        label = ", ".join(job.keys)
        with self._lock:
            token = self._jobs[job.run_id][2]
        if token.cancelled:
            return self.runs.update(job.run_id, "stopped", "stopped before it started")
        found = self._actor(job.project.id, job.user_id)
        if isinstance(found, str):
            return self.runs.update(job.run_id, "skipped", found)
        # A run the Claude account's usage limit would cut off stays queued until it can finish,
        # or starts on another of the developer's Claude accounts when they switched that on.
        actor = found[2]
        if not job.claude:
            return self._work(job, token, label, actor.claude.config_key)
        accounts = {str(claude_config_dir(self.cfg, a)): a for a in (actor.claude, *actor.spare_claudes)}
        with limits.telling(lambda message: self.runs.set_message(job.run_id, message)):
            try:
                account, started = limits.admit(list(accounts), token, label)
            except Cancelled:
                return self.runs.update(job.run_id, "stopped", "stopped before it started")
            try:
                self._work(job, token, label, accounts[account].config_key)
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

    def _work(self, job: Job, token: CancelToken, label: str, claude_key: str) -> None:
        """`claude_key`: the config key of the Claude account the run was admitted on."""
        run_id, user_id, project_id = job.run_id, job.user_id, job.project.id
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
        github = GitHub(actor.github.token)
        if self.held:  # comments on tickets and PRs, as people read them, wait for the developer's working hours
            held = lambda service, call, args: self.held.hold_call(user_id, project_id, service, call, args)
            jira, github = Holding(jira, "jira", actor.settings, held), Holding(github, "github", actor.settings, held)
        cfg = self.cfg.for_run(user.limits)
        on_model = lambda models: self.runs.set_model(run_id, models)
        # A note after a run that asked the developer answers its questions: Claude gets them together.
        # What the run before this one asked the developer: a note answers it, and so does running it again.
        asked = self.runs.asked(user_id, project_id, list(job.keys), before=run_id) if not job.work else {}
        announce = (lambda title, opened, branch: self.runs.set_prs(run_id, [
            {"title": title, "repo": repo.name, "url": url, "branch": branch} for repo, url in opened])) if job.batch else None
        try:
            if job.work:
                status, message = job.work(project, actor, cfg, github, cancel=token, on_model=on_model)
            else:
                status, message = process_issues(list(job.keys), project, actor, cfg, jira, github,
                                                 cancel=token, note=job.note, on_model=on_model,
                                                 on_pr=lambda repo, number, branch, slack: self._track(
                                                     user_id, project_id, repo, number, branch, slack),
                                                 asked=asked, on_questions=lambda q: self.runs.set_questions(run_id, q),
                                                 only=tuple(r for r in job.repos if project.repo(r)), announce=announce,
                                                 hold=(lambda publication: self.held.hold_publication(
                                                     user_id, project_id, run_id, job.batch, publication)) if self.held else None)
            log.info("%s: %s: %s", label, status, message)
            self.runs.update(run_id, status, message)
        except Cancelled:
            log.info("%s: stopped; nothing more was pushed or written to Jira", label)
            self.runs.update(run_id, "stopped", "stopped: nothing more was pushed or written to Jira; "
                                                "the ticket was left as it was")
        except Exception as e:
            log.exception("%s: pipeline failed", label)
            self.runs.update(run_id, "failed", str(e))
            for key in job.keys if not job.work and actor.settings.jira_comments else ():
                try:
                    jira.comment(key, NOT_FINISHED)  # the reason is under Recent runs, on the developer's page
                except Exception:
                    log.exception("%s: could not report failure to Jira", key)
