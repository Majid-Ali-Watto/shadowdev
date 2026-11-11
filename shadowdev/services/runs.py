"""A developer running issues by hand, and stopping their runs."""

from ..automation import rules
from ..automation.pipeline import MAX_GROUP
from ..clients.atlassian import AtlassianOAuth, jira_client
from ..clients.rest import HTTPError
from ..models.user import User
from ..repositories.projects import ProjectRepository
from ..repositories.runs import KEPT_RUNS, RunRepository
from ..repositories.users import UserRepository
from .accounts import form_id
from .errors import UserError
from .projects import run_all
from .run_queue import RunQueue


class RunService:
    def __init__(self, users: UserRepository, projects: ProjectRepository, oauth: AtlassianOAuth, queue: RunQueue,
                 runs: RunRepository):
        self.runs = runs
        self.users = users
        self.projects = projects
        self.oauth = oauth
        self.queue = queue

    def request(self, user: User, issue_keys: str, together: bool, project_id: str = "", note: str = "") -> None:
        """Queue one or more issues (`issue_keys`: separated by spaces, commas or semicolons, and
        ranges like MAR-1 to MAR-10): each on its own branch and PR (run in parallel), or all
        together on one branch with one PR per repo. Of a range, the issues that aren't the
        developer's to run (not assigned to them, not found, or of a type they don't automate) are
        skipped, and listed as one skipped run; a key typed by itself must be theirs. Each key's
        project is found among the developer's; `project_id` picks one when the same Jira key is in
        several of them (e.g. MAR at work and at home). `note`: the developer's own instructions,
        given to Claude with the issues. For issues run separately, lines starting "MAR-12:" go only
        to that issue's run; the lines before them go to every run."""
        try:
            keys, ranged = rules.parse_keys(issue_keys)
        except ValueError as e:
            raise UserError(f"{e}.") from None
        if not keys:
            raise UserError("Enter one or more issue keys, like MAR-123, MAR-124, or a range, like MAR-1 to MAR-10.")
        note = "\n".join(note.split("\r\n")).strip()
        if len(note) > rules.MAX_NOTE_CHARS:
            raise UserError(f"The note is {len(note)} characters; keep it under {rules.MAX_NOTE_CHARS}.")
        general, parts = rules.split_note(note)
        unknown = [k for k in parts if k not in keys]
        if unknown:
            raise UserError(f"The note has a part for {', '.join(unknown)}, which is not among the issue keys.")
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
            actor = user.actor(project.id)
            jira = jira_client(self.oauth, self.users, sub.jira_account_id, project.cloud_id)
            try:
                fields = jira.get_issue(key)["fields"]
            except HTTPError:
                return f"{key} was not found in Jira, or you cannot see it."
            if rules.assignee(fields) != actor.jira_account_id:
                return f"{key} is not assigned to you."
            if key in ranged and not rules.wanted(rules.issue_kind(fields), actor.mode):
                return f"{key} is a {(fields.get('issuetype') or {}).get('name') or 'type'} you don't automate in {project.name}."
            return None

        found = dict(zip(keys, run_all([lambda k=k: assigned_problem(k) for k in keys]), strict=True))
        problems = [p for k, p in found.items() if p and k not in ranged]
        if problems:
            raise UserError(" ".join(problems))
        skipped = {k: p for k, p in found.items() if p}
        keys = [k for k in keys if k not in skipped]
        if not keys:
            raise UserError("None of those issues is yours to run: " + " ".join(skipped.values()))
        if together and len(keys) > MAX_GROUP:
            raise UserError(f"At most {MAX_GROUP} issues on one branch; {len(keys)} are yours in those. "
                            "Run them separately, or in smaller ranges.")
        if skipped:  # shown under Recent runs, so the developer sees which of the range were left out
            first = projects[next(iter(skipped))]
            run_id = self.runs.add(", ".join(skipped), user.id, first.id, first.name)
            self.runs.update(run_id, "skipped", "Not run, from the range: " + " ".join(skipped.values()))
        if together and len(keys) > 1:
            if len({p.id for p in projects.values()}) > 1:
                raise UserError("Issues fixed on one branch must all be in the same project.")
            refusal = self.queue.submit(projects[keys[0]], keys, user.id, note=note)
            if refusal:
                raise UserError(f"Not queued: {refusal}.")
            return
        refused = [f"{k}: {r}" for k in keys if (r := self.queue.submit(
            projects[k], [k], user.id, note="\n".join(filter(None, [general, parts.get(k)]))))]
        if refused:
            queued = len(keys) - len(refused)
            raise UserError(("Queued the others. " if queued else "") + "Not queued: " + "; ".join(refused) + ".")

    def delete(self, user: User, run_ids: list[str]) -> None:
        """Remove finished runs from the developer's Recent runs. Queued and running ones are not
        removed (stop them first); the tickets and PRs they made are not touched."""
        ids = list(dict.fromkeys(int(i) for i in run_ids if i.isdigit()))[:KEPT_RUNS]
        if not ids:
            raise UserError("Select the runs to delete.")
        if not self.runs.delete_finished(user.id, ids):
            raise UserError("Those runs are still queued or running (stop them first), or are already gone.")

    def stop(self, user: User, run_id: str) -> None:
        """Stop one of the developer's queued or running runs. Its ticket is left as it was."""
        if not run_id.isdigit() or not self.queue.stop_run(int(run_id), user.id):
            raise UserError("That run has already finished, or is not yours.")
