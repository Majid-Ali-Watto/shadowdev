"""A developer and the accounts they connected. One person can have several of each kind, e.g. a
personal and an office Jira, GitHub and Claude; each project they join says which ones it uses."""
from dataclasses import dataclass, field

from .limits import Limits
from .settings import Settings

MODES = ("bugs", "all")  # "bugs": only Bugs; "all": Bugs, Tasks and Stories


@dataclass(frozen=True)
class Site:
    """A Jira Cloud site."""
    cloud_id: str
    url: str  # https://acme.atlassian.net
    name: str


@dataclass(frozen=True)
class JiraAccount:
    """An Atlassian account, signed in through OAuth; its tokens stay in the repository."""
    id: int
    account_id: str  # Atlassian account ID: issues assigned to it are this developer's
    display_name: str
    email: str  # may be empty: Atlassian hides it under some privacy settings
    sites: tuple[Site, ...]

    def can_reach(self, cloud_id: str) -> bool:
        return any(s.cloud_id == cloud_id for s in self.sites)


@dataclass(frozen=True)
class GitHubAccount:
    id: int
    label: str
    login: str
    token: str
    git_name: str  # commit author
    git_email: str


@dataclass(frozen=True)
class ClaudeAccount:
    id: int
    label: str
    token: str  # `claude setup-token` OAuth token, or an Anthropic API key
    config_key: str  # its own Claude config directory, data/claude/<config_key>


@dataclass(frozen=True)
class SlackAccount:
    """The developer in Slack: ShadowDev posts and reads there as them, with their user token."""
    token: str  # xoxp-…
    user_id: str  # Slack member ID, as in <@U012ABC> mentions
    team: str  # the workspace's name


@dataclass(frozen=True)
class Subscription:
    """A developer in a project: the accounts it runs with there, and what it automates."""
    project_id: int
    jira_account_id: int
    github_account_id: int | None  # None: removed since; the developer must choose another
    claude_account_id: int | None
    mode: str
    base_branches: dict[str, str] = field(default_factory=dict)  # repo name -> branch; missing = default
    settings: Settings = Settings()


@dataclass(frozen=True)
class Actor:
    """Who a run acts as: one developer, with the accounts of one of their subscriptions."""
    user_id: int
    display_name: str
    jira_account_id: str  # Atlassian account ID the issues must be assigned to
    github: GitHubAccount
    claude: ClaudeAccount
    mode: str
    base_branches: dict[str, str]
    slack: SlackAccount | None = None
    settings: Settings = Settings()


@dataclass
class User:
    id: int
    display_name: str
    email: str
    paused: bool = False
    jira_accounts: tuple[JiraAccount, ...] = ()
    github_accounts: tuple[GitHubAccount, ...] = ()
    claude_accounts: tuple[ClaudeAccount, ...] = ()
    subscriptions: dict[int, Subscription] = field(default_factory=dict)  # project id -> subscription
    slack: SlackAccount | None = None
    limits: Limits = Limits()

    def jira(self, jira_account_id: int | None) -> JiraAccount | None:
        return next((a for a in self.jira_accounts if a.id == jira_account_id), None)

    def github(self, github_account_id: int | None) -> GitHubAccount | None:
        return next((a for a in self.github_accounts if a.id == github_account_id), None)

    def claude(self, claude_account_id: int | None) -> ClaudeAccount | None:
        return next((a for a in self.claude_accounts if a.id == claude_account_id), None)

    @property
    def sites(self) -> dict[str, Site]:
        """Every Jira site any of the developer's Jira accounts can reach, by cloud ID."""
        return {s.cloud_id: s for a in self.jira_accounts for s in a.sites}

    def actor(self, project_id: int) -> Actor | None:
        """The run identity for a project: None unless joined with a GitHub and a Claude account."""
        sub = self.subscriptions.get(project_id)
        jira = self.jira(sub.jira_account_id) if sub else None
        github = self.github(sub.github_account_id) if sub else None
        claude = self.claude(sub.claude_account_id) if sub else None
        if not (jira and github and claude):
            return None
        return Actor(self.id, self.display_name, jira.account_id, github, claude, sub.mode, sub.base_branches, self.slack,
                     sub.settings)

    @property
    def ready_projects(self) -> list[int]:
        return [p for p in self.subscriptions if self.actor(p)]
