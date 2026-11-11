"""python -m shadowdev --reload: for working on ShadowDev itself. The server runs in a child process,
which is restarted whenever ShadowDev's code, its page styles and scripts, or .env change. Without
dependencies: the files' modification times are compared every second.

A restart is a real one: runs in progress are stopped (their processes are killed with the server,
the sandbox dies with its parent) and marked as interrupted, queued ones start again."""
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import PROJECT_ROOT

log = logging.getLogger("shadowdev")

WATCHED = (PROJECT_ROOT / "shadowdev", PROJECT_ROOT / ".env")
SUFFIXES = {".py", ".css", ".js"}
POLL_SECONDS = 1.0
SETTLE_SECONDS = 0.3  # an editor saving several files: restart once, after the last
STOP_SECONDS = 10  # then the server is killed


def snapshot() -> dict[Path, int]:
    """Modification time of every watched file."""
    found = {}
    for root in WATCHED:
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.suffix in SUFFIXES and p.is_file()]
        for path in files:
            try:
                found[path] = path.stat().st_mtime_ns
            except OSError:  # deleted meanwhile
                pass
    return found


def changes(before: dict[Path, int], after: dict[Path, int]) -> list[Path]:
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def start(argv: list[str]) -> subprocess.Popen:
    """The server, in its own process group, so stopping it also stops what it started."""
    return subprocess.Popen([sys.executable, "-m", "shadowdev", *argv], cwd=PROJECT_ROOT, start_new_session=True)


def stop(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, STOP_SECONDS), (signal.SIGKILL, None)):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            return
        try:
            child.wait(wait)
            return
        except subprocess.TimeoutExpired:
            log.warning("the server did not stop in %ds; killing it", STOP_SECONDS)


def watch(argv: list[str]) -> None:
    """Run the server with `argv`, and restart it after every change, until Ctrl+C. A server that
    crashed (a syntax error, say) is started again at the next change, not in a loop."""
    files = snapshot()
    child, said = start(argv), False
    log.info("reloading: watching %d files of ShadowDev and .env; Ctrl+C stops", len(files))
    try:
        while True:
            time.sleep(POLL_SECONDS)
            now = snapshot()
            changed = changes(files, now)
            if not changed:
                if not said and child.poll() is not None:
                    log.error("the server exited with %d; it starts again when a file changes", child.returncode)
                    said = True
                continue
            time.sleep(SETTLE_SECONDS)
            files = snapshot()
            names = ", ".join(str(p.relative_to(PROJECT_ROOT)) for p in changed[:5]) + (" …" if len(changed) > 5 else "")
            log.info("changed: %s; restarting the server", names)
            stop(child)
            child, said = start(argv), False
    except KeyboardInterrupt:
        pass
    finally:
        stop(child)
