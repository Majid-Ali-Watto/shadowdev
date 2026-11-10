"""HTML for the developer's pages. Every value from outside is escaped with esc()."""
import html
import time
from pathlib import Path

from ..automation.rules import MAX_NOTE_CHARS
from ..config import Config
from ..models.project import Project
from ..models.user import Subscription, User

STYLE = (Path(__file__).parent / "style.css").read_text()

# The logo: a shape and its shadow. Inline, so the Content-Security-Policy needs no img-src.
MARK = ('<svg class="mark" viewBox="0 0 32 32" aria-hidden="true">'
        '<rect x="10" y="10" width="19" height="19" rx="5" fill="currentColor" opacity=".3"/>'
        '<rect x="3" y="3" width="19" height="19" rx="5" fill="currentColor"/></svg>')

# Success messages are fixed strings chosen by key, so nothing from the request is echoed.
NOTICES = {
    "jira": "Jira account connected.",
    "jira-removed": "Jira account removed, with the projects you ran with it.",
    "github": "GitHub account saved.",
    "github-removed": "GitHub account removed. Projects that used it need another one.",
    "claude": "Claude account saved.",
    "claude-removed": "Claude account removed. Projects that used it need another one.",
    "project": "Project saved.",
    "project-deleted": "Project deleted.",
    "joined": "Project settings saved.",
    "left": "You left the project.",
    "queued": "Queued. Progress shows up under Recent runs.",
    "paused": "Automation paused. Nothing runs for you until you resume; a run in progress finishes.",
    "resumed": "Automation resumed.",
    "stopping": "Stopping the run. Nothing more is pushed or written to Jira; the ticket is left as it was.",
}
MODE_CHOICES = (("bugs", "Bug fixes only", "Bug"), ("all", "Bug fixes and tasks", "Bug, Task, Story, Sub-task"))


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def login_page(public_url: str, error: str = "") -> str:
    return page("Sign in · ShadowDev", f"""
<section class="card login">
  {MARK}
  <h1>ShadowDev</h1>
  <p class="lead">Automates your Jira bugs and tasks: Claude analyses the issue, writes the change
  across the project's repos, runs their tests and opens a pull request per changed repo for you to
  review.</p>
  {alert(error)}
  <a class="button block" href="{esc(public_url)}/login/jira">Sign in with Jira</a>
  <p class="hint">Opens Atlassian, where you grant ShadowDev access to Jira. It then reads the issues
    assigned to you and comments on them as you. No API token needed.</p>
  <p class="hint">Already use ShadowDev with another Jira account, e.g. your office one? Sign in with
    that one, and connect this one from your page, so all your projects are in one place.</p>
</section>""")


def continue_page(url: str, nonce: str) -> str:
    """Opens `url` (Atlassian's consent page) at once; the link is there if scripts are off."""
    return page("Connecting Jira · ShadowDev", f"""
<section class="card login">
  {MARK}
  <h1>Connecting another Jira account</h1>
  <p class="lead">Opening Atlassian… Choose the Jira site and account to connect, and press Accept.</p>
  <a class="button block" id="continue" href="{esc(url)}">Continue to Atlassian</a>
</section>
<script nonce="{esc(nonce)}">location.replace(document.getElementById("continue").href);</script>""")


def card_head(step: int, title: str, done: bool, tag: str = "") -> str:
    """A setup step's heading: its number (a check once done), title, and a tag on the right."""
    return (f'<div class="card-head"><span class="step{" done" if done else ""}" aria-hidden="true">{step}</span>'
            f'<h3>{title}</h3>{tag}</div>')


def tag(text: str, ok: bool) -> str:
    return f'<span class="tag {"ok" if ok else "off"}">{esc(text)}</span>'


def options(items: list[tuple[object, str]], selected=None, empty: str = "") -> str:
    first = f'<option value="">{esc(empty)}</option>' if empty else ""
    return first + "".join(f'<option value="{esc(v)}"{" selected" if v == selected else ""}>{esc(label)}</option>'
                           for v, label in items)


def remove_form(action: str, field: str, value: int, hidden: str, what: str) -> str:
    return (f'<form method="post" action="{action}" data-confirm="Remove {esc(what)}?">{hidden}'
            f'<input type="hidden" name="{field}" value="{esc(value)}"><button class="secondary small">Remove</button></form>')


