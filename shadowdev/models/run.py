"""Which of a developer's runs Recent runs shows: a search, filters and a page, and the page found."""
from dataclasses import dataclass, field, replace
from urllib.parse import urlencode

PAGE_SIZE = 20
MAX_SEARCH = 100  # characters of a search; more is cut off
ACTIVE = ("queued", "running", "stopping")  # a run in one of these can't be deleted

# The status filter: its value, label, and the statuses it shows.
STATUSES = {
    "active": ("Queued or running", ACTIVE),
    "question": ("Asking you", ("question",)),
    "waiting": ("Waiting for the reporter", ("waiting",)),
    "done": ("Done", ("done",)),
    "failed": ("Failed", ("failed",)),
    "stopped": ("Stopped", ("stopped",)),
    "skipped": ("Skipped", ("skipped",)),
}
KINDS = {"issue": "Jira issues", "pr": "Pull requests"}  # a run's issue_key is "MAR-1" or "acme/fe#12"


@dataclass(frozen=True)
class RunFilter:
    text: str = ""     # found in the issue key, the message or the developer's note
    status: str = ""   # a key of STATUSES, or "" for all
    project: str = ""  # a project's name as the run keeps it, or "" for all
    kind: str = ""     # a key of KINDS, or "" for both
    page: int = 1

    @classmethod
    def of(cls, text: str = "", status: str = "", project: str = "", kind: str = "", page: str = "") -> "RunFilter":
        """From what the address says; anything unknown is left out rather than refused."""
        return cls(text.strip()[:MAX_SEARCH], status if status in STATUSES else "", project.strip()[:200],
                   kind if kind in KINDS else "", int(page) if page.isdigit() and 0 < int(page) < 10_000 else 1)

    def query(self, **changes) -> str:
        """This filter (with `changes`) as the query of the Runs tab's address, without the defaults."""
        f = replace(self, **changes)
        return urlencode({k: v for k, v in (("tab", "runs"), ("q", f.text), ("status", f.status),
                                            ("project", f.project), ("kind", f.kind), ("page", f.page)) if v and v != 1})


@dataclass(frozen=True)
class RunPage:
    rows: list
    total: int  # how many runs the filter finds
    filter: RunFilter  # its page is within the pages there are
    projects: list[str] = field(default_factory=list)  # the projects of all the developer's runs, to filter by

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // PAGE_SIZE))
