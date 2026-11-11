"""Watching pull requests in review, for every active developer, GitHub and Slack each as often as
the developer asked (Limits.github_poll_minutes and slack_poll_minutes, a minute by default):

- the PRs ShadowDev opened for them: when reviewers have commented, a run addresses the comments
  (automation/review.address_review); when their CI fails, a run fixes it (fix_ci);
- the Slack channel of each project they joined, read as them: when someone mentions them there
  with a pull request of the project, a run reviews it (review_pr); and when, in that thread,
  they are asked to review again, it is reviewed again.

GitHub and Slack are asked, not listened to, so this works on a machine neither can reach.
"""
import logging
import random
import re
import threading
import time
from datetime import datetime

from ..automation.review import address_review, failed_checks, fix_ci, is_approved, review_pr, waiting_threads
from ..clients.github import GitHub
from ..clients.slack import Slack
from ..config import Config
from ..models.project import Project
from ..models.user import User
from ..repositories.projects import ProjectRepository
from ..repositories.reviews import ReviewRepository
from ..repositories.users import UserRepository
from .run_queue import RunQueue

log = logging.getLogger("shadowdev")

MAX_ROUNDS = 5  # of fixes, and of reviews, per pull request: two ShadowDevs can't go back and forth forever
MAX_CI_ROUNDS = 3  # of CI fixes per pull request
CI_FAILED = {"FAILURE", "ERROR"}
PR_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9-]+/[A-Za-z0-9_.-]+)/pull/([0-9]{1,9})")
TICK_SECONDS = 15  # how often it looks for developers due a poll: the shortest interval allowed
RECENT_MESSAGES = 20  # the channel's last messages read each poll, with their threads; older ones are not looked at
AGAIN_RE = re.compile(r"re-?review|review\s+(it\s+)?again", re.IGNORECASE)


