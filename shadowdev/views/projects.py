"""The Projects tab: the projects on the developer's Jira sites, and joining them with their accounts."""
import time

from ..config import Config
from ..models.project import Project
from ..models.release import ReleaseReport
from ..models.settings import Settings
from ..models.user import Subscription, User
from .components import esc, options, tag

MODE_CHOICES = (("bugs", "Bug fixes only", "Bug"), ("all", "Bug fixes and tasks", "Bug, Task, Story, Sub-task"))
# The automation switches of models.settings, by where they act: (legend, [(setting, label, detail)]).
SWITCHES = (
    ("Issues", (
        ("auto_start", "Start by itself", "Work on new assignments as they come (webhook, polling). Off: only from Run issues now"),
        ("ask_questions", "Ask the reporter", "What only they can say (expected behaviour, missing steps), on the ticket; "
                                               "it waits for their answer (needs Comment on the ticket)"),
        ("ask_developer", "Ask me technical questions", "A costly decision the code can't settle stops the run and asks "
                                                         "you, not the ticket; answer in a note. Off: Claude decides, "
                                                         "and the PR says what it assumed"),
        ("regression_tests", "Prove bug fixes", "Bugs get a regression test, which must fail without the fix"),
        ("continue_reopened", "Work again on reopened issues", "Sent back with a comment: continue on its branch and PR (needs Comment on the ticket, to find its PRs)"),
    )),
    ("Jira", (
        ("jira_status", "Move the ticket", "In Progress, In Review, and back when a run fails"),
        ("jira_comments", "Comment on the ticket", "Root cause or plan, PR links, failures and questions"),
        ("jira_fix_version", "Set the fix version", "Bugs get the highest unreleased version"),
        ("release_moves", "Move complete tickets", "By the project's status rules (e.g. Ready for Release to Done), "
                                                   "once your ticket has what the rule checks. Off: only from Check now"),
    )),
    ("Pull requests", (
        ("slack_review_request", "Ask for review in Slack", "In the project's channel, mentioning the repos' reviewers"),
        ("fix_review_comments", "Fix review comments", "Make the changes reviewers ask for on its PRs, and answer them"),
        ("fix_ci", "Fix failing CI", "Fix what makes its PRs' CI checks fail"),
    )),
    ("Reviews for colleagues", (
        ("review_for_me", "Review when asked in Slack", "When mentioned with a PR of the project in its channel"),
        ("review_approve", "Approve as me", "Approve a PR that needs no changes. Off: comment only"),
    )),
)
MODEL_LABELS = (("auto", "Chosen per issue"), ("haiku", "Always Haiku"), ("sonnet", "Always Sonnet"),
                ("default", "Account's default"))
RELEASE_HOURS_LABELS = (("4", "Every 4 hours"), ("8", "Every 8 hours"), ("12", "Every 12 hours"), ("24", "Once a day"))
PUBLISH_LABELS = (("pr", "Open a pull request"), ("draft", "Open a draft PR"), ("branch", "Only push the branch"))


def projects_panel(cfg: Config, user: User, projects: list[Project], secrets: dict[int, str], hidden: str) -> str:
    cards = "".join(project_card(user, p, hidden, secrets.get(p.id), cfg) for p in projects) or \
        '<section class="card"><p class="muted">No projects on your Jira sites yet. Add the first one.</p></section>'
    return f"""
<div class="row intro"><p class="muted">The projects on your Jira sites. Join one with the accounts it should use:
  your office GitHub and Claude for work, your personal ones for your own.</p>
  <a class="button" href="/project">Add a project</a></div>
{cards}"""


