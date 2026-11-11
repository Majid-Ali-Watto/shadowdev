import logging
import os
import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from .models.limits import Limits

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Where v1 kept its project files; read once, when a v1 database is upgraded.
LEGACY_PROJECTS_DIR = PROJECT_ROOT / "projects"
ANY_SITE = "*"


def read_settings(dotenv: Path) -> dict[str, str]:
    """Settings from the .env file (KEY=VALUE lines), overridden by real environment variables.
    Returned, never put into os.environ: secrets there would be inherited by every process
    ShadowDev starts, including commands running code that Claude wrote."""
    values = {}
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return {**values, **os.environ}


OAUTH_CALLBACK_PATH = "/oauth/jira/callback"
# Under $HOME, mounted into the sandbox: toolchains read-only, package caches writable.
# Everything else in $HOME (keys, browser profiles, other repos) stays invisible.
SANDBOX_TOOLS = (".local/share/nvm", ".nvm", ".local/bin", ".local/share/uv", ".local/share/claude", ".cargo/bin",
                 ".rustup", ".pyenv", ".volta", ".bun", ".local/share/pnpm", ".deno")
SANDBOX_CACHES = (".npm", ".cache/uv", ".cache/pip", ".cache/node-gyp", ".cache/pnpm", ".cache/ms-playwright")


def _home_paths(value: str | None, default: tuple[str, ...], name: str) -> tuple[str, ...]:
    if value is None:
        return default
    paths = tuple(p.strip() for p in value.split(",") if p.strip())
    for p in paths:
        if p.startswith("/") or ".." in Path(p).parts:
            raise SystemExit(f"{name}: {p!r} must be a path inside the home directory, like .local/share/nvm")
    return paths


# Once .env settings, now each developer's own (models.limits, on the Settings tab).
MOVED_TO_SETTINGS = ("JIRA_POLLING", "JIRA_POLL_MINUTES", "REVIEW_POLL_SECONDS", "REVIEW_QUIET_SECONDS",
                     "CLAUDE_TIMEOUT_MINUTES", "COMMAND_TIMEOUT_MINUTES", "MAX_PARALLEL_RUNS", "MAX_FIX_ATTEMPTS")
_DEFAULT = Limits()


def _site_urls(value: str) -> tuple[str, ...]:
    if value.strip() == ANY_SITE:
        return (ANY_SITE,)
    urls = tuple(u.strip().rstrip("/") for u in value.split(",") if u.strip())
    for url in urls:
        if not re.match(r"^https://[A-Za-z0-9.-]+\Z", url):
            raise SystemExit(f"JIRA_SITES: {url!r} must look like https://acme.atlassian.net")
    if not urls:
        raise SystemExit("JIRA_SITES must list at least one Jira site")
    return urls


@dataclass(frozen=True)
class Config:
    # Jira sites developers may sign in with and run projects on, e.g. the office and a personal
    # one; ("*",) allows any site, so anyone with an Atlassian account can use the server.
    jira_sites: tuple[str, ...]
    atlassian_client_id: str  # the ShadowDev OAuth app (developer.atlassian.com)
    atlassian_client_secret: str
    public_url: str  # where developers open ShadowDev; the OAuth callback is registered under it
    webhook_secret: str | None  # optional: the shared secret of the /webhook endpoint (projects have their own)
    secret_key: str  # Fernet key that encrypts developers' tokens in the database
    host: str
    port: int
    # A run's limits: the developer's own (models.limits), set for each run by RunQueue.
    max_fix_attempts: int = _DEFAULT.max_fix_attempts
    claude_timeout_seconds: int = round(_DEFAULT.claude_timeout_minutes * 60)
    command_timeout_seconds: int = round(_DEFAULT.command_timeout_minutes * 60)
    sandbox_tools: tuple[str, ...] = SANDBOX_TOOLS
    sandbox_caches: tuple[str, ...] = SANDBOX_CACHES
    data_dir: Path = PROJECT_ROOT / "data"  # database and per-developer Claude config; never in a sandbox
    worktree_root: Path = PROJECT_ROOT / "worktrees"

    @property
    def oauth_redirect_uri(self) -> str:
        return self.public_url + OAUTH_CALLBACK_PATH

    def for_run(self, limits: Limits) -> "Config":
        """This config with a developer's own limits, for one of their runs."""
        return replace(self, max_fix_attempts=limits.max_fix_attempts,
                       claude_timeout_seconds=round(limits.claude_timeout_minutes * 60),
                       command_timeout_seconds=round(limits.command_timeout_minutes * 60))

    def allows_site(self, url: str) -> bool:
        return self.jira_sites == (ANY_SITE,) or url.rstrip("/") in self.jira_sites

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "Config":
        def required(name: str) -> str:
            value = env.get(name)
            if not value:
                raise SystemExit(f"Missing required environment variable: {name}")
            return value

        for tool in ("git", "claude", "bwrap", "prlimit"):
            if not shutil.which(tool):
                raise SystemExit(f"{tool} is not installed or not on PATH")
        moved = [name for name in MOVED_TO_SETTINGS if name in env]
        if moved:
            logging.getLogger("shadowdev").warning(
                "%s: now set by each developer on the Settings tab; remove from .env", ", ".join(moved))
        port = int(env.get("PORT", "8080"))
        public_url = (env.get("PUBLIC_URL") or f"http://localhost:{port}").rstrip("/")
        if not re.match(r"^https?://[^/]+\Z", public_url):
            raise SystemExit("PUBLIC_URL must look like https://shadowdev.example.com (no path)")
        local = re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?\Z", public_url)
        if not public_url.startswith("https://") and not local:
            raise SystemExit("PUBLIC_URL must be https:// (session cookies would travel unencrypted); "
                             "plain http is only allowed for localhost")
        sites = env.get("JIRA_SITES") or env.get("JIRA_BASE_URL")  # JIRA_BASE_URL: the v1 name, one site
        if not sites:
            raise SystemExit("Missing required environment variable: JIRA_SITES (e.g. https://acme.atlassian.net)")
        return cls(
            jira_sites=_site_urls(sites),
            atlassian_client_id=required("ATLASSIAN_CLIENT_ID"),
            atlassian_client_secret=required("ATLASSIAN_CLIENT_SECRET"),
            public_url=public_url,
            webhook_secret=env.get("WEBHOOK_SECRET") or None,
            secret_key=required("SHADOWDEV_SECRET_KEY"),
            host=env.get("HOST", "127.0.0.1"),
            port=port,
            sandbox_tools=_home_paths(env.get("SANDBOX_HOME_TOOLS"), SANDBOX_TOOLS, "SANDBOX_HOME_TOOLS"),
            sandbox_caches=_home_paths(env.get("SANDBOX_HOME_CACHES"), SANDBOX_CACHES, "SANDBOX_HOME_CACHES"),
        )
