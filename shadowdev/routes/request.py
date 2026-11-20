"""One HTTP request: reading it, and sending the response with the right security headers."""
import hmac
import json
import re
import select
from collections.abc import Callable
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from ..app import App
from ..models.user import User
from ..repositories.sessions import SESSION_SECONDS
from ..views.components import THEMES

MAX_FORM_BYTES = 64_000
COOKIE = "shadowdev_session"
THEME_COOKIE = "shadowdev_theme"  # set by the page's theme switcher (views/static/theme.js)
LOOPBACK = ("localhost", "127.0.0.1")  # the names a developer on this machine may open the server by
_ID = re.compile(r"[0-9]{1,18}")  # ASCII digits only: str.isdigit() accepts "²", and int() then fails


def parse_id(text: str) -> int | None:
    """A database ID as a form or URL sends it, or None: only ASCII digits, so int() can't fail."""
    return int(text) if _ID.fullmatch(text) else None


def id_text(text: str) -> str:
    """`text` when it is a database ID, else "": what services get for an ID field, since they look
    IDs up by their digits and a service that is handed "²" would fail (as a 500)."""
    return text if parse_id(text) is not None else ""


class RequestCut(ConnectionError):
    """The request ended before all of it arrived: the client stopped sending, or went away."""


@dataclass(frozen=True)
class Route:
    """One entry of routes.ROUTES: its handler, and what anyone may do with it.

    `anonymous`: the route does work for someone who has not signed in (the login page, the sign-in,
    the webhook, health). Such a route must have a `limit`: the requests one client may make to its
    path in a minute (routes/ratelimit.py). A limit may also be set on a signed-in route that sends
    messages out."""
    handler: Callable[["Request"], None]
    anonymous: bool = False
    limit: int | None = None