def accounts_section(user: User, hidden: str) -> str:
    jira_items = "".join(
        f'<li><span>{esc(a.display_name)}{f" <span class=muted>({esc(a.email)})</span>" if a.email else ""}'
        f'<br><span class="muted">{esc(", ".join(s.name for s in a.sites) or "no Jira site")}</span></span>'
        f'{remove_form("/jira/remove", "jira_account_id", a.id, hidden, a.display_name) if len(user.jira_accounts) > 1 else ""}</li>'
        for a in user.jira_accounts)
    github_items = "".join(
        f'<li><span>{esc(g.label)} <span class="muted">@{esc(g.login)} · commits as {esc(g.git_name)}</span></span>'
        f'{remove_form("/github/remove", "github_account_id", g.id, hidden, g.label)}</li>' for g in user.github_accounts)
    claude_items = "".join(
        f'<li><span>{esc(c.label)}</span>{remove_form("/claude/remove", "claude_account_id", c.id, hidden, c.label)}</li>'
        for c in user.claude_accounts)
    jira_count, gh_count, claude_count = len(user.jira_accounts), len(user.github_accounts), len(user.claude_accounts)
    return f"""
<h2 class="section">Accounts</h2>
<p class="muted">Connect each account you work with, e.g. a personal and an office one of each. Every
  project you join says which ones it runs with.</p>
<section class="card">
  {card_head(1, "Jira", True, tag(f"{jira_count} account{'s' if jira_count != 1 else ''}", True))}
  <ul class="items">{jira_items}</ul>
  <form method="post" action="/jira/connect">{hidden}<button class="secondary">Connect another Jira account</button></form>
  <p class="hint">ShadowDev reads the issues assigned to you and comments as you. Access renews itself;
    if you revoke it at Atlassian, connect the account again.</p>
</section>

<section class="card">
  {card_head(2, "GitHub", bool(gh_count), tag(f"{gh_count} account{'s' if gh_count != 1 else ''}" if gh_count else "not connected", bool(gh_count)))}
  <ul class="items">{github_items}</ul>
  <form method="post" action="/github">{hidden}
<div class="grid">
  <label>Label <input name="github_label" placeholder="e.g. office (default: the login)" autocomplete="off" maxlength="40"></label>
  <label>Personal access token <input name="github_token" type="password" autocomplete="off" required
    placeholder="github_pat_… or ghp_…"></label>
</div>
<p class="hint">A <b>fine-grained token</b> with access to the projects' repositories and <b>Contents</b> and
  <b>Pull requests</b> set to read and write, or a <b>classic token</b> with the <b>repo</b> scope (add
  <b>workflow</b> if changes may touch <code>.github/workflows</code>). With organization SSO, authorize the
  token for it. A token for an account you already connected replaces its old one.</p>
<button>Save GitHub account</button>
  </form>
</section>

<section class="card">
  {card_head(3, "Claude", bool(claude_count), tag(f"{claude_count} account{'s' if claude_count != 1 else ''}" if claude_count else "not connected", bool(claude_count)))}
  <ul class="items">{claude_items}</ul>
  <form method="post" action="/claude">{hidden}
<div class="grid">
  <label>Label <input name="claude_label" placeholder="e.g. personal (default: Claude)" autocomplete="off" maxlength="40"></label>
  <label>Claude token <input name="claude_token" type="password" autocomplete="off" required
    placeholder="sk-ant-oat… or sk-ant-api…"></label>
</div>
<div class="hint">To get a token for your Claude subscription:
  <ol>
    <li>In a terminal, run <code>claude setup-token</code>. No Claude Code yet? Install it first:
      <code>curl -fsSL https://claude.ai/install.sh | bash</code></li>
    <li>Your browser opens: sign in to the Claude account you want (personal or office) and press <b>Authorize</b>.</li>
    <li>Copy the <code>sk-ant-oat…</code> token the terminal prints, and paste it here.</li>
  </ol>
  An API key from the Anthropic Console works too. A token under a label you already have replaces it.</div>
<button>Save Claude account</button>
  </form>
</section>"""


def subscription_form(user: User, project: Project, sub: Subscription | None, hidden: str) -> str:
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
<div class="actions"><button>{"Save" if sub else "Join"}</button>{leave}</div>
</form>"""


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
  {subscription_form(user, project, sub, hidden)}
</section>"""


def webhook_help(project: Project, secret: str | None, cfg: Config) -> str:
    if not secret:
        return ""
    return f"""<details class="more"><summary>Jira webhook for instant runs</summary>
  <p class="hint">In Jira ({esc(project.site_url)}): <b>Settings → System → WebHooks → Create a WebHook</b>, with
    events Issue <b>created</b> and <b>updated</b>, and JQL <code>project in ({esc(", ".join(sorted(project.jira_projects)))})</code>.</p>
  <span class="label">URL</span><div class="secret">{esc(cfg.public_url)}/webhook?project={esc(project.id)}</div>
  <span class="label">Secret</span><div class="secret">{esc(secret)}</div>
</details>"""


