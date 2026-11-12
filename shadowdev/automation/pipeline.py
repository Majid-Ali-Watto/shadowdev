"""The pipeline for one or more Jira issues (bugs, tasks/stories) of one project, fixed together
on one branch, acting as the developer they are assigned to. A single issue is a group of one.

1. per repo: a shallow clone of the developer's base branch (or the latest release branch, or
   the default branch), with their token, on a new branch named after the issue key(s), all
   inside one workspace (worktrees/<project id>-<BRANCH>/<repo>); each issue moves to "In Progress"
2. a small model rates how hard the issues are, which picks the model for the rest of the run;
   Claude analyses the workspace (read-only) -> per issue, a root cause (bug) or plan (task)
   to Jira, and for bugs the fix version; the repos to change. An issue too unclear to work on
   gets questions instead, and waits for the answer: on the ticket for its reporter (what is
   wanted), or for the developer (a costly technical decision). What Claude decided itself is
   listed in the PR as its assumptions
3. Claude edits and creates files -> each changed repo is set up and tested in a sandbox ->
   new failures fed back (self-heal). For bugs, its regression tests must fail on the base branch
4. per changed repo: commit as the developer, push with their GitHub token, PR into the base
   branch; PR links commented on each issue, which moves to "In Review"

An issue sent back after its PRs (reopened, with a comment saying why) continues on its branch,
and an open PR is updated rather than a new one opened.

No code stays on the server: when the run ends, however it ends, the workspace, the test
reports and Claude's transcript of the run are deleted. Only a dry run keeps its workspace.

Prompts and answer checks are in prompts.py; running and judging the tests in verify.py.
"""
import logging
import os
import re
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial
from pathlib import Path

from ..clients.github import GitHub
from ..clients.jira import Jira
from ..clients.rest import HTTPError
from ..clients.slack import Slack, link, link_issues
from ..clients.slack import escape as slack_escape
from ..config import Config
from ..models.issue import NO_CHANGE, PLAN_HEAD, PRS_UNOPENED, ROOT_CAUSE_HEAD, TESTS_FAILING, Issue
from ..models.project import Project, Repo
from ..models.user import Actor
from . import gitops, guidance, rules
from .agent import (
    EDIT_TOOLS,
    FIXED_MODELS,
    MODELS,
    READ_ONLY_TOOLS,
    TRIAGE_MODEL,
    ClaudeSession,
    claude_config_dir,
    claude_env,
    forget_claude_sessions,
)
from .cancel import NEVER, Cancelled, CancelToken
from .prompts import (
    CHANGE_SCHEMA,
    REGRESSION_SCHEMA,
    TRIAGE_SCHEMA,
    analysis_problem,
    analysis_prompt,
    analysis_schema,
    ask_usable,
    change_prompt,
    regression_problem,
    regression_prompt,
    regression_retry_prompt,
    reopened_text,
    retry_prompt,
    summary_problem,
    triage_prompt,
)
from .timing import Timings
from .verify import Checkout, changed, changed_paths, check, reproduces

log = logging.getLogger("shadowdev")

MAX_GROUP = 10  # issues fixed together on one branch
# Files attached to the issues are put here in the workspace for Claude to read: beside the
# clones (a repo's folder never starts with "_"), so they are never committed.
ATTACHMENTS_DIR = "_attachments"
ATTACHMENT_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".txt", ".log", ".md", ".json", ".csv", ".xml",
                    ".yaml", ".yml", ".har"}
MAX_ATTACHMENTS = 10  # per issue: the latest ones
MAX_ATTACHMENT_BYTES = 10 * 1024**2
MAX_TITLE_CHARS = 200  # GitHub refuses PR titles over 256 characters; a Jira summary can be 255
REGRESSION_ATTEMPTS = 2  # a bug's regression test that passes without the fix goes back to Claude once


def branch_name(keys: list[str]) -> str:
    """MAR-1 for one issue; for issues fixed together, their keys, with three or more consecutive
    ones as a range, so ten issues don't make a branch name of ten keys: MAR-1_MAR-2,
    MAR-1-to-MAR-10, MAR-1-to-MAR-4_MAR-7. The name says exactly which issues it covers."""
    return rules.compact_keys(keys)


def handled_by(branch: str, key: str) -> bool:
    """The branch is a run's (see branch_name; also the older MAR-1_MAR-2_MAR-3) and covers the issue."""
    return key in (rules.branch_keys(branch) or ())


# Jira and workspace helpers

def move_issue(jira: Jira, key: str, path: tuple[str, ...]) -> None:
    """Walk the issue's Jira status along `path`. A workflow that does not allow it is
    logged, not fatal: the code work matters more than the status."""
    try:
        status, moved = jira.move_along(key, list(path))
    except HTTPError as e:
        log.warning("%s: could not move the issue to %s: %s", key, path[-1], e)
        return
    if moved:
        log.info("%s: Jira status → %s", key, " → ".join(moved))
    if status.lower() != path[-1].lower():
        log.warning("%s: issue is in %r; no transition from there towards %r", key, status, path[-1])


