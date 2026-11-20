"""Git, as the developer: every remote operation (list branches, clone, push) uses their GitHub
token, handed to git by a credential helper from the environment, so it never appears in argv,
URLs or git config. The server keeps no clones: each run clones what it needs and deletes it.

Git runs outside the sandbox, in work trees whose files came from Claude and from code it wrote.
So every command ignores hooks, fsmonitor and any global or system git config, and gets an
environment without secrets.

A clone's git dir is not in its work tree: it lives in the run's own `<workspace>.git` folder, which
no sandboxed command can write. Every git command here names the git dir and the work tree
explicitly, so a `.git` planted in the work tree (a folder or a file) is never read. Before each
command the work tree is checked to be the same folder as when it was cloned, so a moved or
replaced one stops the run instead of being committed from."""
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Clone:
    """One repo's clone in a run. `path` is its work tree, where Claude and the commands work; `gitdir`
    its git dir, outside every folder a command can write. `identity` is (device, inode) of `path` when
    it was cloned: git runs only while `path` is still that folder."""
    path: Path
    gitdir: Path
    identity: tuple[int, int]

    @property
    def name(self) -> str:
        return self.path.name


def gitdirs_of(workspace: Path) -> Path:
    """The folder that holds a run's git dirs, beside its workspace (worktrees/MAR-1.git/<repo>):
    outside the workspace and the scratch folder, so no sandboxed command can write to them."""
    return workspace.with_name(f"{workspace.name}.git")


def _env(**extra: str) -> dict:
    kept = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
    return {**kept, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", **extra}


def _scoped(clone: Clone, args: list[str]) -> list[str]:
    """`args` as a git command on this clone: its git dir and work tree named, never found."""
    return ["--git-dir", str(clone.gitdir), "--work-tree", str(clone.path), *args]


def _verified(clone: Clone) -> None:
    """Refuse to run git in a work tree that is no longer the folder that was cloned: the sandbox
    may have moved it aside and put another in its place."""
    try:
        st = os.lstat(clone.path)
    except OSError:
        st = None
    if st is None or not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != clone.identity:
        raise RuntimeError(f"{clone.name}: its folder was moved or replaced during the run; nothing is committed "
                           "or pushed from it")


def _proc(cmd: list[str], cwd: Path | None = None, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *SAFE, *cmd], cwd=cwd, env=_env(**env), capture_output=True, text=True)


def _run(cmd: list[str], cwd: Path | None = None, **env: str) -> str:
    proc = _proc(cmd, cwd, **env)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(cmd[:2])} … failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def git(clone: Clone, *args: str, **env: str) -> str:
    """A git command on the clone (its git dir and work tree named explicitly)."""
    _verified(clone)
    return _run(_scoped(clone, list(args)), clone.path, **env)


def _remote(token: str, *args: str, clone: Clone | None = None) -> str:
    """A git command that talks to GitHub, authenticated with the developer's token; on a clone when given."""
    cmd = ["-c", f"credential.helper={CREDENTIAL_HELPER}"]
    cwd = None
    if clone is not None:
        _verified(clone)
        cmd += _scoped(clone, list(args))
        cwd = clone.path
    else:
        cmd += list(args)
    return _run(cmd, cwd, **{TOKEN_ENV: token})


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


def clone(owner_repo: str, branch: str, dest: Path, gitdir: Path, token: str, shallow: bool = True) -> Clone:
    """Clone of one branch into the new folder `dest`, its git dir at the new `gitdir` (outside dest):
    by default shallow (just its latest commit); with `shallow` False its whole history, which a merge
    needs to find where two branches met. `dest/.git` is left as an empty folder: the sandbox mounts
    the git dir there, read-only, for tools that use git."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    gitdir.parent.mkdir(parents=True, exist_ok=True)
    depth = ["--depth", "1"] if shallow else []
    _remote(token, "clone", "--quiet", *depth, "--single-branch", "--no-tags", "--branch", branch,
            "--separate-git-dir", str(gitdir), repo_url(owner_repo), str(dest))
    if (dest / ".git").is_file():  # the gitfile git wrote: not needed, the git dir is named explicitly
        (dest / ".git").unlink()
    (dest / ".git").mkdir()
    st = dest.stat()
    return Clone(dest, gitdir, (st.st_dev, st.st_ino))


def existing(path: Path, gitdir: Path) -> Clone:
    """A clone left in place by a finished run (a dry run keeps its change), as it is now."""
    st = path.stat()
    return Clone(path, gitdir, (st.st_dev, st.st_ino))


def head(clone: Clone) -> str:
    return git(clone, "rev-parse", "HEAD")


def checkout_new(clone: Clone, branch: str) -> None:
    git(clone, "checkout", "--quiet", "-b", branch)


def start_merge(clone: Clone, owner_repo: str, base: str, token: str, name: str, email: str) -> tuple[str, list[str]]:
    """Merge the latest `base` into the clone's branch (a full clone), without committing: the merge
    is left for its conflicts to be resolved, then committed by commit_all. Returns (the commit of
    `base` merged, the files in conflict: none when git merged it all by itself)."""
    _remote(token, "fetch", "--quiet", "--no-tags", repo_url(owner_repo), f"+refs/heads/{base}:refs/remotes/base/{base}",
            clone=clone)
    commit = git(clone, "rev-parse", f"refs/remotes/base/{base}")
    who = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    _verified(clone)
    proc = _proc(["--git-dir", str(clone.gitdir), "--work-tree", str(clone.path), "merge", "--no-commit", "--no-ff",
                  "--no-edit", commit], clone.path, **who)
    conflicted = git(clone, "diff", "--name-only", "--diff-filter=U").splitlines()
    if proc.returncode != 0 and not conflicted:  # not a conflict: unrelated histories, a dirty clone, …
        raise RuntimeError(f"git merge {base} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return commit, conflicted


def commit_all(clone: Clone, message: str, name: str, email: str) -> None:
    """Commit everything as the developer."""
    git(clone, "add", "-A")
    git(clone, "commit", "--no-verify", "-m", message,
        GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)


def push(clone: Clone, owner_repo: str, branch: str, token: str) -> None:
    _remote(token, "push", repo_url(owner_repo), f"HEAD:refs/heads/{branch}", clone=clone)


def snapshot(clone: Clone, index_file: Path) -> str:
    """Tree ID of every file a commit would take (tracked and new, not ignored), using a scratch
    index so the clone's own index is untouched. Seeded from it, so unchanged files aren't re-hashed."""
    real = clone.gitdir / "index"
    if real.exists():
        shutil.copyfile(real, index_file)
    git(clone, "add", "-A", GIT_INDEX_FILE=str(index_file))
    return git(clone, "write-tree", GIT_INDEX_FILE=str(index_file))


def changed_files(clone: Clone, tree_a: str, tree_b: str) -> list[str]:
    return git(clone, "diff-tree", "-r", "--name-only", tree_a, tree_b).splitlines() if tree_a != tree_b else []
