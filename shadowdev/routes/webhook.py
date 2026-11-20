"""What other machines call: the Jira webhook (/webhook?project=<id> per project, or the shared
/webhook), and /health for a reverse proxy or monitor."""
from .request import Request, Route, parse_id

MAX_BODY_BYTES = 1_000_000
WEBHOOK_LIMIT = 600  # per client and minute: a Jira site sends every issue event of its projects
HEALTH_LIMIT = 60  # per client and minute: a monitor asks every few seconds at most


def receive(req: Request) -> None:
    length = req.content_length()
    if not 0 < length <= MAX_BODY_BYTES:
        return req.json(413 if length else 400, "bad body size")
    project = req.arg("project")
    project_id = parse_id(project) if project else None
    if project and project_id is None:
        return req.json(401, "bad signature")  # as for an unknown project: IDs can't be probed
    status, message = req.app.webhooks.handle(req.read_body(length), req.headers.get("X-Hub-Signature"), project_id)
    req.json(status, message)


def health(req: Request) -> None:
    """200 while the server answers and its database can be read. Says nothing else."""
    req.app.runs.ping()
    req.json(200, "ok")


ROUTES = {
    ("POST", "/webhook"): Route(receive, anonymous=True, limit=WEBHOOK_LIMIT),
    ("GET", "/health"): Route(health, anonymous=True, limit=HEALTH_LIMIT),
}
