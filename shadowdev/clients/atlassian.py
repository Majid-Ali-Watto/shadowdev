"""Atlassian OAuth 2.0 (3LO): developers sign in by granting ShadowDev access to Jira in the
browser, instead of creating API tokens. The company registers ShadowDev once as an OAuth
app in the Atlassian developer console (see README).

https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/
"""
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlencode

from ..models.tokens import Tokens
from ..models.user import Site
from .jira import Jira
from .rest import HTTPError, request, request_json

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"  # noqa: S105 (a URL, not a secret)
API = "https://api.atlassian.com"
# read/write issues, comments, fields and versions; read the signed-in user; refresh tokens.
SCOPES = ("read:jira-work", "write:jira-work", "read:jira-user", "offline_access")
REFRESH_MARGIN_SECONDS = 120
REPORT_URL = f"{API}/app/report-accounts/"
MAX_REPORTED_ACCOUNTS = 90  # per request


class JiraAccessLost(RuntimeError):
    """The developer's grant expired or was revoked; they must sign in again."""


def jira_api_base(cloud_id: str) -> str:
    return f"{API}/ex/jira/{cloud_id}"


class AtlassianOAuth:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def authorize_url(self, state: str) -> str:
        """Where the browser goes to grant access. Atlassian redirects back to redirect_uri
        with ?code=...&state=... (or ?error=access_denied)."""
        return AUTHORIZE_URL + "?" + urlencode({
            "audience": "api.atlassian.com",
            "client_id": self.client_id,
            "scope": " ".join(SCOPES),
            "redirect_uri": self.redirect_uri,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
        })

    def _grant(self, grant: dict) -> Tokens:
        data = request_json("POST", TOKEN_URL, {},
                            {"client_id": self.client_id, "client_secret": self.client_secret, **grant}, "Atlassian")
        if not data.get("refresh_token"):
            raise RuntimeError("Atlassian returned no refresh token (is offline_access allowed for the app?)")
        return Tokens(data["access_token"], data["refresh_token"], time.time() + int(data.get("expires_in", 3600)))

    def exchange(self, code: str) -> Tokens:
        return self._grant({"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri})

    def refresh(self, refresh_token: str) -> Tokens:
        return self._grant({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def sites(self, access_token: str) -> list[Site]:
        """The Jira sites the grant reaches. One Atlassian account can reach several."""
        resources = request_json("GET", f"{API}/oauth/token/accessible-resources",
                                 {"Authorization": f"Bearer {access_token}"}, label="Atlassian") or []
        return [Site(r["id"], r["url"].rstrip("/"), r.get("name") or r["url"]) for r in resources
                if r.get("id") and r.get("url") and "read:jira-work" in (r.get("scopes") or ["read:jira-work"])]


_refresh_locks: dict[int, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def access_token(oauth: AtlassianOAuth, users, jira_account_id: int) -> Callable[[], str]:
    """The access token of one of a developer's Jira accounts, as a function. `users`
    (UserRepository) keeps its tokens (jira_tokens/save_jira_tokens/mark_jira_lost). An expired access token is
    refreshed on use and the rotated refresh token saved. Each refresh token works once, so
    refreshes for one account happen one at a time (its sites share the grant)."""
    with _refresh_locks_guard:
        lock = _refresh_locks.setdefault(jira_account_id, threading.Lock())

    def access_token() -> str:
        tokens = users.jira_tokens(jira_account_id)
        if tokens.expires_at - REFRESH_MARGIN_SECONDS > time.time():
            return tokens.access_token
        with lock:
            tokens = users.jira_tokens(jira_account_id)  # another thread may have refreshed meanwhile
            if tokens.expires_at - REFRESH_MARGIN_SECONDS <= time.time():
                try:
                    tokens = oauth.refresh(tokens.refresh_token)
                except HTTPError as e:
                    if e.status in (400, 401, 403):
                        users.mark_jira_lost(jira_account_id)  # shown on the developer's page
                        raise JiraAccessLost("Jira access expired or was revoked: sign in to ShadowDev again") from None
                    raise
                users.save_jira_tokens(jira_account_id, tokens)
            return tokens.access_token

    return access_token


def jira_client(oauth: AtlassianOAuth, users, jira_account_id: int, cloud_id: str) -> Jira:
    """Jira on one site, acting as one of a developer's Jira accounts."""
    return Jira(jira_api_base(cloud_id), access_token(oauth, users, jira_account_id))


def rfc3339(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def report_accounts(token: str, accounts: list[tuple[str, float]]) -> tuple[list[dict], int | None]:
    """Atlassian's personal data reporting: which of these accounts (account ID, when their personal
    data was fetched) were closed or changed since. Returns ([{accountId, status: closed|updated}],
    the cycle period in seconds if Atlassian sent one). Raises HTTPError (429: see retry_after).
    https://developer.atlassian.com/cloud/jira/platform/user-privacy-developer-guide/"""
    body = {"accounts": [{"accountId": a, "updatedAt": rfc3339(at)} for a, at in accounts]}
    data, headers = request("POST", REPORT_URL, {"Authorization": f"Bearer {token}"}, body, "Atlassian")
    cycle = (headers.get("Cycle-Period") or "").strip()
    return list((data or {}).get("accounts") or []), int(cycle) if cycle.isdigit() and int(cycle) > 0 else None
