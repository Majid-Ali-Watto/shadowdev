"""Polling Jira for new assignments, for machines Jira's webhooks can't reach (a laptop). Off unless
JIRA_POLLING is set. Every 15 minutes, for each project each active developer joined, it asks Jira,
as the Jira account of that subscription, for the project's unresolved issues assigned to that
account and updated since the last poll, and queues the ones of a type the developer automates.
Like a webhook, a repeat is harmless: an issue already queued, running or handled is skipped."""
import logging
import random
import threading
import time

from ..automation import rules
from ..clients.atlassian import AtlassianOAuth, jira_client
from ..models.project import Project
from ..models.user import User
from ..repositories.projects import ProjectRepository
from ..repositories.users import UserRepository
from .run_queue import RunQueue

log = logging.getLogger("shadowdev")

POLL_SECONDS = 15 * 60
FIRST_LOOKBACK_MINUTES = 15  # a project never polled before: issues updated in the last 15 minutes
OVERLAP_MINUTES = 2  # each poll looks a little further back than the last one, so nothing slips between
MAX_ISSUES = 50  # per project and poll


class Poller:
    def __init__(self, users: UserRepository, projects: ProjectRepository, oauth: AtlassianOAuth, queue: RunQueue):
        self.users = users
        self.projects = projects
        self.oauth = oauth
        self.queue = queue

    def start(self) -> None:
        threading.Thread(target=self.run_forever, daemon=True, name="poller").start()

    def run_forever(self) -> None:
        while True:
            try:
                self.poll()
            except Exception:
                log.exception("polling Jira failed; trying again in 15 minutes")
            time.sleep(POLL_SECONDS + random.uniform(0, 60))

    def poll(self, now: float | None = None) -> int:
        """One round over every active developer's projects. Returns how many issues were queued."""
        now = now or time.time()
        queued = 0
        for user in self.users.all():
            if user.paused:
                continue
            for project in self.projects.on_sites(list(user.sites)):
                if user.actor(project.id):
                    try:
                        queued += self._poll_project(user, project, now)
                    except Exception as e:  # e.g. revoked Jira access: the other projects still run
                        log.warning("polling %s for %s failed: %s", project.name, user.display_name, e)
        return queued

    def _poll_project(self, user: User, project: Project, now: float) -> int:
        actor = user.actor(project.id)
        last = self.users.polled_at(user.id, project.id)
        minutes = FIRST_LOOKBACK_MINUTES if not last else int((now - last) / 60) + OVERLAP_MINUTES
        keys = ", ".join(sorted(project.jira_projects))  # validated: letters, digits and _
        jql = (f'project in ({keys}) AND assignee = currentUser() AND statusCategory != Done '
               f'AND updated >= "-{minutes}m" ORDER BY updated ASC')
        jira = jira_client(self.oauth, self.users, user.subscriptions[project.id].jira_account_id, project.cloud_id)
        issues = jira.search(jql, ["issuetype", "assignee"], MAX_ISSUES)
        queued = 0
        for issue in issues:
            key, fields = issue.get("key") or "", issue.get("fields") or {}
            if (rules.ISSUE_KEY_RE.match(key) and project.handles(key) and rules.assignee(fields) == actor.jira_account_id
                    and rules.wanted(rules.issue_kind(fields), actor.mode)
                    and self.queue.submit(project, [key], user.id, automatic=True) is None):
                log.info("%s: queued for %s by polling", key, project.name)
                queued += 1
        self.users.mark_polled(user.id, project.id, now)
        return queued