def settings_page(cfg: Config, user: User, projects: list[Project], secrets: dict[int, str], runs: list, csrf: str,
                  nonce: str, error: str = "", notice: str = "") -> str:
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
    ready = user.ready_projects
    if user.paused:
        status = "Paused: nothing runs for you until you resume. Your settings are kept."
    elif not ready:
        status = "Not active yet: connect GitHub and Claude, and join a project with them."
    else:
        status = f"Active in {len(ready)} project{'s' if len(ready) != 1 else ''}: issues assigned to you there are worked on automatically."

    cards = "".join(project_card(user, p, hidden, secrets.get(p.id), cfg) for p in projects) or \
        '<section class="card"><p class="muted">No projects on your Jira sites yet. Add the first one.</p></section>'
    joined = [p for p in projects if p.id in user.subscriptions]
    project_pick = ""
    if len(joined) > 1:
        project_pick = (f'<label>Project <select name="project_id">'
                        f'{options([(p.id, p.name) for p in joined], None, "Find it from the issue key")}</select></label>')

    if user.paused:
        pause_title, pause_text = "Automation paused", "Issues assigned to you are not worked on, and runs can't be started."
        pause_form = f'<form method="post" action="/resume">{hidden}<button>Resume automation</button></form>'
    else:
        pause_title = "Pause automation"
        pause_text = "Stops ShadowDev from working on your issues, without changing your settings. A run in progress finishes."
        pause_form = f'<form method="post" action="/pause">{hidden}<button class="secondary">Pause automation</button></form>'

    return page("ShadowDev", f"""
<header class="top">
  <div class="brand">{MARK}<div><h1>ShadowDev</h1>
    <p class="muted">Signed in as {esc(user.display_name)}{f" ({esc(user.email)})" if user.email else ""}</p></div></div>
  <form method="post" action="/logout">{hidden}<button class="secondary">Sign out</button></form>
</header>
<p class="status {'ok' if ready and not user.paused else 'warn'}" role="status">{esc(status)}</p>
{alert(error)}{alert(notice, "notice")}
{accounts_section(user, hidden)}

<h2 class="section">Projects</h2>
<div class="row intro"><p class="muted">The projects on your Jira sites. Join one with the accounts it should use:
  your office GitHub and Claude for work, your personal ones for your own.</p>
  <a class="button" href="/project">Add a project</a></div>
{cards}

<h2 class="section">Runs</h2>
<section class="card">
  <div class="card-head"><h3>Run issues now</h3></div>
  <p class="muted">New assignments start automatically. Use this for issues that were assigned to you
    before you joined. Up to {cfg.max_parallel_runs} runs work at the same time; more wait their turn.
    An issue that already has a ShadowDev branch on GitHub is skipped: to run it again, close its PR and
    delete its branch.</p>
  <form method="post" action="/run">{hidden}
<label>Issue keys <input name="issue_keys" placeholder="MAR-123, MAR-124" required spellcheck="false" autocomplete="off"></label>
{project_pick}
<label>Note for Claude <span class="muted">(optional: anything the tickets don't say, e.g. where to look or
  what to leave alone. Start a line with a key, like <code>MAR-123:</code>, for a note that is only for
  that issue; lines before those are for all of them)</span>
  <textarea name="note" maxlength="{MAX_NOTE_CHARS}" placeholder="Don't change the API.&#10;MAR-123: the bug is in the checkout total, not the cart."></textarea></label>
<div class="field"><span class="label">With more than one issue</span>
  <div class="choices">
    <label class="choice"><input type="radio" name="grouping" value="separate" checked>
      <span>Separately<small>A branch and PR for each issue, worked on in parallel</small></span></label>
    <label class="choice"><input type="radio" name="grouping" value="together">
      <span>Together<small>One branch for all of them, one PR per changed repo (same project); each ticket
        still gets its own analysis and comments</small></span></label>
  </div>
</div>
<div class="actions"><button>Run</button></div>
  </form>
</section>

<section class="card">
  <div class="card-head"><h3>Recent runs</h3></div>
  <div class="table"><table><thead><tr><th>Issue</th><th>Project</th><th>Status</th><th>Details</th><th>Updated</th></tr></thead>
  <tbody id="runs">{run_rows(runs, csrf)}</tbody></table></div>
</section>

<h2 class="section">Account</h2>
<section class="card">
  <div class="row">
    <div><h3>{pause_title}</h3><p class="muted">{esc(pause_text)}</p></div>
    {pause_form}
  </div>
  <div class="row">
    <div><h3>Delete account</h3>
      <p class="muted">Stops your queued and running runs (nothing more is pushed or written to Jira), and
        removes your accounts, tokens, settings and run history from ShadowDev. Projects you own pass to
        their longest-standing member. To revoke access completely, also remove it at Atlassian
        (Connected apps), GitHub (your tokens) and Claude.</p></div>
    <form method="post" action="/delete-account" data-confirm="Delete your ShadowDev account? Your queued and running runs are stopped and your stored tokens removed.">{hidden}
      <button class="danger">Delete my ShadowDev account</button></form>
  </div>
</section>
{CONFIRM_SCRIPT.format(nonce=esc(nonce))}{RUNS_SCRIPT.format(nonce=esc(nonce))}""")


