"""Sign in with Jira (Atlassian OAuth), connect another Jira account, and sign out."""
import hmac
import secrets

from ..config import OAUTH_CALLBACK_PATH
from ..repositories.sessions import SESSION_SECONDS
from ..services.errors import UserError
from ..views.auth import continue_page, login_page
from .request import COOKIE, FormPost, Request, form_action
from .settings import render

# Ties the OAuth callback to the browser that started it, and says what it is for:
# "signin.<state>" or "connect.<state>" (connect another Jira account to the signed-in developer).
STATE_COOKIE = "shadowdev_oauth_state"
STATE_SECONDS = 600


def _atlassian_url(req: Request, intent: str) -> tuple[str, str]:
    """Atlassian's consent page, and the cookie remembering its one-time state."""
    state = secrets.token_urlsafe(24)
    return (req.app.oauth.authorize_url(state),
            req.set_cookie(STATE_COOKIE, f"{intent}.{state}", STATE_SECONDS, OAUTH_CALLBACK_PATH))


def start_sign_in(req: Request) -> None:
    """Send the browser to Atlassian to grant access (a link, so a plain redirect)."""
    req.redirect(*_atlassian_url(req, "signin"))


@form_action
def connect_jira(req: Request, post: FormPost) -> None:
    """A POST with the CSRF token, so another site can't start connecting an account to yours.
    Answered with a page that opens Atlassian, not a redirect: browsers apply the page's
    form-action 'self' to a form's redirects, so redirecting to Atlassian would be blocked."""
    url, cookie = _atlassian_url(req, "connect")
    nonce = secrets.token_urlsafe(16)
    req.html(continue_page(url, nonce, req.theme()), nonce, cookies=(cookie,))


def finish_sign_in(req: Request) -> None:
    """Atlassian redirects here after the developer granted (or refused) access."""
    public_url = req.app.cfg.public_url
    intent, _, expected = (req.cookie(STATE_COOKIE) or "").partition(".")
    clear_state = req.set_cookie(STATE_COOKIE, "", 0, OAUTH_CALLBACK_PATH)
    session = req.session() if intent == "connect" else None
    user = req.app.users.get(session[1]) if session else None

    def fail(message: str) -> None:
        if user:
            return render(req, user, session[2], tab="accounts", error=message, status=400, cookies=(clear_state,))
        nonce = secrets.token_urlsafe(16)
        req.html(login_page(public_url, nonce, req.theme(), message), nonce, status=400, cookies=(clear_state,))

    if req.arg("error"):
        return fail("Jira access was not granted.")
    if (not expected or intent not in ("signin", "connect") or not hmac.compare_digest(req.arg("state").encode(), expected.encode())
            or not req.arg("code") or (intent == "connect" and not user)):
        return fail(f"The sign-in expired or was started elsewhere. Open {public_url} and sign in again.")
    try:
        user_id = req.app.accounts.complete_jira(req.arg("code"), user)
    except UserError as e:
        return fail(str(e))
    if user:
        return req.redirect(public_url + "/?ok=jira", clear_state)
    req.redirect(public_url + "/", clear_state, req.set_cookie(COOKIE, req.app.sessions.create(user_id), SESSION_SECONDS))


@form_action
def sign_out(req: Request, post: FormPost) -> None:
    req.app.sessions.end(post.session_token)
    req.redirect("/", req.set_cookie(COOKIE, "", 0))


ROUTES = {
    ("GET", "/login/jira"): start_sign_in,
    ("POST", "/jira/connect"): connect_jira,
    ("GET", OAUTH_CALLBACK_PATH): finish_sign_in,
    ("POST", "/logout"): sign_out,
}
