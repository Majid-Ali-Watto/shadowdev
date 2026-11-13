"""What other machines call: the Jira webhook (/webhook?project=<id> per project, or the shared
/webhook), and /health for a reverse proxy or monitor."""
from .request import Request

MAX_BODY_BYTES = 1_000_000


def receive(req: Request) -> None:
    length = req.content_length()
    if not 0 < length <= MAX_BODY_BYTES:
        return req.json(413 if length else 400, "bad body size")
    status, message = req.app.webhooks.handle(req.read_body(length), req.headers.get("X-Hub-Signature"),
                                              req.arg("project"))
    req.json(status, message)


def health(req: Request) -> None:
    """200 while the server answers and its database can be read. Says nothing else."""
    req.app.runs.batches()
    req.json(200, "ok")


ROUTES = {
    ("POST", "/webhook"): receive,
    ("GET", "/health"): health,
}
