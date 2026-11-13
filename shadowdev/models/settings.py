"""What ShadowDev does for a developer in one project. Every automation can be switched off, so
the same server can do everything for one project and only, say, reviews for another."""
import json
import re
from dataclasses import asdict, dataclass, fields
from fnmatch import fnmatchcase

MODEL_CHOICES = ("auto", "haiku", "sonnet", "default")  # auto: rated per issue
PUBLISH_CHOICES = ("pr", "draft", "branch")  # a PR, a draft PR, or only the pushed branch
RELEASE_HOURS_CHOICES = ("4", "8", "12", "24")  # how often tickets are checked against the project's status rules
# Branch names or patterns (release/*), separated by commas: what a PR's branches are matched against.
BRANCH_PATTERNS_RE = re.compile(r"[\w./*?\-]+(\s*,\s*[\w./*?\-]+)*|")
# Jira status names, separated by commas.
STATUSES_RE = re.compile(r"[\w .&'()/\-]+(\s*,\s*[\w .&'()/\-]+)*|")
# The text settings: what each must look like, and what to say when it does not.
TEXT_FIELDS = {
    "approve_from": (BRANCH_PATTERNS_RE, "is not a list of branch names or patterns like release/*, separated by commas"),
    "approve_into": (BRANCH_PATTERNS_RE, "is not a list of branch names or patterns like release/*, separated by commas"),
    "auto_statuses": (STATUSES_RE, "is not a list of Jira statuses, separated by commas"),
}
MAX_BRANCH_PATTERNS = 200  # characters, of any text setting


@dataclass(frozen=True)
class Settings:
    # Issues
    auto_start: bool = True  # work on new assignments by itself (webhook, polling); off: only "Run issues now"
    # What an issue must be like to start by itself: in one of these statuses (empty: any), and, with
    # auto_sprint, in a sprint that is active (started and not completed).
    auto_statuses: str = "To Do, Backlog, Ready for Pickup, Reopened"
    auto_sprint: bool = True
    model: str = "auto"  # which Claude model works on an issue: rated per issue, or always this one
    ask_questions: bool = True  # ask the reporter, on the ticket, what only they can say (expected behaviour)
    ask_developer: bool = True  # ask the developer a costly technical decision the code can't settle
    regression_tests: bool = True  # a bug fix comes with a test that fails without it
    continue_reopened: bool = True  # work again on an issue sent back after its PRs
    switch_claude: bool = False  # at the Claude account's usage limit, go on with another of the developer's
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
    # A PR from a branch matching approve_from into one matching approve_into (e.g. release/* into
    # main: its changes were reviewed on their way in) is approved without a review. Both empty: off.
    approve_from: str = ""
    approve_into: str = ""
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

    def start_problem(self, fields: dict) -> str | None:
        """Why an issue (its Jira fields, with its sprint field) does not start by itself, or None."""
        allowed = {s.strip().lower() for s in self.auto_statuses.split(",") if s.strip()}
        status = str((fields.get("status") or {}).get("name") or "")
        if allowed and status.lower() not in allowed:
            return f"its status {status or '(none)'} is not one that starts by itself"
        if self.auto_sprint:
            found = sprints(fields)
            if not found:
                return "it is in no sprint"
            if not any(str(s.get("state")).lower() == "active" for s in found):
                return "its sprint has not started, or has ended"
        return None

    def approves_unreviewed(self, head: str, base: str) -> bool:
        """Whether a colleague's PR from branch `head` into `base` is approved without a review."""
        def matches(patterns: str, branch: str) -> bool:
            return any(fnmatchcase(branch, p) for p in branch_patterns(patterns))

        return self.review_approve and matches(self.approve_from, head) and matches(self.approve_into, base)

    @property
    def approves_some_unreviewed(self) -> bool:
        return self.review_approve and bool(self.approve_from and self.approve_into)

    @property
    def watches_prs(self) -> bool:
        return self.fix_review_comments or self.fix_ci


CHOICES = {"model": MODEL_CHOICES, "publish": PUBLISH_CHOICES, "release_hours": RELEASE_HOURS_CHOICES}


def sprints(fields: dict) -> list[dict]:
    """The sprints an issue is in. Jira keeps them in a custom field whose ID differs per site: a
    list of sprints, each with its state (future, active, closed) and its board."""
    for name, value in fields.items():
        if name.startswith("customfield_") and isinstance(value, list) and value \
                and all(isinstance(s, dict) and "state" in s and "boardId" in s for s in value):
            return value
    return []


def branch_patterns(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def missing_branches(settings: Settings, branches: set[str]) -> list[str]:
    """The names and patterns of approve_from and approve_into that none of `branches` matches."""
    return [p for p in dict.fromkeys(branch_patterns(settings.approve_from) + branch_patterns(settings.approve_into))
            if not any(fnmatchcase(b, p) for b in branches)]


def valid(name: str, value) -> bool:
    if name in TEXT_FIELDS:
        return len(value) <= MAX_BRANCH_PATTERNS and bool(TEXT_FIELDS[name][0].fullmatch(value))
    return value in CHOICES[name] if name in CHOICES else True


def from_form(values: dict[str, str], repos: tuple[str, ...] = ()) -> Settings:
    """Settings from a form: a switch is on when its checkbox was sent; a choice must be one of
    its CHOICES, and branch patterns must be branch names. Raises ValueError for one that is not.
    `repos`: the project's repo names, each reviewed when its checkbox review:<name> was sent."""
    picked = {}
    for f in fields(Settings):
        if f.name == "no_review_repos":
            picked[f.name] = tuple(r for r in repos if values.get(f"review:{r}") != "on")
        elif f.name in TEXT_FIELDS:
            value = " ".join(values.get(f.name, "").split())
            if not valid(f.name, value):
                raise ValueError(f"{value[:60]!r} {TEXT_FIELDS[f.name][1]}")
            picked[f.name] = value
        elif f.name in CHOICES:
            value = values.get(f.name, getattr(Settings(), f.name))
            if value not in CHOICES[f.name]:
                raise ValueError(f"{value!r} is not a choice for {f.name}")
            picked[f.name] = value
        else:
            picked[f.name] = values.get(f.name) == "on"
    return Settings(**picked)
