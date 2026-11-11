"""Git, as the developer: every remote operation (list branches, clone, push) uses their GitHub
token, handed to git by a credential helper from the environment, so it never appears in argv,
URLs or git config. The server keeps no clones: each run clones what it needs and deletes it.

Git runs outside the sandbox, in clones whose files came from Claude and from code it wrote.
So every command ignores hooks, fsmonitor and any global or system git config, and gets an
environment without secrets. (The clone's own .git is read-only inside the sandbox, so its
config cannot be changed either.)"""
import os
import shutil
import subprocess
from pathlib import Path

TOKEN_ENV = "SHADOWDEV_GIT_TOKEN"  # noqa: S105 (a variable's name, not a secret)
# Answers only for https://github.com, so a rewritten URL can never send the token elsewhere.
CREDENTIAL_HELPER = (
    '!f() { test "$1" = get || exit 0; h=; p=; '
    'while IFS= read -r l && [ -n "$l" ]; do case "$l" in host=*) h="${l#host=}";; protocol=*) p="${l#protocol=}";; esac; done; '
    f'[ "$p" = https ] && [ "$h" = github.com ] && printf "username=x-access-token\\npassword=%s\\n" "${TOKEN_ENV}"; }}; f'
)
SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "protocol.ext.allow=never",
        "-c", "core.sshCommand=false", "-c", "credential.helper="]


def _env(**extra: str) -> dict:
    kept = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
    return {**kept, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", **extra}


def _run(cmd: list[str], cwd: Path | None = None, **env: str) -> str:
    proc = subprocess.run(["git", *SAFE, *cmd], cwd=cwd, env=_env(**env), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(cmd[:2])} … failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def git(cwd: Path, *args: str) -> str:
    return _run(list(args), cwd)


def _remote(token: str, *args: str, cwd: Path | None = None) -> str:
    """A git command that talks to GitHub, authenticated with the developer's token."""
    return _run(["-c", f"credential.helper={CREDENTIAL_HELPER}", *args], cwd, **{TOKEN_ENV: token})


def repo_url(owner_repo: str) -> str:
    return f"https://github.com/{owner_repo}.git"


def remote_branches(owner_repo: str, token: str, prefix: str = "") -> list[str]:
    pattern = [f"refs/heads/{prefix}*"] if prefix else []
    out = _remote(token, "ls-remote", "--heads", repo_url(owner_repo), *pattern)
    return [line.split("refs/heads/", 1)[1] for line in out.splitlines() if "refs/heads/" in line]


def remote_branch_exists(owner_repo: str, token: str, branch: str) -> bool:
    return bool(remote_branch_sha(owner_repo, token, branch))


def remote_branch_sha(owner_repo: str, token: str, branch: str) -> str | None:
    """The commit the branch is at on GitHub; None when there is no such branch."""
    out = _remote(token, "ls-remote", "--heads", repo_url(owner_repo), f"refs/heads/{branch}")
    return next((line.split("\t")[0] for line in out.splitlines() if line.endswith(f"refs/heads/{branch}")), None)


def default_branch(owner_repo: str, token: str) -> str | None:
    """The branch the repo's HEAD points to on GitHub, e.g. main."""
    out = _remote(token, "ls-remote", "--symref", repo_url(owner_repo), "HEAD")
    first = out.splitlines()[0] if out else ""
    return first.split("refs/heads/", 1)[1].split("\t")[0] if first.startswith("ref: refs/heads/") else None


def valid_branch_name(name: str) -> bool:
    return subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True, env=_env()).returncode == 0


def clone(owner_repo: str, branch: str, dest: Path, token: str) -> str:
    """Shallow clone of one branch (just its latest commit); returns that commit."""
    _remote(token, "clone", "--quiet", "--depth", "1", "--single-branch", "--no-tags", "--branch", branch,
            repo_url(owner_repo), str(dest))
    return git(dest, "rev-parse", "HEAD")


def commit_all(worktree: Path, message: str, name: str, email: str) -> None:
    """Commit everything as the developer."""
    git(worktree, "add", "-A")
    _run(["commit", "--no-verify", "-m", message], worktree,
         GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)


def push(worktree: Path, owner_repo: str, branch: str, token: str) -> None:
    _remote(token, "push", repo_url(owner_repo), f"HEAD:refs/heads/{branch}", cwd=worktree)


def snapshot(worktree: Path, index_file: Path) -> str:
    """Tree ID of every file a commit would take (tracked and new, not ignored), using a scratch
    index so the clone's own index is untouched. Seeded from it, so unchanged files aren't re-hashed."""
    real = worktree / ".git" / "index"
    if real.exists():
        shutil.copyfile(real, index_file)
    _run(["add", "-A"], worktree, GIT_INDEX_FILE=str(index_file))
    return _run(["write-tree"], worktree, GIT_INDEX_FILE=str(index_file))


def changed_files(worktree: Path, tree_a: str, tree_b: str) -> list[str]:
    return git(worktree, "diff-tree", "-r", "--name-only", tree_a, tree_b).splitlines() if tree_a != tree_b else []