def move_back(jira: Jira, issue: Issue, project: Project) -> None:
    """A run that failed without pushing anything: the issue goes back to the status it had
    before the run, back along the start statuses, as far as the workflow allows."""
    if issue.status and issue.status.lower() != project.start_statuses[-1].lower():
        move_issue(jira, issue.key, (*reversed(project.start_statuses[:-1]), issue.status))


def fetch_attachments(jira: Jira, issues: list[Issue], workspace: Path, label: str) -> None:
    """Put the issues' attached screenshots, logs and documents in the workspace, for Claude to
    read. They are untrusted, like the issue text: only known file types of a sane size are
    fetched, under names made safe, and they are data, never run. One that can't be fetched
    is skipped."""
    for issue in issues:
        wanted = []
        for item in issue.fields.get("attachment") or []:
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(str(item.get("filename") or "")).name).lstrip(".")
            size = item.get("size")
            if (str(item.get("id") or "").isdigit() and Path(name).suffix.lower() in ATTACHMENT_TYPES
                    and isinstance(size, int) and 0 < size <= MAX_ATTACHMENT_BYTES):
                wanted.append((str(item["id"]), name[-100:]))
        folder = workspace / ATTACHMENTS_DIR / issue.key
        for attachment_id, name in wanted[-MAX_ATTACHMENTS:]:
            try:
                data = jira.attachment(attachment_id, MAX_ATTACHMENT_BYTES)
            except HTTPError as e:
                log.warning("%s: attachment %s of %s not fetched: %s", label, name, issue.key, e)
                continue
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = folder / f"{attachment_id}-{name}"
            path.write_bytes(data)
            path.chmod(0o600)
            issue.attachments.append(str(path.relative_to(workspace)))
        if issue.attachments:
            log.info("%s: %d attachment(s) of %s for Claude to read", label, len(issue.attachments), issue.key)


def ask_for_review(actor: Actor, project: Project, title: str, opened: list[tuple[Repo, str]], label: str,
                   thread: tuple[str, str] | None = None, again: bool = False, done: str = "") -> tuple[str, str] | None:
    """Ask in the project's Slack channel, as the developer, for review of the PRs, mentioning
    each repo's reviewers (`thread`: as a reply to that earlier request): "Kindly review these PRs:
    <title>", the PRs, "Thanks". `done`: what changed since they last looked, said first. Returns
    (channel, timestamp) of the message; None without Slack, or when it could not be posted: not fatal."""
    channel = thread[0] if thread else project.slack_channel
    if not (actor.slack and channel and opened):
        return None
    ask = f"Kindly review {'this PR' if len(opened) == 1 else 'these PRs'}{' again' if again else ''}"
    # The title starts with its issue keys ("MAR-12: summary", "MAR-1 to MAR-4: …"): those become links.
    keys = rules.ISSUE_KEYS_RE.findall(title.partition(": ")[0])
    ask = f"{done} {ask}".lstrip() + (f": {link_issues(slack_escape(title), project.site_url, keys)}" if title else ".")
    lines = [f"• {repo.name}: {link(url)} {' '.join(f'<@{member}>' for member in repo.reviewers)}".rstrip() for repo, url in opened]
    try:
        ts = Slack(actor.slack.token).post(channel, "\n".join([ask, *lines, "Thanks"]), thread and thread[1])
    except HTTPError as e:
        log.warning("%s: could not ask for review in Slack: %s", label, e)
        return None
    log.info("%s: review asked for in Slack", label)
    return thread or (channel, ts)


def notify_developer(actor: Actor, text: str, label: str, site_url: str = "", keys=()) -> None:
    """Tell the developer, in Slack, in their own direct messages (if they connected Slack), with
    the issue `keys` in it linked to their issues on `site_url`. Not fatal: the run's message on
    their page says the same."""
    if not actor.slack:
        return
    try:
        Slack(actor.slack.token).post(actor.slack.user_id, link_issues(slack_escape(text), site_url, keys) if site_url
                                      else slack_escape(text))
        log.info("%s: %s told in Slack", label, actor.display_name)
    except HTTPError as e:
        log.warning("%s: could not tell the developer in Slack: %s", label, e)


def compare_url(owner_repo: str, base: str, branch: str) -> str:
    """Where to open a PR from `branch` into `base` by hand."""
    return f"https://github.com/{owner_repo}/compare/{base}...{branch}?expand=1"


def comment_all(jira: Jira, issues: list[Issue], text: str) -> None:
    for issue in issues:
        jira.comment(issue.key, text)


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


def remove_code(workspace: Path, scratch: Path, claude_dir: Path) -> None:
    delete_tree(workspace)
    delete_tree(scratch)
    forget_claude_sessions(claude_dir, workspace)


