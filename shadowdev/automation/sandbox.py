"""The bubblewrap sandbox for everything that handles untrusted input: the repos' setup and
test commands (they run code Claude wrote from ticket text) and Claude itself.

Built as an allowlist. Inside, a process sees:
- the system read-only (/usr, /etc, ...), with a private /tmp, /dev and /proc, and an empty
  /run, which hides the D-Bus/systemd user sockets, the docker socket and SSH agents
- an empty home directory, with only the configured toolchains (read-only) and package
  caches (writable, one set per project) mounted back
- its own run's folders, writable; each clone's git dir read-only, at its own path and at the
  clone's .git (ShadowDev never trusts a .git in a work tree, so this is for tools only)
- none of ShadowDev's files: not the .env, the token database or other runs' code
- an allowlisted environment, so no secrets
- a memory and process cap, when systemd can give one (see limits_prefix), and tmpfs sizes
The network stays available (npm, uv, services like Redis on localhost); with nothing else
visible, a run can only send out what its developer can read anyway.
"""
import functools
import logging
import os
import re
import subprocess
from pathlib import Path

from ..config import PROJECT_ROOT, Config
from . import gitops

log = logging.getLogger("shadowdev")

# Read by tools; never secrets. Everything else in the environment stays outside.
ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "NVM_DIR", "NVM_BIN", "NVM_INC")
# How systemd-run reaches the user's bus, which the limits need, outside the sandbox. Unset inside it.
BUS_ENV = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
MB = 1024**2


def env(home: Path, **extra: str) -> dict:
    kept = {k: os.environ[k] for k in (*ENV_ALLOW, *BUS_ENV) if k in os.environ}
    return {**kept, "HOME": str(home), "TMPDIR": "/tmp", "CI": "1", **extra}  # noqa: S108


def run_dirs(cfg: Config, cwd: Path) -> list[Path]:
    """The folders of the run a command in `cwd` belongs to: its workspace (worktrees/MAR-1) and
    scratch (worktrees/MAR-1.shadowdev). Empty when cwd is not inside a run."""
    try:
        top = cwd.resolve().relative_to(cfg.worktree_root.resolve()).parts[0]
    except (ValueError, IndexError):
        return []
    stem = top.removesuffix(".shadowdev")
    return [cfg.worktree_root / stem, cfg.worktree_root / f"{stem}.shadowdev"]


def cache_key(workspace: Path) -> str | None:
    """The package caches a run shares: its project's, named by the project's ID, which is what the
    workspace's name starts with (worktrees/<project id>-<branch>). None for no run."""
    project_id = workspace.name.split("-", 1)[0]
    return project_id if project_id.isdigit() else None


def git_mounts(workspace: Path) -> list[tuple[Path, Path]]:
    """(git dir, where it is mounted read-only) for the clones of a run: each git dir at its own
    path (a baseline worktree's .git file points there), and at <clone>/.git for tools that use git."""
    gits = gitops.gitdirs_of(workspace)
    if not gits.is_dir():
        return []
    mounts = [(d, d) for d in sorted(gits.iterdir()) if d.is_dir()]
    if workspace.is_dir():
        for clone in sorted(workspace.iterdir()):
            if clone.is_dir() and (gits / clone.name).is_dir():
                mounts.append((gits / clone.name, clone / ".git"))
    return mounts


