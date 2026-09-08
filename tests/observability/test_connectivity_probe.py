"""The host-connectivity probe (2026-09-08) — measuring the silence a back-off creates.

The correlated guard stops a set polling when every feed fails at once, which is right. The cost is
that the engine then learns nothing: eight episodes in one day were each reported as "recovered
after 5m", the back-off's own length, while the neighbouring set — just under the ratio, still
polling — was fetching 265 articles again 21 seconds after the same failure.

These cases cover what the probe may claim, because a probe that overstates is worse than none.
"""
import socket
import time
from datetime import datetime, timezone

import pytest

from finiexragengine.core.observability.connectivity_probe import (
    format_probe,
    probe_connectivity,
)
from finiexragengine.types.ingest_types import HostProbe


class _Sock:
    """The context manager `socket.create_connection` returns — closing is all it has to do."""
    def __enter__(self) -> '_Sock':
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _connects(record: dict = None):
    """A `create_connection` stand-in that succeeds and optionally records the address it got."""
    def _connect(address, timeout=None):
        if record is not None:
            record['address'] = address
        return _Sock()
    return _connect


def _probe(dns_ok: bool, tcp_ok: bool) -> HostProbe:
    return HostProbe(at=datetime.now(timezone.utc), dns_ok=dns_ok, dns_ms=1.0,
                     tcp_ok=tcp_ok, tcp_ms=1.0)


# --- the verdict, which is the whole diagnostic value ---------------------------------------------

@pytest.mark.parametrize('dns_ok, tcp_ok, verdict', [
    (True, True, 'ok'),
    (False, True, 'dns_only'),        # the resolver is the fault; the path is fine
    (True, False, 'transport_only'),  # names resolve (cache, most likely) and packets do not land
    (False, False, 'blocked'),
])
def test_the_verdict_separates_a_resolver_fault_from_a_dead_path(dns_ok, tcp_ok, verdict):
    """`dns_only` is the one worth having: it turns eleven feed failures into one resolver fault."""
    assert _probe(dns_ok, tcp_ok).verdict == verdict


def test_reachable_needs_both_halves():
    """Either half failing means the engine cannot do its work — the verdict says which."""
    assert _probe(True, True).reachable
    assert not _probe(True, False).reachable and not _probe(False, True).reachable


# --- the measurement ------------------------------------------------------------------------------

def test_a_probe_reports_instead_of_raising(monkeypatch):
    """A probe runs *during* an outage. Raising there would turn a diagnostic into a second fault."""
    def _no_dns(*args, **kwargs):
        raise socket.gaierror(11001, 'getaddrinfo failed')

    def _no_socket(*args, **kwargs):
        raise OSError('network is unreachable')

    monkeypatch.setattr(socket, 'getaddrinfo', _no_dns)
    monkeypatch.setattr(socket, 'create_connection', _no_socket)

    probe = probe_connectivity('cloudflare.com', '1.1.1.1:53', 1.0)

    assert probe.verdict == 'blocked'
    assert probe.dns_ms >= 0.0 and probe.tcp_ms >= 0.0


def test_the_dns_half_is_timed_rather_than_bounded_by_the_timeout(monkeypatch):
    """`getaddrinfo` takes no timeout argument, and that unboundedness IS the number worth having.

    On 2026-09-08 a failing ingest pass took 55 seconds against a 10-second per-feed deadline,
    because the OS resolver's own retry schedule is what decides. So the probe is given a timeout
    far shorter than the lookup takes, and the lookup must still complete and still be measured —
    capping it would hide exactly the figure this probe exists to produce.
    """
    asked = []

    def _slow_dns(name, port):
        asked.append(name)
        time.sleep(0.05)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.2.3.4', 0))]

    monkeypatch.setattr(socket, 'getaddrinfo', _slow_dns)
    monkeypatch.setattr(socket, 'create_connection', _connects())

    probe = probe_connectivity('cloudflare.com', '1.1.1.1:53', timeout_seconds=0.001)

    assert asked[0] == 'cloudflare.com', 'the control lookup asks the configured name'
    assert probe.dns_ok, 'a 1ms socket timeout must not cut a 50ms name lookup short'
    assert probe.dns_ms >= 50.0, f'the resolver time is measured, not capped ({probe.dns_ms:.0f}ms)'