@contextmanager
def run_folders(cfg: Config, project: Project, name: str, claude_dir: Path, keep_change: bool = False):
    """The folders of one run, as (workspace, scratch). The run's code lives only in the workspace
    (Claude's) and scratch (baselines, reports), side by side so Claude can't see scratch and
    `git add -A` never picks it up. Both, and Claude's transcript, are deleted when the run ends,
    however it ends; `keep_change` (a dry run) keeps only the workspace, for inspection."""
    workspace = cfg.worktree_root / workspace_name(project, name)
    scratch = cfg.worktree_root / f"{workspace.name}.shadowdev"
    remove_code(workspace, scratch, claude_dir)  # left by a crashed earlier run
    cfg.worktree_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg.worktree_root.chmod(0o700)  # other local users must not read or write live code
    workspace.mkdir(mode=0o700)
    try:
        yield workspace, scratch
    finally:
        if keep_change:
            delete_tree(scratch)
            forget_claude_sessions(claude_dir, workspace)
        else:
            remove_code(workspace, scratch, claude_dir)
            log.info("%s: code removed from the server", name)


def workspace_name(project: Project, branch: str) -> str:
    """Per project, so the same key on two Jira sites (MAR-1 at work and at home) never collides."""
    return f"{project.id}-{branch}"


def select_issues(keys: list[str], project: Project, actor: Actor, jira: Jira) -> tuple[list[Issue], list[str]]:
    """The issues this run may work on, fetched from Jira itself rather than trusting the
    webhook payload or the form; and why the others are skipped."""
    issues, skipped = [], []
    for key in keys:
        fields = jira.get_issue(key)["fields"]
        kind = rules.issue_kind(fields)
        if (fields.get("project") or {}).get("key") not in project.jira_projects:
            skipped.append(f"{key}: not in a Jira project of {project.name}")
        elif rules.assignee(fields) != actor.jira_account_id:
            skipped.append(f"{key}: not assigned to {actor.display_name}")
        elif not rules.wanted(kind, actor.mode):
            skipped.append(f"{key}: issue type {(fields.get('issuetype') or {}).get('name')!r} is not automated for you")
        else:
            issues.append(Issue(key, fields, kind, me=actor.jira_account_id))
    return issues, skipped


def earlier_work(project: Project, branch: str, remote: dict[str, list[str]], github: GitHub, token: str,
                 publish: bool, label: str) -> dict[str, dict | None]:
    """For an issue sent back after the PRs of an earlier run, what to continue: per repo that has the run's
    branch, its open PR (the run pushes to it), or None (the run continues the branch and opens a
    PR). A branch whose PR was merged, with nothing pushed since, is not continued: its work is in
    the base branch, so the branch is deleted (unless this is a dry run) and the repo starts afresh."""
    kept = {}
    for repo in project.repos:
        if branch not in remote[repo.name]:
            continue
        prs = github.prs_for_branch(repo.github, branch)
        open_pr = next((p for p in prs if p["state"] == "open"), None)
        merged = next((p for p in prs if p.get("merged_at")), None)
        if open_pr:
            kept[repo.name] = open_pr
        elif merged and gitops.remote_branch_sha(repo.github, token, branch) == merged["head"]["sha"]:
            if publish:
                github.delete_branch(repo.github, branch)
            log.info("%s: %s branch %s was merged in %s; starting afresh from the base branch", label, repo.name,
                     branch, merged["html_url"])
        else:
            kept[repo.name] = None
    return kept


def regression_tests(checkouts: list[Checkout], named: list[str], scratch: Path) -> list[tuple[Checkout, list[str]]]:
    """The test files Claude named (repo/path) per repo, kept only when the change adds or modifies them."""
    found = []
    for c in checkouts:
        prefix = c.repo.name + "/"
        wanted = {t.removeprefix("./")[len(prefix):] for t in named if t.removeprefix("./").startswith(prefix)}
        tests = sorted(wanted & changed_paths(c, scratch))
        if tests:
            found.append((c, tests))
    return found


