"""Every temporary folder the tests make (tempfile.mkdtemp) goes into one folder, deleted when the
tests end, also the ones whose test code locked its folders."""
import atexit
import tempfile
from pathlib import Path

from shadowdev.automation.pipeline import delete_tree

tempfile.tempdir = tempfile.mkdtemp(prefix="shadowdev-tests-")
atexit.register(delete_tree, Path(tempfile.tempdir))
