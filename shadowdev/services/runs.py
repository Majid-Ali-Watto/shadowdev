"""A developer running issues by hand, and stopping their runs."""
import re

from ..automation import rules
from ..automation.pipeline import MAX_GROUP
from ..clients.atlassian import AtlassianOAuth, jira_client
from ..clients.rest import HTTPError
from ..models.user import User
from ..repositories.projects import ProjectRepository
from ..repositories.users import UserRepository
from .accounts import form_id
from .errors import UserError
from .projects import run_all
from .run_queue import RunQueue


class RunService:
    def __init__(self, users: UserRepository, projects: ProjectRepository, oauth: AtlassianOAuth, queue: RunQueue):
        self.users = users
        self.projects = projects
        self.oauth = oauth
        self.queue = queue

    def request(self, user: User, issue_keys: str, together: bool, project_id: str = "") -> None:
        """Queue one or more issues (`issue_keys`: separated by spaces, commas or semicolons):
        each on its own branch and PR (run in parallel), or all together on one branch with one
        PR per repo. Each key's project is found among the developer's; `project_id` picks one
        when the same Jira key is in several of them (e.g. MAR at work and at home)."""
        keys = list(dict.fromkeys(re.split(r"[\s,;]+", issue_keys.strip().upper())))
        keys = [k for k in keys if k]
        if not keys:
            raise UserError("Enter one or more issue keys, like MAR-123, MAR-124.")
        bad = [k for k in keys if not rules.ISSUE_KEY_RE.match(k)]
        if bad:
            raise UserError(f"Not an issue key: {', '.join(bad)}")
        if len(keys) > MAX_GROUP:
            raise UserError(f"At most {MAX_GROUP} issues at a time.")
        if user.paused:
            raise UserError("Your automation is paused. Resume it to run issues.")
        mine = [p for p in self.projects.on_sites(list(user.sites)) if p.id in user.subscriptions]
        if project_id:
            mine = [p for p in mine if p.id == form_id(project_id)]
        if not mine:
            raise UserError("Join a project first.")
        projects = {}
        for key in keys:
            matches = [p for p in mine if p.handles(key)]
            if not matches:
                raise UserError(f"{key} is not in one of your projects.")
            if len(matches) > 1:
                raise UserError(f"{key} is in several of your projects ({', '.join(p.name for p in matches)}): "
                                "choose the project.")
            project = matches[0]
            if not user.actor(project.id):
                raise UserError(f"{project.name}: choose the GitHub and the Claude account to run it with first.")
            projects[key] = project

        def assigned_problem(key: str) -> str | None:
            project, sub = projects[key], user.subscriptions[projects[key].id]
            jira = jira_client(self.oauth, self.users, sub.jira_account_id, project.cloud_id)
            try:
                fields = jira.get_issue(key)["fields"]
            except HTTPError:
                return f"{key} was not found in Jira, or you cannot see it."
            if rules.assignee(fields) != user.actor(project.id).jira_account_id:
                return f"{key} is not assigned to you."
            return None

        problems = [p for p in run_all([lambda k=k: assigned_problem(k) for k in keys]) if p]
        if problems:
            raise UserError(" ".join(problems))
        if together and len(keys) > 1:
            if len({p.id for p in projects.values()}) > 1:
                raise UserError("Issues fixed on one branch must all be in the same project.")
            refusal = self.queue.submit(projects[keys[0]], keys, user.id)
            if refusal:
                raise UserError(f"Not queued: {refusal}.")
            return
        refused = [f"{k}: {r}" for k in keys if (r := self.queue.submit(projects[k], [k], user.id))]
        if refused:
            queued = len(keys) - len(refused)
            raise UserError(("Queued the others. " if queued else "") + "Not queued: " + "; ".join(refused) + ".")

    def stop(self, user: User, run_id: str) -> None:
        """Stop one of the developer's queued or running runs. Its ticket is left as it was."""
        if not run_id.isdigit() or not self.queue.stop_run(int(run_id), user.id):
            raise UserError("That run has already finished, or is not yours.")
