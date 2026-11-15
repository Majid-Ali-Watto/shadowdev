"""Moving finished tickets along the Jira workflow once they are complete, e.g. Ready for Release ->
Done when the ticket has its fix version, parent, estimate and logged time (and, for a bug, its root
cause), or In Progress -> PR Review once a comment links its PR. A project's owner sets its rules
(none by default: workflows differ);
each developer switches the moves on for themselves (Settings.release_moves), and they act only on tickets assigned to that developer."""
import json
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .project import Project

# (name, label): what a ticket must have filled before a rule moves it.
CHECKS = (
    ("root_cause", "root cause"),  # bugs only
    ("fix_version", "fix version"),
    ("parent", "parent"),
    ("original_estimate", "original estimate"),
    ("time_spent", "logged time"),
    ("pr_link", "PR link"),  # a comment links to a pull request of one of the project's repos
)
LABELS = dict(CHECKS)
ALIASES = {**{label.lower(): name for name, label in CHECKS}, **{name: name for name, _ in CHECKS},
           "time tracking": "time_spent", "time spent": "time_spent", "estimate": "original_estimate",
           "parent ticket": "parent", "fix versions": "fix_version", "pull request": "pr_link",
           "pr": "pr_link", "ticket comment has pr link": "pr_link", "comment has pr link": "pr_link"}
_PR_URL_RE = re.compile(r"github\.com/([A-Za-z0-9-]+/[A-Za-z0-9_.-]+)/pull/[0-9]+")
MAX_RULES = 8
MAX_STATUS_CHARS = 60


@dataclass(frozen=True)
class StatusRule:
    from_status: str
    to_status: str
    checks: tuple[str, ...] = ()  # names of CHECKS; none: moved as it is

    def __str__(self) -> str:
        checks = ", ".join(LABELS[c] for c in self.checks)
        return f"{self.from_status} -> {self.to_status}" + (f": {checks}" if checks else "")


DEFAULT_RULES: tuple[StatusRule, ...] = ()  # workflows differ: a project's owner writes its own


def parse_rules(text: str) -> tuple[StatusRule, ...]:
    """Rules as typed, one per line: `From -> To: check, check` (no checks: moved as it is). Raises
    ValueError with a message for the developer."""
    rules = []
    for line in (raw.strip() for raw in text.replace("→", "->").splitlines()):
        if not line:
            continue
        source, arrow, rest = line.partition("->")
        target, _, checks = rest.partition(":")
        source, target = " ".join(source.split()), " ".join(target.split())
        if not arrow or not source or not target:
            raise ValueError(f"{line!r}: write a rule as From status -> To status: checks")
        for status in (source, target):
            if len(status) > MAX_STATUS_CHARS or any(c in status for c in '"\\'):
                raise ValueError(f"{status!r} is not a Jira status name")
        names = []
        for word in (w.strip().lower() for w in checks.split(",") if w.strip()):
            if " ".join(word.split()) not in ALIASES:
                raise ValueError(f"{word!r} is not a check; the checks are {', '.join(LABELS.values())}")
            names.append(ALIASES[" ".join(word.split())])
        rules.append(StatusRule(source, target, tuple(dict.fromkeys(names))))
    if len(rules) > MAX_RULES:
        raise ValueError(f"at most {MAX_RULES} rules")
    if len({r.from_status.lower() for r in rules}) != len(rules):
        raise ValueError("one rule per status to move from")
    return tuple(rules)


def rules_text(rules: tuple[StatusRule, ...]) -> str:
    return "\n".join(str(r) for r in rules)


def rules_to_json(rules: tuple[StatusRule, ...]) -> str:
    return json.dumps([asdict(r) for r in rules])


def rules_from_json(text: str | None) -> tuple[StatusRule, ...]:
    """Stored rules; never set (NULL): the defaults."""
    if text is None:
        return DEFAULT_RULES
    return tuple(StatusRule(r["from_status"], r["to_status"], tuple(c for c in r["checks"] if c in LABELS))
                 for r in json.loads(text))


def fields_needed(root_cause_field: str | None) -> list[str]:
    """The Jira fields a ticket is searched with to check it."""
    return ["issuetype", "status", "fixVersions", "parent", "timeoriginalestimate", "timespent", "comment",
            *([root_cause_field] if root_cause_field else [])]


def missing(rule: StatusRule, fields: dict, project: "Project") -> list[str]:
    """Labels of the rule's checks the ticket does not pass. Root cause: for an issue of one of the
    project's bug types, its root cause field, or, for a project without one, a comment that gives
    it. PR link: a comment linking to a pull request of one of the project's repos."""
    root_cause_field = project.root_cause_field
    is_bug = project.issue_kind(fields) == "bug"
    comments = json.dumps(fields.get("comment") or {})
    ours = {r.github.lower() for r in project.repos}
    passed = {
        "root_cause": not is_bug or (filled(fields.get(root_cause_field)) if root_cause_field
                                     else "root cause" in comments.lower()),
        "fix_version": bool(fields.get("fixVersions")),
        "parent": bool(fields.get("parent")),
        "original_estimate": _positive(fields.get("timeoriginalestimate")),
        "time_spent": _positive(fields.get("timespent")),
        "pr_link": any(m.lower() in ours for m in _PR_URL_RE.findall(comments)),
    }
    return [LABELS[c] for c in rule.checks if not passed[c]]


def filled(value) -> bool:
    """Whether a Jira field has a value: text that is not blank (also as an Atlassian document), a
    chosen option, or a non-empty list."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(filled(v) for v in value)
    if isinstance(value, dict):
        if value.get("type") and ("content" in value or "text" in value):  # an Atlassian document (node)
            return filled(value.get("text")) or filled(value.get("content"))
        return bool(value)
    return value is not None and value is not False


def _positive(value) -> bool:
    return type(value) in (int, float) and value > 0


@dataclass(frozen=True)
class ReleaseReport:
    """The last check of a developer's tickets in a project, shown on the project's card."""
    at: float
    moved: tuple[str, ...] = ()  # "MAR-1 to Done"
    held: tuple[str, ...] = ()  # "MAR-2 (In QA): no fix version, parent"
    failed: tuple[str, ...] = ()  # what could not be done, e.g. a transition Jira refused

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str | None) -> "ReleaseReport | None":
        try:
            stored = json.loads(text or "null")
            return cls(float(stored["at"]), *(tuple(str(s) for s in stored[k]) for k in ("moved", "held", "failed")))
        except (ValueError, TypeError, KeyError):
            return None
