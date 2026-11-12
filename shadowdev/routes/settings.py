"""The developer's page: their accounts, projects, recent runs, and the actions on them."""
import secrets
from collections.abc import Callable

from ..models.run import RunFilter
from ..models.user import User
from ..services.errors import UserError
from ..views.auth import login_page
from ..views.home import NOTICES, home_page
from ..views.runs import run_results
from .request import COOKIE, FormPost, Request, form_action


def render(req: Request, user: User, csrf: str, tab: str = "", error: str = "", notice: str = "", status: int = 200,
           cookies: tuple[str, ...] = (), answering=None, run_filter: RunFilter | None = None) -> None:
    nonce = secrets.token_urlsafe(16)
    projects = req.app.project_service.visible(user)
    hooks = {p.id: req.app.project_service.webhook_secret(user, p) for p in projects if p.owner_id == user.id}
    page = home_page(req.app.cfg, user, projects, hooks, req.app.runs.search(user.id, run_filter or RunFilter()), csrf, nonce,
                     theme=req.theme(), tab=tab, error=error, notice=notice, answering=answering)
    req.html(page, nonce, status=status, cookies=cookies)


def show(req: Request) -> None:
    session = req.session()
    user = req.app.users.get(session[1]) if session else None
    if not user:
        nonce = secrets.token_urlsafe(16)
        return req.html(login_page(req.app.cfg.public_url, nonce, req.theme()), nonce)
    tab, notice = NOTICES.get(req.arg("ok"), ("", ""))
    # ?answer=<run>: the form filled in to answer the questions that run asked (only the developer's own).
    answer = req.arg("answer")
    run = req.app.runs.get(user.id, int(answer)) if answer.isdigit() else None
    answering = run if run and run["status"] == "question" else None
    render(req, user, session[2], tab="runs" if answering else req.arg("tab") or tab, notice=notice, answering=answering,
           run_filter=_run_filter(req))


def runs(req: Request) -> None:
    """Recent runs, as the address's search, filters and page find them: the page fetches it to keep
    itself up to date and to search without reloading."""
    session = req.session(renew=False)
    if not session:
        return req.json(401, "signed out")
    req.html(run_results(req.app.runs.search(session[1], _run_filter(req)), session[2]))


def _run_filter(req: Request) -> RunFilter:
    return RunFilter.of(req.arg("q"), req.arg("status"), req.arg("project"), req.arg("kind"), req.arg("page"))


def action(notice: str, do: Callable[[Request, FormPost], None]) -> Callable[[Request], None]:
    """A form on the page: on success back to the page with `notice` (a key of home.NOTICES), which
    opens the form's tab; a UserError shows that tab again with the error."""
    @form_action
    def route(req: Request, post: FormPost) -> None:
        try:
            do(req, post)
        except UserError as e:
            return render(req, post.user, post.csrf, tab=NOTICES[notice][0], error=str(e), status=400)
        req.redirect(f"/?ok={notice}")
    return route


@form_action
def delete_account(req: Request, post: FormPost) -> None:
    req.app.accounts.delete_account(post.user)
    req.redirect("/", req.set_cookie(COOKIE, "", 0))


ROUTES = {
    ("GET", "/"): show,
    ("GET", "/runs"): runs,
    ("POST", "/jira/remove"): action("jira-removed", lambda req, p: req.app.accounts.remove_jira(
        p.user, p.form.get("jira_account_id", ""))),
    ("POST", "/github"): action("github", lambda req, p: req.app.accounts.connect_github(
        p.user, p.form.get("github_token", ""), p.form.get("github_label", ""))),
    ("POST", "/github/remove"): action("github-removed", lambda req, p: req.app.accounts.remove_github(
        p.user, p.form.get("github_account_id", ""))),
    ("POST", "/claude"): action("claude", lambda req, p: req.app.accounts.connect_claude(
        p.user, p.form.get("claude_token", ""), p.form.get("claude_label", ""))),
    ("POST", "/claude/remove"): action("claude-removed", lambda req, p: req.app.accounts.remove_claude(
        p.user, p.form.get("claude_account_id", ""))),
    ("POST", "/slack"): action("slack", lambda req, p: req.app.accounts.connect_slack(p.user, p.form.get("slack_token", ""))),
    ("POST", "/slack/remove"): action("slack-removed", lambda req, p: req.app.accounts.remove_slack(p.user)),
    ("POST", "/run"): action("queued", lambda req, p: req.app.run_service.request(
        p.user, p.form.get("issue_keys", ""), together=p.form.get("grouping") == "together",
        project_id=p.form.get("project_id", ""), note=p.form.get("note", ""))),
    ("POST", "/runs/delete"): action("runs-deleted", lambda req, p: req.app.run_service.delete(
        p.user, p.lists.get("run_id", []))),
    ("POST", "/stop"): action("stopping", lambda req, p: req.app.run_service.stop(p.user, p.form.get("run_id", ""))),
    ("POST", "/pause"): action("paused", lambda req, p: req.app.accounts.set_paused(p.user, True)),
    ("POST", "/resume"): action("resumed", lambda req, p: req.app.accounts.set_paused(p.user, False)),
    ("POST", "/limits"): action("limits", lambda req, p: req.app.accounts.set_limits(p.user, p.form)),
    ("POST", "/delete-account"): delete_account,
}
