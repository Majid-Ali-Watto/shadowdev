"""How long a run's steps take: each step is logged when it ends, and the whole run as one line,
so slow steps (Claude, setup, tests) show in the log without a profiler."""
import logging
import time
from contextlib import contextmanager

log = logging.getLogger("shadowdev")

END = "===== END ====="  # marks the last log line of a finished task


def duration(seconds: float) -> str:
    """4.2s, 1m05s or 1h02m."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    return f"{minutes // 60}h{minutes % 60:02d}m"


class Timings:
    def __init__(self, label: str):
        self.label = label
        self.started = time.monotonic()
        self.steps: list[tuple[str, float]] = []

    @contextmanager
    def step(self, name: str):
        """Time a step; it is recorded also when it raises or the run is stopped."""
        started = time.monotonic()
        try:
            yield
        finally:
            took = time.monotonic() - started
            self.steps.append((name, took))
            log.info("%s: step %s took %s", self.label, name, duration(took))

    def report(self, outcome: str) -> None:
        total = time.monotonic() - self.started
        steps = " · ".join(f"{name} {duration(took)}" for name, took in self.steps) or "no steps"
        log.info("%s: %s in %s (%s)", self.label, outcome, duration(total), steps)
