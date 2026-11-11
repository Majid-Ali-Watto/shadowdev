"""Shared test setup: a config, and developers with accounts and projects in a real database."""
from pathlib import Path

from cryptography.fernet import Fernet

from shadowdev.config import Config
from shadowdev.models.project import Project, Repo
from shadowdev.models.settings import Settings
from shadowdev.models.tokens import Tokens
from shadowdev.models.user import Site, Subscription
from shadowdev.repositories.projects import ProjectRepository
from shadowdev.repositories.users import UserRepository

OFFICE = Site("cloud-office", "https://office.atlassian.net", "office")
HOME = Site("cloud-home", "https://home.atlassian.net", "home")
TOKENS = Tokens("access", "refresh", 0)


def make_cfg(tmp: Path, **overrides) -> Config:
    values = dict(jira_sites=(OFFICE.url, HOME.url), atlassian_client_id="cid", atlassian_client_secret="cs",
                  public_url="http://localhost:8080", webhook_secret="hook-secret", secret_key=Fernet.generate_key().decode(),
                  max_fix_attempts=1, host="127.0.0.1", port=0, data_dir=tmp / "data", worktree_root=tmp / "worktrees")
    return Config(**{**values, **overrides})


def repo(name: str, test: str | None = "true", owner: str = "acme") -> Repo:
    return Repo(name=name, github=f"{owner}/{name}", description=f"the {name} repo", release_prefix="release/",
                setup=None, test=test)


def add_developer(users: UserRepository, account_id: str, sites=(OFFICE,), github: str | None = "gh",
                  claude: str | None = "sk-ant-oat01-x", user_id: int | None = None) -> int:
    """A developer signed in with `account_id` (or that Jira account connected to `user_id`), with a
    GitHub and a Claude account labelled after it."""
    uid = users.save_jira_account(user_id, account_id, account_id, f"{account_id}@x", list(sites), TOKENS)
    if github:
        users.save_github(uid, account_id, github, account_id, account_id, f"{account_id}@x")
    if claude:
        users.save_claude(uid, account_id, claude, f"key-{account_id}")
    return uid


def add_project(projects: ProjectRepository, owner_id: int, name: str, keys, repos=None, site: Site = OFFICE) -> Project:
    pid = projects.save(Project(0, name, owner_id, site.cloud_id, site.url, frozenset(keys), None,
                                tuple(repos or (repo("fe"), repo("services")))))
    return projects.get(pid)


def join(users: UserRepository, user_id: int, project: Project, account_id: str, mode: str = "bugs",
         bases: dict | None = None, github_login: str | None = None, claude_label: str | None = None,
         settings: Settings = Settings()) -> None:
    """Join with the Jira account `account_id`, and the GitHub/Claude accounts labelled after it
    (or the given ones)."""
    user = users.get(user_id)
    jira = next(a for a in user.jira_accounts if a.account_id == account_id)
    github = next(g for g in user.github_accounts if g.login == (github_login or account_id))
    claude = next(c for c in user.claude_accounts if c.label == (claude_label or account_id))
    users.subscribe(user_id, Subscription(project.id, jira.id, github.id, claude.id, mode, bases or {}, settings))
