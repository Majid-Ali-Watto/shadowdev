"""What a run leaves for its developer besides its result: the lines the server logged while it
worked (what Claude read, said and changed, the commands and their outcome), and what Claude
reported its requests took. A run works on one thread, so its record is that thread's."""
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field

MAX_LINES = 400  # the last lines of a run are kept
MAX_LINE_CHARS = 500


@dataclass
class Record:
    lines: deque = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    count: int = 0  # lines logged so far, the ones no longer kept too: where a live reader left off
    cost: float = 0  # US dollars, as Claude reported
    turns: int = 0
    on_step: Callable[[str], None] = lambda name: None  # told each step the run starts, for its progress
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)  # the run's thread adds as others read

    def add(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)
            self.count += 1

    @property
    def log(self) -> str:
        with self._lock:
            return "\n".join(self.lines)

    def since(self, seen: int) -> tuple[int, list[str]]:
        """(how many lines were logged so far, the ones after the first `seen` that are still kept),
        for the Live logs page to show only what is new."""
        with self._lock:
            kept, total = list(self.lines), self.count
        new = max(0, total - max(seen, total - len(kept)))
        return total, kept[len(kept) - new:] if new else []


_run = threading.local()


@contextmanager
def recording(on_step: Callable[[str], None] = lambda name: None):
    """Within it, what this thread logs and the Claude usage it reports are kept in the Record given,
    and `on_step` is told each step the thread starts."""
    record = _run.record = Record(on_step=on_step)
    try:
        yield record
    finally:
        _run.record = None


def step(name: str) -> None:
    """The run on this thread starts the step `name` (automation/timing.Timings.step)."""
    record = getattr(_run, "record", None)
    if record:
        try:
            record.on_step(name)
        except Exception:  # showing progress must not break the run
            logging.getLogger("shadowdev").exception("could not record the step %s", name)


def add_usage(cost, turns) -> None:
    """What one Claude request took, from its result: `total_cost_usd` and `num_turns`."""
    record = getattr(_run, "record", None)
    if record:
        record.cost += cost if isinstance(cost, int | float) else 0
        record.turns += turns if isinstance(turns, int) else 0


class _Handler(logging.Handler):
    def emit(self, entry: logging.LogRecord) -> None:
        record = getattr(_run, "record", None)
        if record is None:
            return
        try:
            text = entry.getMessage()
        except Exception:  # a bad log call must not break the run
            return
        stamp = time.strftime("%H:%M:%S", time.localtime(entry.created))
        for line in text.splitlines() or [""]:
            record.add(f"{stamp} {line[:MAX_LINE_CHARS]}")


logging.getLogger("shadowdev").addHandler(_Handler())
