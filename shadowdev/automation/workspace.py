"""What a run's folders and the Claude session working in them have in common, for the pipeline (issues)
and for reviews of PRs: the run folders (workspace, scratch, git dirs), their deletion, the model a
session escalates through when it cannot do a change, and the self-heal loop that tests a change and
feeds failures back to Claude.

A run's folders: the workspace (the clones, which Claude edits), the scratch folder (baseline and
regression worktrees, test reports, the scratch index) and the git dirs of the clones, beside the
workspace (see gitops.gitdirs_of). All are deleted when the run ends, however it ends."""
import logging
import os
import shutil
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from ..config import Config
from ..models.project import Project
from . import gitops
from .agent import EDIT_TOOLS, MODELS, ClaudeSession, forget_claude_sessions
from .cancel import CancelToken
from .prompts import CHANGE_SCHEMA, ask_usable, retry_prompt, summary_check
from .timing import Timings
from .verify import Checkout, changed, check

log = logging.getLogger("shadowdev")


def workspace_name(project: Project, branch: str) -> str:
    """Per project, so the same key on two Jira sites (MAR-1 at work and at home) never collides."""
    return f"{project.id}-{branch}"


def delete_tree(path: Path) -> None:
    """Delete a run's folder. The setup and test commands ran code Claude wrote in it, which may
    have left folders without write or read permission; those are opened up first, so that
    nothing of the run stays behind."""
    if not path.exists():
        return
    for root, dirs, _ in os.walk(path):  # top-down: each folder is opened up before it is entered
        for name in [".", *dirs]:
            folder = os.path.join(root, name)
            if not os.path.islink(folder):
                try:
                    os.chmod(folder, 0o700)
                except OSError:
                    pass
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        log.error("%s could not be deleted completely; remove it by hand", path)


def remove_code(workspace: Path, scratch: Path, gits: Path, claude_dirs: list[Path]) -> None:
    """Delete everything a run left of its code: its clones, scratch, git dirs, and Claude's transcripts."""
    delete_tree(workspace)
    delete_tree(scratch)
    delete_tree(gits)
    for claude_dir in claude_dirs:
        forget_claude_sessions(claude_dir, workspace)


@contextmanager
def run_folders(cfg: Config, project: Project, name: str, claude_dirs: list[Path], keep_change: bool = False):
    """The folders of one run, as (workspace, scratch, gits). The run's code lives only in the workspace
    (Claude's) and the git dirs (beside it), and scratch (baselines, reports), side by side so Claude
    can't see scratch and `git add -A` never picks it up. All, and Claude's transcript (in `claude_dirs`:
    the config dirs of the Claude accounts the run may work with), are deleted when the run ends, however
    it ends; `keep_change` (a dry run) keeps the workspace and its git dirs, for inspection."""
    workspace = cfg.worktree_root / workspace_name(project, name)
    scratch = cfg.worktree_root / f"{workspace.name}.shadowdev"
    gits = gitops.gitdirs_of(workspace)
    remove_code(workspace, scratch, gits, claude_dirs)  # left by a crashed earlier run
    cfg.worktree_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg.worktree_root.chmod(0o700)  # other local users must not read or write live code
    workspace.mkdir(mode=0o700)
    try:
        yield workspace, scratch, gits
    finally:
        if keep_change:
            delete_tree(scratch)
            for claude_dir in claude_dirs:
                forget_claude_sessions(claude_dir, workspace)
        else:
            remove_code(workspace, scratch, gits, claude_dirs)
            log.info("%s: code removed from the server", name)


def model_name(model: str | None, effort: str | None) -> str:
    return (model or "default") + (f" ({effort})" if effort else "")


def step_up(claude: ClaudeSession) -> bool:
    """Give the session the next stronger model (it keeps its conversation). False when it
    already has the strongest: the Claude account's default."""
    tiers = list(MODELS.values())  # weakest first
    at = tiers.index((claude.model, claude.effort)) if (claude.model, claude.effort) in tiers else len(tiers) - 1
    if at == len(tiers) - 1:
        return False
    claude.model, claude.effort = tiers[at + 1]
    return True


def escalating(claude: ClaudeSession, on_model: Callable[[str], None], label: str) -> Callable[[], bool]:
    """Reports the session's model to `on_model`, and returns stronger(): what the model could not
    do, the next stronger one tries. False when there is none."""
    models = [model_name(claude.model, claude.effort)]
    on_model(models[0])

    def stronger() -> bool:
        if not step_up(claude):
            return False
        models.append(model_name(claude.model, claude.effort))
        on_model(" → ".join(models))
        log.info("%s: %s could not do it; continuing with %s", label, models[-2], models[-1])
        return True

    return stronger


def heal(claude: ClaudeSession, checkouts: list[Checkout], scratch: Path, cfg: Config, label: str, timings: Timings,
         cancel: CancelToken, summary: str, stronger: Callable[[], bool], redo: str | None = None
         ) -> tuple[list[Checkout], str, list[str]]:
    """Test every repo Claude changed, and feed new failures back to it (self-heal), up to
    cfg.max_fix_attempts test runs. `redo`: what to ask the next stronger model when nothing was
    changed. Returns (the changed checkouts: none when nothing was changed; Claude's last summary;
    the failures left: none when the tests pass)."""
    for attempt in range(1, cfg.max_fix_attempts + 1):
        to_ship = changed(checkouts)
        while not to_ship and redo and stronger():
            with timings.step("Claude change (stronger model)"):
                summary = ask_usable(claude, redo, EDIT_TOOLS, CHANGE_SCHEMA, summary_check, "summary")["summary"]
            to_ship = changed(checkouts)
        if not to_ship:
            return [], summary, []
        failures = []
        for c in to_ship:
            # The first check of a repo includes its setup and the base branch baseline (unless cached).
            with timings.step(f"test {c.repo.name} (attempt {attempt})"):
                passed, output = check(c, scratch, cfg)
            log.info("%s: %s tests %s (attempt %d/%d)", label, c.repo.name,
                     "passed" if passed else "failed", attempt, cfg.max_fix_attempts)
            if not passed:
                failures.append(output)
        cancel.check()
        if not failures or attempt == cfg.max_fix_attempts:
            return to_ship, summary, failures
        stronger()
        log.info("%s: Claude fixing the test failures", label)
        with timings.step(f"Claude fix (attempt {attempt})"):
            summary = ask_usable(claude, retry_prompt(failures), EDIT_TOOLS, CHANGE_SCHEMA, summary_check,
                                 "summary")["summary"]
