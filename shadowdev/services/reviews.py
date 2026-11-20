"""Watching pull requests in review, for every active developer, GitHub and Slack each as often as
the developer asked (Limits.github_poll_minutes and slack_poll_minutes, a minute by default):

- the PRs ShadowDev opened for them: when reviewers have commented, a run addresses the comments
  (automation/review.address_review); when one conflicts with its base branch, a run merges that
  in and resolves the conflicts (fix_conflicts); when their CI fails, a run fixes it (fix_ci); when one has
  had no review for the developer's Settings.remind_hours since it was asked for in Slack, its
  reviewers are asked again in that thread, at most once a day and MAX_REMINDERS times;
- the Slack channel of each project they joined, read as them: when someone mentions them there
  with a pull request of the project, a run reviews it (review_pr); and when, in that thread,
  they are asked to review again, it is reviewed again. The message that asked gets 👀 while its
  PRs are reviewed, then ✅ when all were approved, or 📝 when some got comments. When someone
  asked with the developer about a PR reacted 👀 first, the PR is theirs and is not reviewed.
  Mentioned later in the thread of a message that asked others, and asked to review there, the
  developer reviews the PRs of that first message, unless someone it asked has started (👀).

GitHub and Slack are asked, not listened to, so this works on a machine neither can reach. What
the Slack messages say is read by services/review_requests.py.
"""
import logging
import random
import threading
import time
from collections.abc import Callable
from datetime import datetime

from ..automation.review import (
    address_review,
    failed_checks,
    fix_ci,
    fix_conflicts,
    is_approved,
    review_pr,
    review_state,
    waiting_threads,
)
from ..clients.github import GitHub
from ..clients.rest import HTTPError
from ..clients.slack import Slack, link
from ..config import Config
from ..models.limits import Limits
from ..models.project import Project
from ..models.user import User
from ..repositories.held import HeldRepository
from ..repositories.projects import ProjectRepository
from ..repositories.reviews import ReviewRepository
from ..repositories.users import UserRepository
from .errors import UserError
from .review_requests import REVIEWING, asked_in_reply, asked_prs, asks_again, claimed_by
from .run_queue import RunQueue

log = logging.getLogger("shadowdev")

MAX_ROUNDS = 5  # of fixes, and of reviews, per pull request: two ShadowDevs can't go back and forth forever
MAX_CI_ROUNDS = 3  # of CI fixes per pull request
MAX_CONFLICT_ROUNDS = 3  # of merges of the base branch per pull request
MAX_REMINDERS = 3  # of a PR's reviewers, in its Slack thread
REMIND_EVERY = 24 * 3600  # seconds between two reminders of the same PR
CI_FAILED = {"FAILURE", "ERROR"}
# Reactions on the message that asked for reviews: while they run, and once all are done.
ALL_APPROVED, SOME_COMMENTED, SOME_FAILED = "white_check_mark", "memo", "warning"
TICK_SECONDS = 15  # how often it looks for developers due a poll: the shortest interval allowed
RECENT_MESSAGES = 20  # the channel's last messages read each poll, with their threads; older ones are not looked at


def _guarded(what: str, do: Callable, *args) -> None:
    """One PR, channel or thread that fails doesn't stop the rest. What is expected (a refused token,
    a deleted channel, a message the developer should read) is logged as a warning; anything else
    with its traceback, so a bug is not hidden."""
    try:
        do(*args)
    except (HTTPError, UserError) as e:
        log.warning("%s failed: %s", what, e)
    except Exception:
        log.exception("%s failed", what)


def _watched(user: User, project_id: int) -> bool:
    actor = user.actor(project_id)
    return bool(actor and actor.settings.watches_prs)


def _reviews_for(user: User, project_id: int) -> bool:
    actor = user.actor(project_id)
    return bool(actor and actor.settings.review_for_me)


