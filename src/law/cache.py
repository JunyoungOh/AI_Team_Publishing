"""Tiny in-memory TTL cache for law.go.kr responses.

Two buckets: `search` (short TTL, volatile result lists) and
`full` (long TTL, full article text / precedent body).
"""
from __future__ import annotations

import time
from threading import Lock
from typing import Any


class TTLCache:
    """Thread-safe (key -> (expire_epoch, value)) store."""

    def __init__(self, default_ttl: int) -> None:
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = Lock()
        self._default_ttl = default_ttl

    def get(self, key: str) -> Any | None:
        now = time.time()
        with self._lock:
            hit = self._store.get(key)
            if hit is None:
                return None
            expire, value = hit
            if expire < now:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        ttl = ttl if ttl is not None else self._default_ttl
        with self._lock:
            self._store[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