def prove_regression(claude, checkouts: list[Checkout], to_ship: list[Checkout], scratch: Path, cfg: Config, label: str,
                     timings: Timings, cancel: CancelToken, stronger: Callable[[], bool]
                     ) -> tuple[list[Checkout], list[str], str]:
    """A bug fix must come with a test that reproduces the bug: Claude names its regression tests,
    and they are run on the base branch without the fix, where they must fail (they pass with it:
    the tests just passed). Tests that pass there too go back to Claude, up to REGRESSION_ATTEMPTS
    times; after that the change still ships, and says so. Returns (the changed checkouts, the
    test failures left after Claude changed the tests, a line for the PR saying what the test proves)."""
    for attempt in range(1, REGRESSION_ATTEMPTS + 1):
        testable = [c for c in to_ship if c.repo.test]
        if not testable:
            return to_ship, [], ""
        with timings.step("Claude regression tests"):
            answer = ask_usable(claude, regression_prompt(), READ_ONLY_TOOLS, REGRESSION_SCHEMA, regression_problem, "answer")
        named = regression_tests(testable, answer["tests"], scratch)
        if not named:
            reason = answer["reason"] or "none of the files named is a test this change adds or modifies"
            log.info("%s: no regression test: %s", label, reason)
            return to_ship, [], f"**No regression test:** {reason}"
        failing = []
        for c, tests in named:
            with timings.step(f"regression tests {c.repo.name} without the fix (attempt {attempt})"):
                failing += reproduces(c, tests, scratch, cfg)
        cancel.check()
        files = [f"{c.repo.name}/{t}" for c, tests in named for t in tests]
        if failing:
            log.info("%s: the regression tests fail without the fix: %s", label, "; ".join(failing))
            return to_ship, [], ("**Regression test:** fails without the fix and passes with it: "
                                 + "; ".join(f"`{t}`" for t in failing[:10]))
        log.info("%s: the regression tests %s pass without the fix too (attempt %d/%d)", label, ", ".join(files), attempt,
                 REGRESSION_ATTEMPTS)
        if attempt == REGRESSION_ATTEMPTS:
            return to_ship, [], (f"**Regression test:** {', '.join(f'`{f}`' for f in files)} passes without the fix "
                                 "too, so it does not prove the fix; review it.")
        with timings.step("Claude regression test again"):
            summary = ask_usable(claude, regression_retry_prompt(files), EDIT_TOOLS, CHANGE_SCHEMA, summary_problem,
                                 "summary")["summary"]
        to_ship, _, failures = heal(claude, checkouts, scratch, cfg, label, timings, cancel, summary, stronger)
        if failures or not to_ship:
            return to_ship, failures, ""
    return to_ship, [], ""


def resolve_base(repo: Repo, chosen: str | None, branches: list[str], token: str) -> str | None:
    """The developer's base branch, else the latest release branch, else the repo's default branch."""
    return (chosen or rules.latest_release_branch([b for b in branches if b.startswith(repo.release_prefix)],
                                                  repo.release_prefix)
            or gitops.default_branch(repo.github, token))


def choose_model(session, issues: list[Issue], note: str, label: str) -> tuple[str | None, str | None]:
    """The (model, effort) to work on the issues with, from a small model's rating of their text:
    a typo does not need the model a race condition does. `session(model=...)` starts a Claude
    session of the run. A rating that fails is not fatal: the run then uses the Claude account's
    default model, as for a complex issue."""
    try:
        complexity = session(model=TRIAGE_MODEL).ask(triage_prompt(issues, note), "", TRIAGE_SCHEMA).get("complexity")
    except RuntimeError as e:
        log.warning("%s: could not rate the issue, so the default model works on it: %s", label, e)
        complexity = None
    model, effort = MODELS.get(complexity, MODELS["complex"])
    log.info("%s: rated %s: worked on by %s%s", label, complexity or "unrated",
             model or "the Claude account's default model", f" (effort {effort})" if effort else "")
    return model, effort


def model_name(model: str | None, effort: str | None) -> str:
    return (model or "default") + (f" ({effort})" if effort else "")


def step_up(claude) -> bool:
    """Give the session the next stronger model (it keeps its conversation). False when it
    already has the strongest: the Claude account's default."""
    tiers = list(MODELS.values())  # weakest first
    at = tiers.index((claude.model, claude.effort)) if (claude.model, claude.effort) in tiers else len(tiers) - 1
    if at == len(tiers) - 1:
        return False
    claude.model, claude.effort = tiers[at + 1]
    return True


def escalating(claude, on_model: Callable[[str], None], label: str) -> Callable[[], bool]:
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


def heal(claude, checkouts: list[Checkout], scratch: Path, cfg: Config, label: str, timings: Timings, cancel: CancelToken,
         summary: str, stronger: Callable[[], bool], redo: str | None = None) -> tuple[list[Checkout], str, list[str]]:
    """Test every repo Claude changed, and feed new failures back to it (self-heal), up to
    cfg.max_fix_attempts test runs. `redo`: what to ask the next stronger model when nothing was
    changed. Returns (the changed checkouts: none when nothing was changed; Claude's last summary;
    the failures left: none when the tests pass)."""
    for attempt in range(1, cfg.max_fix_attempts + 1):
        to_ship = changed(checkouts)
        while not to_ship and redo and stronger():
            with timings.step("Claude change (stronger model)"):
                summary = ask_usable(claude, redo, EDIT_TOOLS, CHANGE_SCHEMA, summary_problem, "summary")["summary"]
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
            summary = ask_usable(claude, retry_prompt(failures), EDIT_TOOLS, CHANGE_SCHEMA, summary_problem,
                                 "summary")["summary"]