class Request:
    def __init__(self, handler: BaseHTTPRequestHandler, app: App):
        self.handler = handler
        self.app = app
        url = urlsplit(handler.path)
        self.path = url.path
        self.query = parse_qs(url.query)
        self.headers = handler.headers
        self._https = app.cfg.public_url.startswith("https://")
        self._secure = "; Secure" if self._https else ""
        self.sent = False  # a response went out
        self._renewed: list[tuple[str, str]] = []  # the session cookie again, when the session was renewed

    def arg(self, name: str) -> str:
        return (self.query.get(name) or [""])[0]

    def client(self) -> str:
        """The address the request came from. Behind a reverse proxy or tunnel on this machine,
        the one it added last to X-Forwarded-For; a client reaching the server directly can't
        make up its own."""
        peer = self.handler.client_address[0]
        forwarded = (self.headers.get("X-Forwarded-For") or "").rsplit(",", 1)[-1].strip()
        return forwarded if forwarded and peer in ("127.0.0.1", "::1") else peer

    # Reading

    def cookie(self, name: str) -> str | None:
        morsel = SimpleCookie(self.headers.get("Cookie") or "").get(name)
        return morsel.value if morsel else None

    def theme(self) -> str:
        """The theme the developer chose, or "" to follow the system's setting."""
        value = self.cookie(THEME_COOKIE)
        return value if value in THEMES else ""

    def session(self, renew: bool = True) -> tuple[str, int, str] | None:
        """(cookie token, user ID, csrf) of the browser's live session. Using it renews it,
        unless `renew` is off (a page refreshing itself is not the developer using it)."""
        token = self.cookie(COOKIE)
        found = self.app.sessions.get(token) if token else None
        if found and renew and not self._renewed and self.app.sessions.renew(token):
            self._renewed.append(("Set-Cookie", self.set_cookie(COOKIE, token, SESSION_SECONDS)))
        return (token, *found) if found else None

    def content_length(self) -> int:
        """-1 when it is not a number."""
        length = parse_id(self.headers.get("Content-Length") or "0")
        return -1 if length is None else length

    def read_body(self, length: int) -> bytes:
        """The body, all `length` bytes of it. RequestCut when the client sent less."""
        body = self.handler.rfile.read(length)
        if len(body) != length:
            raise RequestCut("the request body ended early")
        return body

    def read_form(self) -> dict[str, list[str]] | None:
        length = self.content_length()
        if not 0 <= length <= MAX_FORM_BYTES:
            return None
        # Blank values are kept: the project form's repo fields are matched up by position.
        return parse_qs(self.read_body(length).decode(errors="replace"), keep_blank_values=True, max_num_fields=400)

    def same_origin(self) -> bool:
        """The form came from ShadowDev's own page: its PUBLIC_URL origin. Host is not trusted (a proxy
        may rewrite it, and a page on another site can send any Host it likes). On this machine, the
        same server by either loopback name (make dev, a browser on 127.0.0.1) counts too."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # browsers send Origin on every form post, so a request without one is not a form's
        public = urlsplit(self.app.cfg.public_url)
        allowed = {self.app.cfg.public_url}
        if public.hostname in LOOPBACK:
            port = f":{public.port}" if public.port else ""
            allowed |= {f"{public.scheme}://{name}{port}" for name in LOOPBACK}
        return origin.rstrip("/") in allowed

    # Responding

    def set_cookie(self, name: str, value: str, max_age: int, path: str = "/") -> str:
        """A Set-Cookie value. Lax, not Strict: the browser must send it when Atlassian redirects
        back to the callback. Cross-site form posts are still refused (Lax, CSRF tokens, Origin check)."""
        return f"{name}={value}; Path={path}; HttpOnly; SameSite=Lax; Max-Age={max_age}{self._secure}"

    def send(self, status: int, body: bytes, content_type: str, headers: list[tuple[str, str]] = ()) -> None:
        h = self.handler
        self.sent = True
        h.send_response(status)
        h.send_header("Content-Type", content_type)
        h.send_header("Content-Length", str(len(body)))
        h.send_header("X-Content-Type-Options", "nosniff")
        if self._https:
            h.send_header("Strict-Transport-Security", "max-age=31536000")
        for name, value in [*self._renewed, *headers]:  # a route's own cookie (signing out) comes last, and wins
            h.send_header(name, value)
        h.end_headers()
        h.wfile.write(body)

    def json(self, status: int, message: str, headers: list[tuple[str, str]] = ()) -> None:
        self.send(status, json.dumps({"status": message}).encode(), "application/json", headers)

    def text(self, text: str) -> None:
        """Plain text, never read as anything else (nosniff), and not kept by the browser."""
        self.send(200, text.encode(), "text/plain; charset=utf-8", [("Cache-Control", "no-store")])

    def html(self, html: str, nonce: str | None = None, status: int = 200, cookies: tuple[str, ...] = ()) -> None:
        """A page whose scripts carry `nonce`; without one (a fragment fetched into a page), no script at all."""
        if nonce == "":
            raise ValueError("a page's script nonce is never empty: pass None for a page without scripts")
        scripts = f"'nonce-{nonce}'" if nonce else "'none'"
        headers = [
            ("Content-Security-Policy", f"default-src 'none'; style-src 'unsafe-inline'; script-src {scripts}; connect-src 'self'; "
                                        "img-src data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
            ("Referrer-Policy", "same-origin"),  # no-referrer would make browsers send Origin: null on form posts
            ("Cache-Control", "no-store"),
            *(("Set-Cookie", c) for c in cookies),
        ]
        self.send(status, html.encode(), "text/html; charset=utf-8", headers)

    def events(self) -> None:
        """Start a stream of server-sent events (text/event-stream): no length, sent as it is written
        with write(), and the connection closes when the route returns."""
        h = self.handler
        self.sent = True
        h.send_response(200)
        for name, value in [("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-store"),
                            ("X-Content-Type-Options", "nosniff"), ("X-Accel-Buffering", "no"),  # nginx: don't hold it back
                            ("Connection", "close"), *self._renewed]:
            h.send_header(name, value)
        if self._https:
            h.send_header("Strict-Transport-Security", "max-age=31536000")
        h.end_headers()
        h.wfile.flush()

    def gone(self, seconds: float) -> bool:
        """Wait up to `seconds` for the browser to close a stream started with events(): True once it
        has. It sends nothing after its request, so anything to read means it closed (or misbehaves)."""
        readable, _, _ = select.select([self.handler.connection], [], [], seconds)
        return bool(readable)

    def write(self, data: bytes) -> None:
        """More of a stream started with events(). Raises OSError once the browser has gone."""
        self.handler.wfile.write(data)
        self.handler.wfile.flush()

    def redirect(self, location: str, *cookies: str) -> None:
        headers = [("Location", location), ("Cache-Control", "no-store"), *(("Set-Cookie", c) for c in cookies)]
        self.send(303, b"", "text/plain", headers)


@dataclass
class FormPost:
    """A form posted from the developer's page by a signed-in developer."""
    user: User
    form: dict[str, str]  # each field's last value
    lists: dict[str, list[str]]  # every value of each field
    session_token: str
    csrf: str


def form_action(action: Callable[[Request, FormPost], None]) -> Callable[[Request], None]:
    """Guards a POST route of the developer's page: same origin, a live session, and its CSRF token."""
    def route(req: Request) -> None:
        if not req.same_origin():
            return req.json(403, "cross-origin request")
        lists = req.read_form()
        if lists is None:
            return req.json(413, "form too large")
        form = {k: v[-1] for k, v in lists.items()}
        session = req.session()
        if not session or not hmac.compare_digest(form.get("csrf", "").encode(), session[2].encode()):
            return req.redirect("/")  # expired session or forged form
        token, user_id, csrf = session
        user = req.app.users.get(user_id)
        if not user:
            return req.redirect("/")
        action(req, FormPost(user, form, lists, token, csrf))
    return route
