"""Pull requests in review, acting as the developer.

address_review: reviewers commented on a PR ShadowDev opened. Claude makes the changes they ask
for on the PR's branch, the tests run as for any change, the fix is pushed, and each thread is
answered and, when addressed, resolved. Nothing is pushed when the tests fail, and nothing is
done once the PR is approved: the reviewers accepted it with those comments.

fix_ci: the CI checks of a PR ShadowDev opened failed. Claude reads their output and fixes what
in the PR makes them fail, on the PR's branch; the tests run as for any change, and the fix is
pushed. A failure that is not the PR's (flaky, infrastructure) is left, and the PR says so.

review_pr: the developer was asked in Slack to review a colleague's PR. Claude reads the diff
and the code (read-only); its findings are posted as a review with comments, or the PR is
approved, and the Slack thread is told. A PR someone approved already, or that another reviewer
has commented on, is left alone: it is reviewed, or being reviewed, by them.

Like any run, both work in their own folders, which are deleted when they end.
"""
import logging
import re

from ..clients.github import GitHub
from ..clients.rest import HTTPError
from ..clients.slack import Slack, link
from ..config import Config
from ..models.project import Project, Repo
from ..models.user import Actor
from . import gitops, guidance
from .agent import EDIT_TOOLS, MODELS, READ_ONLY_TOOLS, ClaudeSession, claude_config_dir, claude_env
from .cancel import NEVER, CancelToken
from .pipeline import ask_for_review, escalating, heal, model_name, run_folders
from .prompts import (
    CHANGE_SCHEMA,
    REVIEW_SCHEMA,
    ask_usable,
    ci_prompt,
    fix_problem,
    fix_prompt,
    fix_schema,
    review_problem,
    review_prompt,
    summary_problem,
)
from .timing import Timings
from .verify import Checkout

log = logging.getLogger("shadowdev")

