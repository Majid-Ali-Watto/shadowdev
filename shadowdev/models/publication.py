"""What a run publishes once its branch is pushed: its PRs, opened or updated, with the Slack review
request and the PR links on its tickets that follow. Outside its developer's working hours in the
project these involve other people, so the run leaves them for those hours (Settings.work_starts)."""
import json
from dataclasses import asdict, dataclass

KINDS = ("new", "update", "branch")  # a PR to open, an open PR the push updated, a branch to open one from by hand


@dataclass(frozen=True)
class Change:
    """One repo's pushed change."""
    repo: str  # Repo.name
    base: str  # the branch its PR goes into
    kind: str  # KINDS
    text: str = ""  # a new PR's body, or the comment on the open PR the push updated
    pr: int = 0  # that open PR
    url: str = ""  # its PR, or, for "branch", where to open one


@dataclass(frozen=True)
class Publication:
    keys: tuple[str, ...]  # its issues
    branch: str
    title: str
    changes: tuple[Change, ...]
    again: bool = False  # a PR was open before: its reviewers are asked again

    @property
    def involves_others(self) -> bool:
        """A PR is opened or commented on: something for reviewers, not only the developer."""
        return any(c.kind != "branch" for c in self.changes)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "Publication":
        data = json.loads(text)
        return cls(tuple(data["keys"]), data["branch"], data["title"],
                   tuple(Change(**c) for c in data["changes"]), bool(data.get("again")))
