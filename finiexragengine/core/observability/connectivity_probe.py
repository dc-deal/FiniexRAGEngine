"""Is it the host, and for how long — measured, while the back-off keeps the engine quiet.

The correlated-failure guard (ISSUE_84) stops a set polling for `correlated_backoff_minutes` when
every feed fails at once, which is right: eleven feeds failing together is not eleven feed problems.
But it also means the engine stops looking, so the closing event can only report the **back-off's**
length. On 2026-09-08 eight episodes were each reported as "recovered after 5m" — while the
neighbouring set, which stayed just under the ratio and therefore kept polling, was fetching 265
articles again **21 seconds** after the same failure. The outage was seconds; the report said
minutes, and nothing distinguished the two.

So during a back-off this fills the silence, with two syscalls per pass and no traffic to any feed:

- **resolve a name the engine does not poll.** Deliberately not one of our own hosts: a feed we
  fetch every 15 s stays in the OS resolver cache and answers happily through an outage. That cache
  is what made 2026-09-08 look like two different faults — the feeds polled often enough to stay
  cached reported `timed out` (the name resolved, the packets did not arrive), while the slower
  central-bank feeds reported `getaddrinfo failed`. One cause, two symptoms, sorted by poll cadence.
- **open a socket to a literal address.** No name in front of it, so the transport is tested by
  itself. DNS failing while this succeeds is a resolver fault; both failing is the path.

**The DNS half is deliberately not bounded by the timeout.** `getaddrinfo` takes no timeout
argument — the OS resolver's own retry schedule decides, and on 2026-09-08 that was the reason a
failing ingest pass took 55 seconds against a 10 s per-feed deadline. Bounding it here would hide
exactly the number worth having, so the probe measures how long the resolver took instead.
"""
import logging
import socket
from datetime import datetime, timezone
from time import perf_counter
from typing import Tuple

from finiexragengine.types.ingest_types import HostProbe

logger = logging.getLogger(__name__)

# Fallback when `connectivity_probe_tcp` is not `host:port` — a malformed setting must degrade to a
# working probe rather than to an exception inside an outage response.
_DEFAULT_TCP: Tuple[str, int] = ('1.1.1.1', 53)


def _split_target(target: str) -> Tuple[str, int]:
    host, separator, port = target.rpartition(':')
    if not separator or not port.isdigit():
        logger.warning('[HOST] connectivity_probe_tcp %r is not host:port — probing %s:%d',
                       target, *_DEFAULT_TCP)
        return _DEFAULT_TCP
    return host, int(port)


def probe_connectivity(dns_name: str, tcp_target: str,
                       timeout_seconds: float) -> HostProbe:
    """Resolve a name and open a socket, timing both. Never raises — a probe reports, it does not fail."""
    started = datetime.now(timezone.utc)

    dns_start = perf_counter()
    try:
        socket.getaddrinfo(dns_name, None)
        dns_ok = True
    except OSError:
        # Includes Windows' `[Errno 11001] getaddrinfo failed`, which is the exact text the feed
        # failures carried on 2026-09-08.
        dns_ok = False
    dns_ms = (perf_counter() - dns_start) * 1000.0

    host, port = _split_target(tcp_target)
    tcp_start = perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            tcp_ok = True
    except OSError:
        tcp_ok = False
    tcp_ms = (perf_counter() - tcp_start) * 1000.0

    return HostProbe(at=started, dns_ok=dns_ok, dns_ms=dns_ms, tcp_ok=tcp_ok, tcp_ms=tcp_ms)


def format_probe(probe: HostProbe, dns_name: str, tcp_target: str) -> str:
    """One line, readable in a log next to the feed failures it explains."""
    return (f'probe · dns {dns_name} {"ok" if probe.dns_ok else "FAIL"} ({probe.dns_ms:.0f}ms) · '
            f'tcp {tcp_target} {"ok" if probe.tcp_ok else "FAIL"} ({probe.tcp_ms:.0f}ms) · '
            f'{probe.verdict}')
