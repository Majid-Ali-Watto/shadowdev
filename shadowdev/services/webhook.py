"""Jira webhook events: verified, deduplicated, and queued for the developer the issue is assigned to.

Each project has its own webhook URL (/webhook?project=<id>) and secret, so a Jira site's admin
can only send events for the projects on their site. The shared /webhook, signed with
WEBHOOK_SECRET, finds the project from the site in the event (issue.self) and the issue key."""
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import urlsplit

from ..automation import rules
from ..config import Config
from ..models.project import Project
from ..repositories.projects import ProjectRepository
from ..repositories.runs import RunRepository
from ..repositories.users import UserRepository
from .run_queue import RunQueue

log = logging.getLogger("shadowdev")

WEBHOOK_MAX_AGE_MS = 60 * 60 * 1000  # older signed events are refused, so a captured one can't be replayed later
HANDLED_EVENTS = {"jira:issue_created", "jira:issue_updated"}


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Jira Cloud webhooks with a secret send `X-Hub-Signature: sha256=<hex hmac>`."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def site_of(issue: dict) -> str | None:
    """https://acme.atlassian.net from the issue's REST URL."""
    url = urlsplit(str(issue.get("self") or ""))
    return f"https://{url.netloc}" if url.scheme == "https" and url.netloc else None


def matching_issue(payload: dict, projects: ProjectRepository, users: UserRepository,
                   project: Project | None = None) -> tuple[str, Project, int] | None:
    """(issue key, its project, developer ID) when the assignee is a ShadowDev developer who
    joined that project with that Jira account, and automates this issue type there."""
    if payload.get("webhookEvent") not in HANDLED_EVENTS:
        return None
    issue = payload.get("issue") or {}
    key = issue.get("key") or ""
    if not isinstance(key, str) or not rules.ISSUE_KEY_RE.match(key):
        return None
    if project is None:
        site = site_of(issue)
        cloud_id = projects.site_for_url(site) if site else None
        project = projects.for_issue(cloud_id, key) if cloud_id else None
    if not project or not project.handles(key):
        return None
    fields = issue.get("fields") or {}
    account_id = rules.assignee(fields)
    user_id = users.owner_of(account_id) if isinstance(account_id, str) else None
    user = users.get(user_id) if user_id else None
    actor = user.actor(project.id) if user and not user.paused else None
    if not actor or actor.jira_account_id != account_id or not rules.wanted(rules.issue_kind(fields), actor.mode):
        return None
    return key, project, user.id


class WebhookService:
    def __init__(self, cfg: Config, users: UserRepository, projects: ProjectRepository, runs: RunRepository,
                 queue: RunQueue):
        self.cfg = cfg
        self.users = users
        self.projects = projects
        self.runs = runs
        self.queue = queue

    def handle(self, body: bytes, signature: str | None, project_param: str = "") -> tuple[int, str]:
        """(HTTP status, message) for a webhook request body, its X-Hub-Signature and the
        `project` query parameter of its URL."""
        project = None
        if project_param:
            project = self.projects.get(int(project_param)) if project_param.isdigit() else None
            secret = self.projects.webhook_secret(project.id) if project else None
        else:
            secret = self.cfg.webhook_secret
        if not secret or not verify_signature(secret, body, signature):
            return 401, "bad signature"  # also for an unknown project, so IDs can't be probed
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return 400, "invalid json"
        if not isinstance(payload, dict):
            return 400, "invalid json"
        # Jira signs the event's timestamp too: refuse old events and ones already seen.
        stamp = payload.get("timestamp")
        now = int(time.time() * 1000)
        if not isinstance(stamp, int) or not now - WEBHOOK_MAX_AGE_MS < stamp < now + 5 * 60 * 1000:
            return 400, "stale or missing timestamp"
        event_id = hashlib.sha256(json.dumps(
            [project_param, payload.get("webhookEvent"), (payload.get("issue") or {}).get("key"), stamp]).encode()).hexdigest()
        if not self.runs.first_time(event_id, stamp, now - WEBHOOK_MAX_AGE_MS):
            return 200, "duplicate event"
        match = matching_issue(payload, self.projects, self.users, project)
        if not match:
            return 200, "ignored"
        key, project, user_id = match
        refusal = self.queue.submit(project, [key], user_id)
        if refusal:
            return 200, refusal
        log.info("%s: queued for %s", key, project.name)
        return 202, "queued"