def process_issues(keys: list[str], project: Project, actor: Actor, cfg: Config, jira: Jira, github: GitHub,
                   publish: bool = True, cancel: CancelToken = NEVER, note: str = "",
                   on_model: Callable[[str], None] = lambda models: None,
                   on_pr: Callable[..., None] = lambda *pr: None, asked: dict[str, list[str]] | None = None,
                   on_questions: Callable[[dict[str, list[str]]], None] = lambda questions: None) -> tuple[str, str]:
    """Work on the issues together, on one branch. `note`: the developer's own instructions for
    this run, given to Claude with the issues; `asked`: the questions an earlier run asked the
    developer, by issue key, which the note answers. `on_questions` is told the questions this run
    asks the developer, when it stops for them. `on_model` is told which model(s) work on it,
    e.g. "haiku → sonnet (medium)"; `on_pr` of each PR opened: (repo, its number, the branch, and
    (channel, timestamp) of the Slack message that asked for its review, or None). Returns
    (status, message): status "done", "skipped", "waiting" (questions asked on the ticket),
    "question" (the developer is asked) or "failed". Raises on errors, and Cancelled when
    `cancel` stops the run. With publish=False (dry run) it stops once the tests pass: nothing is committed, pushed or
    opened, and the change stays in the workspace. Each step's time is logged, and at the end
    the outcome with the whole run's time."""
    if not 1 <= len(keys) <= MAX_GROUP:
        raise ValueError(f"between 1 and {MAX_GROUP} issues at a time")
    timings = Timings(branch_name(keys))
    outcome = "failed"
    try:
        outcome, message = _process(keys, project, actor, cfg, jira, github, publish, cancel, timings, note, on_model,
                                    on_pr, asked or {}, on_questions)
        return outcome, message
    except Cancelled:
        outcome = "stopped"
        raise
    finally:
        timings.report(outcome)


def _process(keys: list[str], project: Project, actor: Actor, cfg: Config, jira: Jira, github: GitHub,
             publish: bool, cancel: CancelToken, timings: Timings, note: str, on_model, on_pr, asked,
             on_questions) -> tuple[str, str]:
    with timings.step("Jira and GitHub checks"):
        issues, skipped = select_issues(keys, project, actor, jira)
        for issue in list(issues):
            if not note and rules.awaiting_answer(issue.fields, issue.me):  # a developer's note answers for the reporter
                issues.remove(issue)
                skipped.append(f"{issue.key}: waiting for answers to the questions on the ticket (or run it from the "
                               "page with a note)")

        # Issues already worked on (alone or in a group) have a branch on GitHub. One request per
        # repo, at the same time. One issue sent back since (reopened) is worked on again.
        token = actor.github.token
        with ThreadPoolExecutor(max_workers=min(8, len(project.repos))) as pool:
            listed = pool.map(lambda r: gitops.remote_branches(r.github, token), project.repos)
            remote = dict(zip((r.name for r in project.repos), listed, strict=True))
        reopened = False
        for issue in list(issues):
            existing = next((b for names in remote.values() for b in names if handled_by(b, issue.key)), None)
            if existing and len(keys) == 1 and existing == issue.key and actor.settings.continue_reopened \
                    and rules.reopened(issue.fields, project.review_statuses[-1], issue.me):
                reopened = True
            elif existing:
                issues.remove(issue)
                skipped.append(f"{issue.key}: already handled on branch {existing}")
    if not issues:
        return "skipped", "; ".join(skipped)
    branch = branch_name([i.key for i in issues])
    label = branch
    for s in skipped:
        log.info("%s: skipping %s", label, s)
    log.info("%s: %s for %s: %s", label, project.name, actor.display_name,
             "; ".join(f"{i.key} ({i.type_name}) {i.summary}" for i in issues))
    continued: dict[str, dict | None] = {}
    if reopened:
        log.info("%s: sent back after ShadowDev's PRs; working on it again", label)
        with timings.step("earlier branches and PRs"):
            continued = earlier_work(project, branch, remote, github, token, publish, label)

    # Resolve and check every repo before creating anything.
    with timings.step("base branches and push access"):
        targets = []
        for repo in project.repos:
            base = resolve_base(repo, actor.base_branches.get(repo.name), remote[repo.name], token)
            if not base:
                raise RuntimeError(f"{repo.name}: no base branch on {repo.github} (no '{repo.release_prefix}*' "
                                   "branch and no default branch)")
            if base not in remote[repo.name]:
                raise RuntimeError(f"{repo.name}: your base branch {base} does not exist on {repo.github}")
            if continued.get(repo.name):  # an open PR keeps the base it has
                base = continued[repo.name]["base"]["ref"]
            targets.append((repo, base))
        with ThreadPoolExecutor(max_workers=min(8, len(project.repos))) as pool:
            no_push = [r.github for r, ok in zip(project.repos, pool.map(lambda r: github.can_push(r.github), project.repos), strict=True)
                       if not ok]
        if no_push:
            raise RuntimeError(f"your GitHub account {actor.github.login} cannot push to {', '.join(no_push)}")
    cancel.check()
    if actor.settings.jira_status:
        with timings.step("Jira status to start"):
            for issue in issues:
                move_issue(jira, issue.key, project.start_statuses)

    claude_dir = claude_config_dir(cfg, actor.claude)
    pushed: list[str] = []  # repos whose branch reached GitHub
    put_back = True
    with run_folders(cfg, project, branch, claude_dir, keep_change=not publish) as (workspace, scratch):
        try:
            outcome = _work(issues, skipped, branch, targets, continued, reopened, workspace, scratch, claude_dir,
                            project, actor, cfg, jira, github, publish, cancel, timings, note, on_model, on_pr, pushed,
                            asked, on_questions)
            put_back = outcome[0] in ("failed", "waiting", "question")
            return outcome
        except Cancelled:
            put_back = False  # a stopped run writes nothing more to Jira
            raise
        finally:
            if put_back and not pushed and actor.settings.jira_status:  # with a branch on GitHub, it stays in progress
                for issue in issues:
                    try:
                        move_back(jira, issue, project)
                    except Exception:
                        log.exception("%s: could not move the issue back", issue.key)


