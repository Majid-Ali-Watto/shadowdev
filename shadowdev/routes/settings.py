"""The developer's page: their accounts, projects, recent runs, and the actions on them."""
import json
import secrets
import threading
import time
from collections import Counter
from collections.abc import Callable

from ..models.run import RunFilter
from ..models.user import User
from ..services.errors import UserError
from ..views.auth import login_page
from ..views.home import NOTICES, home_page
from ..views.overview import overview_panel
from ..views.runs import run_results
from .request import COOKIE, FormPost, Request, Route, form_action, id_text, parse_id

STREAM_SECONDS = 300  # a live update stream ends after this, and the browser opens another
CHECK_SECONDS = 1  # how often a stream looks for changes
BEAT_SECONDS = 15  # with no change for this long, it sends a comment
MAX_STREAMS = 16  # of all developers at once
MAX_LIVE_RUNS = 50  # runs one Live logs request may say it has seen; more are ignored
MAX_STREAMS_EACH = 3  # of one developer (their open tabs): browsers allow 6 connections to a server
PAGE_LIMIT = 120  # per client and minute: the page, which anyone gets (the login page) and every app swap loads


def render(req: Request, user: User, csrf: str, tab: str = "", error: str = "", notice: str = "", status: int = 200,
           cookies: tuple[str, ...] = (), answering=None, run_filter: RunFilter | None = None) -> None:
    nonce = secrets.token_urlsafe(16)
    projects = req.app.project_service.visible(user)
    hooks = {p.id: req.app.project_service.webhook_secret(user, p) for p in projects if p.owner_id == user.id}
    activity = req.app.users.activity(user.id, _page(req.arg("activity_page")))
    page = home_page(req.app.cfg, user, projects, hooks, req.app.runs.search(user.id, run_filter or RunFilter()), csrf, nonce,
                     theme=req.theme(), tab=tab, error=error, notice=notice, answering=answering,
                     claude_usage=req.app.accounts.claude_usage(user), activity=activity,
                     overview=req.app.overview.of(user))
    req.html(page, nonce, status=status, cookies=cookies)


def show(req: Request) -> None:
    session = req.session()
    user = req.app.users.get(session[1]) if session else None
    if not user:
        nonce = secrets.token_urlsafe(16)
        return req.html(login_page(req.app.cfg.public_url, nonce, req.theme()), nonce)
    tab, notice = NOTICES.get(req.arg("ok"), ("", ""))
    # ?answer=<run>: the form filled in to answer the questions that run asked (only the developer's own).
    run_id = parse_id(req.arg("answer"))
    run = req.app.runs.get(user.id, run_id) if run_id is not None else None
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


def overview(req: Request) -> None:
    """The Overview tab's contents: the page fetches it as its runs change, to keep itself up to date."""
    session = req.session(renew=False)
    user = req.app.users.get(session[1]) if session else None
    if not user:
        return req.json(401, "signed out")
    req.html(overview_panel(user, req.app.overview.of(user)))


def live_logs(req: Request) -> None:
    """Live logs: what the developer's working runs logged since the page last asked. `seen` says how
    many lines of each run it has: "12:340,15:20" (run id: lines)."""
    session = req.session(renew=False)
    if not session:
        return req.json(401, "signed out")
    seen = {}
    for pair in req.arg("seen").split(",")[:MAX_LIVE_RUNS]:
        run, _, count = pair.partition(":")
        run_id, lines = parse_id(run), parse_id(count)
        if run_id is not None and lines is not None:
            seen[run_id] = lines
    body = json.dumps({"runs": req.app.run_service.live(session[1], seen)}).encode()
    req.send(200, body, "application/json", [("Cache-Control", "no-store")])


def run_events(req: Request) -> None:
    """Live updates of Recent runs, as server-sent events: `runs` whenever one of the developer's runs
    is added, changes (its status, step, message) or is removed, for the page to fetch GET /runs then.
    A stream ends as soon as the browser closes it (the page was left or replaced), after
    STREAM_SECONDS, when the session ends, and when the server stops; the browser then opens another
    (and falls back to asking every few seconds when it is refused)."""
    session = req.session(renew=False)
    if not session:
        return req.json(401, "signed out")
    token, user_id, _ = session
    if not STREAMS.open(user_id):
        return req.json(503, "too many live updates", [("Retry-After", "60")])
    try:
        req.events()
        req.write(b"retry: 5000\n\n")
        seen, said = req.app.runs.version(user_id), time.monotonic()
        ends = said + STREAM_SECONDS
        while (not req.gone(CHECK_SECONDS) and not req.app.stopping.is_set() and time.monotonic() < ends
               and req.app.sessions.get(token)):
            now = req.app.runs.version(user_id)
            if now != seen:
                seen, said = now, time.monotonic()
                req.write(b"event: runs\ndata: changed\n\n")
            elif time.monotonic() - said >= BEAT_SECONDS:  # finds a browser that has gone, and keeps proxies from closing it
                said = time.monotonic()
                req.write(b": still here\n\n")
    except OSError:
        pass  # the browser went away
    finally:
        STREAMS.close(user_id)


