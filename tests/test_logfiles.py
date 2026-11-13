import logging
import os
import tempfile
import time
import unittest
from pathlib import Path

from shadowdev import logfiles


class LogFilesTest(unittest.TestCase):
    def test_prune_deletes_only_day_files_older_than_five_days(self):
        directory = Path(tempfile.mkdtemp())
        now = time.time()
        ages = {"shadowdev.log": 9, "shadowdev.log.2026-11-01": 9, "shadowdev.log.2026-11-09": 4, "other.txt": 9}
        for name, days in ages.items():
            (directory / name).write_text("x")
            os.utime(directory / name, (now - days * 86400,) * 2)
        logfiles.prune(directory, now)
        self.assertEqual(sorted(p.name for p in directory.iterdir()),
                         ["other.txt", "shadowdev.log", "shadowdev.log.2026-11-09"])

    def test_rollover_starts_a_new_file_and_prunes(self):
        directory = Path(tempfile.mkdtemp())
        old = directory / "shadowdev.log.2000-01-01"
        old.write_text("x")
        os.utime(old, (0, 0))
        directory.chmod(0o755)
        handler = logfiles.DailyFileHandler(directory)
        self.assertFalse(old.exists())  # pruned at startup
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        logger = logging.getLogger("shadowdev.test_logfiles")
        logger.addHandler(handler)
        try:
            logger.warning("first day")
            handler.doRollover()
            logger.warning("second day")
        finally:
            logger.removeHandler(handler)
            handler.close()
        rotated = [p for p in directory.iterdir() if p.name != "shadowdev.log"]
        self.assertEqual(len(rotated), 1)
        self.assertEqual({p.stat().st_mode & 0o777 for p in directory.iterdir()}, {0o600})  # only its user reads them
        self.assertIn("first day", rotated[0].read_text())
        self.assertIn("second day", (directory / "shadowdev.log").read_text())
