"""Projects on the web page: any developer can add one for a Jira site they can reach; its owner
edits or deletes it; developers with access to its Jira site join it with the Jira, GitHub and
Claude accounts it should run with, and leave it.

Only the owner can edit a project, and only while their GitHub account can push to all its repos:
its setup/test commands run on every member's runs, so changing them needs the same access as
changing the repos' own code."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..automation import gitops
from ..clients.atlassian import AtlassianOAuth, jira_client
from ..clients.github import GitHub
from ..clients.rest import HTTPError
from ..models.project import (CUSTOM_FIELD_RE, JIRA_PROJECT_RE, PROJECT_NAME_RE, REPO_NAME_RE, Project, Repo,
                              default_repo_name, parse_github)
from ..models.user import MODES, Subscription, User
from ..repositories.projects import ProjectRepository, Taken
from ..repositories.users import UserRepository
from .accounts import form_id
from .errors import UserError
from .run_queue import RunQueue

MAX_REPOS = 10
MAX_KEYS = 20
MAX_COMMAND_CHARS = 4000


@dataclass
class RepoForm:
    github: str = ""  # owner/name, or its URL
    name: str = ""
    description: str = ""
    release_prefix: str = ""
    setup: str = ""
    test: str = ""


@dataclass
class ProjectForm:
    name: str = ""
    cloud_id: str = ""
    jira_keys: str = ""  # "MAR, MARS"
    github_account_id: str = ""  # the owner's account the repos are checked with
    root_cause_field: str = ""
    start_statuses: str = ""  # "To Do, In Progress"
    review_statuses: str = ""
    repos: list[RepoForm] = field(default_factory=list)


def split_list(text: str) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in text.replace(";", ",").split(",") if s.strip()))


def statuses(text: str, default: tuple[str, ...], what: str) -> tuple[str, ...]:
    names = split_list(text) or list(default)
    if len(names) > 8 or any(len(n) > 60 for n in names):
        raise UserError(f"{what}: at most 8 Jira status names, separated by commas.")
    return tuple(names)


def run_all(checks: list):
    """Run the checks (functions) at once: each asks GitHub or Jira over the network."""
    if not checks:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(checks))) as pool:
        return list(pool.map(lambda check: check(), checks))


class ProjectService:
    def __init__(self, users: UserRepository, projects: ProjectRepository, oauth: AtlassianOAuth, queue: RunQueue):
        self.users = users
        self.projects = projects
        self.oauth = oauth
        self.queue = queue

    def visible(self, user: User) -> list[Project]:
        """Projects on the Jira sites the developer can reach."""
        return self.projects.on_sites(list(user.sites))

    def visible_project(self, user: User, project_id: str | int) -> Project:
        project = self.projects.get(project_id if isinstance(project_id, int) else form_id(project_id) or 0)
        if not project or project.cloud_id not in user.sites:
            raise UserError("No such project, or it is on a Jira site you can't reach.")
        return project

    def owned_project(self, user: User, project_id: str) -> Project:
        project = self.visible_project(user, project_id)
        if project.owner_id != user.id:
            raise UserError("Only the project's owner can change it.")
        return project

    def _jira_for(self, user: User, cloud_id: str, jira_account_id: int | None = None):
        account = next((a for a in user.jira_accounts if a.can_reach(cloud_id)
                        and (jira_account_id is None or a.id == jira_account_id)), None)
        if not account:
            raise UserError("Choose one of your Jira accounts on the project's Jira site.")
        return account, jira_client(self.oauth, self.users, account.id, cloud_id)

    def save(self, user: User, form: ProjectForm, project_id: str = "") -> int:
        """Create a project (owned by the developer, who joins it), or update one they own.
        Returns its ID."""
        existing = self.owned_project(user, project_id) if project_id else None
        cloud_id = existing.cloud_id if existing else form.cloud_id
        site = user.sites.get(cloud_id)
        if not site:
            raise UserError("Choose one of your Jira sites.")
        name = " ".join(form.name.split())
        if not PROJECT_NAME_RE.match(name):
            raise UserError("Name the project: letters, digits, spaces, - _ and ., up to 60 characters.")
        keys = [k.upper() for k in split_list(form.jira_keys)]
        if not keys or len(keys) > MAX_KEYS or not all(JIRA_PROJECT_RE.match(k) for k in keys):
            raise UserError("Jira projects: the keys of the Jira projects whose issues it handles, like MAR, MARS.")
        root_cause = form.root_cause_field.strip() or None
        if root_cause and not CUSTOM_FIELD_RE.match(root_cause):
            raise UserError("Root cause field: a Jira custom field ID like customfield_10201, or empty.")
        start = statuses(form.start_statuses, Project.start_statuses, "Start statuses")
        review = statuses(form.review_statuses, Project.review_statuses, "Review statuses")
        github_account = user.github(form_id(form.github_account_id))
        if not github_account:
            raise UserError("Choose your GitHub account that can push to the repos.")
        rows = [r for r in form.repos if r.github.strip() or r.test.strip()]
        if not rows or len(rows) > MAX_REPOS:
            raise UserError(f"Add the project's repos: 1 to {MAX_REPOS}.")
        repos = []
        for row in rows:
            owner_repo = parse_github(row.github)
            if not owner_repo:
                raise UserError(f"Repo {row.github.strip()!r}: give the GitHub repo as owner/name or its URL.")
            repo_name = row.name.strip().lower() or default_repo_name(owner_repo)
            if not REPO_NAME_RE.match(repo_name):
                raise UserError(f"{owner_repo}: folder name {repo_name!r}: lowercase letters, digits, - and _.")
            prefix = row.release_prefix.strip() or "release/"
            if not gitops.valid_branch_name(prefix + "1"):
                raise UserError(f"{owner_repo}: {prefix!r} is not a usable branch prefix.")
            setup, test = row.setup.strip() or None, row.test.strip() or None
            if len(setup or "") > MAX_COMMAND_CHARS or len(test or "") > MAX_COMMAND_CHARS:
                raise UserError(f"{owner_repo}: commands are limited to {MAX_COMMAND_CHARS} characters.")
            repos.append(Repo(repo_name, owner_repo, " ".join(row.description.split()), prefix, setup, test))
        if len({r.name for r in repos}) != len(repos) or len({r.github for r in repos}) != len(repos):
            raise UserError("Each repo, and its folder name, can be in the project only once.")
        for key in keys:
            other = self.projects.for_issue(cloud_id, f"{key}-1")
            if other and (not existing or other.id != existing.id):
                raise UserError(f"Jira project {key} is already handled by the project {other.name} on {site.name}.")

        jira_account, jira = self._jira_for(user, cloud_id)
        github = GitHub(github_account.token)
        problems = [p for p in run_all(
            [lambda k=k: self._jira_problem(jira, k, site.name) for k in keys]
            + [lambda r=r: self._push_problem(github, github_account.login, r.github) for r in repos]) if p]
        if problems:
            raise UserError("Not saved:\n" + "\n".join(problems))
        # A repo without a description gets GitHub's, which tells Claude what lives there.
        infos = run_all([lambda r=r: github.repo(r.github) for r in repos if not r.description])
        described = iter(infos)
        repos = [r if r.description else Repo(r.name, r.github, (next(described) or {}).get("description")
                                              or f"The {r.github.split('/')[1]} repository", r.release_prefix,
                                              r.setup, r.test) for r in repos]
        project = Project(existing.id if existing else 0, name, existing.owner_id if existing else user.id, cloud_id,
                          site.url, frozenset(keys), root_cause, tuple(repos), start, review)
        try:
            pid = self.projects.save(project)
        except Taken as e:
            raise UserError(f"There is already a project named {name} on {site.name}." if e.what == "name"
                            else "One of these Jira projects is already handled by another project.") from None
        if not existing:
            claude = user.claude_accounts[0].id if len(user.claude_accounts) == 1 else None
            self.users.subscribe(user.id, Subscription(pid, jira_account.id, github_account.id, claude, "bugs", {}))
        return pid

    @staticmethod
    def _jira_problem(jira, key: str, site: str) -> str | None:
        try:
            jira.project(key)
        except HTTPError:
            return f"Your Jira account cannot see Jira project {key} on {site}."
        return None

    @staticmethod
    def _push_problem(github: GitHub, login: str, owner_repo: str) -> str | None:
        return None if github.can_push(owner_repo) else f"GitHub account {login} cannot push to {owner_repo}."

    def delete(self, user: User, project_id: str) -> None:
        project = self.owned_project(user, project_id)
        self.queue.stop_runs(project_ids={project.id})
        self.projects.delete(project.id)

    def webhook_secret(self, user: User, project: Project) -> str | None:
        """Shown to the owner, who sets up the Jira webhook."""
        return self.projects.webhook_secret(project.id) if project.owner_id == user.id else None

    def join(self, user: User, project_id: str, jira_account_id: str, github_account_id: str, claude_account_id: str,
             mode: str, base_branches: dict[str, str]) -> None:
        """Join, or change how the developer runs in, a project. `base_branches`: repo name ->
        branch they typed; empty means the latest release branch, else the default branch."""
        project = self.visible_project(user, project_id)
        jira_account, jira = self._jira_for(user, project.cloud_id, form_id(jira_account_id))
        github_account = user.github(form_id(github_account_id))
        claude_account = user.claude(form_id(claude_account_id))
        if not github_account or not claude_account:
            raise UserError("Choose the GitHub and the Claude account to work on this project with.")
        if mode not in MODES:
            raise UserError("Choose what to automate.")
        bases = {}
        for repo in project.repos:
            branch = base_branches.get(repo.name, "").strip()
            if branch:
                if not gitops.valid_branch_name(branch):
                    raise UserError(f"{repo.name}: {branch!r} is not a branch name.")
                bases[repo.name] = branch
        github, token = GitHub(github_account.token), github_account.token
        site = user.sites[project.cloud_id].name

        def branch_problem(repo: Repo) -> str | None:
            branch = bases.get(repo.name)
            if branch and not gitops.remote_branch_exists(repo.github, token, branch):
                return f"{repo.name}: branch {branch!r} does not exist on {repo.github}."
            return None

        problems = [p for p in run_all(
            [lambda k=k: self._jira_problem(jira, k, site) for k in sorted(project.jira_projects)]
            + [lambda r=r: self._push_problem(github, github_account.login, r.github) for r in project.repos]
            + [lambda r=r: branch_problem(r) for r in project.repos]) if p]
        if problems:
            raise UserError("Not saved:\n" + "\n".join(problems))
        self.users.subscribe(user.id, Subscription(project.id, jira_account.id, github_account.id, claude_account.id,
                                                   mode, bases))

    def leave(self, user: User, project_id: str) -> None:
        project = self.visible_project(user, project_id)
        self.queue.stop_runs(user.id, {project.id})
        self.users.unsubscribe(user.id, project.id)