# Whose review comments are acted on: people with access to the repository, not any passer-by.
REVIEWERS = {"OWNER", "MEMBER", "COLLABORATOR"}
MAX_DIFF_CHARS = 200_000
FAILED_CHECKS = {"failure", "timed_out", "startup_failure"}  # conclusions a change can fix
FAILED_STATUSES = {"failure", "error"}
MAX_LOG_BYTES = 20 * 1024**2  # a GitHub Actions job log bigger than this is not fetched
MAX_LOG_CHARS = 12_000  # the end of each failed job's log, where the error is
MAX_CI_CHARS = 40_000  # all failed checks together
LOG_NOISE_RE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z |\x1b\[[0-9;]*[A-Za-z]", re.MULTILINE)  # timestamps, colours


def waiting_threads(threads: list[dict], login: str) -> list[dict]:
    """The unresolved threads where a reviewer spoke last: the ones waiting for the PR's author."""
    return [t for t in threads if not t["resolved"] and t["comments"]
            and t["comments"][-1]["author"].lower() != login.lower() and t["comments"][-1]["association"] in REVIEWERS]


def is_approved(reviews: list[dict]) -> bool:
    """A reviewer with access to the repository approved the PR, and none of them still asks for changes."""
    states = {r["state"] for r in reviews if r["association"] in REVIEWERS}
    return "APPROVED" in states and "CHANGES_REQUESTED" not in states


def reviewed_by_others(pr: dict, reviews: list[dict], threads: list[dict], login: str) -> str | None:
    """Why the developer should not review the PR: it is approved (by anyone, them too), or a
    reviewer other than them and its author has commented on it or asked for changes. None: review it."""
    if any(r["state"] == "APPROVED" for r in reviews):
        return "it is already approved"
    mine = {login.lower(), pr["user"]["login"].lower()}
    others = [r["author"] for r in reviews if r["association"] in REVIEWERS and r["author"].lower() not in mine]
    others += [c["author"] for t in threads for c in t["comments"]
               if c["association"] in REVIEWERS and c["author"].lower() not in mine]
    return f"{others[0]} is already reviewing it" if others else None


def address_review(repo: Repo, number: int, branch: str, slack: tuple[str, str] | None, project: Project, actor: Actor,
                   cfg: Config, github: GitHub, cancel: CancelToken = NEVER, on_model=lambda models: None) -> tuple[str, str]:
    """`slack`: (channel, timestamp) of the message that asked for the PR's review; the reviewers
    are asked there to look again. Returns (status, message) like process_issues."""
    label = f"{repo.github}#{number}"
    url = f"https://github.com/{repo.github}/pull/{number}"
    timings = Timings(label)
    outcome = "failed"
    try:
        state, threads = github.review_threads(repo.github, number)
        threads = waiting_threads(threads, actor.github.login)
        if state != "OPEN" or not threads:
            outcome = "skipped"
            return outcome, "the pull request is closed" if state != "OPEN" else "no review comments are waiting"
        if is_approved(github.latest_reviews(repo.github, number)):
            outcome = "skipped"
            return outcome, f"{url} is approved: its review comments are left as they are"
        log.info("%s: %d review thread(s) to address for %s", label, len(threads), actor.display_name)
        claude_dir = claude_config_dir(cfg, actor.claude)
        token = actor.github.token
        with run_folders(cfg, project, f"fix-{repo.name}-{number}", claude_dir) as (workspace, scratch):
            worktree = workspace / repo.name
            with timings.step(f"clone {repo.name}"):
                commit = gitops.clone(repo.github, branch, worktree, token)
            checkout = Checkout(label, repo, branch, commit, worktree, cancel=cancel)
            model, effort = MODELS["standard"]
            claude = ClaudeSession(workspace, claude_env(actor.claude.token, claude_dir), label=label, cfg=cfg, cancel=cancel,
                                   model=model, effort=effort)
            stronger = escalating(claude, on_model, label)
            with timings.step("Claude change"):
                result = ask_usable(claude, fix_prompt(repo, number, threads, guidance.find(worktree, repo.name)), EDIT_TOOLS,
                                    fix_schema(threads),
                                    lambda r: fix_problem(r, threads), "summary")
            answers = {t["id"]: t for t in result["threads"]}
            to_ship, summary, failures = heal(claude, [checkout], scratch, cfg, label, timings, cancel, result["summary"],
                                              stronger)
            if failures:
                github.comment(repo.github, number, "I worked on these review comments, but my change doesn't pass the "
                                                    "tests yet, so nothing is pushed. I'll follow up on them.")
                return outcome, f"review fixes for {url} still fail the tests; nothing pushed"
            cancel.check()
            if to_ship:
                with timings.step("commit and push"):
                    gitops.commit_all(worktree, f"Address review comments\n\n{summary}", actor.github.git_name,
                                      actor.github.git_email)
                    gitops.push(worktree, repo.github, branch, token)
                log.info("%s: review fixes pushed to %s", label, branch)
            with timings.step("answer and resolve threads"):
                for thread in threads:
                    answer = answers[thread["id"]]
                    try:
                        github.reply_to_thread(thread["id"], answer["reply"].strip() or summary)
                        if to_ship and answer["addressed"]:
                            github.resolve_thread(thread["id"])
                    except HTTPError as e:
                        log.warning("%s: could not answer a review thread: %s", label, e)
            outcome = "done"
            if not to_ship:
                return outcome, f"answered the review comments on {url}; no code needed changing"
            ask_for_review(actor, project, "", [(repo, url)], label, slack, again=True, done="Review comments addressed.")
            return outcome, f"review comments addressed on {url}"
    finally:
        timings.report(outcome)


def failed_checks(github: GitHub, owner_repo: str, sha: str) -> list[dict] | None:
    """The CI checks and statuses that failed on the commit, [{name, url, job (a GitHub Actions job
    ID, for its log) or None, text}]; None while any is still running, so all failures are seen at once."""
    runs, statuses = github.check_runs(owner_repo, sha), github.statuses(owner_repo, sha)
    if any(r["status"] != "completed" for r in runs) or any(s["state"] == "pending" for s in statuses):
        return None
    failed = []
    for r in runs:
        if r.get("conclusion") in FAILED_CHECKS:
            output = r.get("output") or {}
            failed.append({"name": r["name"], "url": r.get("html_url") or "",
                           "job": r["id"] if (r.get("app") or {}).get("slug") == "github-actions" else None,
                           "text": "\n".join(str(output.get(k) or "") for k in ("title", "summary", "text")).strip()})
    for st in statuses:
        if st["state"] in FAILED_STATUSES:
            failed.append({"name": st["context"], "url": st.get("target_url") or "", "job": None,
                           "text": st.get("description") or ""})
    return failed


def ci_output(github: GitHub, owner_repo: str, check: dict, label: str) -> str:
    """What a failed check said: the end of its job's log, or else its own summary."""
    text = check["text"]
    if check["job"]:
        try:
            text = LOG_NOISE_RE.sub("", github.job_log(owner_repo, check["job"], MAX_LOG_BYTES))
        except HTTPError as e:  # e.g. a token without Actions read access: the summary will do
            log.warning("%s: could not read the log of %s: %s", label, check["name"], e)
    return f'<ci_check name="{check["name"]}">\n{text[-MAX_LOG_CHARS:] or "(no output)"}\n</ci_check>'


def fix_ci(repo: Repo, number: int, branch: str, sha: str, project: Project, actor: Actor, cfg: Config, github: GitHub,
           cancel: CancelToken = NEVER, on_model=lambda models: None) -> tuple[str, str]:
    """The CI of the PR's head commit `sha` failed. Returns (status, message) like process_issues."""
    label = f"{repo.github}#{number}"
    url = f"https://github.com/{repo.github}/pull/{number}"
    timings = Timings(label)
    outcome = "skipped"
    try:
        pr = github.pr(repo.github, number)
        if pr["state"] != "open":
            return outcome, f"{url} is closed"
        if pr["head"]["sha"] != sha:
            return outcome, f"new commits were pushed to {url} since its CI failed; their CI is checked in turn"
        checks = failed_checks(github, repo.github, sha)
        if not checks:
            return outcome, f"the CI of {url} no longer fails" if checks is not None else f"the CI of {url} is running again"
        outcome = "failed"
        names = ", ".join(c["name"] for c in checks)
        log.info("%s: CI failed (%s) for %s", label, names, actor.display_name)
        with timings.step("CI output and diff"):
            failures = "\n\n".join(ci_output(github, repo.github, c, label) for c in checks)[-MAX_CI_CHARS:]
            try:
                diff = github.pr_diff(repo.github, number)[:MAX_DIFF_CHARS]
            except HTTPError as e:
                if e.status != 413:
                    raise
                diff = "(too large to show: read the changed files)"
        claude_dir = claude_config_dir(cfg, actor.claude)
        with run_folders(cfg, project, f"ci-{repo.name}-{number}", claude_dir) as (workspace, scratch):
            worktree = workspace / repo.name
            with timings.step(f"clone {repo.name}"):
                commit = gitops.clone(repo.github, branch, worktree, actor.github.token)
            if commit != sha:
                outcome = "skipped"
                return outcome, f"new commits were pushed to {url} since its CI failed; their CI is checked in turn"
            checkout = Checkout(label, repo, branch, commit, worktree, cancel=cancel)
            model, effort = MODELS["standard"]
            claude = ClaudeSession(workspace, claude_env(actor.claude.token, claude_dir), label=label, cfg=cfg, cancel=cancel,
                                   model=model, effort=effort)
            stronger = escalating(claude, on_model, label)
            with timings.step("Claude change"):
                summary = ask_usable(claude, ci_prompt(repo, pr, diff, [failures], guidance.find(worktree, repo.name)),
                                     EDIT_TOOLS, CHANGE_SCHEMA,
                                     summary_problem, "summary")["summary"]
            to_ship, summary, left = heal(claude, [checkout], scratch, cfg, label, timings, cancel, summary, stronger)
            if left:
                github.comment(repo.github, number, f"CI failed ({names}). I worked on a fix, but it doesn't pass the "
                                                    "tests yet, so nothing is pushed. I'll follow up.")
                return outcome, f"the fix for the failing CI of {url} still fails the tests; nothing pushed"
            cancel.check()
            outcome = "done"
            if not to_ship:
                github.comment(repo.github, number, f"CI failed ({names}). I looked into it and changed nothing: {summary}")
                return outcome, f"looked into the failing CI of {url}; no code change: it needs a human"
            with timings.step("commit and push"):
                gitops.commit_all(worktree, f"Fix failing CI checks\n\n{summary}", actor.github.git_name,
                                  actor.github.git_email)
                gitops.push(worktree, repo.github, branch, actor.github.token)
            log.info("%s: CI fix pushed to %s", label, branch)
            github.comment(repo.github, number, f"CI failed ({names}). Pushed a fix: {summary}")
            return outcome, f"fix for the failing CI pushed to {url}"
    finally:
        timings.report(outcome)


def review_pr(repo: Repo, number: int, thread: tuple[str, str], project: Project, actor: Actor, cfg: Config,
              github: GitHub, cancel: CancelToken = NEVER, on_model=lambda models: None) -> tuple[str, str, str]:
    """`thread`: (channel, timestamp) of the Slack message that asked; ShadowDev answers there, as
    the developer. Returns (status, message, what became of the request: commented, approved,
    or closed when there is nothing to review)."""
    label = f"{repo.github}#{number}"
    timings = Timings(label)
    outcome = "failed"

    def say(text: str) -> None:
        try:
            Slack(actor.slack.token).post(thread[0], text, thread[1])
        except HTTPError as e:
            log.warning("%s: could not answer in Slack: %s", label, e)

    try:
        pr = github.pr(repo.github, number)
        url = pr["html_url"]
        refusal = ("it is closed" if pr["state"] != "open"
                   else "it is your own" if pr["user"]["login"].lower() == actor.github.login.lower()
                   else "it comes from a fork" if ((pr["head"].get("repo") or {}).get("full_name") or "").lower() != repo.github.lower()
                   else None)
        threads = []
        if not refusal:
            _, threads = github.review_threads(repo.github, number)
            refusal = reviewed_by_others(pr, github.latest_reviews(repo.github, number), threads, actor.github.login)
        if refusal:
            outcome = "skipped"
            return outcome, f"{url} was not reviewed: {refusal}", "closed"
        say(f"Reviewing {link(url, f'PR {number}')}")
        log.info("%s: reviewing for %s", label, actor.display_name)
        with timings.step("diff"):
            diff = github.pr_diff(repo.github, number)
            if len(diff) > MAX_DIFF_CHARS:
                diff = diff[:MAX_DIFF_CHARS] + "\n… (the diff is cut here: read the rest in the files)"
        claude_dir = claude_config_dir(cfg, actor.claude)
        with run_folders(cfg, project, f"review-{repo.name}-{number}", claude_dir) as (workspace, scratch):
            with timings.step(f"clone {repo.name}"):
                gitops.clone(repo.github, pr["head"]["ref"], workspace / repo.name, actor.github.token)
            with timings.step("instructions from the base branch"):
                # The repository's instructions for AI tools, as on the base branch: the PR can't
                # change how it is judged. Cloned outside the workspace; only those files are copied in.
                base = scratch / "base" / repo.name
                base.parent.mkdir(parents=True, exist_ok=True)
                gitops.clone(repo.github, pr["base"]["ref"], base, actor.github.token)
                guides = guidance.find(base, f"{guidance.COPY_DIR}/{repo.name}")
                guidance.copy_into(guides, base, workspace)
            claude = ClaudeSession(workspace, claude_env(actor.claude.token, claude_dir), label=label, cfg=cfg, cancel=cancel)
            on_model(model_name(None, None))
            with timings.step("Claude review"):
                result = ask_usable(claude, review_prompt(repo, pr, diff, threads, guides), READ_ONLY_TOOLS, REVIEW_SCHEMA,
                                    review_problem, "review")
        cancel.check()
        outcome = "done"
        if result["verdict"] == "approve" and actor.settings.review_approve:
            github.create_review(repo.github, number, "APPROVE", result["summary"])
            say(f"Approved {link(url, f'PR {number}')} ✅")
            return outcome, f"approved {url}", "approved"
        if result["verdict"] == "approve":  # the developer approves themselves: say only that nothing must change
            github.create_review(repo.github, number, "COMMENT", f"Nothing that must change. {result['summary']}")
            say(f"Reviewed {link(url, f'PR {number}')}, nothing blocking")
            return outcome, f"reviewed {url}: nothing to change; not approved, as chosen", "approved"
        comments = result["comments"]
        try:
            github.create_review(repo.github, number, "COMMENT", result["summary"], comments)
        except HTTPError as e:
            if e.status != 422:
                raise
            # A comment was not on a line of the diff: GitHub refuses the whole review. Post them as text.
            listed = "\n".join(f"- `{c['path']}:{c['line']}` {c['body']}" for c in comments)
            github.create_review(repo.github, number, "COMMENT", f"{result['summary']}\n\n{listed}")
        say(f"Comments added on {link(url, f'PR {number}')} ({len(comments)})")
        return outcome, f"{len(comments)} review comment(s) on {url}", "commented"
    finally:
        timings.report(outcome)
