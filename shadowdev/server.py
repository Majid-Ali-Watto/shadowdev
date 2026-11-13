"""HTTP server: dispatches each request to its route (see routes/), and starts the run queue."""
import logging
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app import App, build_app
from .config import Config
from .routes import ROUTES
from .routes.ratelimit import RateLimiter
from .routes.request import Request

log = logging.getLogger("shadowdev")

REQUEST_TIMEOUT_SECONDS = 20  # a client that sends nothing for this long is dropped (slowloris)
MAX_CONNECTIONS = 64  # handled at once; more wait in the listen backlog instead of each getting a thread
DRAIN_SECONDS = 15 * 60  # what the runs in progress get to finish when the server is stopped


class Server(ThreadingHTTPServer):
    """A thread per connection, but at most MAX_CONNECTIONS at once, so a flood of connections
    can't exhaust the server's threads and memory."""
    daemon_threads = True

    def __init__(self, address, handler, max_connections: int = MAX_CONNECTIONS):
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(max_connections)

    def process_request(self, request, client_address):
        self._slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_handler(app: App, limiter: RateLimiter | None = None) -> type[BaseHTTPRequestHandler]:
    limiter = limiter or RateLimiter()

    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_SECONDS

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            req = Request(self, app)
            route = ROUTES.get((method, req.path))
            if not route:
                return req.json(404, "not found")
            wait = limiter.retry_after(req.client(), req.path)
            if wait:
                return req.json(429, "too many requests", [("Retry-After", str(wait))])
            try:
                route(req)
            except Exception:
                log.exception("%s %s failed", method, req.path)
                if not req.sent:
                    req.json(500, "internal error")

        def log_message(self, fmt, *args):  # route access logs through logging
            log.debug("%s - " + fmt, self.address_string(), *args)

    return Handler


def serve(cfg: Config) -> None:
    app = build_app(cfg)
    app.queue.start()  # resumes the runs that were queued when the server stopped
    app.privacy.start()  # Atlassian's personal data reporting
    app.watcher.start()  # review comments on its PRs, and review requests in Slack
    app.poller.start()  # Jira, for the developers who turned polling on in Settings
    app.releases.start()  # finished tickets moved by their projects' status rules, for those who switched it on
    log.info("Jira sites: %s", ", ".join(cfg.jira_sites))
    server = Server((cfg.host, cfg.port), make_handler(app))
    log.info("listening on http://%s:%d; developers open %s (Jira webhook at /webhook)", cfg.host, cfg.port, cfg.public_url)
    # Stopped by SIGTERM (systemd, `make down`) or Ctrl+C: nothing new is taken, the runs in progress
    # finish, and what is queued starts again with the server. A second signal stops those runs now.
    stop, hurry = threading.Event(), threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: (hurry if stop.is_set() else stop).set())
    threading.Thread(target=server.serve_forever, daemon=True, name="http").start()
    stop.wait()
    server.shutdown()  # Jira sends again the events it could not deliver
    server.server_close()
    stopped = app.queue.drain(DRAIN_SECONDS, hurry)
    log.info("stopped%s", f"; {stopped} run(s) in progress were stopped" if stopped else "")
