"""The pipeline for one or more Jira issues (bugs, tasks/stories) of one project, fixed together
on one branch, acting as the developer they are assigned to. A single issue is a group of one.

1. per repo: a shallow clone of the developer's base branch (or the latest release branch, or
   the default branch), with their token, on a new branch named after the issue key(s), all
   inside one workspace (worktrees/<project id>-<BRANCH>/<repo>); each issue moves to "In Progress"
2. a small model rates how hard the issues are, which picks the model for the rest of the run;
   Claude analyses the workspace (read-only) -> per issue, a root cause (bug) or plan (task)
   to Jira, and for bugs the fix version; the repos to change
3. Claude edits and creates files -> each changed repo is set up and tested in a sandbox ->
   new failures fed back (self-heal)
4. per changed repo: commit as the developer, push with their GitHub token, PR into the base
   branch; PR links commented on each issue, which moves to "In Review"

No code stays on the server: when the run ends, however it ends, the workspace, the test
reports and Claude's transcript of the run are deleted. Only a dry run keeps its workspace.

Prompts and answer checks are in prompts.py; running and judging the tests in verify.py.
"""
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from . import gitops, rules
from .agent import (EDIT_TOOLS, MODELS, READ_ONLY_TOOLS, TRIAGE_MODEL, ClaudeSession, claude_config_dir, claude_env,
                    forget_claude_sessions)
from .cancel import NEVER, CancelToken, Cancelled
from .timing import Timings
from .prompts import (CHANGE_SCHEMA, TRIAGE_SCHEMA, analysis_problem, analysis_prompt, analysis_schema, ask_usable,
                      change_prompt, retry_prompt, summary_problem, triage_prompt)
from .verify import NOT_TESTED, Checkout, changed, check
from ..clients.github import GitHub
from ..clients.jira import Jira
from ..clients.rest import HTTPError
from ..config import Config
from ..models.issue import Issue
from ..models.project import Project, Repo
from ..models.user import Actor

log = logging.getLogger("shadowdev")

MAX_GROUP = 10  # issues fixed together on one branch


def branch_name(keys: list[str]) -> str:
    """MAR-1 for one issue; MAR-1_MAR-2 for issues fixed together."""
    return "_".join(keys)


def handled_by(branch: str, key: str) -> bool:
    return key in branch.split("_")


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


def comment_all(jira: Jira, issues: list[Issue], text: str) -> None:
    for issue in issues:
        jira.comment(issue.key, text)


def remove_code(workspace: Path, scratch: Path, claude_dir: Path) -> None:
    shutil.rmtree(workspace, ignore_errors=True)
    shutil.rmtree(scratch, ignore_errors=True)
    forget_claude_sessions(claude_dir, workspace)


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
            issues.append(Issue(key, fields, kind))
    return issues, skipped


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


def process_issues(keys: list[str], project: Project, actor: Actor, cfg: Config, jira: Jira, github: GitHub,
                   publish: bool = True, cancel: CancelToken = NEVER, note: str = "") -> tuple[str, str]:
    """Work on the issues together, on one branch. `note`: the developer's own instructions for
    this run, given to Claude with the issues. Returns (status, message): status "done",
    "skipped" or "failed". Raises on errors, and Cancelled when `cancel` stops the run. With
    publish=False (dry run) it stops once the tests pass: nothing is committed, pushed or
    opened, and the change stays in the workspace. Each step's time is logged, and at the end
    the outcome with the whole run's time."""
    if not 1 <= len(keys) <= MAX_GROUP:
        raise ValueError(f"between 1 and {MAX_GROUP} issues at a time")
    timings = Timings(branch_name(keys))
    outcome = "failed"
    try:
        outcome, message = _process(keys, project, actor, cfg, jira, github, publish, cancel, timings, note)
        return outcome, message
    except Cancelled:
        outcome = "stopped"
        raise
    finally:
        timings.report(outcome)