def project_card(user: User, project: Project, hidden: str, secret: str | None, cfg: Config) -> str:
    sub = user.subscriptions.get(project.id)
    site = user.sites[project.cloud_id].name
    if not sub:
        state = tag("not joined", False)
    elif user.actor(project.id):
        state = tag("joined", True)
    else:
        state = tag("choose accounts", False)
    repos = ", ".join(f"{r.name} ({r.github})" for r in project.repos)
    owner = ""
    if project.owner_id == user.id:
        owner = (f'<p class="muted">You own this project. <a href="/project?id={esc(project.id)}">Edit it</a>.</p>'
                 + webhook_help(project, secret, cfg))
    return f"""<section class="card">
  <div class="card-head"><h3>{esc(project.name)}</h3>{state}</div>
  <p class="muted">Jira {esc(", ".join(sorted(project.jira_projects)))} on {esc(site)} · repos {esc(repos)}</p>
  {owner}
  {_subscription_form(user, project, sub, hidden)}
  {_release_checks(project, sub, hidden, bool(user.slack)) if sub else ""}
</section>"""


def _subscription_form(user: User, project: Project, sub: Subscription | None, hidden: str) -> str:
    jira = [(a.id, f"{a.display_name} ({a.email})" if a.email else a.display_name)
            for a in user.jira_accounts if a.can_reach(project.cloud_id)]
    jira_field = (f'<input type="hidden" name="jira_account_id" value="{esc(jira[0][0])}">' if len(jira) == 1 else
                  f'<label>Jira account <select name="jira_account_id">{options(jira, sub and sub.jira_account_id)}</select></label>')
    github = [(g.id, f"{g.label} (@{g.login})") for g in user.github_accounts]
    claude = [(c.id, c.label) for c in user.claude_accounts]
    one = lambda items: items[0][0] if len(items) == 1 else None
    mode = sub.mode if sub else "bugs"
    modes = "".join(
        f'<label class="choice"><input type="radio" name="mode" value="{m}"{" checked" if mode == m else ""}>'
        f'<span>{label}<small>{detail}</small></span></label>' for m, label, detail in MODE_CHOICES)
    bases = sub.base_branches if sub else {}
    branches = "".join(
        f'<label>{esc(r.name)} <input name="base:{esc(r.name)}" value="{esc(bases.get(r.name, ""))}"'
        f' placeholder="latest {esc(r.release_prefix)}*, else default" spellcheck="false" autocomplete="off"></label>'
        for r in project.repos)
    leave = (f'<button class="secondary" formaction="/project/leave" formnovalidate data-confirm="Leave {esc(project.name)}?">Leave</button>'
             if sub else "")
    return f"""<form method="post" action="/project/join">{hidden}<input type="hidden" name="project_id" value="{esc(project.id)}">
{jira_field}
<div class="grid">
  <label>GitHub account <select name="github_account_id" required>{options(github, sub.github_account_id if sub else one(github), "Choose…")}</select></label>
  <label>Claude account <select name="claude_account_id" required>{options(claude, sub.claude_account_id if sub else one(claude), "Choose…")}</select></label>
</div>
<div class="field"><span class="label">Automate</span><div class="choices">{modes}</div></div>
<details class="more"><summary>Base branches</summary>
  <fieldset><legend>Work is branched from here and the PR goes into it</legend>{branches}</fieldset></details>
{_settings_fields(sub.settings if sub else Settings(), project)}
<div class="actions"><button>{"Save" if sub else "Join"}</button>{leave}</div>
</form>"""  # noqa: S608