class Streams:
    """The live update streams open: each holds one of the server's connections (server.MAX_CONNECTIONS)
    for as long as its page is open, so there are at most MAX_STREAMS, MAX_STREAMS_EACH per developer."""

    def __init__(self):
        self._open: Counter = Counter()
        self._lock = threading.Lock()

    def open(self, user_id: int) -> bool:
        with self._lock:
            if self._open.total() >= MAX_STREAMS or self._open[user_id] >= MAX_STREAMS_EACH:
                return False
            self._open[user_id] += 1
            return True

    def close(self, user_id: int) -> None:
        with self._lock:
            self._open[user_id] -= 1
            if self._open[user_id] <= 0:
                del self._open[user_id]


STREAMS = Streams()


def run_log(req: Request) -> None:
    """What was logged while one of the developer's own runs worked (so far, while it works), as plain text."""
    session = req.session()
    run_id = parse_id(req.arg("run"))
    log = req.app.queue.log(session[1], run_id) if session and run_id is not None else None
    if log is None:
        return req.json(404, "not found")  # also another developer's run, so IDs can't be probed
    req.text(log)


def _page(arg: str) -> int:
    number = parse_id(arg)
    return number if number and number < 10_000 else 1


def _run_filter(req: Request) -> RunFilter:
    # The page number is read by the model as digits too: one that is not is the first page.
    return RunFilter.of(req.arg("q"), req.arg("status"), req.arg("project"), req.arg("kind"),
                        id_text(req.arg("page")) or "1")


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
    ("GET", "/"): Route(show, anonymous=True, limit=PAGE_LIMIT),  # the login page, to anyone
    ("GET", "/runs"): Route(runs),
    ("GET", "/overview"): Route(overview),
    ("GET", "/logs/live"): Route(live_logs),
    ("GET", "/runs/log"): Route(run_log),
    ("GET", "/runs/events"): Route(run_events),
    ("POST", "/jira/remove"): Route(action("jira-removed", lambda req, p: req.app.accounts.remove_jira(
        p.user, id_text(p.form.get("jira_account_id", ""))))),
    ("POST", "/github"): Route(action("github", lambda req, p: req.app.accounts.connect_github(
        p.user, p.form.get("github_token", ""), p.form.get("github_label", "")))),
    ("POST", "/github/remove"): Route(action("github-removed", lambda req, p: req.app.accounts.remove_github(
        p.user, id_text(p.form.get("github_account_id", ""))))),
    ("POST", "/claude"): Route(action("claude", lambda req, p: req.app.accounts.connect_claude(
        p.user, p.form.get("claude_token", ""), p.form.get("claude_label", "")))),
    ("POST", "/claude/remove"): Route(action("claude-removed", lambda req, p: req.app.accounts.remove_claude(
        p.user, id_text(p.form.get("claude_account_id", ""))))),
    ("POST", "/slack"): Route(action("slack", lambda req, p: req.app.accounts.connect_slack(
        p.user, p.form.get("slack_token", "")))),
    ("POST", "/slack/remove"): Route(action("slack-removed", lambda req, p: req.app.accounts.remove_slack(p.user))),
    ("POST", "/run"): Route(action("queued", lambda req, p: req.app.run_service.request(
        p.user, p.form.get("issue_keys", ""), together=p.form.get("grouping") == "together",
        project_id=id_text(p.form.get("project_id", "")), note=p.form.get("note", ""), repos=p.lists.get("repos", []),
        one_message=p.form.get("message") == "one", sequence=p.form.get("grouping") == "sequence",
        delay_minutes=p.form.get("delay", "")))),
    ("POST", "/runs/delete"): Route(action("runs-deleted", lambda req, p: req.app.run_service.delete(
        p.user, [i for i in p.lists.get("run_id", []) if id_text(i)]))),
    ("POST", "/stop"): Route(action("stopping", lambda req, p: req.app.run_service.stop(
        p.user, id_text(p.form.get("run_id", ""))))),
    ("POST", "/pause"): Route(action("paused", lambda req, p: req.app.accounts.set_paused(p.user, True))),
    ("POST", "/resume"): Route(action("resumed", lambda req, p: req.app.accounts.set_paused(p.user, False))),
    ("POST", "/limits"): Route(action("limits", lambda req, p: req.app.accounts.set_limits(p.user, p.form))),
    ("POST", "/delete-account"): Route(delete_account),
}