def tested(c: Checkout) -> str:
    """What the PR says about its tests, as the developer would."""
    if c.baseline is None:
        return "**Tests:** not run locally (this repo has no test command set up); relying on CI."
    already = len(c.baseline.failures)
    return (f"**Tests:** pass locally, with no new failures against `{c.base}`"
            + (f" ({already} already failing there)." if already else "."))


def _work(issues: list[Issue], skipped: list[str], branch: str, targets: list[tuple[Repo, str]],
          continued: dict[str, dict | None], reopened: bool, workspace: Path, scratch: Path, claude_dir: Path,
          project: Project, actor: Actor, cfg: Config, jira: Jira, github: GitHub, publish: bool, cancel: CancelToken,
          timings: Timings, note: str, on_model, on_pr, pushed: list[str], asked: dict[str, list[str]],
          on_questions) -> tuple[str, str]:
    """`targets`: (repo, the base branch its PR goes into). `continued`: the repos whose existing
    branch the run continues (see earlier_work); the others are cut from their base branch."""
    label = branch
    token = actor.github.token
    pr_base = {repo.name: base for repo, base in targets}

    # 1. Per repo: a shallow clone of the base branch, on the run's branch; or of the run's branch,
    # continued. The tests then compare against what the clone started from.
    checkouts = []
    for repo, base in targets:
        cancel.check()
        worktree = workspace / repo.name
        start = branch if repo.name in continued else base
        with timings.step(f"clone {repo.name}"):
            commit = gitops.clone(repo.github, start, worktree, token)
            if start != branch:
                gitops.git(worktree, "checkout", "--quiet", "-b", branch)
        checkouts.append(Checkout(label, repo, start, commit, worktree, cancel=cancel))
        log.info("%s: %s cloned at %s (%s), on branch %s", label, repo.name, start, commit[:10], branch)

    with timings.step("Jira attachments"):
        fetch_attachments(jira, issues, workspace, label)
    guides = {c.repo.name: guidance.find(c.worktree, c.repo.name) for c in checkouts}  # the projects' own AI instructions
    for name, found in guides.items():
        if found:
            log.info("%s: %s keeps %d instruction file(s) for AI tools: %s", label, name, len(found),
                     ", ".join(e.source for e in found[:8]) + (" …" if len(found) > 8 else ""))

    # 2. Per issue: root cause (bug) or plan (task) on the ticket; fix version for bugs.
    session = partial(ClaudeSession, workspace, claude_env(actor.claude.token, claude_dir), label=label, cfg=cfg,
                      cancel=cancel)
    if actor.settings.model == "auto":
        with timings.step("Claude model choice"):
            model, effort = choose_model(session, issues, note, label)
    else:  # the developer chose the model for every issue
        model, effort = FIXED_MODELS[actor.settings.model]
    claude = session(model=model, effort=effort)
    stronger = escalating(claude, on_model, label)
    log.info("%s: Claude analysing", label)
    keys_here = [i.key for i in issues]
    # Claude may ask the reporter instead of guessing, a few times per issue, on the ticket; a
    # developer's note is their answer.
    settings = actor.settings
    may_ask = (settings.ask_questions and settings.jira_comments and not note
               and all(rules.questions_asked(i.fields, i.me) < rules.MAX_QUESTION_ROUNDS for i in issues))
    may_ask_developer = settings.ask_developer and not note  # the note of the next run is their answer
    again = reopened_text(branch, sorted(continued)) if reopened else ""
    with timings.step("Claude analysis"):
        answered = {i.key: asked[i.key] for i in issues if i.key in asked} if note else {}
        result = ask_usable(claude, analysis_prompt(issues, checkouts, note, may_ask, again, guides, may_ask_developer,
                                                    answered),
                            READ_ONLY_TOOLS, analysis_schema(issues, project.repos, may_ask, may_ask_developer),
                            lambda r: analysis_problem(r, keys_here), "analysis")
    analyses = {item["key"]: item["analysis"] for item in result["issues"]}
    questions = {item["key"]: item["questions"] for item in result["issues"] if may_ask and item["questions"]}
    for_developer = {item["key"]: item["developer_questions"] for item in result["issues"]
                     if may_ask_developer and item["developer_questions"]}
    assumptions = result["assumptions"]
    target_repos = result["repos"]
    cancel.check()
    if questions:
        with timings.step("Jira questions"):
            for issue in issues:
                if issue.key in questions:
                    jira.comment(issue.key, rules.questions_comment(questions[issue.key], issue.reporter))
                    log.info("%s: asked its reporter: %s", issue.key, " | ".join(questions[issue.key]))
    if for_developer:  # technical: for the developer alone, never on the ticket
        asked = "\n".join(f"{key}: {q}" for key, qs in for_developer.items() for q in qs)
        log.info("%s: asked %s: %s", label, actor.display_name, asked.replace("\n", " | "))
        message = (f"Your decision is needed before work on {', '.join(for_developer)} goes on:\n{asked}\n"
                   "Answer in a note (a line per issue, like the questions) when you run it again from Run issues now.")
        notify_developer(actor, message, label, project.site_url, list(for_developer))
        on_questions(for_developer)
        return "question", message + ("\nIts questions for the reporter are on the ticket too." if questions else "")
    if questions:
        return "waiting", (f"waiting for answers: the reporter was asked on {', '.join(questions)} what the work "
                           "needs to know; it runs again once someone answers there")
    with timings.step("Jira analysis updates"):
        version = None
        if settings.jira_fix_version and any(i.kind == "bug" for i in issues):
            # The highest open release, replacing what the ticket had (e.g. a wrong one from an earlier run).
            version = rules.latest_fix_version(jira.project_versions(issues[0].fields["project"]["key"]))
            if not version:
                log.warning("%s: no unreleased numbered Jira version found; fix version not set", label)
        for issue in issues:
            analysis = analyses[issue.key]
            log.info("%s: %s (repos: %s): %s", issue.key, "root cause" if issue.kind == "bug" else "plan",
                     ", ".join(target_repos), analysis)
            if not settings.jira_comments:
                pass
            elif issue.kind == "bug" and project.root_cause_field:
                jira.set_field(issue.key, project.root_cause_field, analysis)
                log.info("%s: Jira %s (root cause) set", issue.key, project.root_cause_field)
            else:
                heading = ROOT_CAUSE_HEAD if issue.kind == "bug" else PLAN_HEAD
                jira.comment(issue.key, f"{heading}\n{analysis}")
                log.info("%s: Jira comment with the %s posted", issue.key, heading.strip("*:").lower())
            if issue.kind == "bug" and version:
                jira.set_fix_versions(issue.key, [version])
                log.info("%s: Jira fix version set to %s", issue.key, version)

    # 3. Change, test every changed repo, self-heal.
    log.info("%s: Claude making the change in %s", label, ", ".join(target_repos))
    with timings.step("Claude change"):
        summary = ask_usable(claude, change_prompt(issues, target_repos), EDIT_TOOLS, CHANGE_SCHEMA,
                             summary_problem, "summary")["summary"]
    to_ship, summary, failures = heal(claude, checkouts, scratch, cfg, label, timings, cancel, summary, stronger,
                                      redo=change_prompt(issues, target_repos))
    proof = ""
    if settings.regression_tests and to_ship and not failures and any(i.kind == "bug" for i in issues):
        to_ship, failures, proof = prove_regression(claude, checkouts, to_ship, scratch, cfg, label, timings, cancel,
                                                    stronger)
    if not to_ship:
        if settings.jira_comments:
            comment_all(jira, issues, NO_CHANGE)
        return "failed", "Claude made no changes"
    if failures:
        if settings.jira_comments:
            comment_all(
                jira, issues,
                f"{TESTS_FAILING} What I tried: {summary}\n"
                f"{{noformat}}{chr(10).join(failures)[-2000:]}{{noformat}}",
            )
        return "failed", f"tests still failing after {cfg.max_fix_attempts} attempts; nothing pushed"
    log.info("%s: change: %s", label, summary)

    if not publish:
        changed_repos = ", ".join(c.repo.name for c in to_ship)
        return "done", f"dry run: tests pass; change in {changed_repos} kept at {workspace}, nothing pushed"

    # 4. Per changed repo: commit and push as the developer, PR into the base branch.
    if len(issues) == 1:
        title = f"{issues[0].key}: {issues[0].summary or ('bug fix' if issues[0].kind == 'bug' else 'task')}"
        if len(title) > MAX_TITLE_CHARS:
            title = title[:MAX_TITLE_CHARS - 1].rstrip() + "…"
    else:
        title = f"{rules.compact_keys(keys_here, ', ', ' to ')}: {len(issues)} issues fixed together"
    per_issue = "\n\n".join(
        f"### {i.key}: {i.summary}\n**{'Root cause' if i.kind == 'bug' else 'Plan'}:** {analyses[i.key]}" for i in issues)
    commit_details = "\n\n".join(
        f"{i.key}: {i.summary}\n{'Root cause' if i.kind == 'bug' else 'Plan'}: {analyses[i.key]}" for i in issues)
    links = ", ".join(f"[{i.key}]({project.site_url}/browse/{i.key})" for i in issues)
    proven = f"{proof}\n\n" if proof else ""
    if assumptions:  # what Claude decided that the issue left open: for the reviewers to check
        proven += "**Assumptions** (the issue left these open):\n" + "\n".join(
            f"- {a}" for a in assumptions) + "\n\n"
    prs, unopened, opened, rows, new = [], [], [], [], []
    for c in to_ship:
        cancel.check()  # last chance: nothing is pushed for a stopped run
        base, existing = pr_base[c.repo.name], continued.get(c.repo.name)
        with timings.step(f"commit, push and PR {c.repo.name}"):
            name, email = actor.github.git_name, actor.github.git_email
            gitops.commit_all(c.worktree, f"{title}\n\n{summary}\n\n{commit_details}", name, email)
            log.info("%s: %s committed as %s <%s>; pushing branch %s to %s", label, c.repo.name, name, email, branch,
                     c.repo.github)
            gitops.push(c.worktree, c.repo.github, branch, token)
            pushed.append(c.repo.name)
            if existing:  # the issue was sent back while its PR is open: the push updated it
                pr_url = existing["html_url"]
                try:
                    github.comment(c.repo.github, existing["number"],
                                   f"Pushed more changes for {', '.join(keys_here)} after it was reopened.\n\n"
                                   f"{per_issue}\n\n## Change\n{summary}\n\n{proven}{tested(c)}")
                except HTTPError as e:
                    log.warning("%s: could not comment on %s: %s", label, pr_url, e)
            elif settings.publish == "branch":  # the developer opens the PR
                pr_url = compare_url(c.repo.github, base, branch)
                prs.append(f"{c.repo.name}: branch {branch} pushed; open its PR into {base} at {pr_url}")
                rows.append((c.repo.name, base, pr_url))
                log.info("%s: %s branch %s pushed; no PR opened, as chosen", label, c.repo.name, branch)
                continue
            else:
                try:
                    pr_url = github.create_pr(
                        c.repo.github,
                        base=base,
                        head=branch,
                        title=title,
                        body=f"Resolves {links}\n\n{per_issue}\n\n## Change\n{summary}\n\n{proven}{tested(c)}",
                        draft=settings.publish == "draft",
                    )
                except HTTPError as e:  # the change is on GitHub: say where, so the PR can be opened by hand
                    log.warning("%s: %s branch pushed, but the PR could not be opened: %s", label, c.repo.name, e)
                    unopened.append(f"{c.repo.name}: branch {branch} is pushed, but its PR into {base} could not be "
                                    f"opened (GitHub answered {e.status}); open it at "
                                    f"{compare_url(c.repo.github, base, branch)}")
                    continue
                new.append((c.repo, pr_url))
        prs.append(f"{c.repo.name} into {base}: {pr_url}{' (updated)' if existing else ''}")
        opened.append((c.repo, pr_url))
        rows.append((c.repo.name, base, pr_url))
        log.info("%s: %s PR %s%s", label, c.repo.name, pr_url, " updated" if existing else "")
    cancel.check()
    if unopened:
        # Not in review until every PR is open: the issue stays in progress, for its developer.
        with timings.step("Jira: PRs to open by hand"):
            lines = "\n".join(f"* {line}" for line in [*unopened, *(f"opened {p}" for p in prs)])
            for issue in issues:
                if settings.jira_comments:
                    try:
                        jira.comment(issue.key, f"{PRS_UNOPENED}\n" + lines)
                    except HTTPError as e:
                        log.warning("%s: could not comment the branches to open PRs for: %s", issue.key, e)
                if settings.jira_status:
                    move_issue(jira, issue.key, project.start_statuses)
        return "failed", "; ".join([*unopened, *(f"opened {p}" for p in prs)])
    asked = None
    if settings.slack_review_request and settings.publish == "pr":  # a draft is not ready for review yet
        with timings.step("Slack review request"):
            asked = ask_for_review(actor, project, title, opened, label, again=bool(continued))
    for repo, pr_url in new:  # an updated PR is watched already
        on_pr(repo, int(pr_url.rstrip("/").rsplit("/", 1)[1]), branch, asked)
    unreported = []
    with timings.step("Jira PR links and review status"):
        for issue in issues:
            others = [k for k in keys_here if k != issue.key]
            if settings.jira_comments:
                try:  # the work is pushed: a Jira error now must not make the run look failed
                    jira.comment(issue.key, rules.pr_links_comment(rows, others))
                    log.info("%s: Jira comment with the PR links posted", issue.key)
                except HTTPError as e:
                    log.warning("%s: could not comment the PR links: %s", issue.key, e)
                    unreported.append(f"{issue.key}: the PR links could not be commented on the ticket")
            if settings.jira_status and opened:  # in review once there is a PR to review
                move_issue(jira, issue.key, project.review_statuses)
    return "done", "; ".join([*prs, *unreported, *(f"skipped {s}" for s in skipped)])