def _process(keys: list[str], project: Project, actor: Actor, cfg: Config, jira: Jira, github: GitHub,
             publish: bool, cancel: CancelToken, timings: Timings, note: str) -> tuple[str, str]:
    with timings.step("Jira and GitHub checks"):
        issues, skipped = select_issues(keys, project, actor, jira)

        # Issues already worked on (alone or in a group) have a branch on GitHub. One request per
        # repo, at the same time.
        token = actor.github.token
        with ThreadPoolExecutor(max_workers=min(8, len(project.repos))) as pool:
            listed = pool.map(lambda r: gitops.remote_branches(r.github, token), project.repos)
            remote = dict(zip((r.name for r in project.repos), listed))
        for issue in list(issues):
            existing = next((b for names in remote.values() for b in names if handled_by(b, issue.key)), None)
            if existing:
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
            targets.append((repo, base))
        with ThreadPoolExecutor(max_workers=min(8, len(project.repos))) as pool:
            no_push = [r.github for r, ok in zip(project.repos, pool.map(lambda r: github.can_push(r.github), project.repos))
                       if not ok]
        if no_push:
            raise RuntimeError(f"your GitHub account {actor.github.login} cannot push to {', '.join(no_push)}")
    cancel.check()
    with timings.step("Jira status to start"):
        for issue in issues:
            move_issue(jira, issue.key, project.start_statuses)

    # The run's code lives only in the workspace (Claude's) and scratch (baselines, reports),
    # side by side so Claude can't see scratch and `git add -A` never picks it up. Both, and
    # Claude's transcript, are deleted when the run ends, however it ends.
    workspace = cfg.worktree_root / workspace_name(project, branch)
    scratch = cfg.worktree_root / f"{workspace.name}.shadowdev"
    claude_dir = claude_config_dir(cfg, actor.claude)
    remove_code(workspace, scratch, claude_dir)  # left by a crashed earlier run
    cfg.worktree_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg.worktree_root.chmod(0o700)  # other local users must not read or write live code
    workspace.mkdir(mode=0o700)
    try:
        return _work(issues, skipped, branch, targets, workspace, scratch, claude_dir, project, actor, cfg, jira, github,
                     publish, cancel, timings, note)
    finally:
        if publish:
            remove_code(workspace, scratch, claude_dir)
            log.info("%s: code removed from the server", label)
        else:  # a dry run keeps only the change, for inspection
            shutil.rmtree(scratch, ignore_errors=True)
            forget_claude_sessions(claude_dir, workspace)


def tested(c: Checkout) -> str:
    if c.baseline is None:
        return f"**Not tested:** {NOT_TESTED}. Review it carefully, and rely on CI."
    return (f"Tests run by ShadowDev: no new failures compared to `{c.base}` ({len(c.baseline.failures)} already "
            "failing there). The tests ran this change's own code, so they are a check, not a guarantee: "
            "review the change, and rely on CI.")


