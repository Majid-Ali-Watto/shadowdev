"""The waiting line of the run queue (services/run_queue.py): jobs handed out one developer at a time."""
import bisect
import itertools
import threading
from collections import Counter, deque
from collections.abc import Callable


class FairQueue:
    """Jobs waiting for a worker, handed out one developer at a time in turn: a developer with ten
    queued jobs doesn't hold up one who just queued one. A developer with `limit(developer)` jobs
    running gets no more until one is done(), or wake() says a limit was raised.

    Each developer's jobs wait in one lane per project, the most urgent first (put's `urgency`,
    the highest first; of the same urgency, the oldest). `lane(developer, project)` is (that
    project's own limit, its rank): a project with that many jobs running gets no more, and of a
    developer's projects with room, the lowest rank (the highest priority) goes first, then the
    one whose next job is the most urgent, then in turn."""

    def __init__(self, limit: Callable[[int], int], lane: Callable[[int, int], tuple[int, int]]):
        # (developer, project) -> its waiting jobs, sorted: (-urgency, order put, job)
        self._jobs: dict[tuple[int, int], list[tuple[int, int, object]]] = {}
        self._order = itertools.count()
        self._lanes: dict[int, deque[int]] = {}  # developer -> their projects with waiting jobs, in turn
        self._turn: deque[int] = deque()  # developers with waiting jobs, whose turn is next first
        self._running: Counter = Counter()  # developer, and (developer, project) -> jobs handed out and not done
        self._limit = limit
        self._lane = lane
        self._ready = threading.Condition()

    def put(self, user_id: int, project_id: int, job, urgency: int = 0) -> None:
        with self._ready:
            if user_id not in self._lanes:
                self._lanes[user_id] = deque()
                self._turn.append(user_id)
            if (user_id, project_id) not in self._jobs:
                self._jobs[user_id, project_id] = []
                self._lanes[user_id].append(project_id)
            bisect.insort(self._jobs[user_id, project_id], (-urgency, next(self._order), job))  # orders never tie
            self._ready.notify()

    def _pick(self) -> tuple[int, int] | None:
        """The developer whose turn it is and who has room, and their project to run next."""
        for user_id in self._turn:
            if self._running[user_id] >= self._limit(user_id):
                continue
            open_lanes = []
            for order, project_id in enumerate(self._lanes[user_id]):
                room, rank = self._lane(user_id, project_id)
                if self._running[user_id, project_id] < room:
                    open_lanes.append((rank, self._jobs[user_id, project_id][0][0], order, project_id))
            if open_lanes:
                return user_id, min(open_lanes)[3]
        return None

    def get(self):
        with self._ready:
            while (picked := self._pick()) is None:
                self._ready.wait()
            user_id, project_id = picked
            jobs, lanes = self._jobs[picked], self._lanes[user_id]
            job = jobs.pop(0)[2]
            lanes.remove(project_id)
            if jobs:
                lanes.append(project_id)
            else:
                del self._jobs[picked]
            self._turn.remove(user_id)
            if lanes:
                self._turn.append(user_id)
            else:
                del self._lanes[user_id]
            self._running[user_id] += 1
            self._running[picked] += 1
            return job

    def done(self, user_id: int, project_id: int) -> None:
        with self._ready:
            self._running[user_id] -= 1
            self._running[user_id, project_id] -= 1
            self._ready.notify_all()

    def wake(self) -> None:
        with self._ready:
            self._ready.notify_all()
