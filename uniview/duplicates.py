"""Duplicate finder: assets whose content is identical (the same texture shipped in several bundles...),
for any engine - it only needs the session's content_hash()."""

import threading
import weakref

from PySide6.QtCore import QObject, Signal

from uniview.constants import log

SKIP_KINDS = ("scene",)  # built from many objects: no single content to compare
_fingerprints = weakref.WeakKeyDictionary()  # session -> {asset uid: digest or None} (shared by every scan)


def group_duplicates(hashes):
    """{hash: [Asset, ...]} for hashes shared by 2+ assets, biggest waste first. hashes: [(Asset, hash)]."""
    groups = {}
    for asset, digest in hashes:
        if digest is not None:
            groups.setdefault(digest, []).append(asset)
    dupes = {d: sorted(a, key=lambda x: x.name.lower()) for d, a in groups.items() if len(a) > 1}
    return dict(sorted(dupes.items(), key=lambda kv: -wasted(kv[1])))


def wasted(copies):
    """Bytes taken by all but one copy (sizes may be unknown)."""
    size = max((a.size or 0) for a in copies)
    return size * (len(copies) - 1)


def extra_copies(groups):
    """uids of every copy except the first of each group (what 'hide duplicate copies' hides)."""
    return {a.uid for copies in groups.values() for a in copies[1:]}


class DuplicateScan(QObject):
    """Hashes a session's assets in the background (remembered per game, so the next scan is instant).
    After it finishes, .hashes holds [(Asset, digest)] for the version diff."""

    progress = Signal(int, int)           # done, total
    finished = Signal(object, int, bool)  # groups, hashed, cancelled

    def __init__(self):
        super().__init__()
        self._run_id = 0
        self.hashes = []
        self.session = None

    def stop(self):
        self._run_id += 1

    def start(self, session, kinds=None):
        self._run_id += 1
        assets = [a for a in session.assets if a.kind not in SKIP_KINDS and (kinds is None or a.kind in kinds)]
        threading.Thread(target=self._run, args=(self._run_id, session, assets), daemon=True,
                         name="duplicates").start()

    def _run(self, run, session, assets):
        hashes, total = [], len(assets)
        known = _fingerprints.setdefault(session, {})
        for n, asset in enumerate(assets):
            if run != self._run_id:
                self.finished.emit(group_duplicates(hashes), len(hashes), True)
                return
            if asset.uid not in known:
                try:
                    known[asset.uid] = session.content_hash(asset)
                except Exception as e:
                    log.debug("Duplicates: can't fingerprint %s: %s", asset.name, e)
                    known[asset.uid] = None
            if known[asset.uid] is not None:
                hashes.append((asset, known[asset.uid]))
            if n % 100 == 0 or n == total - 1:
                self.progress.emit(n + 1, total)
        self.hashes, self.session = hashes, session
        self.finished.emit(group_duplicates(hashes), len(hashes), False)
