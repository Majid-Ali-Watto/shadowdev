"""Moving a developer's finished tickets along by their project's status rules (models.release), e.g.
Ready for Release -> Done once the ticket has its fix version, parent, estimate and logged time, and
for a bug its root cause. For the developers who switched it on (Settings.release_moves) every
Settings.release_hours, and whenever they ask on the Projects tab. Only tickets assigned to the
Jira account of the developer's subscription are moved; one that misses a check stays where it is,
the check's report on the project's card says what it misses, and the developer is told in a Slack
direct message (if they connected Slack): on every check they ask for, and on a scheduled one when
something is missing that the last check did not report, so the same list does not come every few hours."""
import logging
import random
import threading
import time

from ..automation import rules
from ..clients.atlassian import AtlassianOAuth, JiraAccessLost, jira_client
from ..clients.jira import Jira
from ..clients.rest import HTTPError
from ..clients.slack import Slack, issue_link
from ..clients.slack import escape as slack_escape
from ..models import release
from ..models.project import Project
from ..models.release import ReleaseReport, StatusRule
from ..models.user import User
from ..repositories.projects import ProjectRepository
from ..repositories.users import UserRepository
from .accounts import form_id
from .errors import UserError

log = logging.getLogger("shadowdev")

MAX_ISSUES = 50  # per rule and check
TICK_SECONDS = 300  # how often it looks for subscriptions due a check


class ReleaseMover:
    def __init__(self, users: UserRepository, projects: ProjectRepository, oauth: AtlassianOAuth):
        self.users = users
        self.projects = projects
        self.oauth = oauth

    def start(self) -> None:
        threading.Thread(target=self.run_forever, daemon=True, name="releases").start()

    def run_forever(self) -> None:
        while True:
            try:
                self.poll()
            except Exception:
                log.exception("moving released tickets failed; trying again later")
            time.sleep(TICK_SECONDS + random.uniform(0, 30))

    def poll(self, now: float | None = None) -> int:
        """Check the projects of every active developer who switched the moves on, that are due by
        their interval. Returns how many tickets were moved."""
        now = now or time.time()
        moved = 0
        for user in self.users.all():
            if user.paused:
                continue
            for project in self.projects.on_sites(list(user.sites)):
                sub = user.subscriptions.get(project.id)
                if not sub or not sub.settings.release_moves or not project.status_rules:
                    continue
                if now - self.users.released_at(user.id, project.id) < int(sub.settings.release_hours) * 3600:
                    continue
                try:
                    moved += len(self._check(user, project, now).moved)
                except Exception as e:  # e.g. revoked Jira access: the other projects still run
                    log.warning("moving released tickets of %s for %s failed: %s", project.name, user.display_name, e)
        return moved

    def check_now(self, user: User, project_id: str) -> ReleaseReport:
        """The developer asked: check their tickets in the project now, switched on or not."""
        project = self.projects.get(form_id(project_id) or 0)
        if not project or project.cloud_id not in user.sites or project.id not in user.subscriptions:
            raise UserError("Join the project first.")
        if user.paused:
            raise UserError("Your automation is paused. Resume it to move tickets.")
        if not project.status_rules:
            raise UserError(f"{project.name} has no status rules; its owner sets them in the project's Jira workflow.")
        try:
            return self._check(user, project, time.time(), asked=True)
        except JiraAccessLost as e:
            raise UserError(f"{e}.") from None

    def _check(self, user: User, project: Project, now: float, asked: bool = False) -> ReleaseReport:
        """`asked`: the developer pressed Check now (always told in Slack what is missing)."""
        sub = user.subscriptions[project.id]
        me = user.jira(sub.jira_account_id).account_id
        jira = jira_client(self.oauth, self.users, sub.jira_account_id, project.cloud_id)
        keys = ", ".join(sorted(project.jira_projects))  # validated: letters, digits and _
        moved, held, failed = [], [], []
        gaps_of: list[tuple[str, StatusRule, list[str]]] = []  # (key, rule, what it misses), for Slack
        for rule in project.status_rules:
            # Status names hold no quote or backslash (release.parse_rules).
            jql = (f'project in ({keys}) AND assignee = currentUser() AND status = "{rule.from_status}" '
                   f"ORDER BY key ASC")
            try:
                issues = jira.search(jql, ["assignee", *release.fields_needed(project.root_cause_field)], MAX_ISSUES)
            except HTTPError as e:
                if e.status == 0:
                    raise
                failed.append(f"Jira could not search for tickets in {rule.from_status}: is it a status of the project?")
                continue
            for issue in issues:
                key, fields = issue.get("key") or "", issue.get("fields") or {}
                if not (rules.ISSUE_KEY_RE.match(key) and project.handles(key) and rules.assignee(fields) == me):
                    continue
                gaps = release.missing(rule, fields, project.root_cause_field)
                if gaps:
                    held.append(f"{key} ({rule.from_status}): no {', '.join(gaps)}")
                    gaps_of.append((key, rule, gaps))
                    continue
                try:
                    problem = self._move(jira, key, rule)
                except HTTPError as e:
                    if e.status == 0:
                        raise
                    problem = f"{key}: Jira refused the move to {rule.to_status} (does the transition ask for a field?)"
                if problem:
                    failed.append(problem)
                else:
                    log.info("%s: moved from %s to %s for %s", key, rule.from_status, rule.to_status, user.display_name)
                    moved.append(f"{key} to {rule.to_status}")
        previous = sub.release_report.held if sub.release_report else ()
        if gaps_of and user.slack and (asked or set(held) - set(previous)):
            try:
                Slack(user.slack.token).post(user.slack.user_id, missing_message(project, gaps_of))
                log.info("%s: %s told in Slack about %d incomplete tickets", project.name, user.display_name, len(gaps_of))
            except HTTPError as e:
                log.warning("%s: could not tell %s in Slack about incomplete tickets: %s", project.name, user.display_name, e)
                failed.append("Could not send you the missing fields in Slack: reconnect Slack on the Accounts tab?")
        report = ReleaseReport(now, tuple(moved), tuple(held), tuple(failed))
        self.users.save_release_report(user.id, project.id, report)
        return report

    @staticmethod
    def _move(jira: Jira, key: str, rule: StatusRule) -> str | None:
        """Move one ticket by the rule; what went wrong, or None. Its status is read again first, so
        a ticket someone moved since the search is left alone."""
        status = jira.status(key)
        if status.lower() != rule.from_status.lower():
            return f"{key}: now in {status}, left alone"
        step = next((t for t in jira.transitions(key) if t["to"]["name"].lower() == rule.to_status.lower()), None)
        if not step:
            return f"{key}: the workflow has no transition from {rule.from_status} to {rule.to_status}"
        jira.transition(key, step["id"])
        return None


def missing_message(project: Project, gaps_of: list[tuple[str, StatusRule, list[str]]]) -> str:
    """The developer's direct message: each ticket left where it is, linked, and what it misses."""
    lines = [f"• {issue_link(project.site_url, key)} stays in {slack_escape(rule.from_status)}, "
             f"missing {slack_escape(', '.join(gaps))} (needed for {slack_escape(rule.to_status)})"
             for key, rule, gaps in gaps_of]
    return "These tickets are not complete yet, so they were not moved:\n" + "\n".join(lines)
