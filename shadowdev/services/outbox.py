"""What waits for a developer's working hours in a project (Settings.work_starts), sent once they
begin: the PRs a run pushed its branch for, with their review request and PR links on the tickets
(pipeline.open_prs), and replies to reviewers in Slack. Only what other people see waits: runs
work at any time, and direct messages and emails to the developer go at once."""
import json
import logging
import random
import threading
import time
from collections.abc import Callable

from ..automation.pipeline import ask_for_review, ask_for_reviews, open_prs
from ..clients.atlassian import AtlassianOAuth, jira_client
from ..clients.github import GitHub
from ..clients.rest import HTTPError
from ..clients.slack import Slack
from ..models.project import Project
from ..models.publication import Publication
from ..models.user import Actor, User
from ..repositories.held import HeldRepository
from ..repositories.projects import ProjectRepository
from ..repositories.reviews import ReviewRepository
from ..repositories.runs import RunRepository
from ..repositories.users import UserRepository

log = logging.getLogger("shadowdev")

TICK_SECONDS = 60


class Outbox:
    def __init__(self, users: UserRepository, projects: ProjectRepository, runs: RunRepository,
                 reviews: ReviewRepository, held: HeldRepository, oauth: AtlassianOAuth):
        self.users = users
        self.projects = projects
        self.runs = runs
        self.reviews = reviews
        self.held = held
        self.oauth = oauth
        self._sending = threading.Lock()  # held while a developer's things are sent, so stopping waits for it
        self._closing = False

    def start(self) -> None:
        threading.Thread(target=self.run_forever, daemon=True, name="outbox").start()

    def run_forever(self) -> None:
        while not self._closing:
            time.sleep(TICK_SECONDS + random.uniform(0, 5))
            try:
                self.flush()
            except Exception:
                log.exception("sending what waited for working hours failed; trying again shortly")

    def drain(self, seconds: float) -> None:
        """The server is stopping: nothing more is started, and what is being sent is finished."""
        self._closing = True
        if self._sending.acquire(timeout=seconds):
            self._sending.release()

    def flush(self, now: float | None = None) -> int:
        """Send what waits for the developers now within their working hours. Returns how many
        were sent. What belongs to a project the developer left, or can no longer run, is dropped."""
        now = now or time.time()
        groups: dict[tuple[int, int, int], list] = {}
        for row in self.held.waiting():  # runs started together: their PRs get one review request
            together = row["batch"] if row["kind"] == "prs" and row["batch"] else -row["id"]
            groups.setdefault((row["user_id"], row["project_id"], together), []).append(row)
        sent = 0
        for (user_id, project_id, _), rows in groups.items():
            with self._sending:
                if self._closing:
                    break
                user, project = self.users.get(user_id), self.projects.get(project_id)
                actor = user.actor(project_id) if user and project else None
                if actor and (user.paused or not actor.settings.at_work(now)):
                    continue
                try:
                    if actor:
                        self._send(user, project, actor, rows)
                        sent += len(rows)
                    else:
                        log.info("dropped %d thing(s) waiting for working hours: the developer left the project, or "
                                 "its accounts are gone", len(rows))
                        for row in rows:
                            if row["run_id"]:
                                self.runs.update(row["run_id"], "done", "its branch is pushed, but its PRs were not "
                                                 "opened: you left the project, or its GitHub or Claude account was removed")
                finally:  # sent once: what failed is in the log and the run's message, not tried again
                    self.held.done([row["id"] for row in rows])
        return sent

    def _send(self, user: User, project: Project, actor: Actor, rows: list) -> None:
        for row in rows:
            if row["kind"] == "slack" and actor.slack:
                data = json.loads(row["data"])
                try:
                    Slack(actor.slack.token).post(data["channel"], data["text"], data["ts"])
                    log.info("%s: a Slack reply that waited for %s's working hours posted", project.name,
                             user.display_name)
                except HTTPError as e:
                    log.warning("%s: could not post a Slack reply that waited for working hours: %s", project.name, e)
        publications = [(row, Publication.from_json(row["data"])) for row in rows if row["kind"] == "prs"]
        if not publications:
            return
        jira = jira_client(self.oauth, self.users, user.subscriptions[project.id].jira_account_id, project.cloud_id)
        github = GitHub(actor.github.token)
        together: list[tuple[str, object, str]] = []  # (title, repo, PR) of runs started together
        announce: Callable | None = ((lambda title, opened, branch: together.extend((title, r, url) for r, url in opened))
                                     if len(publications) > 1 else None)
        for row, publication in publications:
            label = ", ".join(publication.keys)
            log.info("%s: opening the PRs that waited for %s's working hours", label, user.display_name)
            try:
                status, message = open_prs(publication, project, actor, jira, github, label,
                                           on_pr=lambda repo, number, branch, slack: self.reviews.track_pr(
                                               user.id, project.id, repo.name, repo.github, number, branch, slack),
                                           announce=announce)
            except Exception as e:
                log.exception("%s: the PRs that waited for working hours could not be opened", label)
                status, message = "failed", f"its branch is pushed, but its PRs could not be opened: {e}"
            if row["run_id"]:
                self.runs.update(row["run_id"], status, message)
        if together:
            label = ", ".join(k for _, p in publications for k in p.keys)
            if len({title for title, _, _ in together}) == 1:  # only one of them opened PRs: as for a run by itself
                slack = ask_for_review(actor, project, together[0][0], [(r, url) for _, r, url in together], label)
            else:
                slack = ask_for_reviews(actor, project, together, label)
            for _, repo, url in together if slack else ():
                self.reviews.set_pr_slack(user.id, repo.github, int(url.rstrip("/").rsplit("/", 1)[1]), slack)
