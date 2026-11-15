"""The Projects tab: the projects on the developer's Jira sites, and joining them with their accounts."""
import time

from ..config import Config
from ..models.project import Project
from ..models.release import ReleaseReport
from ..models.settings import DAYS, MAX_BRANCH_PATTERNS, PARALLEL_RUNS_CHOICES, REMIND_HOURS_CHOICES, Settings
from ..models.user import MAX_TESTS, Subscription, User
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
        ("switch_claude", "Switch Claude account at its limit", "When this project's Claude account reaches its usage limit, "
                                                                "go on with another of your Claude accounts. Off: wait for the reset"),
    )),
    ("Jira", (
        ("jira_status", "Move the ticket", "In Progress, In Review, and back when a run fails"),
        ("jira_comments", "Comment on the ticket", "The plan, or a bug's root cause when the project has no root "
                                                   "cause field; PR links, failures and questions"),
        ("jira_root_cause", "Fill the root cause field", "A bug's root cause goes into the project's root cause field"),
        ("jira_fix_version", "Set the fix version", "Bugs get an unreleased version, as the project's owner chose"),
        ("release_moves", "Move complete tickets", "By the project's status rules (e.g. Ready for Release to Done), "
                                                   "once your ticket has what the rule checks. Off: only from Check now"),
    )),
    ("Pull requests", (
        ("slack_review_request", "Ask for review in Slack", "In the project's channel, mentioning the repos' reviewers"),
        ("fix_review_comments", "Fix review comments", "Make the changes reviewers ask for on its PRs, and answer them"),
        ("fix_ci", "Fix failing CI", "Fix what makes its PRs' CI checks fail"),
        ("fix_conflicts", "Fix merge conflicts", "When its base branch moves on and a PR conflicts with it, merge it in, "
                                                 "resolve the conflicts and push once the tests pass (never a force push)"),
        ("remind_reviewers", "Remind reviewers", "When a PR has no review a while after it was asked for in Slack, "
                                                 "ask its reviewers again in that thread: at most once a day, 3 times"),
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
PARALLEL_RUNS_LABELS = tuple((n, f"{n} at a time") for n in PARALLEL_RUNS_CHOICES)
PRIORITY_LABELS = (("high", "High"), ("normal", "Normal"), ("low", "Low"))
REMIND_HOURS_LABELS = tuple((h, f"After {h} hour{'' if h == '1' else 's'}") for h in REMIND_HOURS_CHOICES)
NOTIFY_LABELS = (("slack", "In Slack"), ("email", "By email"), ("both", "Slack and email"), ("none", "Only on this page"))


def projects_panel(cfg: Config, user: User, projects: list[Project], secrets: dict[int, str], hidden: str,
                   claude_usage: dict[int, tuple[str, bool]]) -> str:
    cards = "".join(project_card(user, p, hidden, secrets.get(p.id), cfg, claude_usage) for p in projects) or \
        '<section class="card"><p class="muted">No projects on your Jira sites yet. Add the first one.</p></section>'
    return f"""
<div class="row intro"><p class="muted">The projects on your Jira sites. Join one with the accounts it should use:
  your office GitHub and Claude for work, your personal ones for your own.</p>
  <a class="button" href="/project">Add a project</a></div>
{cards}"""


def project_card(user: User, project: Project, hidden: str, secret: str | None, cfg: Config,
                 claude_usage: dict[int, tuple[str, bool]]) -> str:
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
  {_subscription_form(user, project, sub, hidden, _active_claude(user, sub, claude_usage), bool(cfg.smtp))}
  {_release_checks(project, sub, hidden, bool(user.slack)) if sub else ""}
</section>"""


def _active_claude(user: User, sub: Subscription | None, usage: dict[int, tuple[str, bool]]) -> str:
    """Which Claude account the project's runs work with now: its own, or, while that is at its
    usage limit and switching is on, the next of the developer's that is not."""
    own = user.claude(sub.claude_account_id) if sub else None
    if not own or len(user.claude_accounts) < 2:
        return ""
    held = lambda account: usage.get(account.id, ("", False))[1]
    if not held(own):
        return f'<p class="hint active">Active Claude account: <b>{esc(own.label)}</b>.</p>'
    other = next((a for a in user.claude_accounts if a.id != own.id and not held(a)), None) if sub.settings.switch_claude else None
    if other:
        return (f'<p class="hint active">Active Claude account: <b>{esc(other.label)}</b>. {esc(own.label)} is at its limit: '
                f'{esc(usage[own.id][0])}.</p>')
    why = "your other Claude accounts are too" if sub.settings.switch_claude else "switching accounts is off"
    return (f'<p class="hint active held">Active Claude account: none. <b>{esc(own.label)}</b> is at its limit: {esc(usage[own.id][0])}, '
            f'and {why}. Runs wait for the reset.</p>')


def _subscription_form(user: User, project: Project, sub: Subscription | None, hidden: str, active: str = "",
                       mail: bool = False) -> str:
    """`active`: which Claude account its runs work with now (_active_claude), shown under the accounts.
    `mail`: the server can send email."""
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
{active}
<div class="field"><span class="label">Automate</span><div class="choices">{modes}</div></div>
<details class="more"><summary>Base branches</summary>
  <fieldset><legend>Work is branched from here and the PR goes into it</legend>{branches}</fieldset></details>
{_settings_fields(sub.settings if sub else Settings(), project, mail, user.email_for(sub) if sub else ("", ""),
                  bool(user.slack), sub)}
<div class="actions"><button>{"Save" if sub else "Join"}</button>{leave}</div>
</form>"""  # noqa: S608


def _settings_fields(settings: Settings, project: Project, mail: bool = False,
                     email: tuple[str, str] = ("", ""), slack: bool = False, sub: Subscription | None = None) -> str:
    """The automation switches of a subscription, each on or off, its two choices, and, under
    review_for_me, the repos whose PRs are reviewed for the developer. `email`: (address, where it is
    from) emails go to now (User.email_for), and `slack`: the developer connected Slack, both shown
    under Tell me."""
    def switch(name: str, label: str, detail: str) -> str:
        if not applies(name, project):  # kept as it is, for when the project's owner changes that
            return f'<input type="hidden" name="set:{name}" value="on">' if getattr(settings, name) else ""
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
                    " data-needs-release")
           + choice("remind_hours", "Remind reviewers", "How long a PR waits for its first review before Remind "
                    "reviewers (under Pull requests) asks again in its Slack thread.", REMIND_HOURS_LABELS)
           + choice("parallel_runs", "Runs at the same time", "Of this project's runs; more wait their turn. All your "
                    "projects together run at most what Runs at the same time on the Settings tab allows.",
                    PARALLEL_RUNS_LABELS)
           + choice("priority", "Priority", "When your runs at the same time are all taken, a free one goes to the "
                    "waiting project with the highest priority; projects of the same priority take turns.",
                    PRIORITY_LABELS))
    tell = (choice("notify_decisions", "Decisions a run needs from you", "A costly technical choice the code can't "
                   "settle (with Ask me technical questions on). The run waits for your answer.", NOTIFY_LABELS)
            + choice("notify_missing", "Tickets not complete yet", "Tickets the status rules hold, and what each misses.",
                     NOTIFY_LABELS))
    missing = [w for w, ok in (("Slack is not connected: connect it on the Accounts tab", slack),
                               ("this server sends no email: its .env needs SMTP_HOST", mail)) if not ok]
    note = "; ".join(missing)
    tell_hint = f'<p class="hint">Everything is on this page too. {esc(note[:1].upper() + note[1:])}{"." if note else ""}</p>'
    def branches(name: str, label: str, hint: str, example: str, suggest: str = "") -> str:
        """`suggest`: where the datalist of suggestions is loaded from once the field is used."""
        field = f"set-{project.id}-{name}"
        listed = (f' list="{field}-list"', f'<datalist id="{field}-list" data-src="{esc(suggest)}"></datalist>') \
            if suggest else ("", "")
        return f"""<div class="setting">
      <div class="setting-text"><label for="{field}">{esc(label)}</label>
        <p class="hint" id="{field}-hint">{esc(hint)}</p></div>
      <input id="{field}" name="set:{name}" value="{esc(getattr(settings, name))}" placeholder="{esc(example)}"
        maxlength="{MAX_BRANCH_PATTERNS}" spellcheck="false" autocomplete="off" aria-describedby="{field}-hint"{listed[0]}{" data-statuses" if suggest else ""}>{listed[1]}
    </div>"""

    start = (branches("auto_statuses", "In one of these statuses", "Jira statuses, separated by commas. Empty: any.",
                      "To Do, Open, Backlog, Reopened", f"/project/statuses?id={project.id}")
             + (switch("auto_sprint", "", "") if not applies("auto_sprint", project) else f"""<div class="setting">
      <div class="setting-text"><label for="set-{project.id}-auto_sprint">In an active sprint</label>
        <p class="hint">Started and not ended. An issue in no sprint, or a future one, waits.</p></div>
      <input type="checkbox" class="switch" role="switch" id="set-{project.id}-auto_sprint" name="set:auto_sprint"
        {" checked" if settings.auto_sprint else ""}>
    </div>"""))
    unreviewed = (branches("approve_from", "From branch", "The branch the PR comes from.", "release/*")
                  + branches("approve_into", "Into branch", "The branch the PR is merged into.", "main, develop"))
    return f"""<details class="more automation"><summary>Automation</summary><input type="hidden" name="settings" value="1">
  <fieldset class="settings how"><legend>How</legend>{how}</fieldset>
  {groups}
  <fieldset class="settings how"><legend>Tell me</legend>{tell_hint}{tell}{_email_address(settings, project, email)}
    {_test_buttons(sub, slack, mail, email[0])}</fieldset>
  <fieldset class="settings how start"><legend>Start by itself only when</legend>
    <p class="hint">For issues assigned to you that start by themselves (webhook, polling), with Start by itself on.
      Run issues now starts any issue of yours.</p>{start}</fieldset>
  <fieldset class="settings how"><legend>Working hours</legend>
    <p class="hint">Runs work at any time. Outside these hours, what other people see waits for them: PRs open (with
      their review request in Slack and their links on the ticket) once they begin, and so do replies and reminders
      to reviewers; a review you are asked for is still done, and answered with a reaction only. Messages to you are
      never held. Leave the times empty for any time.</p>
    {_work_hours(settings, project)}</fieldset>
  <fieldset class="settings how"><legend>Approve without review</legend>
    <p class="hint">A PR between these two branches is approved as you without being read, for changes that were
      already reviewed (a release branch going into main). Branch names or patterns, separated by commas; both must
      match. Leave them empty to review every PR.</p>{unreviewed}</fieldset></details>"""


def applies(name: str, project: Project) -> bool:
    """Whether a setting means anything in the project, by how its Jira projects work."""
    return {"auto_sprint": project.uses_sprints, "jira_fix_version": project.uses_fix_versions,
            "jira_root_cause": bool(project.root_cause_field)}.get(name, True)


def _work_hours(settings: Settings, project: Project) -> str:
    """Two times, the days as one row of toggles, and a time zone; with what holds now."""
    field = f"set-{project.id}-work"
    days = "".join(f'<label class="day"><input type="checkbox" name="set:day:{d}" aria-label="{DAY_NAMES[i]}"'
                   f'{" checked" if d in settings.work_days else ""}><span>{d.title()}</span></label>'
                   for i, d in enumerate(DAYS))
    server = time.strftime("%Z") or "UTC"
    now = time.time()
    opens = settings.work_starts(now)
    state = ("Any time: nothing waits." if not (settings.work_from and settings.work_until)
             else "Now within them." if opens <= now
             else f"Now outside them: what others see waits until {settings.local(opens).strftime('%a %H:%M')}.")
    return f"""<div class="setting">
      <div class="setting-text"><label for="{field}-from">Hours</label>
        <p class="hint" id="{field}-hint">{esc(state)} An end before the start runs overnight.</p></div>
      <div class="time-range"><input type="time" id="{field}-from" name="set:work_from" value="{esc(settings.work_from)}"
        aria-label="Start" aria-describedby="{field}-hint"><span aria-hidden="true">to</span><input type="time"
        name="set:work_until" value="{esc(settings.work_until)}" aria-label="End"></div>
    </div>
    <div class="setting">
      <div class="setting-text"><span class="label" id="{field}-days">Days</span></div>
      <div class="days" role="group" aria-labelledby="{field}-days">{days}</div>
    </div>
    <div class="setting">
      <div class="setting-text"><label for="{field}-zone">Time zone</label>
        <p class="hint" id="{field}-zone-hint">Like Europe/London or Asia/Karachi. Empty: the server's ({esc(server)}).</p></div>
      <input id="{field}-zone" name="set:work_timezone" value="{esc(settings.work_timezone)}" placeholder="Europe/London"
        maxlength="64" spellcheck="false" autocomplete="off" aria-describedby="{field}-zone-hint">
    </div>"""


DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _release_checks(project: Project, sub: Subscription, hidden: str, slack: bool) -> str:
    """The project's status rules, the last check of the developer's tickets against them, and
    checking now."""
    rules = "".join(f"<li>{esc(r)}</li>" for r in project.status_rules) or "<li>None: its owner sets them</li>"
    state = (f"On, every {esc(sub.settings.release_hours)} hours" if sub.settings.release_moves
             else "Off: only when you check now")
    return f"""<details class="more"><summary>Status rules · {state}</summary>
  <ul class="hint">{rules}</ul>
  <p class="hint">A ticket that misses something stays where it is{_told(sub, slack)}.</p>
  {_release_report(sub.release_report)}
  <form method="post" action="/project/release">{hidden}<input type="hidden" name="project_id" value="{esc(project.id)}">
    <button class="secondary small">Check now</button></form>
</details>"""


def _told(sub: Subscription, slack: bool) -> str:
    """How the developer hears what a held ticket misses, by their Tell me choice."""
    tells = sub.settings.tells
    ways = [w for w, on in (("in Slack", tells("missing", "slack") and slack), ("by email", tells("missing", "email"))) if on]
    if ways:
        return f", and you are told {' and '.join(ways)} what it misses"
    if tells("missing", "slack"):
        return ". Connect Slack on the Accounts tab, or choose email under Tell me, to hear what it misses"
    return ". Choose Slack or email for Tickets not complete yet, under Tell me, to hear what it misses"


def _release_report(report: ReleaseReport | None) -> str:
    if not report:
        return '<p class="hint">Not checked yet.</p>'
    parts = [("Moved", report.moved), ("Not moved", report.held), ("Problems", report.failed)]
    lines = "".join(f'<p class="hint"><b>{label}:</b> {esc("; ".join(items))}</p>' for label, items in parts if items)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(report.at))
    return f'<p class="hint">Last checked {esc(when)}.{"" if lines else " None of your tickets were in these statuses."}</p>{lines}'


def _test_buttons(sub: Subscription | None, slack: bool, mail: bool, address: str) -> str:
    """Send a test Slack message or email, each up to MAX_TESTS times, to see how messages look. Only
    once joined: they use the saved settings, not the form's."""
    if not sub:
        return ""
    def button(way: str, what: str, ready: bool) -> str:
        left = sub.tests_left(way, address)
        label = f"Send a test {what}" + (f" ({left} left)" if left < MAX_TESTS else "") if left else f"{MAX_TESTS} test {what}s sent"
        return (f'<button class="secondary small" formaction="/project/test-message" formnovalidate name="way" '
                f'value="{way}"{"" if ready and left else " disabled"}>{esc(label)}</button>')
    return f"""<div class="setting">
      <div class="setting-text"><span class="label">Try it</span>
        <p class="hint">Sample messages with made-up tickets, {MAX_TESTS} of each, each a different kind. They go
          where your saved settings say: save changes first.</p></div>
      <div class="actions">{button("slack", "Slack message", slack)}{button("email", "email", mail and bool(address))}</div>
    </div>"""


def _email_address(settings: Settings, project: Project, email: tuple[str, str]) -> str:
    """Where emails about the project go: the address the developer types, else their accounts'."""
    field = f"set-{project.id}-notify_address"
    address, source = email
    now = (f"Now: {esc(address)}, the address {esc(source)}." if address else
           "None of your accounts shows an email: type one to get emails.")
    fallback = "" if settings.notify_address else address
    return f"""<div class="setting">
      <div class="setting-text"><label for="{field}">Email me at</label>
        <p class="hint" id="{field}-hint">{now} Empty: the email of this project's Jira account, else its GitHub
          account's, else the one you signed in with.</p></div>
      <input id="{field}" type="email" name="set:notify_address" value="{esc(settings.notify_address)}"
        placeholder="{esc(fallback or "you@example.com")}" maxlength="{MAX_BRANCH_PATTERNS}" spellcheck="false"
        autocomplete="email" aria-describedby="{field}-hint">
    </div>"""


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
