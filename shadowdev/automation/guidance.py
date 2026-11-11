"""What a repository tells AI tools about itself: CLAUDE.md, AGENTS.md, Copilot, Cursor, Windsurf
and Cline rules, path-scoped instructions, and Claude's agents, commands and skills. Every project
keeps its own, so ShadowDev doesn't assume any: it finds them in each clone, and Claude is told
which to read and follow (prompts.guidance_text), whatever the project.

Only files committed to the repository count (`git ls-files`): no local settings, no dependencies.
They are read as text, never run: a repository's Claude settings and hooks (.claude/settings*.json)
are not among them, and Claude Code doesn't load them either, since it runs in the workspace that
holds the repositories, not in one of them."""
import logging
import re
import shutil
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from . import gitops

log = logging.getLogger("shadowdev")

# Instructions for any work in the repository, Claude's own first.
ALWAYS = ("CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md", ".github/copilot-instructions.md", "GEMINI.md", ".cursorrules",
          ".windsurfrules", ".clinerules", "CONVENTIONS.md", ".specify/memory/constitution.md")
# Instructions for some files only (their frontmatter says which); a nested CLAUDE.md or AGENTS.md
# is for its folder.
SCOPED = (".claude/rules/*.md", ".claude/rules/*/*.md", ".github/instructions/*.instructions.md", ".cursor/rules/*.mdc",
          ".cursor/rules/*/*.mdc", ".windsurf/rules/*.md", ".clinerules/*.md", ".kiro/steering/*.md")
NESTED = ("CLAUDE.md", "AGENTS.md")
# Guides for particular work: (pattern, kind).
GUIDES = ((".claude/agents/*.md", "agent"), (".github/agents/*.agent.md", "agent"), (".claude/commands/*.md", "command"),
          (".claude/commands/*/*.md", "command"), (".claude/skills/*/SKILL.md", "skill"),
          (".github/prompts/*.prompt.md", "prompt"))
MAX_FILE_BYTES = 1024**2  # a bigger file is not an instruction file
MAX_ENTRIES = 60  # per repository
MAX_NOTE_CHARS = 200
COPY_DIR = "_guidance"  # a review's instructions, copied from the base branch, beside the clones


@dataclass(frozen=True)
class Entry:
    source: str  # its path in the repository
    path: str  # where Claude reads it, relative to the workspace
    kind: str  # always, scoped, agent, command, skill, prompt
    note: str = ""  # what it is for: its description, or which files it applies to


def matches(path: str, pattern: str) -> bool:
    """`path` matches the glob `pattern` folder by folder: "*" never crosses a "/"."""
    parts, wanted = path.split("/"), pattern.split("/")
    return len(parts) == len(wanted) and all(fnmatchcase(p, w) for p, w in zip(parts, wanted))


def frontmatter(text: str) -> dict[str, str]:
    """The top-level `key: value` lines of a leading --- block, quotes removed; lists kept as text."""
    if not text.startswith("---"):
        return {}
    block = text[3:].split("\n---", 1)[0]
    found = {}
    for line in block.splitlines():
        match = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
        if match:
            found[match.group(1)] = match.group(2).strip().strip("'\"")
    return found


def _note(text: str, *keys: str) -> str:
    meta = frontmatter(text)
    note = next((meta[k] for k in keys if meta.get(k)), "")
    if not note:  # no description: its first heading
        note = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")), "")
    note = " ".join(note.split())
    return note if len(note) <= MAX_NOTE_CHARS else note[:MAX_NOTE_CHARS - 1] + "…"


def _kind(source: str) -> str | None:
    if source in ALWAYS:
        return "always"
    if any(matches(source, p) for p in SCOPED):
        return "scoped"
    if source.rsplit("/", 1)[-1] in NESTED and "/" in source and not source.startswith("."):
        return "scoped"
    return next((kind for pattern, kind in GUIDES if matches(source, pattern)), None)


def find(root: Path, prefix: str) -> list[Entry]:
    """The instruction files committed in the clone at `root`, for Claude to read at `prefix/<path>`
    (the clone's folder in the workspace): the ones for all work first, in ALWAYS order."""
    entries = []
    for source in gitops.git(root, "ls-files", "-z").split("\0"):
        kind = _kind(source) if source else None
        file = root / source
        if not kind or file.is_symlink() or not file.is_file() or file.stat().st_size > MAX_FILE_BYTES:
            continue
        text = file.read_text(errors="replace")
        if kind == "scoped":
            meta = frontmatter(text)
            if meta.get("alwaysApply", "").lower() == "true":
                kind, note = "always", ""
            elif "/" in source and source.rsplit("/", 1)[-1] in NESTED and not source.startswith("."):
                note = f"for files under {source.rsplit('/', 1)[0]}/"
            else:
                scope = meta.get("applyTo") or meta.get("globs") or meta.get("paths") or ""
                note = f"for files matching {scope}" if scope else _note(text, "description")
        else:
            note = "" if kind == "always" else _note(text, "description", "name")
        entries.append(Entry(source, f"{prefix}/{source}", kind, note))
    order = {name: i for i, name in enumerate(ALWAYS)}
    entries.sort(key=lambda e: (e.kind != "always", order.get(e.source, len(order)), e.kind, e.source))
    if len(entries) > MAX_ENTRIES:
        log.info("%s: %d instruction files; only the first %d are listed for Claude", prefix, len(entries), MAX_ENTRIES)
    return entries[:MAX_ENTRIES]


def copy_into(entries: list[Entry], root: Path, workspace: Path) -> None:
    """Copy the instruction files of the clone at `root` (outside the workspace) to where `entries`
    say Claude reads them: for a review, the base branch's, so the PR under review can't change them."""
    for e in entries:
        target = workspace / e.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / e.source, target)