def _settings_fields(settings: Settings, project: Project) -> str:
    """The automation switches of a subscription, each on or off, its two choices, and, under
    review_for_me, the repos whose PRs are reviewed for the developer."""
    def switch(name: str, label: str, detail: str) -> str:
        return (f'<label class="choice"><input type="checkbox" name="set:{name}"{" checked" if getattr(settings, name) else ""}>'
                f'<span>{esc(label)}<small>{esc(detail)}</small></span></label>'
                + (_review_repos(settings, project) if name == "review_for_me" else ""))

    groups = "".join(
        f'<fieldset class="settings"><legend>{esc(legend)}</legend><div class="choices">'
        + "".join(switch(*s) for s in switches) + "</div></fieldset>" for legend, switches in SWITCHES)
    def choice(name: str, label: str, hint: str, items, extra: str = "") -> str:
        """A row: what the choice is and what it means, beside its dropdown. IDs carry the project:
        every project's card is on the same page."""
        field = f"set-{project.id}-{name}"
        return f"""<div class="setting"{extra}>
      <div class="setting-text"><label for="{field}">{esc(label)}</label>
        <p class="hint" id="{field}-hint">{esc(hint)}</p></div>
      <select id="{field}" name="set:{name}" aria-describedby="{field}-hint">{options(items, getattr(settings, name))}</select>
    </div>"""

    how = (choice("model", "Claude model", "Chosen per issue: Haiku, Sonnet or the account's default, by how hard the "
                  "issue is.", MODEL_LABELS)
           + choice("publish", "When a change passes the tests", "Only push the branch: you open the PR yourself, and the "
                    "ticket stays In Progress.", PUBLISH_LABELS)
           + choice("release_hours", "Check for complete tickets", "How often Move complete tickets (under Jira) "
                    "checks your tickets. Check now under Status rules runs it any time.", RELEASE_HOURS_LABELS,
                    " data-needs-release"))
    return f"""<details class="more automation"><summary>Automation</summary><input type="hidden" name="settings" value="1">
  <fieldset class="settings how"><legend>How</legend>{how}</fieldset>
  {groups}</details>"""


def _release_checks(project: Project, sub: Subscription, hidden: str, slack: bool) -> str:
    """The project's status rules, the last check of the developer's tickets against them, and
    checking now."""
    rules = "".join(f"<li>{esc(r)}</li>" for r in project.status_rules) or "<li>None: its owner sets them</li>"
    state = (f"On, every {esc(sub.settings.release_hours)} hours" if sub.settings.release_moves
             else "Off: only when you check now")
    return f"""<details class="more"><summary>Status rules · {state}</summary>
  <ul class="hint">{rules}</ul>
  <p class="hint">A ticket that misses something stays where it is{
      ", and you get a Slack message with what it misses" if slack else
      ". Connect Slack on the Accounts tab to get a message with what it misses"}.</p>
  {_release_report(sub.release_report)}
  <form method="post" action="/project/release">{hidden}<input type="hidden" name="project_id" value="{esc(project.id)}">
    <button class="secondary small">Check now</button></form>
</details>"""


def _release_report(report: ReleaseReport | None) -> str:
    if not report:
        return '<p class="hint">Not checked yet.</p>'
    parts = [("Moved", report.moved), ("Not moved", report.held), ("Problems", report.failed)]
    lines = "".join(f'<p class="hint"><b>{label}:</b> {esc("; ".join(items))}</p>' for label, items in parts if items)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(report.at))
    return f'<p class="hint">Last checked {esc(when)}.{"" if lines else " None of your tickets were in these statuses."}</p>{lines}'


def _review_repos(settings: Settings, project: Project) -> str:
    """One checkbox per repo of the project: its PRs are reviewed for the developer when ticked."""
    repos = "".join(
        f'<label class="choice"><input type="checkbox" name="set:review:{esc(r.name)}"'
        f'{"" if r.name in settings.no_review_repos else " checked"}>'
        f'<span>{esc(r.name)}<small class="mono">{esc(r.github)}</small></span></label>' for r in project.repos)
    return f"""<div class="sub-choices">
  <div class="sub-head">Repos you review</div>
  <p class="hint">PRs of an unticked repo are left to the others mentioned with you.</p>
  <div class="choices repos">{repos}</div>
</div>"""


def webhook_help(project: Project, secret: str | None, cfg: Config) -> str:
    if not secret:
        return ""
    return f"""<details class="more"><summary>Jira webhook for instant runs</summary>
  <p class="hint">In Jira ({esc(project.site_url)}): <b>Settings → System → WebHooks → Create a WebHook</b>, with
    events Issue <b>created</b> and <b>updated</b>, and JQL <code>project in ({esc(", ".join(sorted(project.jira_projects)))})</code>.</p>
  <span class="label">URL</span><div class="secret">{esc(cfg.public_url)}/webhook?project={esc(project.id)}</div>
  <span class="label">Secret</span><div class="secret">{esc(secret)}</div>
</details>"""
