"""Minimal in-process rate limiter (per key, sliding window). Not distributed — it is per-worker
and approximate, which is all the public fragment/spectrum endpoints need: it blocks a single client
from scraping the blob-backed spectra in a tight loop. Strong global limits would live at the edge
(Azure Front Door / API Management), not here.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_hits: dict[str, deque] = defaultdict(deque)
_last_gc = [0.0]


def allow(key: str, limit: int, window_s: float) -> bool:
    """True if `key` is under `limit` events within the last `window_s`; records the event if so."""
    now = time.time()
    with _lock:
        q = _hits[key]
        cutoff = now - window_s
        while q and q[0] < cutoff:
            q.popleft()
        # opportunistic GC so idle clients don't leak entries forever
        if now - _last_gc[0] > 300:
            for k in [k for k, dq in _hits.items() if not dq or dq[-1] < cutoff]:
                if k != key:
                    _hits.pop(k, None)
            _last_gc[0] = now
        if len(q) >= limit:
            return False
        q.append(now)
        return True
