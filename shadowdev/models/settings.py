"""What ShadowDev does for a developer in one project. Every automation can be switched off, so
the same server can do everything for one project and only, say, reviews for another."""
import json
from dataclasses import asdict, dataclass, fields

MODEL_CHOICES = ("auto", "haiku", "sonnet", "default")  # auto: rated per issue
PUBLISH_CHOICES = ("pr", "draft", "branch")  # a PR, a draft PR, or only the pushed branch
RELEASE_HOURS_CHOICES = ("4", "8", "12", "24")  # how often tickets are checked against the project's status rules


@dataclass(frozen=True)
class Settings:
    # Issues
    auto_start: bool = True  # work on new assignments by itself (webhook, polling); off: only "Run issues now"
    model: str = "auto"  # which Claude model works on an issue: rated per issue, or always this one
    ask_questions: bool = True  # ask the reporter, on the ticket, what only they can say (expected behaviour)
    ask_developer: bool = True  # ask the developer a costly technical decision the code can't settle
    regression_tests: bool = True  # a bug fix comes with a test that fails without it
    continue_reopened: bool = True  # work again on an issue sent back after its PRs
    # Jira
    jira_status: bool = True  # move the ticket: In Progress, In Review, back on failure
    jira_comments: bool = True  # root cause or plan, PR links, failures, questions
    jira_fix_version: bool = True  # bugs get the highest unreleased version
    release_moves: bool = False  # move complete tickets by the project's status rules (Ready for Release -> Done)
    release_hours: str = "8"  # every this many hours
    # Pull requests
    publish: str = "pr"
    slack_review_request: bool = True  # ask the repos' reviewers in the project's Slack channel
    fix_review_comments: bool = True  # address reviewers' comments on its PRs
    fix_ci: bool = True  # fix failing CI checks on its PRs
    # Reviews of colleagues' PRs
    review_for_me: bool = True  # review a PR when mentioned with it in the project's Slack channel
    review_approve: bool = True  # approve one that needs no changes; off: comment only
    no_review_repos: tuple[str, ...] = ()  # names of the project's repos whose PRs are not reviewed for them

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str | None) -> "Settings":
        """Stored settings; unknown or malformed values fall back to the defaults, and settings added
        since they were saved get theirs."""
        try:
            stored = json.loads(text or "{}")
        except ValueError:
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        default = cls()
        values = {}
        for f in fields(cls):
            value = stored.get(f.name, getattr(default, f.name))
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                value = tuple(value)  # JSON has no tuples
            if not isinstance(value, type(getattr(default, f.name))) or not valid(f.name, value):
                value = getattr(default, f.name)
            values[f.name] = value
        return cls(**values)

    def reviews(self, repo_name: str) -> bool:
        """Whether a colleague's PR of this repo is reviewed when the developer is asked in Slack."""
        return self.review_for_me and repo_name not in self.no_review_repos

    @property
    def watches_prs(self) -> bool:
        return self.fix_review_comments or self.fix_ci


CHOICES = {"model": MODEL_CHOICES, "publish": PUBLISH_CHOICES, "release_hours": RELEASE_HOURS_CHOICES}


def valid(name: str, value) -> bool:
    return value in CHOICES[name] if name in CHOICES else True


def from_form(values: dict[str, str], repos: tuple[str, ...] = ()) -> Settings:
    """Settings from a form: a switch is on when its checkbox was sent; a choice must be one of
    its CHOICES. Raises ValueError for one that is not. `repos`: the project's repo names, each
    reviewed when its checkbox review:<name> was sent."""
    picked = {}
    for f in fields(Settings):
        if f.name == "no_review_repos":
            picked[f.name] = tuple(r for r in repos if values.get(f"review:{r}") != "on")
        elif f.name in CHOICES:
            value = values.get(f.name, getattr(Settings(), f.name))
            if value not in CHOICES[f.name]:
                raise ValueError(f"{value!r} is not a choice for {f.name}")
            picked[f.name] = value
        else:
            picked[f.name] = values.get(f.name) == "on"
    return Settings(**picked)