def _work(issues: list[Issue], skipped: list[str], branch: str, targets: list[tuple[Repo, str]], workspace: Path,
          scratch: Path, claude_dir: Path, project: Project, actor: Actor, cfg: Config, jira: Jira, github: GitHub,
          publish: bool, cancel: CancelToken, timings: Timings, note: str) -> tuple[str, str]:
    label = branch
    token = actor.github.token

    # 1. Per repo: a shallow clone of the base branch, on the run's branch.
    checkouts = []
    for repo, base in targets:
        cancel.check()
        worktree = workspace / repo.name
        with timings.step(f"clone {repo.name}"):
            commit = gitops.clone(repo.github, base, worktree, token)
            gitops.git(worktree, "checkout", "--quiet", "-b", branch)
        checkouts.append(Checkout(label, repo, base, commit, worktree, cancel=cancel))
        log.info("%s: %s cloned at %s (%s), on branch %s", label, repo.name, base, commit[:10], branch)

    # 2. Per issue: root cause (bug) or plan (task) on the ticket; fix version for bugs.
    session = partial(ClaudeSession, workspace, claude_env(actor.claude.token, claude_dir), label=label, cfg=cfg,
                      cancel=cancel)
    with timings.step("Claude model choice"):
        model, effort = choose_model(session, issues, note, label)
    claude = session(model=model, effort=effort)
    log.info("%s: Claude analysing", label)
    keys_here = [i.key for i in issues]
    with timings.step("Claude analysis"):
        result = ask_usable(claude, analysis_prompt(issues, checkouts, note), READ_ONLY_TOOLS,
                            analysis_schema(issues, project.repos), lambda r: analysis_problem(r, keys_here), "analysis")
    analyses = {item["key"]: item["analysis"] for item in result["issues"]}
    target_repos = result["repos"]
    cancel.check()
    with timings.step("Jira analysis updates"):
        version = None
        if any(i.kind == "bug" for i in issues):
            # The highest open release, replacing what the ticket had (e.g. a wrong one from an earlier run).
            version = rules.latest_fix_version(jira.project_versions(issues[0].fields["project"]["key"]))
            if not version:
                log.warning("%s: no unreleased numbered Jira version found; fix version not set", label)
        for issue in issues:
            analysis = analyses[issue.key]
            log.info("%s: %s (repos: %s): %s", issue.key, "root cause" if issue.kind == "bug" else "plan",
                     ", ".join(target_repos), analysis)
            if issue.kind == "bug" and project.root_cause_field:
                jira.set_field(issue.key, project.root_cause_field, analysis)
                log.info("%s: Jira %s (root cause) set", issue.key, project.root_cause_field)
            else:
                heading = "Root cause" if issue.kind == "bug" else "Implementation plan"
                jira.comment(issue.key, f"*{heading} (ShadowDev):*\n{analysis}")
                log.info("%s: Jira comment with the %s posted", issue.key, heading.lower())
            if issue.kind == "bug" and version:
                jira.set_fix_versions(issue.key, [version])
                log.info("%s: Jira fix version set to %s", issue.key, version)

    # 3. Change, test every changed repo, self-heal.
    log.info("%s: Claude making the change in %s", label, ", ".join(target_repos))
    with timings.step("Claude change"):
        summary = ask_usable(claude, change_prompt(issues, target_repos), EDIT_TOOLS, CHANGE_SCHEMA,
                             summary_problem, "summary")["summary"]
    for attempt in range(1, cfg.max_fix_attempts + 1):
        to_ship = changed(checkouts)
        if not to_ship:
            comment_all(jira, issues, "ShadowDev analysed this issue but made no code changes; needs a human look.")
            return "failed", "Claude made no changes"
        failures = []
        for c in to_ship:
            # The first check of a repo includes its setup and the base branch baseline (unless cached).
            with timings.step(f"test {c.repo.name} (attempt {attempt})"):
                passed, output = check(c, scratch, cfg)
            log.info("%s: %s tests %s (attempt %d/%d)", label, c.repo.name,
                     "passed" if passed else "failed", attempt, cfg.max_fix_attempts)
            if not passed:
                failures.append(output)
        if not failures:
            break
        cancel.check()
        if attempt == cfg.max_fix_attempts:
            comment_all(
                jira, issues,
                f"ShadowDev could not produce a change that passes the tests after {attempt} attempts, "
                f"so nothing was pushed. Its last attempt: {summary}\n"
                f"{{noformat}}{chr(10).join(failures)[-2000:]}{{noformat}}",
            )
            return "failed", f"tests still failing after {attempt} attempts; nothing pushed"
        log.info("%s: Claude fixing the test failures", label)
        with timings.step(f"Claude fix (attempt {attempt})"):
            summary = ask_usable(claude, retry_prompt(failures), EDIT_TOOLS, CHANGE_SCHEMA, summary_problem,
                                 "summary")["summary"]
    log.info("%s: change: %s", label, summary)

    if not publish:
        changed_repos = ", ".join(c.repo.name for c in to_ship)
        return "done", f"dry run: tests pass; change in {changed_repos} kept at {workspace}, nothing pushed"

    # 4. Per changed repo: commit and push as the developer, PR into the base branch.
    if len(issues) == 1:
        title = f"{issues[0].key}: {issues[0].summary or ('bug fix' if issues[0].kind == 'bug' else 'task')}"
    else:
        title = f"{', '.join(keys_here)}: {len(issues)} issues fixed together"
    per_issue = "\n\n".join(
        f"### {i.key}: {i.summary}\n**{'Root cause' if i.kind == 'bug' else 'Plan'}:** {analyses[i.key]}" for i in issues)
    commit_details = "\n\n".join(
        f"{i.key}: {i.summary}\n{'Root cause' if i.kind == 'bug' else 'Plan'}: {analyses[i.key]}" for i in issues)
    links = ", ".join(f"[{i.key}]({project.site_url}/browse/{i.key})" for i in issues)
    prs = []
    for c in to_ship:
        cancel.check()  # last chance: nothing is pushed for a stopped run
        with timings.step(f"commit, push and PR {c.repo.name}"):
            name, email = actor.github.git_name, actor.github.git_email
            gitops.commit_all(c.worktree, f"{title}\n\n{summary}\n\n{commit_details}", name, email)
            log.info("%s: %s committed as %s <%s>; pushing branch %s to %s", label, c.repo.name, name, email, branch,
                     c.repo.github)
            gitops.push(c.worktree, c.repo.github, branch, token)
            pr_url = github.create_pr(
                c.repo.github,
                base=c.base,
                head=branch,
                title=title,
                body=f"Resolves {links}\n\n{per_issue}\n\n## Change\n{summary}\n\n{tested(c)}\n\n_Opened by ShadowDev._",
            )
        prs.append(f"{c.repo.name} into {c.base}: {pr_url}")
        log.info("%s: %s PR %s", label, c.repo.name, pr_url)
    cancel.check()
    with timings.step("Jira PR links and review status"):
        for issue in issues:
            others = [k for k in keys_here if k != issue.key]
            together = f"\nFixed together with {', '.join(others)}." if others else ""
            jira.comment(issue.key, "ShadowDev opened:\n" + "\n".join(f"* {p}" for p in prs) + together)
            log.info("%s: Jira comment with the PR links posted", issue.key)
            move_issue(jira, issue.key, project.review_statuses)
    return "done", "; ".join([*prs, *(f"skipped {s}" for s in skipped)])
