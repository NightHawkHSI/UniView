"""Job list shared by the background workers (thumbnails, stats). No Qt here.

Not a plain queue.Queue because the workers need to *replace* waiting jobs (only the thumbnails on
screen matter after a scroll), let urgent jobs jump the line, and drop results of cancelled work.
One worker thread per queue is enough: plugins decode under session.lock, so more threads would
only wait for each other.
"""

import threading


class JobQueue:
    """Thread-safe jobs for one worker thread. A job is a tuple whose first item is its key."""

    def __init__(self):
        self._cv = threading.Condition()
        self._urgent = []
        self._normal = []
        self._generation = 0

    def put_urgent(self, jobs):
        """Run these before everything else (in the given order). A key already waiting moves up."""
        jobs = list(jobs)
        keys = {job[0] for job in jobs}
        with self._cv:
            self._urgent = jobs + [j for j in self._urgent if j[0] not in keys]
            self._normal = [j for j in self._normal if j[0] not in keys]
            self._cv.notify()

    def replace(self, jobs, cancel=False):
        """New normal jobs instead of the waiting ones. cancel=True also starts a new generation, so
        results of jobs taken before this call can be recognized as stale (is_current)."""
        with self._cv:
            self._normal = list(jobs)
            if cancel:
                self._generation += 1
            self._cv.notify()

    def clear(self):
        """Drop every waiting job and start a new generation."""
        with self._cv:
            self._urgent, self._normal = [], []
            self._generation += 1

    def take(self, n=1, timeout=None):
        """Wait for jobs; returns (generation, up to n jobs), urgent ones first. ([] after a timeout.)"""
        with self._cv:
            if not self._cv.wait_for(lambda: self._urgent or self._normal, timeout):
                return self._generation, []
            batch = []
            for source in (self._urgent, self._normal):
                take = min(n - len(batch), len(source))
                batch += source[:take]
                del source[:take]
            return self._generation, batch

    def is_current(self, generation):
        with self._cv:
            return generation == self._generation

    def pending(self):
        with self._cv:
            return len(self._urgent) + len(self._normal)

    def pending_if_current(self, generation):
        """Jobs still waiting, or None if `generation` was cancelled (checked atomically)."""
        with self._cv:
            if generation != self._generation:
                return None
            return len(self._urgent) + len(self._normal)
