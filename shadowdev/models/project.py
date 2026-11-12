"""A project: the Jira projects (on one Jira site) whose issues it handles, and the repos it changes.
Projects are created and edited by developers on the web page and kept in the database."""
import re
from dataclasses import dataclass

from .release import DEFAULT_RULES, StatusRule

# Repo names become directory names inside the issue's workspace.
REPO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}\Z")
# owner/name. GitHub owners are letters, digits and -; repo names never "." or "..".
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.{1,2}\Z)[A-Za-z0-9_.-]{1,100}\Z")
JIRA_PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,19}\Z")
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}\Z")
SLACK_CHANNEL_RE = re.compile(r"^[CG][A-Z0-9]{6,20}\Z")  # a channel's ID, not its name
SLACK_MEMBER_RE = re.compile(r"^[UW][A-Z0-9]{6,20}\Z")
CUSTOM_FIELD_RE = re.compile(r"^customfield_[0-9]{1,10}\Z")
_GITHUB_URL_RE = re.compile(r"^(?:https://github\.com/|git@github\.com:)?([^/\s]+/[^/\s]+?)(?:\.git)?/?\Z")


def parse_github(value: str) -> str | None:
    """owner/name from what a developer pastes: owner/name, its https URL, or its SSH address."""
    match = _GITHUB_URL_RE.match(value.strip())
    owner_repo = match.group(1) if match else ""
    return owner_repo if GITHUB_REPO_RE.match(owner_repo) else None


def default_repo_name(owner_repo: str) -> str:
    """The workspace directory for a repo when none is given: market-place-fe -> market-place-fe."""
    name = re.sub(r"[^a-z0-9_-]+", "-", owner_repo.split("/")[1].lower()).strip("-_")[:40]
    return name if REPO_NAME_RE.match(name) else "repo"


@dataclass(frozen=True)
class Repo:
    name: str  # directory name in the issue's workspace, e.g. "fe"
    github: str  # owner/name; each run clones it with the developer's token and deletes it after
    description: str  # tells Claude what lives in this repo
    release_prefix: str  # default base branch: the highest numeric <prefix><version>, else the default branch
    setup: str | None  # run once in a fresh worktree before its first test run, e.g. "npm ci"
    test: str | None  # writes JUnit XML to $SHADOWDEV_REPORT_DIR; None: the change is not tested
    reviewers: tuple[str, ...] = ()  # Slack member IDs mentioned when a PR of this repo needs review


@dataclass(frozen=True)
class Project:
    id: int
    name: str  # what developers see
    owner_id: int  # the developer who may edit it
    cloud_id: str  # the Jira site
    site_url: str  # e.g. https://acme.atlassian.net
    jira_projects: frozenset[str]  # Jira project keys on that site, e.g. {"MAR"}
    root_cause_field: str | None  # e.g. customfield_10201; None -> post root cause as a comment
    repos: tuple[Repo, ...]
    # Jira statuses to walk the issue through when work starts, and once the PR is open.
    start_statuses: tuple[str, ...] = ("In Progress",)
    review_statuses: tuple[str, ...] = ("In Review",)
    slack_channel: str | None = None  # channel ID where PRs are announced for review, and reviews are asked for
    # Finished tickets moved along once complete (Settings.release_moves), e.g. Ready for Release -> Done.
    status_rules: tuple[StatusRule, ...] = DEFAULT_RULES

    def repo(self, name: str) -> Repo | None:
        return next((r for r in self.repos if r.name == name), None)

    def handles(self, issue_key: str) -> bool:
        return issue_key.rpartition("-")[0] in self.jira_projects
