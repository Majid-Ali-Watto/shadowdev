"""Running a repo's setup and test commands in the sandbox, and judging a change by them.

Test gate: "no new failures". Each repo's `test` command writes JUnit XML reports into
$SHADOWDEV_REPORT_DIR. The same command is first run on the untouched base branch (once per
base commit, shared by parallel runs); tests that already fail there are not held against
the change.
"""
import logging
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from . import gitops, sandbox
from .cancel import NEVER, CancelToken
from ..config import Config
from ..models.project import Repo

log = logging.getLogger("shadowdev")

OUTPUT_LIMIT = 8000
MAX_FILE_BYTES = 2 * 1024**3  # a setup/test command cannot write a file bigger than this
MAX_REPORT_BYTES = 50 * 1024**2  # a bigger JUnit report is not read
MAX_REPORTS = 500
REPORT_DIR_ENV = "SHADOWDEV_REPORT_DIR"
MAX_BASELINES = 64  # baseline results kept (by repo, base commit and commands); the oldest go first
NOT_TESTED = "no test command is set for this repo, so the change was not tested"


@dataclass
class TestRun:
    ok: bool  # exit code 0
    output: str
    reports: set[str]  # JUnit report file names written
    failures: set[str]  # ids of failed/errored test cases


@dataclass
class Checkout:
    label: str  # the branch, for logs
    repo: Repo
    base: str  # branch the work is cut from and the PR goes into
    base_commit: str
    worktree: Path  # this run's clone
    prepared: bool = False  # setup command has run successfully
    baseline: TestRun | None = None  # test run on the untouched base branch
    cancel: CancelToken = NEVER


