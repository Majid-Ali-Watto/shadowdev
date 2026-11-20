"""A limit on the requests one client may send to the routes that carry a `limit` (routes.ROUTES:
the ones anyone can call without signing in, and those that send messages out). Counted per client
address and path, in memory, per minute. The limits come from the route table, so they can't drift
from the routes."""
import threading
import time

WINDOW_SECONDS = 60
MAX_CLIENTS = 10_000  # counters kept in one minute; past it, a client that is not counted yet is refused


class RateLimiter:
    def __init__(self, limits: dict[str, int]):
        self.limits = limits  # path -> requests per client in a minute
        self._counts: dict[tuple[str, str], int] = {}  # (client, path) -> requests, in the window below
        self._window = -1  # the minute the counts are for: they start again with each minute
        self._lock = threading.Lock()

    def retry_after(self, client: str, path: str, now: float | None = None) -> int:
        """Count a request. 0 when it may go on, else the seconds until the client may try again."""
        limit = self.limits.get(path)
        if limit is None:
            return 0
        now = time.time() if now is None else now
        window = int(now // WINDOW_SECONDS)
        with self._lock:
            if window > self._window:  # a new minute: the last one's counts are dropped, once, here
                self._window, self._counts = window, {}
            key = (client, path)
            if len(self._counts) >= MAX_CLIENTS and key not in self._counts:
                return WINDOW_SECONDS  # full of this minute's clients: no room to count another
            count = self._counts[key] = self._counts.get(key, 0) + 1
        return 0 if count <= limit else max(1, int((window + 1) * WINDOW_SECONDS - now))
