"""The developer's page: a sidebar with the sections (Overview, run issues, recent runs, live logs,
projects, accounts, settings, activity, features) and who is signed in, and beside it their status and the
section shown. Every section is in the page, so switching is instant; ?tab= picks the one shown, so
links, reloads and the page after a form all land on the right one, with scripts off too."""
from ..config import Config
from ..models.overview import Overview
from ..models.project import Project
from ..models.run import RunPage
from ..models.user import User
from .accounts import accounts_panel
from .activity import activity_panel
from .components import alert, app_scripts, csrf_field, esc, page
from .features import features_panel
from .layout import TABS, automation_state, page_head, shell, sidebar
from .logs import logs_panel
from .overview import needs_count, overview_panel
from .projects import projects_panel
from .runs import recent_panel, runs_panel
from .settings import settings_panel

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
    "test-sent": ("projects", "Test message sent: look in your Slack direct messages or your inbox."),
    "released": ("projects", "Tickets checked. What was moved, and what is missing, is under the project's status rules."),
    "queued": ("runs", "Queued. Progress shows up under Recent runs."),
    "paused": ("settings", "Automation paused. Nothing runs for you until you resume; a run in progress finishes."),
    "resumed": ("settings", "Automation resumed."),
    "limits": ("settings", "Timing and limits saved. Polls use them from their next round, runs from their start."),
    "runs-deleted": ("recent", "Selected runs removed from Recent runs."),
    "stopping": ("recent", "Stopping the run. Nothing more is pushed or written to Jira; the ticket is left as it was."),
}


def first_tab(user: User) -> str:
    """Where a developer starts: the next setup step, or their runs once set up."""
    if not (user.github_accounts and user.claude_accounts):
        return "accounts"
    return "overview" if user.ready_projects else "projects"


def home_page(cfg: Config, user: User, projects: list[Project], secrets: dict[int, str], runs: RunPage, csrf: str,
              nonce: str, theme: str = "", tab: str = "", error: str = "", notice: str = "", answering=None,
              claude_usage: dict[int, tuple[str, bool]] | None = None,
              activity: tuple[list[tuple[float, str, str]], int, int] = ([], 1, 1), overview: Overview | None = None) -> str:
    """`tab`: the one shown (a key of TABS; anything else means first_tab). `error` and `notice`
    show at the top of that tab. `answering`: a run whose questions the Run issues tab's form answers.
    `claude_usage`: AccountService.claude_usage, per Claude account. `activity`: a page of UserRepository.activity.
    `overview`: OverviewService.of the developer."""
    overview = overview or Overview()
    claude_usage = claude_usage or {}
    hidden = csrf_field(csrf)
    if tab not in dict(TABS):
        tab = first_tab(user)
    joined = [p for p in projects if p.id in user.subscriptions]
    panels = {
        "overview": page_head("Overview", (), "", _status(user)) + overview_panel(user, overview),
        "runs": runs_panel(user.limits.max_parallel_runs, joined, csrf, answering),
        "recent": recent_panel(runs, csrf),
        "logs": logs_panel(),
        "projects": projects_panel(cfg, user, projects, secrets, hidden, claude_usage),
        "accounts": accounts_panel(user, hidden, claude_usage),
        "settings": settings_panel(user, hidden),
        "activity": activity_panel(*activity),
        "features": features_panel(),
    }
    sections = "".join(
        f'<section id="panel-{key}" role="tabpanel" aria-labelledby="tab-{key}"{"" if key == tab else " hidden"}>'
        f'{alert(error) + alert(notice, "notice") if key == tab else ""}{panels[key]}</section>'
        for key, _ in TABS)
    side = sidebar(user, hidden, tab, needs_count(user, overview), tabs=True)
    return page("ShadowDev", shell(side, sections) + app_scripts(nonce), theme, app=True)


def _status(user: User) -> str:
    """Overview's line under its title: whether automation works for the developer, in a sentence
    (the sidebar says it in a word, layout.automation_state)."""
    kind, _, sentence, _ = automation_state(user)
    return f'<span class="state-line {kind}">{esc(sentence)}</span>'
