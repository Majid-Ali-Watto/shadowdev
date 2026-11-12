"""The developer's page: a header, their status, and a tab per area (runs, projects, accounts, settings).
Every tab is in the page, so switching is instant; ?tab= picks the one shown, so links, reloads and
the page after a form all land on the right one, with scripts off too."""
from ..config import Config
from ..models.project import Project
from ..models.run import RunPage
from ..models.user import User
from .accounts import accounts_panel
from .components import MARK, THEME_TOGGLE, alert, csrf_field, esc, page, plural, script
from .projects import projects_panel
from .runs import runs_panel
from .settings import settings_panel

TABS = (("runs", "Runs"), ("projects", "Projects"), ("accounts", "Accounts"), ("settings", "Settings"))

# Success messages are fixed strings chosen by key, so nothing from the request is echoed.
# Each names the tab its form is on, where the page opens after it (or with its error).
NOTICES = {
    "jira": ("accounts", "Jira account connected."),
    "jira-removed": ("accounts", "Jira account removed, with the projects you ran with it."),
    "github": ("accounts", "GitHub account saved."),
    "github-removed": ("accounts", "GitHub account removed. Projects that used it need another one."),
    "claude": ("accounts", "Claude account saved."),
    "claude-removed": ("accounts", "Claude account removed. Projects that used it need another one."),
    "slack": ("accounts", "Slack account saved."),
    "slack-removed": ("accounts", "Slack account removed. Nothing is posted to Slack or read there for you any more."),
    "project": ("projects", "Project saved."),
    "project-deleted": ("projects", "Project deleted."),
    "joined": ("projects", "Project settings saved."),
    "left": ("projects", "You left the project."),
    "released": ("projects", "Tickets checked. What was moved, and what is missing, is under the project's status rules."),
    "queued": ("runs", "Queued. Progress shows up under Recent runs."),
    "paused": ("settings", "Automation paused. Nothing runs for you until you resume; a run in progress finishes."),
    "resumed": ("settings", "Automation resumed."),
    "limits": ("settings", "Timing and limits saved. Polls use them from their next round, runs from their start."),
    "runs-deleted": ("runs", "Selected runs removed from Recent runs."),
    "stopping": ("runs", "Stopping the run. Nothing more is pushed or written to Jira; the ticket is left as it was."),
}


def first_tab(user: User) -> str:
    """Where a developer starts: the next setup step, or their runs once set up."""
    if not (user.github_accounts and user.claude_accounts):
        return "accounts"
    return "runs" if user.ready_projects else "projects"


def home_page(cfg: Config, user: User, projects: list[Project], secrets: dict[int, str], runs: RunPage, csrf: str,
              nonce: str, theme: str = "", tab: str = "", error: str = "", notice: str = "", answering=None) -> str:
    """`tab`: the one shown (a key of TABS; anything else means first_tab). `error` and `notice`
    show at the top of that tab. `answering`: a run whose questions the Runs tab's form answers."""
    hidden = csrf_field(csrf)
    if tab not in dict(TABS):
        tab = first_tab(user)
    joined = [p for p in projects if p.id in user.subscriptions]
    panels = {
        "runs": runs_panel(user.limits.max_parallel_runs, joined, runs, csrf, answering),
        "projects": projects_panel(cfg, user, projects, secrets, hidden),
        "accounts": accounts_panel(user, hidden),
        "settings": settings_panel(user, hidden),
    }
    nav = "".join(
        f'<a href="/?tab={key}" id="tab-{key}" role="tab" aria-controls="panel-{key}" data-tab="{key}"'
        f' aria-selected="{"true" if key == tab else "false"}" tabindex="{0 if key == tab else -1}">{label}</a>'
        for key, label in TABS)
    sections = "".join(
        f'<section id="panel-{key}" role="tabpanel" aria-labelledby="tab-{key}"{"" if key == tab else " hidden"}>'
        f'{alert(error) + alert(notice, "notice") if key == tab else ""}{panels[key]}</section>'
        for key, _ in TABS)
    return page("ShadowDev", f"""
<header class="top">
  <div class="brand">{MARK}<div><h1>ShadowDev</h1>
    <p class="muted">Signed in as {esc(user.display_name)}{f" ({esc(user.email)})" if user.email else ""}</p></div></div>
  <div class="header-actions">{THEME_TOGGLE}
    <form method="post" action="/logout">{hidden}<button class="secondary">Sign out</button></form></div>
</header>
{_status(user)}
<nav class="tabs" role="tablist" aria-label="Sections">{nav}</nav>
{sections}
{script("theme.js", nonce)}{script("tabs.js", nonce)}{script("confirm.js", nonce)}{script("runs.js", nonce)}""", theme)


def _status(user: User) -> str:
    ready = user.ready_projects
    if user.paused:
        text = "Paused: nothing runs for you until you resume. Your settings are kept."
    elif not ready:
        text = "Not active yet: connect GitHub and Claude, and join a project with them."
    else:
        text = f"Active in {plural(len(ready), 'project')}: issues assigned to you there are worked on automatically."
    return f'<p class="status {"ok" if ready and not user.paused else "warn"}" role="status">{esc(text)}</p>'
