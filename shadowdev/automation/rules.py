"""Pure decision logic: which issues to handle, which release branch, which fix version."""
import re

from ..models.issue import PR_COMMENT_HEAD, QUESTIONS_HEAD, asked, by_run, comments_of

# Issue keys become branch names and directory names, so they must be strictly validated.
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-[0-9]+\Z")
ISSUE_KEYS_RE = re.compile(r"[A-Z][A-Z0-9_]+-[0-9]+")  # each key in a run's branch name, MAR-1_MAR-2
MAX_KEYS = 50  # issues in one request, ranges expanded
_KEY = r"[A-Z][A-Z0-9_]+-[0-9]+"
# A run's branch: its keys joined by "_", three or more consecutive ones as a range: MAR-1-to-MAR-10_MAR-14.
BRANCH_SEGMENT_RE = re.compile(rf"({_KEY})(?:-to-({_KEY}))?")
RUN_BRANCH_RE = re.compile(rf"{_KEY}(?:-to-{_KEY})?(?:_{_KEY}(?:-to-{_KEY})?)*")
# A range typed by a developer: MAR-1 to MAR-10, MAR-1..MAR-10, MAR-1 - MAR-10, MAR-1 to 10.
TYPED_RANGE_RE = re.compile(r"\b([A-Z][A-Z0-9_]+)-([0-9]+)\s*(?:\bTO\b|\.\.|[–—]|\s-\s)\s*(?:([A-Z][A-Z0-9_]+)-)?([0-9]+)\b")


def _number(key: str) -> tuple[str, int]:
    project, _, number = key.rpartition("-")
    return project, int(number)


def key_range(start: str, end: str) -> list[str]:
    """MAR-1 .. MAR-4 -> [MAR-1, MAR-2, MAR-3, MAR-4]. Raises ValueError for one across Jira
    projects, backwards, or of more than MAX_KEYS issues."""
    (project, a), (other, b) = _number(start), _number(end)
    if project != other:
        raise ValueError(f"{start} to {end}: a range stays in one Jira project")
    if a > b:
        raise ValueError(f"{start} to {end}: the range ends before it starts")
    if b - a + 1 > MAX_KEYS:
        raise ValueError(f"{start} to {end}: at most {MAX_KEYS} issues at a time")
    return [f"{project}-{n}" for n in range(a, b + 1)]


# Everything after it is left out; not a Jira project's key that happens to be the word (NOT-12).
EXCLUDE_WORD_RE = re.compile(r"\b(?:EXCEPT|SKIP|WITHOUT|NOT)\b(?!-[0-9])")
# A key or range marked to leave out: !MAR-3, -MAR-3, !MAR-3 to MAR-5 (a "-" with a space after it is a range's).
EXCLUDE_MARK_RE = re.compile(rf"(?:^|(?<=[\s,;]))[!-]((?:{TYPED_RANGE_RE.pattern})|{_KEY})")


def _expand(text: str) -> tuple[list[str], set[str], set[str]]:
    """(the keys in `text` with its ranges expanded, in order and once each; those of ranges; those
    typed by themselves)."""
    ranged = []

    def expand(match) -> str:
        project, start, other, end = match.groups()
        keys = key_range(f"{project}-{start}", f"{other or project}-{end}")
        ranged.extend(keys)
        return " " + " ".join(keys) + " "

    words = lambda t: [w for w in re.split(r"[\s,;]+", t.strip()) if w]
    keys = list(dict.fromkeys(words(TYPED_RANGE_RE.sub(expand, text))))
    bad = [k for k in keys if not ISSUE_KEY_RE.match(k)]
    if bad:
        raise ValueError(f"Not an issue key: {', '.join(bad)}")
    return keys, set(ranged), set(words(TYPED_RANGE_RE.sub(" ", text)))


def parse_keys(text: str) -> tuple[list[str], set[str]]:
    """The issue keys a developer typed, separated by spaces, commas or semicolons, with ranges
    (MAR-1 to MAR-10) expanded, and without the ones they leave out: after "except" (or "skip",
    "without", "not"), or marked with "!" or "-" (MAR-1 to MAR-10 except MAR-3, MAR-7; or
    MAR-1 to MAR-10, !MAR-3). Returns (the keys, in order and once each; those only in a range, not
    typed themselves). Raises ValueError for a bad range, a word that is not a key, or leaving out
    a key that is not among them."""
    text = text.upper()
    marked = [m.group(1) for m in EXCLUDE_MARK_RE.finditer(text)]
    text = EXCLUDE_MARK_RE.sub(" ", text)
    word = EXCLUDE_WORD_RE.search(text)
    wanted, left_out = (text[:word.start()], text[word.end():]) if word else (text, "")
    keys, ranged, alone = _expand(wanted)
    excluded = set(_expand(" ".join([left_out, *marked]))[0])
    unknown = sorted(excluded - set(keys), key=_number)
    if unknown:
        raise ValueError(f"{', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} left out, but not among the issues")
    keys = [k for k in keys if k not in excluded]
    if len(keys) > MAX_KEYS:
        raise ValueError(f"At most {MAX_KEYS} issues at a time")
    return keys, ranged - alone - excluded


