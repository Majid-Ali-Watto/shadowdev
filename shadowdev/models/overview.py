"""What the Overview tab shows a developer: what needs them, what runs, their PRs, their Claude
usage and what waits for their working hours. Built by services/overview.py."""
import sqlite3
from dataclasses import dataclass, field

WEEK = 7 * 24 * 3600  # the "last 7 days" of the tiles, and how far back "needs you" looks

# How a PR the developer opened stands (pull_requests.review, automation/review.review_state):
# what is said of it, and whether it needs someone ("warn"), is done ("ok"), or waits ("off").
PR_STATES = {
    "conflict": ("conflicts with its base branch", "warn"),
    "ci": ("CI failing", "warn"),
    "changes": ("changes requested", "warn"),
    "comments": ("{} to answer", "warn"),  # "1 comment", "2 comments"
    "approved": ("approved by {}", "ok"),
    "waiting": ("waiting for review", "off"),
}


def pr_state(review: str) -> tuple[str, str]:
    """(words, kind) for a PR's stored review state; one not read yet is waiting."""
    name, _, count = review.partition(":")
    words, kind = PR_STATES.get(name, ("not checked yet", "off"))
    if name == "comments":
        count = f"{count} comment{'' if count == '1' else 's'}"
    return words.format(count or "0"), kind


@dataclass(frozen=True)
class Held:
    """What waits for the developer's working hours in one project."""
    project: str
    starts: float  # when the hours begin; until then it waits
    branches: tuple[str, ...] = ()  # runs whose PRs wait (their branches: the issue keys)
    slack: int = 0  # replies to reviewers in Slack
    comments: int = 0  # comments on Jira tickets and GitHub PRs


@dataclass(frozen=True)
class ClaudeUsage:
    """One of the developer's Claude accounts: its usage windows as last reported."""
    label: str
    windows: dict[str, tuple[float, float]]  # automation.limits.Usage.windows
    held: bool  # at a limit: no run starts on it


@dataclass(frozen=True)
class Overview:
    needs: list[sqlite3.Row] = field(default_factory=list)  # runs asking questions or failed (RunRepository.needing)
    active: list[sqlite3.Row] = field(default_factory=list)  # RunRepository.active
    at_once: int = 5  # the developer's Runs at the same time
    totals: dict[str, tuple[int, float, float]] = field(default_factory=dict)  # RunRepository.totals, last 7 days
    prs: list[sqlite3.Row] = field(default_factory=list)  # ReviewRepository.prs_of
    claude: list[ClaudeUsage] = field(default_factory=list)
    held: list[Held] = field(default_factory=list)

    def count(self, *statuses: str) -> int:
        return sum(self.totals.get(s, (0, 0, 0))[0] for s in statuses)

    @property
    def cost(self) -> float:
        return sum(t[1] or 0 for t in self.totals.values())

    @property
    def seconds(self) -> float:
        return sum(t[2] or 0 for t in self.totals.values())
