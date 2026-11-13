"""The server's log, also written to files: data/logs/shadowdev.log for today, renamed to
shadowdev.log.YYYY-MM-DD at midnight. Files older than KEEP_DAYS are deleted at startup and at
every midnight. Only the server writes them: two processes renaming one file would race.
They hold what the server prints (issue keys, what Claude says, test output, developers' names), so
the folder and its files are readable only by the user running ShadowDev."""
import logging
import os
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

KEEP_DAYS = 5
NAME = "shadowdev.log"


def prune(directory: Path, now: float | None = None) -> None:
    """Delete the day files last written to more than KEEP_DAYS ago."""
    cutoff = (time.time() if now is None else now) - KEEP_DAYS * 86400
    for path in directory.glob(f"{NAME}.*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:  # deleted meanwhile
            pass


class DailyFileHandler(TimedRotatingFileHandler):
    """A new file every day, and old ones deleted by age rather than by count, so days the server
    was not running don't keep stale files around."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.directory = directory
        super().__init__(directory / NAME, when="midnight", encoding="utf-8")
        prune(directory)

    def _open(self):
        stream = super()._open()
        os.fchmod(stream.fileno(), 0o600)  # a day's file keeps it when renamed at midnight
        return stream

    def doRollover(self) -> None:
        super().doRollover()
        prune(self.directory)


def write_to_files(directory: Path) -> None:
    """Send everything the root logger prints to the terminal to the day's file as well."""
    root = logging.getLogger()
    handler = DailyFileHandler(directory)
    for existing in root.handlers:
        handler.setFormatter(existing.formatter)
    root.addHandler(handler)