def compact_keys(keys: list[str], joiner: str = "_", to: str = "-to-") -> str:
    """The keys in order, three or more consecutive ones of a Jira project as a range: as a branch
    name MAR-1-to-MAR-10_MAR-14, or for people (joiner ", ", to " to ") MAR-1 to MAR-10, MAR-14."""
    ordered = sorted(dict.fromkeys(keys), key=_number)
    parts, i = [], 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and _number(ordered[j + 1]) == (_number(ordered[i])[0], _number(ordered[j])[1] + 1):
            j += 1
        parts += [f"{ordered[i]}{to}{ordered[j]}"] if j - i >= 2 else ordered[i:j + 1]
        i = j + 1
    return joiner.join(parts)


def branch_keys(branch: str) -> list[str] | None:
    """The issue keys a run's branch covers (MAR-1, MAR-1_MAR-2, MAR-1-to-MAR-10_MAR-14); None for
    any other branch. Jira project keys may contain underscores themselves (MY_APP-1), so the branch
    is read key by key, not split on "_"."""
    if not RUN_BRANCH_RE.fullmatch(branch):
        return None
    keys = []
    for start, end in BRANCH_SEGMENT_RE.findall(branch):
        try:
            keys += key_range(start, end) if end else [start]
        except ValueError:
            return None
    return keys

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


FRONTEND_WORDS = {"fe", "frontend"}
BACKEND_WORDS = {"be", "backend", "service", "services", "api"}


def repo_label(name: str) -> str:
    """How a repo is named on a ticket: FE or BE for a frontend or backend repo (fe, market-place-fe,
    services, backend…), else its own name."""
    words = set(re.split(r"[^a-z0-9]+", name.lower()))
    if words & FRONTEND_WORDS and not words & BACKEND_WORDS:
        return "FE"
    if words & BACKEND_WORDS and not words & FRONTEND_WORDS:
        return "BE"
    return name


def link_text(url: str) -> str:
    """PR #12 for a pull request; for a pushed branch whose PR is to be opened by hand, a link to do so."""
    return f"PR #{url.rstrip('/').rsplit('/', 1)[-1]}" if "/pull/" in url else "open the PR"


def pr_links_comment(prs: list[tuple[str, str, str]], together_with: list[str]) -> str:
    """The Jira comment (wiki markup) listing a run's PRs as a table: (repo name, base branch, PR URL,
    or the URL to open it by hand) each, and the other issues fixed in the same change."""
    rows = "\n".join(f"|*{repo_label(repo)}*|{base}|[{link_text(url)}|{url}]|" for repo, base, url in prs)
    together = f"\n\nFixed together with {compact_keys(together_with, ', ', ' to ')}." if together_with else ""
    return f"{PR_COMMENT_HEAD}\n{rows}{together}"


MAX_QUESTION_ROUNDS = 2  # times a run may ask about one issue before it must work with what it has
MAX_QUESTIONS = 5
MAX_QUESTION_CHARS = 300
ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9:_-]{1,128}")


def questions_comment(questions: list[str], reporter: str | None) -> str:
    """The Jira comment (wiki markup) asking the issue's reporter, as the developer, what the work needs."""
    who = f"[~accountid:{reporter}] " if reporter and ACCOUNT_ID_RE.fullmatch(reporter) else ""
    asked_ = "\n".join(f"# {q}" for q in questions)
    return f"{who}{QUESTIONS_HEAD}:\n{asked_}\nA reply here is enough; I'll pick it up from there."


def clean_questions(questions) -> list[str]:
    """Claude's questions, each on one line and of a sane length; non-strings and empty ones dropped."""
    found = [" ".join(q.split())[:MAX_QUESTION_CHARS] for q in questions or [] if isinstance(q, str)]
    return [q for q in found if q][:MAX_QUESTIONS]


def awaiting_answer(fields: dict, me: str | None) -> bool:
    """A run asked questions on the issue as `me` (the developer's Atlassian account), and nobody has
    commented since."""
    comments = comments_of(fields)
    return bool(comments) and asked(comments[-1], me)


def questions_asked(fields: dict, me: str | None) -> int:
    return sum(1 for c in comments_of(fields) if asked(c, me))


def reopened(fields: dict, review_status: str, me: str | None) -> bool:
    """The issue was sent back after the PRs a run opened as `me`: it is neither in review nor done,
    and someone commented after the comment listing the PRs (saying what is still wrong)."""
    status = fields.get("status") or {}
    if (status.get("statusCategory") or {}).get("key") == "done" or (status.get("name") or "").lower() == review_status.lower():
        return False
    comments = comments_of(fields)
    listed = [i for i, c in enumerate(comments) if by_run(c, me) and c.text.startswith(PR_COMMENT_HEAD)]
    return bool(listed) and any(c.body.strip() and not by_run(c, me) for c in comments[listed[-1] + 1:])




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