def run_rows(runs: list, csrf: str) -> str:
    """The rows of Recent runs: also what GET /runs answers, for the page to refresh itself."""
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
    extra = lambda r, column, what: f'<small>{what}: {esc(r[column])}</small>' if r[column] else ""
    return "".join(
        f'<tr><td>{issue_links(r)}</td><td>{esc(r["project"])}</td>'
        f'<td class="state"><span class="badge {esc(r["status"])}">{esc(r["status"])}</span>{stop_button(r, hidden)}</td>'
        f'<td class="msg">{esc(r["message"])}{extra(r, "model", "Model")}{extra(r, "note", "Your note")}</td>'
        f'<td class="when">{esc(time.strftime("%Y-%m-%d %H:%M", time.localtime(r["updated"])))}</td></tr>'
        for r in runs
    ) or '<tr><td colspan="5" class="empty">No runs yet. Runs show up here once an issue is queued.</td></tr>'


# Keeps Recent runs up to date: every 5 seconds while a run is queued or running, else every 30.
RUNS_SCRIPT = """<script nonce="{nonce}">
  const runs = document.getElementById("runs");
  let tick = 0;
  setInterval(async () => {{
    const busy = runs.querySelector(".badge.queued, .badge.running, .badge.stopping");
    if (document.hidden || (++tick % 6 && !busy)) return;
    const response = await fetch("/runs").catch(() => null);
    if (response && response.ok) runs.innerHTML = await response.text();
  }}, 5000);
</script>"""


# Asks before forms marked data-confirm (or their submit button) are sent.
CONFIRM_SCRIPT = """<script nonce="{nonce}">
  document.addEventListener("submit", e => {{
    const ask = (e.submitter && e.submitter.dataset.confirm) || e.target.dataset.confirm;
    if (ask && !confirm(ask)) e.preventDefault();
  }});
</script>"""


def issue_links(run) -> str:
    keys = run["issue_key"].split(", ")
    if not run["site_url"]:
        return esc(run["issue_key"])
    return ", ".join(f'<a href="{esc(run["site_url"])}/browse/{esc(k)}" target="_blank" rel="noopener">{esc(k)}</a>'
                     for k in keys)


def repo_fields(repo: dict) -> str:
    v = lambda k: esc(repo.get(k) or "")
    return f"""<fieldset class="repo"><legend>{v('github') or "New repo"}</legend>
  <label>GitHub repo <input name="repo_github" value="{v('github')}" placeholder="owner/name or https://github.com/owner/name" spellcheck="false" autocomplete="off"></label>
  <details class="more"><summary>Folder name, description, release branches</summary>
    <label>Folder name <input name="repo_name" value="{v('name')}" placeholder="default: the repo name" spellcheck="false" autocomplete="off"></label>
    <label>Description for Claude <input name="repo_description" value="{v('description')}" placeholder="default: the GitHub description"></label>
    <label>Release branch prefix <input name="repo_release_prefix" value="{v('release_prefix')}" placeholder="release/" spellcheck="false"></label>
  </details>
  <label>Setup command <span class="muted">(optional, once per run, e.g. npm ci)</span>
    <textarea name="repo_setup" spellcheck="false">{v('setup')}</textarea></label>
  <label>Test command <span class="muted">(optional; writes JUnit XML to $SHADOWDEV_REPORT_DIR)</span>
    <textarea name="repo_test" spellcheck="false" placeholder="pytest --junitxml=&quot;$SHADOWDEV_REPORT_DIR/tests.xml&quot;">{v('test')}</textarea></label>
  <button type="button" class="secondary small" data-remove-repo>Remove repo</button>
</fieldset>"""


