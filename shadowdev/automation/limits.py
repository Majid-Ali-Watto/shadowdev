"""A Claude subscription's usage limits: its 5-hour and its weekly window, as the claude CLI
reports them with every request (`rate_limit_event`). A run that would be cut off by a limit is
not started, and one that is cut off waits for the reset and goes on in the same conversation,
so no work is done twice. Kept in memory, per Claude account (its config dir); an account that
reports no windows (a Console API key) is never held."""
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from .cancel import CancelToken

log = logging.getLogger("shadowdev")

FIVE_HOUR_FULL = 0.97  # of the 5-hour window used: no new run starts
WEEK_FULL = 0.90  # of the weekly window used: new runs start only when it resets soon and they fit
RESETS_SOON = 5 * 3600  # a reset this near is waited for; a weekly window this near its reset is used up
RUN_SHARE = 0.02  # of the weekly window one run is taken to need, until a run was seen to need more
AFTER_RESET = 60  # seconds past the reset before trying again
POLL = 30  # seconds between looks, while held


@dataclass
class Account:
    windows: dict[str, tuple[float, float]] = field(default_factory=dict)  # name -> (utilization, resets at)
    refused_until: float = 0  # a request was refused: nothing goes through before this
    running: int = 0  # runs admitted and not done
    share: float = RUN_SHARE

    def window(self, name: str, now: float) -> tuple[float, float] | None:
        """The window as last reported, unless it has reset since."""
        found = self.windows.get(name)
        return found if found and found[1] > now else None


def percent(utilization: float) -> str:
    return f"{round(utilization * 100)}%"


class Usage:
    def __init__(self):
        self._accounts: dict[str, Account] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._accounts.clear()

    def record(self, key: str, info: dict) -> None:
        """What a `rate_limit_event` of the claude CLI says about the account."""
        try:
            windows = {name: (float(w["utilization"]), float(w["resetsAt"]))
                       for name, w in (info.get("unifiedWindows") or {}).items()}
            refused = float(info["resetsAt"]) if info.get("status") == "rejected" else 0
        except (KeyError, TypeError, ValueError, AttributeError):
            return  # not in the form known: the account is then simply not held
        with self._lock:
            account = self._accounts.setdefault(key, Account())
            account.windows.update(windows)
            account.refused_until = max(account.refused_until, refused)

    def refused(self, key: str, now: float, limit_error: bool = False) -> float | None:
        """When the limit that refused a request resets, or None when none did. `limit_error`:
        Claude's error names a limit, so a window that is full counts too."""
        with self._lock:
            account = self._accounts.get(key)
            if not account:
                return None
            if account.refused_until > now:
                return account.refused_until
            full = [w[1] for name in account.windows if (w := account.window(name, now)) and w[0] >= FIVE_HOUR_FULL]
            return min(full) if limit_error and full else None

    @staticmethod
    def _held(account: Account, now: float) -> tuple[float, str] | None:
        if account.refused_until > now:
            return account.refused_until, "the Claude limit is reached"
        five, week = account.window("five_hour", now), account.window("seven_day", now)
        if five and five[0] >= FIVE_HOUR_FULL:
            return five[1], f"{percent(five[0])} of the 5-hour Claude limit is used"
        if week and week[0] >= WEEK_FULL:
            room = 1 - week[0] - account.running * account.share
            if week[1] - now > RESETS_SOON or room < account.share:
                return week[1], f"{percent(week[0])} of the weekly Claude limit is used"
        return None

    def held(self, key: str, now: float) -> tuple[float, str] | None:
        """(until when, why) no new run starts on the account, or None when one may."""
        with self._lock:
            account = self._accounts.get(key)
            return self._held(account, now) if account else None

    def state(self, key: str, now: float) -> tuple[str, bool]:
        """(the account's usage in words, "" when unknown; whether it is held), for the page."""
        with self._lock:
            account = self._accounts.get(key)
            if not account:
                return "", False
            held = self._held(account, now)
            if held:
                return f"{held[1]}; it resets at {when(held[0])}", True
            used = [f"{percent(w[0])} of the {name} limit" for window, name in (("five_hour", "5-hour"), ("seven_day", "weekly"))
                    if (w := account.window(window, now))]
            return (" and ".join(used) + " used" if used else ""), False

    def admit(self, key: str, now: float) -> tuple[float, str] | float | None:
        """Start a run on the account: (until when, why) when it must wait, else the weekly
        utilization it starts at (None when unknown), to be given to done()."""
        with self._lock:
            account = self._accounts.get(key)
            if not account:
                return None
            held = self._held(account, now)
            if held:
                return held
            account.running += 1
            week = account.window("seven_day", now)
            return week[0] if week else None

    def done(self, key: str, started: float | None) -> None:
        """A run admitted at weekly utilization `started` is over: what it used is what the next is taken to need."""
        with self._lock:
            account = self._accounts.get(key)
            if not account:
                return
            account.running = max(0, account.running - 1)
            week = account.window("seven_day", time.time())
            if started is not None and week:
                account.share = max(account.share, week[0] - started)


USAGE = Usage()
_run = threading.local()  # the run of this thread: how it says what it waits for


@contextmanager
def telling(tell):
    """Within it, this thread's waits are told to `tell(message)`; "" when a wait is over."""
    _run.tell = tell
    try:
        yield
    finally:
        _run.tell = None


def tell(message: str) -> None:
    tell = getattr(_run, "tell", None)
    if tell:
        try:
            tell(message)
        except Exception:
            log.exception("could not report a wait")


def when(at: float) -> str:
    local = time.localtime(at)
    return time.strftime("%H:%M" if local[:3] == time.localtime()[:3] else "%H:%M on %d %b", local)


def wait_for_reset(until: float, cancel: CancelToken, label: str) -> None:
    """A run in progress was refused by a limit: keep everything, and return once it has reset."""
    message = f"waiting for the Claude limit to reset at {when(until)}; it goes on from where it stopped"
    log.info("%s: %s", label, message)
    tell(message)
    try:
        while (left := until + AFTER_RESET - time.time()) > 0:
            cancel.wait(min(POLL, left))
    finally:
        tell("")


def admit(keys: list[str], cancel: CancelToken, label: str) -> tuple[str, float | None]:
    """Wait until a run may start on one of the Claude accounts, the first of `keys` by preference
    (the others: the accounts it may switch to). Returns (the account it starts on, what
    Usage.admit returned for it)."""
    told = ""
    try:
        while True:
            for key in keys:
                held = USAGE.admit(key, time.time())
                if not isinstance(held, tuple):
                    return key, held
            held = USAGE.held(keys[0], time.time()) or held  # why its own account holds it
            others = " (your other Claude accounts are at their limits too)" if len(keys) > 1 else ""
            message = f"not started yet: {held[1]}; it resets at {when(held[0])}{others}"
            if message != told:
                log.info("%s: %s", label, message)
                tell(message)
                told = message
            cancel.wait(POLL)
    finally:
        if told:
            tell("")
