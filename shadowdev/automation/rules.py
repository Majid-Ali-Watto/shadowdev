"""Pure decision logic: which issues to handle, which release branch, which fix version."""
import re

# Issue keys become branch names and directory names, so they must be strictly validated.
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-[0-9]+\Z")

MAX_NOTE_CHARS = 2000  # a developer's note for a run they start by hand
NOTE_KEY_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]+-[0-9]+)\s*:\s*")


def split_note(note: str) -> tuple[str, dict[str, str]]:
    """A note's general part, and its part for each issue: a line starting "MAR-12:" begins that
    issue's part, which runs until the next such line. Lines before the first are general."""
    general, parts, current = [], {}, None
    for line in note.split("\n"):
        match = NOTE_KEY_RE.match(line)
        if match:
            current = parts.setdefault(match.group(1).upper(), [])
            line = line[match.end():]
        (general if current is None else current).append(line)
    return "\n".join(general).strip(), {k: "\n".join(v).strip() for k, v in parts.items()}

TASK_TYPES = {"task", "story", "sub-task", "subtask"}


def issue_kind(fields: dict) -> str | None:
    """"bug", "task" (Task, Story, Sub-task), or None for issue types ShadowDev never handles."""
    issue_type = ((fields.get("issuetype") or {}).get("name") or "").lower()
    if issue_type == "bug":
        return "bug"
    return "task" if issue_type in TASK_TYPES else None


def wanted(kind: str | None, mode: str) -> bool:
    """mode "bugs": only bugs; mode "all": bugs and tasks."""
    return kind == "bug" or (kind == "task" and mode == "all")


def assignee(fields: dict) -> str | None:
    return (fields.get("assignee") or {}).get("accountId")


RELEASE_VERSION_RE = re.compile(r"^v?\d+(\.\d+)*$")


def _version_key(name: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", name))


def latest_release_branch(branches: list[str], prefix: str) -> str | None:
    """Highest version among branches like release/1.10.0 (numeric compare, so 1.10 > 1.9).
    Only purely numeric suffixes count, so release/082-some-feature is not a release."""
    candidates = [b for b in branches if b.startswith(prefix) and RELEASE_VERSION_RE.match(b[len(prefix):])]
    return max(candidates, key=lambda b: _version_key(b[len(prefix):]), default=None)


def latest_fix_version(versions: list[dict]) -> str | None:
    """The highest-numbered unreleased, unarchived Jira version ("Rel 15" over "Rel 14" and
    "Rel 0.10"), whatever the base branch. None if there is no numbered open version."""
    numbered = [v for v in versions if not v.get("released") and not v.get("archived") and _version_key(v["name"])]
    return max(numbered, key=lambda v: _version_key(v["name"]))["name"] if numbered else None