@functools.cache
def bwrap_supports_size() -> bool:
    """`bwrap --size` (a tmpfs size) came in bubblewrap 0.10; Docker's bookworm has 0.8."""
    try:
        out = subprocess.run(["bwrap", "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    found = re.search(r"(\d+)\.(\d+)", out)
    return bool(found) and (int(found.group(1)), int(found.group(2))) >= (0, 10)


def _tmpfs(path: str, cfg: Config) -> list[str]:
    if cfg.sandbox_tmp_mb and bwrap_supports_size():
        return ["--size", str(cfg.sandbox_tmp_mb * MB), "--tmpfs", path]
    return ["--tmpfs", path]


def wrap(argv: list[str], cfg: Config, writable: list[Path], readonly: list[tuple[Path, Path]] = (),
         cache: str | None = None) -> list[str]:
    """argv running `argv` in the sandbox, with `writable` as the only writable paths (and the project's
    package caches, `cache` being its key), and `readonly` (source, target) mounted read-only on top."""
    home = Path.home()
    args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            *_tmpfs("/tmp", cfg), "--tmpfs", "/run", *_tmpfs(str(home), cfg),  # noqa: S108
            "--unshare-pid", "--unshare-ipc", "--die-with-parent", "--new-session"]
    for name in BUS_ENV:
        args += ["--unsetenv", name]
    resolv = Path("/etc/resolv.conf").resolve()  # e.g. /run/systemd/resolve/stub-resolv.conf
    if resolv.is_relative_to("/run") and resolv.exists():
        args += ["--ro-bind", str(resolv.parent), str(resolv.parent)]
    # ShadowDev's own files, wherever it is installed (also covered by the empty home).
    for path in (cfg.data_dir, PROJECT_ROOT / ".env", cfg.worktree_root):
        if path.is_dir():
            args += ["--tmpfs", str(path)]
        elif path.exists():
            args += ["--ro-bind", "/dev/null", str(path)]
    for rel in cfg.sandbox_tools:
        if (home / rel).exists():
            args += ["--ro-bind", str(home / rel), str(home / rel)]
    if cache:  # per project: one project's commands never write the caches another project's run reads
        for rel in cfg.sandbox_caches:
            host = cfg.cache_root / cache / rel
            host.mkdir(parents=True, exist_ok=True)
            args += ["--bind", str(host), str(home / rel)]
    for path in writable:
        if path.exists():
            args += ["--bind", str(path), str(path)]
    for source, target in readonly:
        if source.exists() and target.exists():
            args += ["--ro-bind", str(source), str(target)]
    return [*args, "--", *argv]


def wrap_command(command: str, cfg: Config, cwd: Path) -> list[str]:
    """A project setup/test command (shell) in the sandbox: its run's folders are writable, and its
    clones' git dirs read-only."""
    dirs = run_dirs(cfg, cwd)
    if not dirs:  # not inside a run's folders: nothing is writable, and there are no clones
        return wrap(["sh", "-c", command], cfg, [])
    workspace = dirs[0]
    return wrap(["sh", "-c", command], cfg, dirs, git_mounts(workspace), cache_key(workspace))


@functools.cache
def _scope_works() -> bool:
    """Whether systemd can run a command in a scope of its own for this user (not in Docker, not
    without a user manager). Checked once."""
    try:
        return subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--", "true"],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def limits_prefix(cfg: Config) -> list[str]:
    """The argv prefix that caps a sandboxed command's memory and processes, through a systemd scope of
    its own (cgroups count per scope, so the server's own threads don't count). Empty when no cap is
    configured or systemd can't give one: nothing else is safe, since RLIMIT_NPROC counts per user."""
    if not (cfg.sandbox_memory_mb or cfg.sandbox_tasks_max) or not _scope_works():
        return []
    props = []
    if cfg.sandbox_memory_mb:
        props += ["-p", f"MemoryMax={cfg.sandbox_memory_mb}M"]
    if cfg.sandbox_tasks_max:
        props += ["-p", f"TasksMax={cfg.sandbox_tasks_max}"]
    return ["systemd-run", "--user", "--scope", "--quiet", *props, "--"]


def probe(cfg: Config) -> None:
    """At startup: one log line saying which limits commands get."""
    if not (cfg.sandbox_memory_mb or cfg.sandbox_tasks_max):
        log.info("sandbox: no memory or process limit for commands (SANDBOX_MEMORY_MB, SANDBOX_TASKS_MAX are 0)")
    elif limits_prefix(cfg):
        log.info("sandbox: commands and Claude are limited to %s MB of memory and %s processes each",
                 cfg.sandbox_memory_mb or "no", cfg.sandbox_tasks_max or "no")
    else:
        log.info("sandbox: systemd gives no per-command scope here, so commands run without a memory or process limit")
    if not bwrap_supports_size():
        log.info("sandbox: this bwrap has no --size: tmpfs sizes are not capped")