class ReviewWatcher:
    def __init__(self, cfg: Config, users: UserRepository, projects: ProjectRepository, reviews: ReviewRepository,
                 queue: RunQueue, held: HeldRepository):
        self.cfg = cfg
        self.held = held  # replies to reviewers outside the developer's working hours wait here for them
        self.users = users
        self.projects = projects
        self.reviews = reviews
        self.queue = queue
        # developer -> when GitHub, and Slack, were last read for them (since this server started)
        self._polled = {"github": {}, "slack": {}}

    def start(self) -> None:
        threading.Thread(target=self.run_forever, daemon=True, name="reviews").start()

    def run_forever(self) -> None:
        while True:
            time.sleep(TICK_SECONDS + random.uniform(0, 3))
            try:
                self.poll(due_only=True)
            except Exception:
                log.exception("watching pull requests failed; trying again shortly")

    def poll(self, now: float | None = None, due_only: bool = False) -> None:
        """Read GitHub and Slack for every active developer; `due_only`: each only for the developers
        whose own interval for it has passed. A developer not due is not even loaded."""
        now = now or time.time()
        timings = self.users.timings()
        github = self._due("github", timings, now, due_only, lambda limits: limits.github_poll_minutes * 60)
        slack = self._due("slack", timings, now, due_only, lambda limits: limits.slack_poll_minutes * 60)
        self._watch_prs(github, now)
        self._read_slack(slack, now)

    def _due(self, service: str, timings: dict[int, tuple[bool, Limits]], now: float, due_only: bool,
             interval: Callable[[Limits], float]) -> dict[int, User]:
        """The active developers whose `service` is due now, loaded; their poll is recorded."""
        polled = self._polled[service]
        due = [uid for uid, (paused, limits) in timings.items()
               if not paused and (not due_only or now - polled.get(uid, 0) >= interval(limits))]
        polled.update((uid, now) for uid in due)
        loaded = {uid: self.users.loadable(uid) for uid in due}
        return {uid: user for uid, user in loaded.items() if user}

    # The PRs ShadowDev opened: review comments and failing CI

    def _watch_prs(self, github: dict[int, User], now: float) -> None:
        projects: dict[int, Project | None] = {}

        def project(project_id: int) -> Project | None:
            if project_id not in projects:
                projects[project_id] = self.projects.get(project_id)
            return projects[project_id]

        reminding: dict[tuple[int, str, str], list] = {}  # (developer, Slack thread) -> its PRs to remind of
        for row in self.reviews.tracked_prs():
            user = github.get(row["user_id"])
            if user and project(row["project_id"]) and _watched(user, row["project_id"]):
                _guarded(f"checking {row['github']}#{row['number']} for review comments", self._check_pr, row, user,
                         project(row["project_id"]), now, reminding)
        for (user_id, channel, ts), rows in reminding.items():
            _guarded(f"reminding the reviewers of {len(rows)} PR(s)", self._remind, github[user_id], channel, ts, rows, now)

    def _check_pr(self, row, user: User, project: Project, now: float, reminding: dict | None = None) -> None:
        """`reminding`: where a PR whose reviewers are to be reminded goes, by its Slack thread."""
        actor, repo = user.actor(project.id), project.repo(row["repo"])
        if not actor:
            return
        settings = actor.settings
        remind = self._remind_due(row, user, settings, now) and reminding is not None
        fixes = ((settings.fix_review_comments and row["rounds"] < MAX_ROUNDS)
                 or (settings.fix_conflicts and row["conflict_rounds"] < MAX_CONFLICT_ROUNDS)
                 or (settings.fix_ci and row["ci_rounds"] < MAX_CI_ROUNDS))
        if not repo or not (fixes or remind):  # nothing it could do: GitHub is not even asked
            return
        github = GitHub(actor.github.token)
        state, threads = github.review_threads(row["github"], row["number"])
        if state != "OPEN":
            return self.reviews.untrack_pr(row["id"])  # merged or closed: nothing more to watch
        verdicts = github.latest_reviews(row["github"], row["number"])
        if remind and not self._reviewed(verdicts, threads, actor.github.login):
            reminding.setdefault((user.id, row["slack_channel"], row["slack_ts"]), []).append((row, repo))
        # Review comments first: their fix is a new commit, whose CI is then checked in turn.
        waiting = actor.settings.fix_review_comments and self._review_comments(row, user, project, threads, verdicts, now)
        # Then a conflict with the base branch: CI often doesn't even run on a PR that conflicts.
        conflicts = ci_failed = False
        if not waiting and settings.fix_conflicts and row["conflict_rounds"] < MAX_CONFLICT_ROUNDS:
            waiting = conflicts = self._conflict(row, user, project, github)
        if not waiting and settings.fix_ci and row["ci_rounds"] < MAX_CI_ROUNDS:
            ci_failed = self._failing_ci(row, user, project, github)
        self.reviews.set_pr_review(row["id"], review_state(threads, verdicts, actor.github.login, conflicts, ci_failed))

    # Reminding reviewers of a PR nobody has reviewed

    @staticmethod
    def _remind_due(row, user: User, settings, now: float) -> bool:
        """Whether a PR's reviewers are due a reminder, if nobody has reviewed it: it was asked for in
        Slack the developer's hours ago, and they were not reminded in the last day, nor too often. Only
        within the developer's working hours: one due outside them is due once they begin."""
        if not (settings.remind_reviewers and user.slack and row["slack_ts"] and row["reminders"] < MAX_REMINDERS
                and settings.at_work(now)):
            return False
        try:
            asked = float(row["slack_ts"])  # a Slack message's timestamp: when it was posted
        except ValueError:
            return False
        return now - asked >= int(settings.remind_hours) * 3600 and now - row["reminded_at"] >= REMIND_EVERY

    @staticmethod
    def _reviewed(verdicts: list[dict], threads: list[dict], login: str) -> bool:
        """Someone other than the developer has commented on the PR's code, or approved it or asked for
        changes (`verdicts`: GitHub.latest_reviews)."""
        others = lambda author: author.lower() != login.lower()
        if any(others(c["author"]) for t in threads for c in t["comments"]):
            return True
        return any(others(r["author"]) for r in verdicts)

    def _remind(self, user: User, channel: str, ts: str, rows: list, now: float) -> None:
        """One reply in the thread that asked for review of these PRs, mentioning their reviewers."""
        reviewers = " ".join(dict.fromkeys(f"<@{m}>" for _, repo in rows for m in repo.reviewers))
        prs = [f"• {link('https://github.com/{}/pull/{}'.format(row['github'], row['number']))}" for row, _ in rows]
        ask = f"{reviewers} A gentle reminder: {'this PR is' if len(rows) == 1 else 'these PRs are'} still waiting for a review."
        try:
            Slack(user.slack.token).post(channel, "\n".join([ask.lstrip(), *prs, "Thanks"]), ts)
        finally:  # posted or not, not tried again before tomorrow
            for row, _ in rows:
                self.reviews.pr_reminded(row["id"], now)
        log.info("reminded the reviewers of %s for %s", ", ".join(f"{r['github']}#{r['number']}" for r, _ in rows),
                 user.display_name)

    def _review_comments(self, row, user: User, project: Project, threads: list[dict], verdicts: list[dict],
                         now: float) -> bool:
        """Queue a run for the review comments waiting on the PR. True while some wait to be acted on.
        `verdicts`: GitHub.latest_reviews."""
        if row["rounds"] >= MAX_ROUNDS:
            return False
        waiting = waiting_threads(threads, user.actor(project.id).github.login)
        newest = max((t["comments"][-1]["at"] for t in waiting), default="")
        if newest <= row["handled_at"]:
            return False
        # A review is often several comments: act once none came for the developer's quiet time.
        if now - datetime.fromisoformat(newest.replace("Z", "+00:00")).timestamp() < user.limits.review_quiet_minutes * 60:
            return True  # the reviewer may still be writing
        if is_approved(verdicts):
            return False  # approved, comments and all: the reviewers don't need them fixed
        slack = (row["slack_channel"], row["slack_ts"]) if row["slack_ts"] else None
        number, branch = row["number"], row["branch"]

        def work(project, actor, cfg, github, cancel, on_model):
            repo = project.repo(row["repo"])
            if not repo:
                return "skipped", "the repo is no longer in the project"
            return address_review(repo, number, branch, slack, project, actor, cfg, github, cancel, on_model,
                                  later=lambda channel, text, ts: self.held.hold_slack(user.id, project.id, channel, text, ts))

        # Handled whether it worked or not: failing comments are not tried again and again.
        if self.queue.submit(project, [f"{row['github']}#{number}"], user.id, work=work,
                             after=lambda: self.reviews.pr_handled(row["id"], newest)) is None:
            log.info("%s#%d: review comments queued for %s", row["github"], number, user.display_name)
        return True

    def _conflict(self, row, user: User, project: Project, github: GitHub) -> bool:
        """Queue a run that merges the base branch into the PR, once per pair of head and base commits,
        when GitHub says they conflict. True while the PR conflicts (its CI is then left alone)."""
        pr = github.pr(row["github"], row["number"])
        if pr.get("mergeable") is not False:  # mergeable, or None while GitHub is still working it out
            return False
        key = f"{pr['head']['sha']}:{pr['base']['sha']}"
        if key == row["conflict_key"]:
            return True  # acted on already: tried, and it still conflicts, or the merge is not pushed yet
        number, branch = row["number"], row["branch"]

        def work(project, actor, cfg, github, cancel, on_model):
            repo = project.repo(row["repo"])
            if not repo:
                return "skipped", "the repo is no longer in the project"
            return fix_conflicts(repo, number, branch, project, actor, cfg, github, cancel, on_model)

        # Handled whether it worked or not: the same conflict is not tried again and again.
        if self.queue.submit(project, [f"{row['github']}#{number}"], user.id, work=work,
                             after=lambda: self.reviews.conflict_handled(row["id"], key)) is None:
            log.info("%s#%d: conflicts with %s; merge queued for %s", row["github"], number, pr["base"]["ref"],
                     user.display_name)
        return True

    def _failing_ci(self, row, user: User, project: Project, github: GitHub) -> bool:
        """Queue a run for the PR's failing CI, once per head commit, when every check has finished.
        Whether its CI fails."""
        sha, state = github.ci_state(row["github"], row["number"])
        if state not in CI_FAILED:
            return False
        if sha == row["ci_sha"] or failed_checks(github, row["github"], sha) is None:
            return True
        number, branch = row["number"], row["branch"]

        def work(project, actor, cfg, github, cancel, on_model):
            repo = project.repo(row["repo"])
            if not repo:
                return "skipped", "the repo is no longer in the project"
            return fix_ci(repo, number, branch, sha, project, actor, cfg, github, cancel, on_model)

        # Handled whether it worked or not: the same commit's CI is not tried again and again.
        if self.queue.submit(project, [f"{row['github']}#{number}"], user.id, work=work,
                             after=lambda: self.reviews.ci_handled(row["id"], sha)) is None:
            log.info("%s#%d: failing CI queued for %s", row["github"], number, user.display_name)
        return True

    # Review requests in Slack

    def _read_slack(self, slack: dict[int, User], now: float) -> None:
        """The Slack channels of the projects, the threads of PRs asked again, and the review requests
        that were read, started: each for the developers whose Slack is due."""
        for user in slack.values():
            if user.slack:
                for p in self.projects.on_sites(list(user.sites)):
                    if p.slack_channel and _reviews_for(user, p.id):
                        _guarded(f"reading the Slack channel of {p.name} for {user.display_name}", self._read_channel, user, p, now)
        for row in self.reviews.in_state("commented", "failed"):
            user = slack.get(row["user_id"])
            if user and user.slack and row["rounds"] < MAX_ROUNDS and _reviews_for(user, row["project_id"]):
                _guarded(f"reading the Slack thread of {row['github']}#{row['number']}", self._read_thread, row, user)
        for row in self.reviews.in_state("requested"):  # asked in Slack: started as Slack is read
            user = slack.get(row["user_id"])
            if user:
                _guarded(f"starting the review of {row['github']}#{row['number']}", self._start_review, row, user,
                         self.projects.get(row["project_id"]))

    def _read_channel(self, user: User, project: Project, now: float) -> None:
        """The channel's last messages and the replies in their threads, each acted on once: only
        those after `seen`, the newest one read before, count."""
        seen = self.users.subscriptions.slack_seen(user.id, project.id)
        if not seen:  # the first look: only what is said from now on counts
            return self.users.subscriptions.mark_slack_seen(user.id, project.id, f"{now:.6f}")
        slack, channel = Slack(user.slack.token), project.slack_channel
        new = []  # all read before any is acted on: if Slack fails halfway, the next look reads them again
        recent = slack.history(channel, RECENT_MESSAGES)
        for message in recent:
            ts = str(message.get("ts") or "")
            if not ts:
                continue
            if float(ts) > float(seen):
                new.append(message)
            if float(message.get("latest_reply") or 0) > float(seen):  # its thread has replies not read yet
                new += slack.replies(channel, ts, seen)
        repos = {r.github.lower(): r for r in project.repos}
        parents = {str(m.get("ts")): str(m.get("text") or "") for m in recent}  # the threads' first messages
        for message in sorted(new, key=lambda m: float(m["ts"])):
            text, ts = str(message.get("text") or ""), str(message["ts"])
            thread = str(message.get("thread_ts") or ts)
            if message.get("user") != user.slack.user_id:
                settings = user.actor(project.id).settings
                asked = asked_prs(text, user.slack.user_id)
                if not asked and thread != ts and thread in parents:  # mentioned later, in the thread
                    asked = asked_in_reply(text, parents[thread], user.slack.user_id)
                for github, number in asked:
                    repo = repos.get(github.lower())
                    if repo and settings.reviews(repo.name):  # only the project's own repos, those chosen
                        self.reviews.request(user.id, project.id, repo.github, number,
                                             channel, str(message.get("thread_ts") or ts), ts)
                        log.info("%s#%s: %s was asked in Slack to review it", github, number, user.display_name)
            seen = max(seen, ts, key=float)
        self.users.subscriptions.mark_slack_seen(user.id, project.id, seen)

    def _read_thread(self, row, user: User) -> None:
        """Asked again in the thread of an earlier review: by mentioning the developer, or with words."""
        for reply in Slack(user.slack.token).replies(row["channel"], row["ts"], row["last_ts"]):
            text = str(reply.get("text") or "")
            if reply.get("user") != user.slack.user_id and (f"<@{user.slack.user_id}>" in text or asks_again(text)):
                self.reviews.request(user.id, row["project_id"], row["github"], row["number"], row["channel"], row["ts"],
                                     str(reply["ts"]))
                log.info("%s#%d: %s was asked to review it again", row["github"], row["number"], user.display_name)
                return

    def _start_review(self, row, user: User, project: Project | None) -> None:
        repo = next((r for r in project.repos if r.github == row["github"]), None) if project else None
        if not (repo and user.slack and user.actor(project.id) and user.actor(project.id).settings.reviews(repo.name)):
            self.reviews.finish(row["id"], "closed")
            return self._react(row, user)
        number, thread, result = row["number"], (row["channel"], row["ts"]), {}

        def work(project, actor, cfg, github, cancel, on_model):
            if not actor.slack:
                return "skipped", "your Slack account was removed"
            slack, me = Slack(actor.slack.token), actor.slack.user_id
            asking = slack.message(row["channel"], row["ts"], row["last_ts"])
            colleague = asking and claimed_by(asking, me, row["github"], number)
            if not colleague and row["last_ts"] != row["ts"]:
                # Asked in a reply: when the thread's first message asked others about the PR and not
                # the developer, one of them who started (👀) has it.
                first = slack.message(row["channel"], row["ts"], row["ts"])
                if first and (row["github"].lower(), number) not in {(g.lower(), n) for g, n in
                                                                    asked_prs(str(first.get("text") or ""), me)}:
                    colleague = claimed_by(first, me, row["github"], number)
            if colleague:
                result["state"] = "closed"
                return "skipped", f"not reviewed: Slack member {colleague}, asked with you, reacted 👀 first"
            status, message, result["state"] = review_pr(repo, number, thread, project, actor, cfg, github, cancel, on_model)
            return status, message

        def after():
            self.reviews.finish(row["id"], result.get("state", "failed"))
            self._react(row, user)

        # A PR between the branches the developer approves without a review asks Claude nothing,
        # so the Claude account's usage limits don't hold it.
        actor, claude = user.actor(project.id), True
        if actor.settings.approves_some_unreviewed:
            pr = GitHub(actor.github.token).pr(repo.github, number)
            claude = not actor.settings.approves_unreviewed(pr["head"]["ref"], pr["base"]["ref"])
        if self.queue.submit(project, [f"{row['github']}#{number}"], user.id, work=work, after=after, claude=claude) is None:
            log.info("%s#%d: review queued for %s", row["github"], number, user.display_name)
            self._react(row, user)

    def _react(self, row, user: User) -> None:
        """The reaction on the message that asked for the review: 👀 while any of the PRs it asked
        the developer about waits or is reviewed; once none does, how they went."""
        if not user.slack:
            return
        states = [s for s in self.reviews.asked_with(user.id, row["channel"], row["last_ts"]) if s != "closed"]
        done = "requested" not in states
        outcome = (None if not done or not states else ALL_APPROVED if set(states) == {"approved"}
                   else SOME_FAILED if "failed" in states else SOME_COMMENTED)
        slack, channel, ts = Slack(user.slack.token), row["channel"], row["last_ts"]
        try:  # a token without reactions:write (connected before they were used) only misses the reactions
            slack.react(channel, ts, REVIEWING, on=not done)
            if outcome:
                slack.react(channel, ts, outcome)
        except HTTPError as e:
            log.warning("%s#%d: could not react in Slack: %s", row["github"], row["number"], e)
