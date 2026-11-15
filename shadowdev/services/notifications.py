"""Test messages: a developer sees how what needs them looks in Slack, or by email, before a run
sends it for real. Each is a sample of a real kind, built by the same code, with the project's
own issue keys: a decision a run needs, tickets the status rules hold, a decision on two issues.
At most MAX_TESTS of each way per project (by email, per address), COOLDOWN_SECONDS apart."""
import logging
import time

from ..automation.pipeline import decision_message, email_developer
from ..clients.rest import HTTPError
from ..clients.slack import Slack, link_issues
from ..clients.slack import escape as slack_escape
from ..config import Config
from ..models.project import Project
from ..models.release import StatusRule
from ..models.user import MAX_TESTS, User, address_key
from ..repositories.projects import ProjectRepository
from ..repositories.users import UserRepository
from ..views import emails
from .accounts import form_id
from .errors import UserError
from .releases import missing_message

log = logging.getLogger("shadowdev")

COOLDOWN_SECONDS = 30  # between two test messages of one developer in one project
WAYS = {"slack": "Slack message", "email": "email"}


class NotificationTests:
    def __init__(self, cfg: Config, users: UserRepository, projects: ProjectRepository):
        self.cfg = cfg
        self.users = users
        self.projects = projects

    def send(self, user: User, project_id: str, way: str, now: float | None = None) -> None:
        """Send the developer the next test message of the project `way` ("slack", "email")."""
        now = time.time() if now is None else now
        project = self.projects.get(form_id(project_id) or 0)
        sub = user.subscriptions.get(project.id) if project and project.cloud_id in user.sites else None
        if not sub or way not in WAYS:
            raise UserError("Join the project first.")
        actor = user.actor(project.id)
        if not actor:
            raise UserError("Choose this project's GitHub and Claude accounts first, and save.")
        if way == "slack" and not user.slack:
            raise UserError("Connect Slack on the Accounts tab first.")
        if way == "email" and not self.cfg.smtp:
            raise UserError("This server sends no email: its .env needs SMTP_HOST.")
        if way == "email" and not actor.email:
            raise UserError("None of your accounts shows an email: type one under Email me at, and save.")
        left = sub.tests_left(way, actor.email)
        if not left:
            raise UserError(f"You have sent the {MAX_TESTS} test {WAYS[way]}s of {project.name} already.")
        wait = COOLDOWN_SECONDS - (now - float(sub.tests_sent.get("at", 0)))
        if wait > 0:
            raise UserError(f"Wait {int(wait) + 1} seconds before sending another test message.")
        n = MAX_TESTS - left  # 0 for the first: each test is a different sample
        subject, slack_text, text, html = sample(project, n)
        if way == "slack":
            try:
                Slack(user.slack.token).post(user.slack.user_id, slack_text)
            except HTTPError as e:
                log.warning("%s: could not send %s a test Slack message: %s", project.name, user.display_name, e)
                raise UserError("Slack refused the message: reconnect Slack on the Accounts tab?") from None
        elif not email_developer(actor, self.cfg, subject, text, project.name, html):
            raise UserError("The email could not be sent: the server log says why.")
        sent = dict(sub.tests_sent)
        if way == "email" and sent.get("email_to") != address_key(actor.email):
            sent.update(email=0, email_to=address_key(actor.email))  # a new address: counted again
        sent.update({way: int(sent.get(way, 0)) + 1, "at": now})
        self.users.save_tests_sent(user.id, project.id, sent)
        self.users.record(user.id, f"Sent a test {WAYS[way]}", project.name)


def sample(project: Project, n: int) -> tuple[str, str, str, str]:
    """Test message `n` (0 to MAX_TESTS - 1): (email subject, Slack text, email text, email HTML)."""
    key = min(project.jira_projects)
    keys = [f"{key}-{number}" for number in range(101, 106)]
    site = project.site_url
    note = f"Test {n + 1} of {MAX_TESTS}: sample tickets, nothing to do."
    if n == 1:
        rule = StatusRule("Ready for Release", "Done")
        gaps = [(keys[1], rule, ["root cause"]), (keys[2], rule, ["fix version", "logged time"])]
        return (f"Test {n + 1} of {MAX_TESTS}: {project.name}: tickets not complete yet",
                f"{note}\n{missing_message(project, gaps)}",
                f"{note}\n\n{missing_message(project, gaps, slack=False)}", emails.missing(project, gaps, note))
    questions = ({keys[0]: ["Should the discount apply before or after tax? The code does both, in different places."]}
                 if n == 0 else
                 {keys[3]: ["The fix needs changes in the services repo too, but you chose to change only the front end. "
                            "Run it again with both, or keep to the front end?"],
                  keys[4]: ["Keep the old endpoint for older app versions, or remove it with this fix?"]})
    subject, text = decision_message(questions)
    links = "\n\n" + "\n".join(f"{k}: {site}/browse/{k}" for k in questions)
    return (f"Test {n + 1} of {MAX_TESTS}: {subject}", f"{note}\n{link_issues(slack_escape(text), site, list(questions))}",
            f"{note}\n\n{text}{links}", emails.decision(questions, site, note))
