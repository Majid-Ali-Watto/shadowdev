"""Dry run: the real pipeline on the real repos for a made-up issue, without Jira, and without
pushing anything to GitHub.

Claude (with the developer's own token), clones of their base branches (with their GitHub
token), the sandboxed setup and tests and the self-heal loop all run for real. Jira writes are
printed instead of made, and nothing is committed, pushed or opened: the change stays in
worktrees/<project id>-<KEY> for inspection, until the next dry run replaces it.
"""
import dataclasses
import logging
import time

from . import gitops
from .pipeline import process_issues, workspace_name
from .timing import END, duration
from ..config import Config
from ..models.project import Project
from ..models.user import Actor, User
from ..repositories.projects import ProjectRepository
from ..repositories.users import UserRepository

log = logging.getLogger("shadowdev")


class PrintingJira:
    """Stands in for Jira: serves the made-up issue, and prints what would be written."""

    def __init__(self, fields: dict):
        self.fields = fields

    def get_issue(self, key: str) -> dict:
        return {"fields": self.fields}

    def project_versions(self, project_key: str) -> list[dict]:
        return []  # no Jira access in a dry run, so no fix version is picked

    def comment(self, key: str, text: str) -> None:
        print(f"\n--- Jira comment that would be posted on {key} ---\n{text}\n")

    def set_field(self, key: str, field: str, value) -> None:
        print(f"\n--- Jira field {field} that would be set on {key} ---\n{value}\n")

    def set_fix_versions(self, key: str, names: list[str]) -> None:
        print(f"--- Jira fix version that would be set on {key}: {', '.join(names)}")

    def move_along(self, key: str, path: list[str]) -> tuple[str, list[str]]:
        print(f"--- Jira status {key} would move to: {' → '.join(path)}")
        return path[-1], []


class NoGitHub:
    """Nothing is pushed or opened in a dry run, so push access is not checked."""

    def can_push(self, owner_repo: str) -> bool:
        return True


def pick_user(users: UserRepository, who: str | None) -> User:
    developers = users.all()
    if who:
        w = who.lower()
        matches = [u for u in developers if w == u.display_name.lower()
                   or any(w == a.account_id.lower() for a in u.jira_accounts)
                   or any(w == g.login.lower() for g in u.github_accounts)]
    else:
        matches = developers
    if len(matches) != 1:
        names = ", ".join(u.display_name for u in developers) or "nobody has signed up yet"
        raise SystemExit(f"Choose the developer with --as (their name, GitHub login or Jira account ID): {names}")
    return matches[0]


def pick_project(projects: ProjectRepository, user: User, name: str | None) -> Project:
    """One of the developer's projects; they need not have joined it."""
    mine = projects.on_sites(list(user.sites))
    joined = [p for p in mine if p.id in user.subscriptions]
    if name:
        found = [p for p in mine if p.name.lower() == name.lower()]
    else:
        found = joined if len(joined) == 1 else mine if len(mine) == 1 else []
    if len(found) != 1:
        names = ", ".join(p.name for p in mine) or "none: add one on the ShadowDev page"
        raise SystemExit(f"Choose a project with --project: {names}")
    return found[0]


def dry_run_actor(user: User, project: Project) -> Actor:
    """As the developer runs in the project, but whatever they automate; outside it, with their
    first GitHub and Claude accounts."""
    actor = user.actor(project.id)
    if actor:
        return dataclasses.replace(actor, mode="all")
    jira = next((a for a in user.jira_accounts if a.can_reach(project.cloud_id)), None)
    if not (jira and user.github_accounts and user.claude_accounts):
        raise SystemExit(f"{user.display_name} has not connected Claude and GitHub on the ShadowDev page yet")
    return Actor(user.id, user.display_name, jira.account_id, user.github_accounts[0], user.claude_accounts[0], "all", {})


def dry_run(cfg: Config, users: UserRepository, projects: ProjectRepository, project_name: str | None,
            who: str | None, issue_type: str, summary: str, description: str, note: str = "") -> None:
    user = pick_user(users, who)
    project = pick_project(projects, user, project_name)
    actor = dry_run_actor(user, project)

    jira_key = sorted(project.jira_projects)[0]
    key = f"{jira_key}-0"  # Jira numbers issues from 1, so this is never a real issue
    fields = {
        "summary": summary, "description": description, "issuetype": {"name": issue_type},
        "assignee": {"accountId": actor.jira_account_id}, "project": {"key": jira_key},
    }
    log.info("dry run %s as %s on %s: %s %r", key, user.display_name, project.name, issue_type, summary)
    started = time.monotonic()
    try:
        status, message = process_issues([key], project, actor, cfg, PrintingJira(fields), NoGitHub(), publish=False,
                                         note=note)

        print(f"\nResult: {status}: {message}")
        workspace = cfg.worktree_root / workspace_name(project, key)
        for repo in project.repos:
            worktree = workspace / repo.name
            if worktree.exists() and gitops.git(worktree, "status", "--porcelain"):
                print(f"\n{repo.name}: changed files (?? = new file)\n{gitops.git(worktree, 'status', '--short')}")
                print(f"  full diff: git -C {worktree} diff    (new files: open them in {worktree})")
        if workspace.exists():
            print(f"\nThe next dry run replaces {workspace}.")
    finally:
        print(f"\n{END} dry run {key} finished after {duration(time.monotonic() - started)}", flush=True)
