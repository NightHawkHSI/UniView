"""Search inside files: find a piece of text in the stored bytes of a game's assets (any engine), in the
background. Text is matched case-insensitively as UTF-8/ASCII and as UTF-16 (common in game data)."""

import threading

from PySide6.QtCore import QObject, Signal

from uniview.constants import log

# Kinds whose bytes are worth searching by default; pixels, sound and meshes rarely hold readable text.
TEXT_KINDS = ("text", "data", "file", "animation", "other")
MAX_ASSET_BYTES = 64 << 20     # bigger assets are skipped
CACHE_BYTES = 256 << 20        # searched bytes kept for the next search of the same game
MAX_HITS_PER_ASSET = 3
CONTEXT = 40                   # bytes of context on each side of a match


def _patterns(query):
    q = query.lower()
    out = [q.encode("utf-8")]
    try:
        out.append(q.encode("utf-16-le"))
    except UnicodeEncodeError:
        pass
    return [p for p in dict.fromkeys(out) if p]


def snippet(data, start, length, utf16=False):
    """Readable text around a match (non-printable bytes as '.')."""
    lo, hi = max(0, start - CONTEXT * (2 if utf16 else 1)), start + length + CONTEXT * (2 if utf16 else 1)
    chunk = data[lo:hi]
    if utf16:
        chunk = chunk[(start - lo) % 2:]
        text = chunk.decode("utf-16-le", "replace")
    else:
        text = chunk.decode("utf-8", "replace")
    text = "".join(c if c.isprintable() else "." for c in text)
    return ("…" if lo else "") + text + ("…" if hi < len(data) else "")


def find_in(data, query, limit=MAX_HITS_PER_ASSET):
    """[(offset, snippet)] of where query appears in data (case-insensitive), at most limit."""
    lower = data.lower()  # bytes.lower() only folds ASCII - fine for both encodings' ASCII letters
    hits = []
    for i, pat in enumerate(_patterns(query)):
        pos = lower.find(pat)
        while pos >= 0 and len(hits) < limit:
            hits.append((pos, snippet(data, pos, len(pat), utf16=i == 1)))
            pos = lower.find(pat, pos + len(pat))
        if len(hits) >= limit:
            break
    return sorted(hits)


class ContentSearch(QObject):
    """Background search over a session's assets. One search at a time; start() cancels the previous one."""

    hit = Signal(object, object)        # Asset, [(offset, snippet)]
    progress = Signal(int, int)         # searched, total
    finished = Signal(int, int, bool)   # assets with hits, searched, cancelled

    def __init__(self):
        super().__init__()
        self._run_id = 0
        self._cache = {}       # (id(session), asset uid) -> bytes
        self._cache_size = 0
        self._cache_session = None

    def stop(self):
        self._run_id += 1

    def start(self, session, query, kinds=TEXT_KINDS):
        self._run_id += 1
        run = self._run_id
        if self._cache_session is not session:
            self._cache, self._cache_size, self._cache_session = {}, 0, session
        assets = [a for a in session.assets if kinds is None or a.kind in kinds]
        threading.Thread(target=self._run, args=(run, session, query, assets), daemon=True,
                         name="content search").start()

    def _bytes(self, session, asset):
        key = asset.uid
        data = self._cache.get(key)
        if data is not None:
            return data
        if asset.size is not None and asset.size > MAX_ASSET_BYTES:
            return None
        with session.lock:
            data = session.raw(asset)
        if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_ASSET_BYTES:
            return None
        data = bytes(data)
        if self._cache_size + len(data) <= CACHE_BYTES:
            self._cache[key] = data
            self._cache_size += len(data)
        return data

    def _run(self, run, session, query, assets):
        found = searched = 0
        total = len(assets)
        for n, asset in enumerate(assets):
            if run != self._run_id:
                self.finished.emit(found, searched, True)
                return
            try:
                data = self._bytes(session, asset)
            except Exception as e:
                log.debug("Search: can't read %s: %s", asset.name, e)
                data = None
            if data is not None:
                searched += 1
                hits = find_in(data, query)
                if hits:
                    found += 1
                    self.hit.emit(asset, hits)
            if n % 50 == 0 or n == total - 1:
                self.progress.emit(n + 1, total)
        self.finished.emit(found, searched, False)
