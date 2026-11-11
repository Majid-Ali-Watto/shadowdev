"""A developer's own timing and limits: how often ShadowDev looks for their work, and how much a run
may take. Set on the Settings tab; a developer who never changed them gets the defaults.

Every time is in minutes, and may have decimals: 0.5 is 30 seconds. Counts are whole numbers."""
import json
import re
from dataclasses import asdict, dataclass, fields

# (minimum, maximum) of each time, in minutes. Polls have a floor so Jira, GitHub and Slack rate limits hold.
MINUTES = {
    "jira_poll_minutes": (1, 24 * 60),
    "github_poll_minutes": (0.25, 60),
    "slack_poll_minutes": (0.25, 60),
    "review_quiet_minutes": (0, 60),
    "claude_timeout_minutes": (1, 240),
    "command_timeout_minutes": (1, 240),
}
# (minimum, maximum) of each count.
COUNTS = {
    "max_parallel_runs": (1, 20),
    "max_fix_attempts": (1, 10),
}
BOUNDS = {**MINUTES, **COUNTS}
MINUTES_RE = re.compile(r"\d{1,4}(\.\d{1,2})?")  # up to two decimals: 0.25 is 15 seconds


@dataclass(frozen=True)
class Limits:
    jira_polling: bool = False  # ask Jira for new assignments, for a machine its webhooks can't reach (a laptop)
    jira_poll_minutes: float = 15  # how often; a project's first poll looks back this far
    github_poll_minutes: float = 1  # how often its PRs are read on GitHub: review comments, CI
    slack_poll_minutes: float = 1  # how often the projects' Slack channels are read: review requests
    review_quiet_minutes: float = 2  # review comments are acted on once no new one came for this long
    claude_timeout_minutes: float = 30  # one claude call is killed after this
    command_timeout_minutes: float = 30  # one setup or test command is killed after this
    max_parallel_runs: int = 5  # of this developer's runs at the same time; more wait their turn
    max_fix_attempts: int = 3  # test runs of a change before the run gives up

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str | None) -> "Limits":
        """Stored limits; a missing, malformed or out-of-range value is the default."""
        try:
            stored = json.loads(text or "{}")
        except ValueError:
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        values = {}
        for f in fields(cls):
            value, default = stored.get(f.name), getattr(cls, f.name)
            values[f.name] = value if _valid(f.name, value) else default
        return cls(**values)


def _valid(name: str, value) -> bool:
    if name in MINUTES:
        ok = type(value) in (int, float)  # not bool
    elif name in COUNTS:
        ok = type(value) is int
    else:
        ok = type(value) is bool
    return ok and (name not in BOUNDS or BOUNDS[name][0] <= value <= BOUNDS[name][1])


LABELS = {
    "jira_poll_minutes": "Jira interval",
    "github_poll_minutes": "GitHub interval",
    "slack_poll_minutes": "Slack interval",
    "review_quiet_minutes": "Wait after the last review comment",
    "claude_timeout_minutes": "Claude timeout",
    "command_timeout_minutes": "Setup and test command timeout",
    "max_parallel_runs": "Runs at the same time",
    "max_fix_attempts": "Fix attempts",
}


def number(value: float) -> str:
    """A limit as the page shows it: 15, 0.5, 0.25."""
    return f"{value:g}"


def from_form(values: dict[str, str]) -> Limits:
    """Limits from the Settings form: the switch is on when its checkbox was sent; a time is minutes,
    with up to two decimals, and a count a whole number, each within its BOUNDS. Raises ValueError,
    with a message for the developer."""
    picked = {"jira_polling": values.get("jira_polling") == "on"}
    for name, (low, high) in MINUTES.items():
        text = (values.get(name) or "").strip()
        if not MINUTES_RE.fullmatch(text) or not low <= float(text) <= high:
            raise ValueError(f"{LABELS[name]} must be minutes from {number(low)} to {number(high)}, "
                             "like 0.5 for 30 seconds.")
        picked[name] = float(text)
    for name, (low, high) in COUNTS.items():
        text = (values.get(name) or "").strip()
        if not text.isdigit() or not low <= int(text) <= high:
            raise ValueError(f"{LABELS[name]} must be a whole number from {low} to {high}.")
        picked[name] = int(text)
    return Limits(**picked)
