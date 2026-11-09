import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

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
    max_fix_attempts: int
    host: str
    port: int
    max_parallel_runs: int = 5  # issues (or groups fixed together) worked on at the same time
    sandbox_tools: tuple[str, ...] = SANDBOX_TOOLS
    sandbox_caches: tuple[str, ...] = SANDBOX_CACHES
    data_dir: Path = PROJECT_ROOT / "data"  # database and per-developer Claude config; never in a sandbox
    worktree_root: Path = PROJECT_ROOT / "worktrees"

    @property
    def oauth_redirect_uri(self) -> str:
        return self.public_url + OAUTH_CALLBACK_PATH

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
        max_fix_attempts = int(env.get("MAX_FIX_ATTEMPTS", "3"))
        if max_fix_attempts < 1:
            raise SystemExit("MAX_FIX_ATTEMPTS must be at least 1")
        port = int(env.get("PORT", "8080"))
        max_parallel_runs = int(env.get("MAX_PARALLEL_RUNS", "5"))
        if not 1 <= max_parallel_runs <= 20:
            raise SystemExit("MAX_PARALLEL_RUNS must be between 1 and 20")
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
            max_fix_attempts=max_fix_attempts,
            host=env.get("HOST", "127.0.0.1"),
            port=port,
            max_parallel_runs=max_parallel_runs,
            sandbox_tools=_home_paths(env.get("SANDBOX_HOME_TOOLS"), SANDBOX_TOOLS, "SANDBOX_HOME_TOOLS"),
            sandbox_caches=_home_paths(env.get("SANDBOX_HOME_CACHES"), SANDBOX_CACHES, "SANDBOX_HOME_CACHES"),
        )
