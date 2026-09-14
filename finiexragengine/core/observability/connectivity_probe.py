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
import secrets
import socket
from datetime import datetime, timezone
from time import perf_counter
from typing import Tuple

from finiexragengine.types.ingest_types import HostProbe

logger = logging.getLogger(__name__)

# Fallback when `connectivity_probe_tcp` is not `host:port` — a malformed setting must degrade to a
# working probe rather than to an exception inside an outage response.
_DEFAULT_TCP: Tuple[str, int] = ('1.1.1.1', 53)

# A resolver that is alive answers a name it has never seen — with an address or with NXDOMAIN —
# in tens of milliseconds. One that is unreachable spends the OS retry schedule and then fails.
# So the *duration* is the signal, not the outcome, and this is the line between them.
# 2 s, not 1: a live resolver answers in ~100 ms, and a silent one costs the OS retry
# schedule — seconds to tens of seconds with three servers configured. Nothing real lands in
# between, and the first (cold) query of a process was measured at 1.1 s, which a 1 s line
# would have called an outage.
_RESOLVER_ALIVE_SECONDS = 2.0


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

    # The leg that actually reaches the resolver (2026-09-09). The lookup above is a control: this
    # probe re-asks the same name every 15 s, so the OS answers it from cache in ~1 ms and reports
    # healthy straight through an outage — which is exactly what it did for the first two episodes
    # it measured, while eleven feeds could not resolve anything.
    #
    # A random label under the same domain cannot be cached, so the query has to leave the machine.
    # It will almost certainly come back NXDOMAIN, and that is fine: an answer is an answer. What
    # separates a live resolver from a silent one is how long it took.
    resolver_start = perf_counter()
    try:
        socket.getaddrinfo(f'{secrets.token_hex(6)}.{dns_name}', None)
    except OSError:
        pass
    resolver_ms = (perf_counter() - resolver_start) * 1000.0
    resolver_ok = resolver_ms < _RESOLVER_ALIVE_SECONDS * 1000.0

    host, port = _split_target(tcp_target)
    tcp_start = perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            tcp_ok = True
    except OSError:
        tcp_ok = False
    tcp_ms = (perf_counter() - tcp_start) * 1000.0

    return HostProbe(at=started, dns_ok=dns_ok, dns_ms=dns_ms, tcp_ok=tcp_ok, tcp_ms=tcp_ms,
                     resolver_ok=resolver_ok, resolver_ms=resolver_ms)


def format_probe(probe: HostProbe, dns_name: str, tcp_target: str) -> str:
    """One line, readable in a log next to the feed failures it explains."""
    resolver = '' if probe.resolver_ok is None else (
        f'resolver {"alive" if probe.resolver_ok else "SILENT"} ({probe.resolver_ms:.0f}ms) · ')
    return (f'probe · dns {dns_name} {"ok" if probe.dns_ok else "FAIL"} ({probe.dns_ms:.0f}ms, '
            f'cached) · {resolver}'
            f'tcp {tcp_target} {"ok" if probe.tcp_ok else "FAIL"} ({probe.tcp_ms:.0f}ms) · '
            f'{probe.verdict}')
