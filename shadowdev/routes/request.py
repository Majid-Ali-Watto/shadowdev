"""One HTTP request: reading it, and sending the response with the right security headers."""
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from ..app import App
from ..models.user import User
from ..repositories.sessions import SESSION_SECONDS

MAX_FORM_BYTES = 64_000
COOKIE = "shadowdev_session"


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

    # Reading

    def cookie(self, name: str) -> str | None:
        morsel = SimpleCookie(self.headers.get("Cookie") or "").get(name)
        return morsel.value if morsel else None

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
        value = self.headers.get("Content-Length") or "0"
        return int(value) if value.isdigit() else -1

    def read_body(self, length: int) -> bytes:
        return self.handler.rfile.read(length)

    def read_form(self) -> dict[str, list[str]] | None:
        length = self.content_length()
        if not 0 <= length <= MAX_FORM_BYTES:
            return None
        # Blank values are kept: the project form's repo fields are matched up by position.
        return parse_qs(self.read_body(length).decode(errors="replace"), keep_blank_values=True, max_num_fields=400)

    def same_origin(self) -> bool:
        """The form came from ShadowDev's own pages: as published (PUBLIC_URL, also behind a proxy
        that rewrites Host), or as reached directly."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        netloc = urlsplit(origin).netloc
        return bool(netloc) and (origin.rstrip("/") == self.app.cfg.public_url or netloc == self.headers.get("Host"))

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

    def json(self, status: int, message: str) -> None:
        self.send(status, json.dumps({"status": message}).encode(), "application/json")

    def html(self, html: str, nonce: str = "", status: int = 200, cookies: tuple[str, ...] = ()) -> None:
        headers = [
            ("Content-Security-Policy", f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; connect-src 'self'; "
                                        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
            ("Referrer-Policy", "same-origin"),  # no-referrer would make browsers send Origin: null on form posts
            ("Cache-Control", "no-store"),
            *(("Set-Cookie", c) for c in cookies),
        ]
        self.send(status, html.encode(), "text/html; charset=utf-8", headers)

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
