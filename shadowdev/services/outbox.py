"""What waits for a developer's working hours in a project (Settings.work_starts), sent once they
begin, in the order it was held: the PRs a run pushed its branch for, with their review request and
PR links on the tickets (pipeline.open_prs), replies to reviewers in Slack, and comments on Jira
tickets and GitHub PRs (Holding). Only what other people see waits: runs work at any time, reviews
asked for are posted, and direct messages and emails to the developer go at once."""
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
# The calls of a run's clients that wait outside working hours: comments, as people read them.
HELD_CALLS = {"jira": ("comment",), "github": ("comment", "reply_to_thread", "resolve_thread")}


class Holding:
    """A run's Jira or GitHub client whose comments (HELD_CALLS), outside its developer's working
    hours in the project, are handed to `hold(service, call, args)` to be made in those hours;
    every other call goes through at once."""

    def __init__(self, client, service: str, settings, hold: Callable[[str, str, list], None]):
        self._client, self._service, self._settings, self._hold = client, service, settings, hold

    def __getattr__(self, name: str):
        found = getattr(self._client, name)
        if name not in HELD_CALLS[self._service]:
            return found

        def call(*args):
            if self._settings.at_work(time.time()):
                return found(*args)
            self._hold(self._service, name, list(args))
            log.info("outside working hours: the %s %s (%s) waits for them", self._service.replace("jira", "Jira")
                     .replace("github", "GitHub"), name.replace("_", " "), args[0])
            return None
        return call


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
                user, project = self.users.loadable(user_id), self.projects.get(project_id)
                actor = user.actor(project_id) if user and project else None
                if actor and (user.paused or not actor.settings.at_work(now)):
                    continue
                if not actor:
                    log.info("dropped %d thing(s) waiting for working hours: the developer left the project, or "
                             "its accounts are gone", len(rows))
                    for row in rows:
                        if row["run_id"]:
                            self.runs.update(row["run_id"], "done", "its branch is pushed, but its PRs were not "
                                             "opened: you left the project, or its GitHub or Claude account was removed")
                        self.held.done([row["id"]])
                    continue
                self._send(user, project, actor, rows)
                sent += len(rows)
        return sent

    def _send(self, user: User, project: Project, actor: Actor, rows: list) -> None:
        """Makes the rows in order. Each is marked done as soon as it was made, or failed for good (it
        is logged, and a run's message says so): a crash leaves only the rest to send, and one row
        that raises does not drop the others."""
        clients = {}

        def client(service: str):
            if service not in clients:
                clients[service] = GitHub(actor.github.token) if service == "github" else jira_client(
                    self.oauth, self.users, user.subscriptions[project.id].jira_account_id, project.cloud_id)
            return clients[service]

        prs = [row for row in rows if row["kind"] == "prs"]
        together: list[tuple[str, object, str]] = []  # (title, repo, PR) of runs started together
        announce: Callable | None = ((lambda title, opened, branch: together.extend((title, r, url) for r, url in opened))
                                     if len(prs) > 1 else None)
        for row in rows:
            try:
                if row["kind"] in HELD_CALLS:
                    self._comment(row, project, user, client)
                elif row["kind"] == "slack" and actor.slack:
                    self._reply(row, project, user, actor)
                elif row["kind"] == "prs":
                    self._open_prs(row, project, user, actor, client, announce)
            except Exception as e:
                log.exception("%s: a %s that waited for %s's working hours could not be made", project.name, row["kind"],
                              user.display_name)
                if row["kind"] == "prs" and row["run_id"]:
                    self.runs.update(row["run_id"], "failed", f"its branch is pushed, but its PRs could not be opened: {e}")
            self.held.done([row["id"]])  # once: made, or failed for good
        if together:
            try:
                self._announce(together, prs, user, actor, project)
            except Exception:
                log.exception("%s: could not ask for review of the PRs that waited for working hours", project.name)

    def _comment(self, row, project: Project, user: User, client) -> None:
        data = json.loads(row["data"])
        if data["op"] not in HELD_CALLS[row["kind"]]:
            return
        op = data["op"].replace("_", " ")
        try:
            getattr(client(row["kind"]), data["op"])(*data["args"])
        except HTTPError as e:  # refused by the service: not tried again
            log.warning("%s: a %s %s that waited for working hours failed: %s", project.name, row["kind"], op, e)
            return
        log.info("%s: a %s %s that waited for %s's working hours made", project.name, row["kind"], op, user.display_name)

    def _reply(self, row, project: Project, user: User, actor: Actor) -> None:
        data = json.loads(row["data"])
        try:
            Slack(actor.slack.token).post(data["channel"], data["text"], data["ts"])
        except HTTPError as e:
            log.warning("%s: could not post a Slack reply that waited for working hours: %s", project.name, e)
            return
        log.info("%s: a Slack reply that waited for %s's working hours posted", project.name, user.display_name)

    def _open_prs(self, row, project: Project, user: User, actor: Actor, client, announce: Callable | None) -> None:
        publication = Publication.from_json(row["data"])
        label = ", ".join(publication.keys)
        log.info("%s: opening the PRs that waited for %s's working hours", label, user.display_name)
        status, message = open_prs(publication, project, actor, client("jira"), client("github"), label,
                                   on_pr=lambda repo, number, branch, slack: self.reviews.track_pr(
                                       user.id, project.id, repo.name, repo.github, number, branch, slack),
                                   announce=announce)
        if row["run_id"]:
            self.runs.update(row["run_id"], status, message)

    def _announce(self, together: list, prs: list, user: User, actor: Actor, project: Project) -> None:
        """One review request in Slack for the PRs of runs started together, and watched in its thread."""
        label = ", ".join(k for row in prs for k in Publication.from_json(row["data"]).keys)
        if len({title for title, _, _ in together}) == 1:  # only one of them opened PRs: as for a run by itself
            slack = ask_for_review(actor, project, together[0][0], [(r, url) for _, r, url in together], label)
        else:
            slack = ask_for_reviews(actor, project, together, label)
        for _, repo, url in together if slack else ():
            self.reviews.set_pr_slack(user.id, repo.github, int(url.rstrip("/").rsplit("/", 1)[1]), slack)
