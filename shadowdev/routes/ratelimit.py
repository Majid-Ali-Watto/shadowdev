"""A limit on the requests one client may send to the routes anyone can call without signing in:
starting a sign-in, the OAuth callback, and the webhook. Counted per client address and route,
in memory, per minute."""
import threading
import time

from ..config import OAUTH_CALLBACK_PATH

WINDOW_SECONDS = 60
# Requests per client in a window. A Jira site sends every issue event of its projects to /webhook.
LIMITS = {"/login/jira": 30, OAUTH_CALLBACK_PATH: 30, "/webhook": 600}
MAX_CLIENTS = 10_000  # counters kept; past it, those of windows gone by are dropped


class RateLimiter:
    def __init__(self, limits: dict[str, int] | None = None):
        self.limits = LIMITS if limits is None else limits
        self._counts: dict[tuple[str, str, int], int] = {}  # (client, path, window) -> requests
        self._lock = threading.Lock()

    def retry_after(self, client: str, path: str, now: float | None = None) -> int:
        """Count a request. 0 when it may go on, else the seconds until the client may try again."""
        limit = self.limits.get(path)
        if limit is None:
            return 0
        now = time.time() if now is None else now
        window = int(now // WINDOW_SECONDS)
        with self._lock:
            if len(self._counts) >= MAX_CLIENTS:
                self._counts = {key: n for key, n in self._counts.items() if key[2] == window}
            key = (client, path, window)
            if len(self._counts) >= MAX_CLIENTS and key not in self._counts:
                return WINDOW_SECONDS  # full of this window's clients: no room to count another
            count = self._counts[key] = self._counts.get(key, 0) + 1
        return 0 if count <= limit else max(1, int((window + 1) * WINDOW_SECONDS - now))