def test_a_malformed_tcp_target_degrades_to_a_working_probe(monkeypatch):
    """A typo'd setting must not become a second exception inside an outage response."""
    seen: dict = {}
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [])
    monkeypatch.setattr(socket, 'create_connection', _connects(seen))

    probe_connectivity('cloudflare.com', 'no-port-here', 1.0)

    assert seen['address'] == ('1.1.1.1', 53)


# --- the line an operator reads --------------------------------------------------------------------

def test_the_line_names_both_targets_and_the_verdict():
    """A verdict without its destination is not a measurement anyone can act on."""
    line = format_probe(_probe(False, True), 'cloudflare.com', '1.1.1.1:53')

    assert 'cloudflare.com' in line and '1.1.1.1:53' in line
    assert 'FAIL' in line and 'dns_only' in line


# --- the blind spot the first measured episodes exposed (2026-09-09) ------------------------------

def test_the_resolver_leg_asks_a_name_the_cache_cannot_answer(monkeypatch):
    """The flaw this probe shipped with, and the reason it reported `ok` through two outages.

    It re-asks one fixed name every 15 s, so the OS keeps that entry warm and answers in ~1 ms
    straight through a resolver failure — while eleven feeds, needing names the cache had let go,
    could not resolve at all. A random label under the same domain has to leave the machine.
    """
    asked = []
    monkeypatch.setattr(socket, 'getaddrinfo', lambda name, port: asked.append(name) or [])
    monkeypatch.setattr(socket, 'create_connection', _connects())

    probe_connectivity('cloudflare.com', '1.1.1.1:53', 1.0)

    assert asked[0] == 'cloudflare.com', 'the control lookup keeps asking the plain name'
    assert asked[1] != 'cloudflare.com' and asked[1].endswith('.cloudflare.com'), asked
    assert len(asked[1]) > len('cloudflare.com') + 8, 'the random label must be long enough to miss'


def test_two_probes_never_ask_the_same_resolver_name_twice(monkeypatch):
    """Otherwise the second one is a cache hit and the leg is blind again by the next sample."""
    asked = []
    monkeypatch.setattr(socket, 'getaddrinfo', lambda name, port: asked.append(name) or [])
    monkeypatch.setattr(socket, 'create_connection', _connects())

    probe_connectivity('cloudflare.com', '1.1.1.1:53', 1.0)
    probe_connectivity('cloudflare.com', '1.1.1.1:53', 1.0)

    assert asked[1] != asked[3]


def test_nxdomain_is_an_answer_and_means_the_resolver_is_alive(monkeypatch):
    """A random label almost always comes back NXDOMAIN. That is the resolver *working*."""
    def _nxdomain(name, port):
        if name == 'cloudflare.com':
            return []
        raise socket.gaierror(11001, 'getaddrinfo failed')

    monkeypatch.setattr(socket, 'getaddrinfo', _nxdomain)
    monkeypatch.setattr(socket, 'create_connection', _connects())

    probe = probe_connectivity('cloudflare.com', '1.1.1.1:53', 1.0)

    assert probe.resolver_ok is True, 'a fast NXDOMAIN is an answer, not a failure'
    assert probe.verdict == 'ok'


def test_a_silent_resolver_is_the_verdict_even_while_everything_else_looks_fine(monkeypatch):
    """The exact shape of 2026-09-08/09: TCP to a literal address fine, cached names fine, and
    eleven feeds unable to resolve. Without this leg the probe called that `ok`."""
    def _slow_only_for_the_random_label(name, port):
        if name == 'cloudflare.com':
            return []
        time.sleep(2.2)
        raise socket.gaierror(11001, 'getaddrinfo failed')

    monkeypatch.setattr(socket, 'getaddrinfo', _slow_only_for_the_random_label)
    monkeypatch.setattr(socket, 'create_connection', _connects())

    probe = probe_connectivity('cloudflare.com', '1.1.1.1:53', 1.0)

    assert probe.dns_ok is True and probe.tcp_ok is True     # both controls look healthy
    assert probe.resolver_ok is False
    assert probe.verdict == 'resolver_down'
    assert not probe.reachable


def test_a_probe_without_the_resolver_leg_keeps_its_old_verdicts():
    """`resolver_ok=None` is "not asked", which must never read as "answered badly"."""
    assert _probe(True, True).verdict == 'ok'
    assert _probe(False, True).verdict == 'dns_only'