def run_command(cwd: Path, command: str, cfg: Config, label: str, cancel: CancelToken = NEVER, **env: str) -> tuple[bool, str]:
    """Run a project command (setup/test) in the sandbox: it executes code Claude wrote.
    Limits: cfg.command_timeout_seconds, MAX_FILE_BYTES per file written, and only the last
    OUTPUT_LIMIT characters of its output are kept in memory. Logged with `label`."""
    first_line = command.strip().splitlines()[0]
    log.info("%s: $ %s%s", label, first_line, " …" if "\n" in command.strip() else "")
    started, timeout = time.monotonic(), cfg.command_timeout_seconds
    argv = ["prlimit", f"--fsize={MAX_FILE_BYTES}", "--core=0", "--", *sandbox.wrap_command(command, cfg, cwd)]
    proc = subprocess.Popen(argv, cwd=cwd, env=sandbox.env(Path.home(), **env),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    watchdog = threading.Timer(timeout, proc.kill)
    watchdog.start()
    tail = bytearray()
    with cancel.tracking(proc):
        try:
            for chunk in iter(lambda: proc.stdout.read(65536), b""):
                tail += chunk
                del tail[:-OUTPUT_LIMIT * 4]
            proc.wait()
        finally:
            watchdog.cancel()
            proc.stdout.close()
    cancel.check()
    output = tail.decode(errors="replace")[-OUTPUT_LIMIT:]
    took = time.monotonic() - started
    if took >= timeout:
        log.warning("%s: timed out after %ds", label, timeout)
        return False, f"timed out after {timeout}s\n{output}"
    if proc.returncode == 0:
        log.info("%s: ok in %.0fs", label, took)
    else:
        last = "\n".join(output.rstrip().splitlines()[-15:])
        log.info("%s: exit %d in %.0fs; last lines:\n%s", label, proc.returncode, took, last)
    return proc.returncode == 0, output


def junit_results(report_dir: Path) -> tuple[set[str], set[str]]:
    """(readable report file names, ids of failed or errored test cases). The reports were written
    by the code under test, and are read outside the sandbox: only regular files of a sane size
    are opened (a link could point anywhere, a pipe would block, a huge file would fill memory)."""
    reports, failures = set(), set()
    for path in sorted(report_dir.glob("*.xml"))[:MAX_REPORTS]:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REPORT_BYTES:
                continue
            cases = list(ET.parse(path).iter("testcase"))
        except (ET.ParseError, OSError):
            continue  # unreadable counts as missing
        reports.add(path.name)
        for case in cases:
            if case.find("failure") is not None or case.find("error") is not None:
                failures.add(f"{path.stem}: {case.get('classname')} :: {case.get('name')}")
    return reports, failures


def run_tests(repo: Repo, cwd: Path, report_dir: Path, cfg: Config, label: str, cancel: CancelToken = NEVER) -> TestRun:
    shutil.rmtree(report_dir, ignore_errors=True)
    report_dir.mkdir(parents=True)
    ok, output = run_command(cwd, repo.test, cfg, label, cancel, **{REPORT_DIR_ENV: str(report_dir)})
    reports, failures = junit_results(report_dir)
    log.info("%s: %d report(s), %d failing test(s)", label, len(reports), len(failures))
    return TestRun(ok, f"[{repo.name}] $ {repo.test}\n{output}", reports, failures)


def measure_baseline(c: Checkout, commit: str, scratch: Path, cfg: Config) -> TestRun:
    """Run the repo's tests on its untouched base branch, in a throwaway worktree
    outside Claude's workspace."""
    path = scratch / "baseline" / c.repo.name
    label = f"{c.label}: {c.repo.name} baseline on {c.base}"
    gitops.git(c.worktree, "worktree", "add", "--detach", str(path), commit)
    try:
        if c.repo.setup:
            ok, output = run_command(path, c.repo.setup, cfg, f"{label}, setup", c.cancel)
            if not ok:
                raise RuntimeError(f"{c.repo.name}: setup fails on unmodified {c.base}:\n{output[-1500:]}")
        run = run_tests(c.repo, path, scratch / "reports" / f"{c.repo.name}.baseline", cfg, f"{label}, tests", c.cancel)
        if not run.ok and not run.reports:
            raise RuntimeError(
                f"{c.repo.name}: tests on unmodified {c.base} fail without writing a JUnit report "
                f"to ${REPORT_DIR_ENV}; check its test command:\n{run.output[-1500:]}"
            )
        return run
    finally:
        gitops.git(c.worktree, "worktree", "remove", "--force", str(path))


class Baselines:
    """Baseline test results by (repo, base commit, commands). Parallel runs on the same base
    branch share one, instead of each running the whole suite on the untouched code. Only the
    results are kept, never the code, and only the MAX_BASELINES most recently used."""

    def __init__(self, limit: int = MAX_BASELINES):
        self._runs: OrderedDict[tuple, TestRun] = OrderedDict()
        self._locks: dict[tuple, threading.Lock] = {}
        self._guard = threading.Lock()
        self._limit = limit

    def get(self, c: Checkout, scratch: Path, cfg: Config) -> TestRun:
        commit = c.base_commit
        key = (c.repo.github, commit, c.repo.setup, c.repo.test)
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:  # a second run on the same commit waits for the first one's baseline
            with self._guard:
                run = self._runs.get(key)
                if run:
                    self._runs.move_to_end(key)
            if run:
                log.info("%s: %s baseline on %s (%s) already measured; reusing it",
                         c.label, c.repo.name, c.base, commit[:10])
                return run
            run = measure_baseline(c, commit, scratch, cfg)
            with self._guard:
                self._runs[key] = run
                while len(self._runs) > self._limit:
                    old, _ = self._runs.popitem(last=False)
                    self._locks.pop(old, None)
            return run

    def clear(self) -> None:
        with self._guard:
            self._runs.clear()
            self._locks.clear()


BASELINES = Baselines()


def check(c: Checkout, scratch: Path, cfg: Config) -> tuple[bool, str]:
    """Set up the worktree on first use (setup may legitimately change with the change,
    e.g. package.json, so a failure here is reported back too), then run its tests and
    compare against the base branch baseline.

    The commands run the change's code and its dependencies, and are set by the project's owner,
    not the developer whose PR it becomes. So they must leave the code as they found it: anything
    they change would be committed under the developer's name. Raises if they changed it."""
    if not c.repo.test:
        return True, NOT_TESTED
    if c.baseline is None:
        c.baseline = BASELINES.get(c, scratch, cfg)
    scratch.mkdir(parents=True, exist_ok=True)
    index = scratch / f"{c.repo.name}.index"
    before = gitops.snapshot(c.worktree, index)
    result = _check(c, scratch, cfg)
    altered = gitops.changed_files(c.worktree, before, gitops.snapshot(c.worktree, index))
    if altered:
        raise RuntimeError(f"{c.repo.name}: its setup/test commands changed files that would be committed "
                           f"({', '.join(altered[:10])}); nothing was pushed. Make them leave the code as it is "
                           "(no --fix, no snapshot updates), or ignore those files in .gitignore.")
    return result


def _check(c: Checkout, scratch: Path, cfg: Config) -> tuple[bool, str]:
    if not c.prepared and c.repo.setup:
        ok, output = run_command(c.worktree, c.repo.setup, cfg, f"{c.label}: {c.repo.name} change, setup", c.cancel)
        if not ok:
            return False, f"[{c.repo.name}] $ {c.repo.setup}\n{output}"
        c.prepared = True
    run = run_tests(c.repo, c.worktree, scratch / "reports" / c.repo.name, cfg, f"{c.label}: {c.repo.name} change, tests", c.cancel)
    if run.ok:
        return True, run.output
    missing = c.baseline.reports - run.reports
    new = sorted(run.failures - c.baseline.failures)
    if run.reports and not missing and not new:
        return True, run.output  # only failures that already exist on the base branch
    problems = []
    if not run.reports:
        problems.append("The test command failed before producing any test report.")
    if missing:
        problems.append(f"No test report from: {', '.join(sorted(missing))} (suite crashed?).")
    if new:
        problems.append(f"Tests that pass on {c.base} but fail now:\n" + "\n".join(new[:50]))
    return False, "\n".join(problems) + "\n" + run.output


def changed_paths(c: Checkout, scratch: Path) -> set[str]:
    """Files the change adds or modifies in the repo, as git names them (relative to the repo)."""
    scratch.mkdir(parents=True, exist_ok=True)
    tree = gitops.snapshot(c.worktree, scratch / f"{c.repo.name}.paths-index")
    return set(gitops.changed_files(c.worktree, gitops.git(c.worktree, "rev-parse", "HEAD^{tree}"), tree))


def reproduces(c: Checkout, tests: list[str], scratch: Path, cfg: Config) -> list[str]:
    """Run the repo's tests on its untouched base commit with only the change's test files `tests`
    (relative to the repo, among changed_paths) copied in, in a throwaway worktree outside Claude's
    workspace. Returns the tests that fail there but not in the baseline: none means the tests do
    not reproduce the bug. Needs the baseline, so call it after check()."""
    path = scratch / "regression" / c.repo.name
    label = f"{c.label}: {c.repo.name} regression tests on {c.base}"
    gitops.git(c.worktree, "worktree", "add", "--detach", str(path), c.base_commit)
    try:
        for test in tests:
            source = c.worktree / test
            if source.is_symlink() or not source.is_file():
                continue  # only plain files of the change are carried over
            (path / test).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, path / test)
        if c.repo.setup:
            ok, output = run_command(path, c.repo.setup, cfg, f"{label}, setup", c.cancel)
            if not ok:
                log.warning("%s: setup failed, so the regression tests were not checked:\n%s", label, output[-1500:])
                return []
        run = run_tests(c.repo, path, scratch / "reports" / f"{c.repo.name}.regression", cfg, f"{label}, tests", c.cancel)
        return sorted(run.failures - c.baseline.failures)
    finally:
        gitops.git(c.worktree, "worktree", "remove", "--force", str(path))


def changed(checkouts: list[Checkout]) -> list[Checkout]:
    return [c for c in checkouts if gitops.git(c.worktree, "status", "--porcelain")]


