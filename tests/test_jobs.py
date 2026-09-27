"""JobQueue: ordering, replacing, de-duplication, cancelling, and a worker thread on top of it."""

import threading
import time

from uniview.jobs import JobQueue


def keys(batch):
    return [job[0] for job in batch]


def test_urgent_before_normal_in_order():
    q = JobQueue()
    q.replace([("n1",), ("n2",)])
    q.put_urgent([("u1",), ("u2",)])
    assert keys(q.take(10)[1]) == ["u1", "u2", "n1", "n2"]
    assert q.pending() == 0


def test_take_batches():
    q = JobQueue()
    q.put_urgent([("u1",)])
    q.replace([("n1",), ("n2",), ("n3",)])
    assert keys(q.take(2)[1]) == ["u1", "n1"]
    assert keys(q.take(2)[1]) == ["n2", "n3"]


def test_replace_drops_waiting_normal_jobs_not_urgent():
    q = JobQueue()
    q.put_urgent([("u",)])
    q.replace([("old1",), ("old2",)])
    q.replace([("new",)])
    assert keys(q.take(10)[1]) == ["u", "new"]


def test_urgent_dedupes_and_moves_keys_up():
    q = JobQueue()
    q.replace([("a",), ("b",), ("c",)])
    q.put_urgent([("x",), ("b",)])
    q.put_urgent([("x",)])  # the same panel texture requested again: queued once
    assert keys(q.take(10)[1]) == ["x", "b", "a", "c"]


def test_generations():
    q = JobQueue()
    q.replace([("a",)], cancel=True)
    gen, _ = q.take()
    assert q.is_current(gen) and q.pending_if_current(gen) == 0
    q.replace([("b",), ("c",)])            # plain replace: same generation
    assert q.pending_if_current(gen) == 2
    q.replace([("d",)], cancel=True)       # a new game: old results are stale
    assert not q.is_current(gen) and q.pending_if_current(gen) is None
    q.clear()
    assert q.pending() == 0


def test_take_timeout_returns_empty():
    start = time.time()
    assert JobQueue().take(timeout=0.05)[1] == []
    assert time.time() - start < 1


def test_worker_thread_drops_cancelled_results():
    """A worker like StatsWorker: results of a cancelled generation never get delivered."""
    q, delivered, busy, release = JobQueue(), [], threading.Event(), threading.Event()

    def worker():
        while True:
            gen, batch = q.take(10, timeout=0.3)
            if not batch:
                return
            if batch[0][0] == "slow":
                busy.set()
                release.wait(2)
            if q.pending_if_current(gen) is not None:
                delivered.extend(keys(batch))

    t = threading.Thread(target=worker, daemon=True)
    q.replace([("slow",), ("old",)], cancel=True)
    t.start()
    assert busy.wait(2)
    q.replace([("new1",), ("new2",)], cancel=True)  # another game opened while the batch was running
    release.set()
    t.join(5)
    assert delivered == ["new1", "new2"]


def test_blocked_take_wakes_on_put():
    q, got = JobQueue(), []
    t = threading.Thread(target=lambda: got.append(keys(q.take(timeout=2)[1])))
    t.start()
    time.sleep(0.05)
    q.put_urgent([("hi",)])
    t.join(3)
    assert got == [["hi"]]