class ReviewWatcher:
    def __init__(self, cfg: Config, users: UserRepository, projects: ProjectRepository, reviews: ReviewRepository,
                 queue: RunQueue):
        self.cfg = cfg
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
        whose own interval for it has passed."""
        now = now or time.time()
        active = [u for u in self.users.all() if not u.paused]

        def due(service: str, interval) -> dict[int, User]:
            polled = self._polled[service]
            users = {u.id: u for u in active if not due_only or now - polled.get(u.id, 0) >= interval(u.limits)}
            polled.update((uid, now) for uid in users)
            return users

        github = due("github", lambda limits: limits.github_poll_minutes * 60)
        slack = due("slack", lambda limits: limits.slack_poll_minutes * 60)
        projects: dict[int, Project | None] = {}
        project = lambda project_id: projects.setdefault(project_id, self.projects.get(project_id))

        def watched(user: User, project_id: int) -> bool:
            actor = user.actor(project_id)
            return bool(actor and actor.settings.watches_prs)

        def reviews_for(user: User, project_id: int) -> bool:
            actor = user.actor(project_id)
            return bool(actor and actor.settings.review_for_me)

        def each(what: str, do, *args) -> None:  # one that fails (revoked access, a deleted channel) doesn't stop the rest
            try:
                do(*args)
            except Exception as e:
                log.warning("%s failed: %s", what, e)

        for row in self.reviews.tracked_prs():
            if row["user_id"] in github and project(row["project_id"]) and watched(github[row["user_id"]], row["project_id"]):
                each(f"checking {row['github']}#{row['number']} for review comments", self._check_pr, row,
                     github[row["user_id"]], project(row["project_id"]), now)
        for user in slack.values():
            if user.slack:
                for p in self.projects.on_sites(list(user.sites)):
                    if p.slack_channel and reviews_for(user, p.id):
                        each(f"reading the Slack channel of {p.name} for {user.display_name}", self._read_channel, user, p, now)
        for row in self.reviews.in_state("commented", "failed"):
            if row["user_id"] in slack and slack[row["user_id"]].slack and row["rounds"] < MAX_ROUNDS \
                    and reviews_for(slack[row["user_id"]], row["project_id"]):
                each(f"reading the Slack thread of {row['github']}#{row['number']}", self._read_thread, row, slack[row["user_id"]])
        for row in self.reviews.in_state("requested"):  # asked in Slack: started as Slack is read
            if row["user_id"] in slack:
                each(f"starting the review of {row['github']}#{row['number']}", self._start_review, row,
                     slack[row["user_id"]], project(row["project_id"]))

    # The PRs ShadowDev opened: review comments and failing CI

    def _check_pr(self, row, user: User, project: Project, now: float) -> None:
        actor, repo = user.actor(project.id), project.repo(row["repo"])
        if not actor or not repo or (row["rounds"] >= MAX_ROUNDS and row["ci_rounds"] >= MAX_CI_ROUNDS):
            return
        github = GitHub(actor.github.token)
        state, threads = github.review_threads(row["github"], row["number"])
        if state != "OPEN":
            return self.reviews.untrack_pr(row["id"])  # merged or closed: nothing more to watch
        # Review comments first: their fix is a new commit, whose CI is then checked in turn.
        waiting = actor.settings.fix_review_comments and self._review_comments(row, user, project, github, threads, now)
        if not waiting and actor.settings.fix_ci and row["ci_rounds"] < MAX_CI_ROUNDS:
            self._failing_ci(row, user, project, github)

    def _review_comments(self, row, user: User, project: Project, github: GitHub, threads: list[dict], now: float) -> bool:
        """Queue a run for the review comments waiting on the PR. True while some wait to be acted on."""
        if row["rounds"] >= MAX_ROUNDS:
            return False
        waiting = waiting_threads(threads, user.actor(project.id).github.login)
        newest = max((t["comments"][-1]["at"] for t in waiting), default="")
        if newest <= row["handled_at"]:
            return False
        # A review is often several comments: act once none came for the developer's quiet time.
        if now - datetime.fromisoformat(newest.replace("Z", "+00:00")).timestamp() < user.limits.review_quiet_minutes * 60:
            return True  # the reviewer may still be writing
        if is_approved(github.latest_reviews(row["github"], row["number"])):
            return False  # approved, comments and all: the reviewers don't need them fixed
        slack = (row["slack_channel"], row["slack_ts"]) if row["slack_ts"] else None
        number, branch = row["number"], row["branch"]

        def work(project, actor, cfg, github, cancel, on_model):
            repo = project.repo(row["repo"])
            if not repo:
                return "skipped", "the repo is no longer in the project"
            return address_review(repo, number, branch, slack, project, actor, cfg, github, cancel, on_model)

        # Handled whether it worked or not: failing comments are not tried again and again.
        if self.queue.submit(project, [f"{row['github']}#{number}"], user.id, work=work,
                             after=lambda: self.reviews.pr_handled(row["id"], newest)) is None:
            log.info("%s#%d: review comments queued for %s", row["github"], number, user.display_name)
        return True

    def _failing_ci(self, row, user: User, project: Project, github: GitHub) -> None:
        """Queue a run for the PR's failing CI, once per head commit, when every check has finished."""
        sha, state = github.ci_state(row["github"], row["number"])
        if state not in CI_FAILED or sha == row["ci_sha"] or failed_checks(github, row["github"], sha) is None:
            return
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

    # Review requests in Slack

    def _read_channel(self, user: User, project: Project, now: float) -> None:
        """The channel's last messages and the replies in their threads, each acted on once: only
        those after `seen`, the newest one read before, count."""
        seen = self.users.slack_seen(user.id, project.id)
        if not seen:  # the first look: only what is said from now on counts
            return self.users.mark_slack_seen(user.id, project.id, f"{now:.6f}")
        slack, channel = Slack(user.slack.token), project.slack_channel
        new = []  # all read before any is acted on: if Slack fails halfway, the next look reads them again
        for message in slack.history(channel, RECENT_MESSAGES):
            ts = str(message.get("ts") or "")
            if not ts:
                continue
            if float(ts) > float(seen):
                new.append(message)
            if float(message.get("latest_reply") or 0) > float(seen):  # its thread has replies not read yet
                new += slack.replies(channel, ts, seen)
        repos = {r.github.lower(): r for r in project.repos}
        for message in sorted(new, key=lambda m: float(m["ts"])):
            text, ts = str(message.get("text") or ""), str(message["ts"])
            if message.get("user") != user.slack.user_id and f"<@{user.slack.user_id}>" in text:
                for github, number in PR_URL_RE.findall(text):
                    if github.lower() in repos:  # only the project's own repos are reviewed
                        self.reviews.request(user.id, project.id, repos[github.lower()].github, int(number),
                                             channel, str(message.get("thread_ts") or ts), ts)
                        log.info("%s#%s: %s was asked in Slack to review it", github, number, user.display_name)
            seen = max(seen, ts, key=float)
        self.users.mark_slack_seen(user.id, project.id, seen)

    def _read_thread(self, row, user: User) -> None:
        """Asked again in the thread of an earlier review: by mentioning the developer, or with words."""
        for reply in Slack(user.slack.token).replies(row["channel"], row["ts"], row["last_ts"]):
            text = str(reply.get("text") or "")
            if reply.get("user") != user.slack.user_id and (f"<@{user.slack.user_id}>" in text or AGAIN_RE.search(text)):
                self.reviews.request(user.id, row["project_id"], row["github"], row["number"], row["channel"], row["ts"],
                                     str(reply["ts"]))
                log.info("%s#%d: %s was asked to review it again", row["github"], row["number"], user.display_name)
                return

    def _start_review(self, row, user: User, project: Project | None) -> None:
        repo = next((r for r in project.repos if r.github == row["github"]), None) if project else None
        if not (repo and user.slack and user.actor(project.id) and user.actor(project.id).settings.review_for_me):
            return self.reviews.finish(row["id"], "closed")
        number, thread, result = row["number"], (row["channel"], row["ts"]), {}

        def work(project, actor, cfg, github, cancel, on_model):
            if not actor.slack:
                return "skipped", "your Slack account was removed"
            status, message, result["state"] = review_pr(repo, number, thread, project, actor, cfg, github, cancel, on_model)
            return status, message

        if self.queue.submit(project, [f"{row['github']}#{number}"], user.id, work=work,
                             after=lambda: self.reviews.finish(row["id"], result.get("state", "failed"))) is None:
            log.info("%s#%d: review queued for %s", row["github"], number, user.display_name)
