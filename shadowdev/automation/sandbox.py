"""The bubblewrap sandbox for everything that handles untrusted input: the repos' setup and
test commands (they run code Claude wrote from ticket text) and Claude itself.

Built as an allowlist. Inside, a process sees:
- the system read-only (/usr, /etc, ...), with a private /tmp, /dev and /proc, and an empty
  /run, which hides the D-Bus/systemd user sockets, the docker socket and SSH agents
- an empty home directory, with only the configured toolchains (read-only) and package
  caches (writable) mounted back
- its own run's folders, writable, except each clone's .git (read-only, so no hooks or git
  config can be planted for the git commands ShadowDev runs outside the sandbox)
- none of ShadowDev's files: not the .env, the token database or other runs' code
- an allowlisted environment, so no secrets
The network stays available (npm, uv, services like Redis on localhost); with nothing else
visible, a run can only send out what its developer can read anyway.
"""
import os
from pathlib import Path

from ..config import PROJECT_ROOT, Config

# Read by tools; never secrets. Everything else in the environment stays outside.
ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "NVM_DIR", "NVM_BIN", "NVM_INC")


def env(home: Path, **extra: str) -> dict:
    kept = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    return {**kept, "HOME": str(home), "TMPDIR": "/tmp", "CI": "1", **extra}


def run_dirs(cfg: Config, cwd: Path) -> list[Path]:
    """The folders of the run a command in `cwd` belongs to: its workspace (worktrees/MAR-1)
    and scratch (worktrees/MAR-1.shadowdev). Empty when cwd is not inside a run."""
    try:
        top = cwd.resolve().relative_to(cfg.worktree_root.resolve()).parts[0]
    except (ValueError, IndexError):
        return []
    stem = top.removesuffix(".shadowdev")
    return [cfg.worktree_root / stem, cfg.worktree_root / f"{stem}.shadowdev"]


def git_dirs(workspace: Path) -> list[Path]:
    """The .git directories of a run's clones (worktrees/MAR-1/<repo>/.git)."""
    return sorted(p for p in workspace.glob("*/.git") if p.is_dir()) if workspace.is_dir() else []


def wrap(argv: list[str], cfg: Config, writable: list[Path], readonly: list[Path] = ()) -> list[str]:
    """argv running `argv` in the sandbox, with `writable` (and the configured caches) as the
    only writable paths, and `readonly` mounted read-only on top (e.g. .git inside them)."""
    home = Path.home()
    args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", str(home),
            "--unshare-pid", "--unshare-ipc", "--die-with-parent", "--new-session"]
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
    for rel in cfg.sandbox_caches:
        (home / rel).mkdir(parents=True, exist_ok=True)
        args += ["--bind", str(home / rel), str(home / rel)]
    for path in writable:
        if path.exists():
            args += ["--bind", str(path), str(path)]
    for path in readonly:
        if path.exists():
            args += ["--ro-bind", str(path), str(path)]
    return [*args, "--", *argv]


def wrap_command(command: str, cfg: Config, cwd: Path) -> list[str]:
    """A project setup/test command (shell) in the sandbox: its run's folders are writable,
    their clones' .git read-only."""
    dirs = run_dirs(cfg, cwd)
    readonly = [g for d in dirs for g in git_dirs(d)]
    return wrap(["sh", "-c", command], cfg, dirs, readonly)
