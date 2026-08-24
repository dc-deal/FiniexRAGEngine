"""Per-client request throttling for the HTTP surface (ISSUE_98)."""
import threading
import time
from typing import Callable, Dict, Optional, Tuple

from fastapi import HTTPException, Request


class RateLimiter:
    """A token bucket per client key, in process.

    In process rather than in the reverse proxy, deliberately: Caddy's `rate_limit` is a plugin,
    i.e. a custom build, and a limit that lives only in the proxy is gone the moment the engine is
    started without one. This is the single-process API server, so a dict plus a lock is the whole
    mechanism — no dependency, no shared store.

    It is defence in depth, not the gate. The gate is the bearer token; this bounds what an
    anonymous caller can do to `/health` (the one route without a token) and how fast a credential
    can be guessed.
    """

    def __init__(self, per_minute: int) -> None:
        """`per_minute` ≤ 0 disables the limiter entirely (and it then costs nothing per call)."""
        self._capacity = float(per_minute)
        self._refill_per_second = per_minute / 60.0
        # key -> (tokens available, last refill timestamp)
        self._buckets: Dict[str, Tuple[float, float]] = {}
        # One dict mutated from the event loop and, for the auth limiter, from any thread FastAPI
        # runs a sync dependency on. Cheap to hold correctly, expensive to debug once it is not.
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Consume one token for `key`; False when the bucket is empty."""
        if self._capacity <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self._capacity, now))
            # Refill for the elapsed time, capped at capacity — a client that was idle for an hour
            # gets one minute's worth, not an hour's.
            tokens = min(self._capacity, tokens + (now - last) * self._refill_per_second)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True


def client_key(forwarded_for: Optional[str], peer: Optional[str]) -> str:
    """The identity a bucket is keyed on: the originating client, not the proxy.

    Behind the reverse proxy every request arrives from `127.0.0.1`, so keying on the peer address
    would put every caller in the world into **one** bucket — a global limit wearing the costume of
    a per-client one, which fails exactly when several consumers are active.

    So the first entry of `X-Forwarded-For` wins. That header is normally spoofable and is
    trustworthy **here specifically**, because the app binds loopback: the only route in is through
    Caddy, which sets it. **If the bind ever widens, this assumption dies silently** — the header
    would then be attacker-controlled and the limit trivially evaded by varying it.
    """
    if forwarded_for:
        first = forwarded_for.split(',')[0].strip()
        if first:
            return first
    return peer or 'unknown'


def build_rate_limit_dependency(limiter: RateLimiter) -> Callable[[Request], None]:
    """The limiter as a FastAPI dependency, for the routes no token protects.

    Mounted on the public router rather than on `/health` itself, for the same reason the bearer
    dependency sits on the protected one: whatever is added beside it inherits the limit instead of
    needing to remember it.
    """

    def enforce_rate_limit(request: Request) -> None:
        key = client_key(request.headers.get('x-forwarded-for'),
                         request.client.host if request.client else None)
        if not limiter.allow(key):
            # `Retry-After` in seconds: a conforming client backs off on its own instead of
            # hammering a closed door, which is the behaviour the limit exists to produce.
            raise HTTPException(status_code=429, detail='Too many requests',
                                headers={'Retry-After': '60'})

    return enforce_rate_limit