def project_page(cfg: Config, user: User, form: dict, project: Project | None, secret: str | None, csrf: str,
                 nonce: str, error: str = "") -> str:
    """Add a project (project None) or edit one the developer owns. `form`: the values to show."""
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
    v = lambda k: esc(form.get(k) or "")
    sites = [(cid, f"{s.name} ({s.url})") for cid, s in sorted(user.sites.items(), key=lambda i: i[1].name)]
    site_field = (f'<label>Jira site <select name="cloud_id" required>{options(sites, form.get("cloud_id") or (sites[0][0] if len(sites) == 1 else None), "Choose…")}</select></label>'
                  if not project else f'<p class="muted">Jira site: {esc(user.sites[project.cloud_id].name)}</p>')
    github = [(g.id, f"{g.label} (@{g.login})") for g in user.github_accounts]
    chosen = int(form["github_account_id"]) if str(form.get("github_account_id") or "").isdigit() else (
        github[0][0] if len(github) == 1 else None)
    repos = "".join(repo_fields(r) for r in form.get("repos") or [{}])
    title = f"Edit {project.name}" if project else "Add a project"
    delete = (f'<form method="post" action="/project/delete" data-confirm="Delete {esc(project.name)}? Its members stop '
              f'running there and its queued runs are stopped.">{hidden}<input type="hidden" name="project_id" '
              f'value="{esc(project.id)}"><button class="danger">Delete project</button></form>') if project else ""
    return page(f"{title} · ShadowDev", f"""
<header class="top">
  <div class="brand">{MARK}<div><h1>{esc(title)}</h1><p class="muted"><a href="/">Back to your page</a></p></div></div>
</header>
{alert(error)}
<section class="card">
  <form method="post" action="/project/save" id="project">{hidden}
{f'<input type="hidden" name="project_id" value="{esc(project.id)}">' if project else ""}
<label>Name <input name="name" value="{v('name')}" required maxlength="60" placeholder="e.g. marketplace"></label>
{site_field}
<label>Jira projects <input name="jira_keys" value="{v('jira_keys')}" required placeholder="MAR, MARS" spellcheck="false"></label>
<label>Check the repos with GitHub account <select name="github_account_id" required>{options(github, chosen, "Choose…")}</select></label>
<p class="hint">Your Jira account must see these Jira projects, and this GitHub account must be able to push to
  every repo. Only you can change the project later; members bring their own accounts.</p>
<span class="label">Repos</span>
<div id="repos">{repos}</div>
<button type="button" class="secondary" id="add-repo">Add another repo</button>
<details class="more"><summary>Jira workflow and fields</summary>
  <label>Statuses to move an issue through when work starts <input name="start_statuses" value="{v('start_statuses')}" placeholder="In Progress"></label>
  <label>Statuses once the PR is open <input name="review_statuses" value="{v('review_statuses')}" placeholder="In Review"></label>
  <label>Root cause field <input name="root_cause_field" value="{v('root_cause_field')}" placeholder="customfield_10201 (empty: post it as a comment)" spellcheck="false"></label>
</details>
<div class="actions"><button>{"Save project" if project else "Add project"}</button></div>
  </form>
</section>
{f'<section class="card">{webhook_help(project, secret, cfg)}</section>' if project and secret else ""}
{f'<section class="card"><div class="row"><div><h3>Delete project</h3><p class="muted">For every member.</p></div>{delete}</div></section>' if delete else ""}
<template id="repo-template">{repo_fields({})}</template>
<script nonce="{esc(nonce)}">
  const repos = document.getElementById("repos");
  const addRepo = () => repos.appendChild(document.getElementById("repo-template").content.cloneNode(true));
  document.getElementById("add-repo").addEventListener("click", addRepo);
  repos.addEventListener("click", e => {{
    const remove = e.target.closest("[data-remove-repo]");
    if (!remove) return;
    remove.closest("fieldset").remove();
    if (!repos.querySelector("fieldset")) addRepo();  // always at least one to fill in
  }});
</script>
{CONFIRM_SCRIPT.format(nonce=esc(nonce))}""")


def stop_button(run, hidden: str) -> str:
    if run["status"] not in ("queued", "running"):
        return ""
    return (f'<form method="post" action="/stop" class="stop">{hidden}'
            f'<input type="hidden" name="run_id" value="{esc(run["id"])}"><button class="danger small">Stop</button></form>')


def alert(message: str, kind: str = "error") -> str:
    if not message:
        return ""
    return f'<p class="alert {kind}" role="{"alert" if kind == "error" else "status"}">{esc(message)}</p>'


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{esc(title)}</title>
<style>
{STYLE}</style></head>
<body><main>{body}</main></body></html>"""
