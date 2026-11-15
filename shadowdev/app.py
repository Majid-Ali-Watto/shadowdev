"""Wires the layers together: repositories over one database, and the services using them."""
import threading
from dataclasses import dataclass, field

from .clients.atlassian import AtlassianOAuth
from .config import ANY_SITE, LEGACY_PROJECTS_DIR, Config
from .repositories.database import Database
from .repositories.held import HeldRepository
from .repositories.projects import ProjectRepository
from .repositories.reviews import ReviewRepository
from .repositories.runs import RunRepository
from .repositories.sessions import SessionRepository
from .repositories.users import UserRepository
from .services.accounts import AccountService
from .services.notifications import NotificationTests
from .services.outbox import Outbox
from .services.overview import OverviewService
from .services.poller import Poller
from .services.privacy import PrivacyReporter
from .services.projects import ProjectService
from .services.releases import ReleaseMover
from .services.reviews import ReviewWatcher
from .services.run_queue import RunQueue
from .services.runs import RunService
from .services.webhook import WebhookService


@dataclass
class App:
    cfg: Config
    oauth: AtlassianOAuth
    users: UserRepository
    projects: ProjectRepository
    sessions: SessionRepository
    runs: RunRepository
    queue: RunQueue
    accounts: AccountService
    project_service: ProjectService
    run_service: RunService
    webhooks: WebhookService
    privacy: PrivacyReporter
    poller: Poller
    releases: ReleaseMover
    reviews: ReviewRepository
    watcher: ReviewWatcher
    notification_tests: NotificationTests
    outbox: Outbox
    overview: OverviewService
    stopping: threading.Event = field(default_factory=threading.Event)  # set when the server stops: streams end


def database(cfg: Config) -> Database:
    # A v1 database had one Jira site (JIRA_BASE_URL, now the first of JIRA_SITES) and project files.
    legacy_site = cfg.jira_sites[0] if cfg.jira_sites != (ANY_SITE,) else None
    return Database(cfg.data_dir / "shadowdev.db", cfg.secret_key, LEGACY_PROJECTS_DIR, legacy_site)


def build_app(cfg: Config, oauth: AtlassianOAuth | None = None, queue: RunQueue | None = None) -> App:
    """`oauth` and `queue` default to the real ones; tests pass stand-ins."""
    db = database(cfg)
    users, projects, sessions, runs = UserRepository(db), ProjectRepository(db), SessionRepository(db), RunRepository(db)
    oauth = oauth or AtlassianOAuth(cfg.atlassian_client_id, cfg.atlassian_client_secret, cfg.oauth_redirect_uri)
    reviews, held = ReviewRepository(db), HeldRepository(db)
    queue = queue or RunQueue(cfg, users, projects, runs, oauth, reviews, held=held)
    accounts = AccountService(cfg, users, oauth, queue)
    return App(
        cfg=cfg, oauth=oauth, users=users, projects=projects, sessions=sessions, runs=runs, queue=queue,
        accounts=accounts,
        project_service=ProjectService(users, projects, oauth, queue),
        run_service=RunService(users, projects, oauth, queue, runs),
        webhooks=WebhookService(cfg, users, projects, runs, queue),
        privacy=PrivacyReporter(users, oauth, accounts),
        poller=Poller(users, projects, oauth, queue), releases=ReleaseMover(cfg, users, projects, oauth),
        reviews=reviews, watcher=ReviewWatcher(cfg, users, projects, reviews, queue, held),
        notification_tests=NotificationTests(cfg, users, projects),
        outbox=Outbox(users, projects, runs, reviews, held, oauth),
        overview=OverviewService(cfg, runs, reviews, held, projects),
    )
