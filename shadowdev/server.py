"""HTTP server: dispatches each request to its route (see routes/), and starts the run queue."""
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app import App, build_app
from .config import Config
from .routes import ROUTES
from .routes.request import Request

log = logging.getLogger("shadowdev")

REQUEST_TIMEOUT_SECONDS = 20  # a client that sends nothing for this long is dropped (slowloris)
MAX_CONNECTIONS = 64  # handled at once; more wait in the listen backlog instead of each getting a thread


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


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
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
    log.info("Jira sites: %s", ", ".join(cfg.jira_sites))
    server = Server((cfg.host, cfg.port), make_handler(app))
    log.info("up to %d runs in parallel; listening on http://%s:%d; developers open %s (Jira webhook at /webhook)",
             cfg.max_parallel_runs, cfg.host, cfg.port, cfg.public_url)
    server.serve_forever()
