"""The Projects tab: the projects on the developer's Jira sites, and joining them with their accounts."""
from ..config import Config
from ..models.project import Project
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
MODEL_LABELS = (("auto", "Chosen per issue (Haiku, Sonnet or the default, by how hard it is)"), ("haiku", "Always Haiku"),
                ("sonnet", "Always Sonnet"), ("default", "Always the Claude account's default model"))
PUBLISH_LABELS = (("pr", "Open a pull request"), ("draft", "Open a draft pull request"),
                  ("branch", "Only push the branch: I open the PR"))


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
{_settings_fields(sub.settings if sub else Settings())}
<div class="actions"><button>{"Save" if sub else "Join"}</button>{leave}</div>
</form>"""  # noqa: S608


def _settings_fields(settings: Settings) -> str:
    """The automation switches of a subscription, each on or off, and its two choices."""
    groups = "".join(
        f'<fieldset class="settings"><legend>{esc(legend)}</legend><div class="choices">'
        + "".join(f'<label class="choice"><input type="checkbox" name="set:{name}"{" checked" if getattr(settings, name) else ""}>'
                  f'<span>{esc(label)}<small>{esc(detail)}</small></span></label>' for name, label, detail in switches)
        + "</div></fieldset>" for legend, switches in SWITCHES)
    return f"""<details class="more"><summary>Automation</summary><input type="hidden" name="settings" value="1">
  <fieldset class="settings"><legend>How</legend>
    <label>Claude model <select name="set:model">{options(MODEL_LABELS, settings.model)}</select></label>
    <label>After a change passes the tests <select name="set:publish">{options(PUBLISH_LABELS, settings.publish)}</select></label>
  </fieldset>
  {groups}</details>"""


def webhook_help(project: Project, secret: str | None, cfg: Config) -> str:
    if not secret:
        return ""
    return f"""<details class="more"><summary>Jira webhook for instant runs</summary>
  <p class="hint">In Jira ({esc(project.site_url)}): <b>Settings → System → WebHooks → Create a WebHook</b>, with
    events Issue <b>created</b> and <b>updated</b>, and JQL <code>project in ({esc(", ".join(sorted(project.jira_projects)))})</code>.</p>
  <span class="label">URL</span><div class="secret">{esc(cfg.public_url)}/webhook?project={esc(project.id)}</div>
  <span class="label">Secret</span><div class="secret">{esc(secret)}</div>
</details>"""
