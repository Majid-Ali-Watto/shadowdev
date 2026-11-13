"""What a run leaves for its developer besides its result: the lines the server logged while it
worked (what Claude read, said and changed, the commands and their outcome), and what Claude
reported its requests took. A run works on one thread, so its record is that thread's."""
import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field

MAX_LINES = 400  # the last lines of a run are kept
MAX_LINE_CHARS = 500


@dataclass
class Record:
    lines: deque = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    cost: float = 0  # US dollars, as Claude reported
    turns: int = 0

    @property
    def log(self) -> str:
        return "\n".join(self.lines)


_run = threading.local()


@contextmanager
def recording():
    """Within it, what this thread logs and the Claude usage it reports are kept in the Record given."""
    record = _run.record = Record()
    try:
        yield record
    finally:
        _run.record = None


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
            record.lines.append(f"{stamp} {line[:MAX_LINE_CHARS]}")


logging.getLogger("shadowdev").addHandler(_Handler())
