"""The Jira webhook: /webhook?project=<id> per project, or the shared /webhook."""
from .request import Request

MAX_BODY_BYTES = 1_000_000


def receive(req: Request) -> None:
    length = req.content_length()
    if not 0 < length <= MAX_BODY_BYTES:
        return req.json(413 if length else 400, "bad body size")
    status, message = req.app.webhooks.handle(req.read_body(length), req.headers.get("X-Hub-Signature"),
                                              req.arg("project"))
    req.json(status, message)


ROUTES = {
    ("POST", "/webhook"): receive,
}
