"""The host connectivity watcher (2026-09-08) — the second opinion, when the engine is the suspect.

`experiments/netwatch/netwatch.py` runs beside the engine on the server and answers the one question
a probe inside the failing process cannot: is this machine's egress down, or is this machine's stack
broken. What is tested here is the part that can be wrong without looking wrong — the vocabulary it
reports in, and the deadline that keeps it sampling while a resolver hangs.
"""
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'experiments' / 'netwatch'))

import netwatch                                                  # noqa: E402


# --- the words, which have to match the engine's own probe ---------------------------------------

@pytest.mark.parametrize('tcp_ok, dns_ok, state', [
    (True, True, 'ok'),
    (True, False, 'dns_only'),         # the resolver is the fault; the path is fine
    (False, True, 'transport_only'),   # names resolve (from cache) and packets do not land
    (False, False, 'blocked'),
    (True, None, 'dns_pending'),       # the resolver has not answered yet — a state, not an error
    (False, None, 'blocked'),          # with the path down too, a pending lookup adds nothing
])
def test_the_state_word_matches_the_engine_vocabulary(tcp_ok, dns_ok, state):
    """Two logs get read side by side during an incident; one vocabulary or neither is trusted."""
    assert netwatch._describe(tcp_ok, dns_ok) == state


# --- the deadline, which is why this samples at all -----------------------------------------------

def test_a_hanging_resolver_does_not_freeze_the_sampler(monkeypatch):
    """`getaddrinfo` cannot be interrupted and takes no timeout.

    On 2026-09-08 that turned a 10-second per-feed deadline into a 55-second ingest pass. A sampler
    that called it inline would stop sampling for exactly as long as the outage it is measuring, so
    the lookup runs on its own thread and the sampler gives up waiting.
    """
    monkeypatch.setattr(socket, 'getaddrinfo', lambda name, port: time.sleep(5) or [])
    probe = netwatch._DnsProbe('cloudflare.com')

    started = time.perf_counter()
    ok, elapsed_ms = probe.sample(deadline=0.2)
    waited = time.perf_counter() - started

    assert ok is None, 'a lookup that has not answered is pending, not a failure'
    assert waited < 1.0, f'the sampler waited {waited:.2f}s on a hanging resolver'
    assert elapsed_ms >= 200.0


def test_a_stalled_lookup_costs_one_thread_and_not_one_per_second(monkeypatch):
    """A minute of stalled resolver must not leave sixty threads behind it."""
    monkeypatch.setattr(socket, 'getaddrinfo', lambda name, port: time.sleep(3) or [])
    probe = netwatch._DnsProbe('cloudflare.com')
    before = threading.active_count()

    for _ in range(10):
        assert probe.sample(deadline=0.02)[0] is None

    assert threading.active_count() - before <= 1


def test_a_resolver_that_answers_is_reported_as_answering(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda name, port: [('inet', 'stream')])
    probe = netwatch._DnsProbe('cloudflare.com')

    ok, elapsed_ms = probe.sample(deadline=2.0)

    assert ok is True and elapsed_ms >= 0.0


def test_a_fast_nxdomain_means_the_resolver_answered(monkeypatch):
    """The semantics changed on 2026-09-09, and this is the change.

    The leg now asks a **random** label, which comes back NXDOMAIN — and that is the resolver
    *working*. Reading a raised `gaierror` as failure would call every healthy sample an outage.
    What separates alive from silent is how long it took.
    """
    def _fast_nxdomain(name, port):
        raise socket.gaierror(11001, 'getaddrinfo failed')

    monkeypatch.setattr(socket, 'getaddrinfo', _fast_nxdomain)

    assert netwatch._DnsProbe('cloudflare.com').sample(deadline=2.0)[0] is True


def test_a_silent_resolver_is_read_as_failure_by_its_duration(monkeypatch):
    """The failure this leg exists for: no server answers, and the OS spends its retry schedule."""
    def _silent(name, port):
        time.sleep(netwatch._RESOLVER_ALIVE_SECONDS + 0.3)
        raise socket.gaierror(11001, 'getaddrinfo failed')

    monkeypatch.setattr(socket, 'getaddrinfo', _silent)
    probe = netwatch._DnsProbe('cloudflare.com')

    # Long enough for the thread to finish, so this is a verdict rather than a pending sample.
    assert probe.sample(deadline=netwatch._RESOLVER_ALIVE_SECONDS + 2.0)[0] is False


def test_the_resolver_leg_never_asks_the_plain_name(monkeypatch):
    """Asking the configured name would be a cache hit, which is the blind spot being removed."""
    asked = []
    monkeypatch.setattr(socket, 'getaddrinfo', lambda name, port: asked.append(name) or [])
    probe = netwatch._DnsProbe('cloudflare.com')

    probe.sample(deadline=2.0)
    time.sleep(0.05)

    assert asked and asked[0] != 'cloudflare.com' and asked[0].endswith('.cloudflare.com'), asked


# --- the transport half ----------------------------------------------------------------------------

def test_an_unreachable_target_fails_at_the_deadline_rather_than_hanging():
    """A literal address is used precisely so this measures the path and nothing else."""
    ok, elapsed_ms = netwatch._tcp_probe('10.255.255.1', 9, timeout=0.3)

    assert ok is False
    assert elapsed_ms < 2000.0, 'the connect ignored its timeout'


# --- the stamp, which is this project's oldest trap --------------------------------------------------

def test_every_line_carries_both_clocks(monkeypatch):
    """The engine's log is stamped local; everything else it produces is UTC.

    Correlating the two cost an RDP session once. A diagnostic written to be read *next to* that log
    carries the conversion already done, or it becomes one more thing to get wrong at 3 a.m.
    """
    from datetime import datetime, timezone

    stamp = netwatch._stamp(datetime(2026, 9, 8, 20, 18, 3, tzinfo=timezone.utc))

    assert stamp.endswith('(20:18:03Z)')
    assert 'T' in stamp.split(' ')[0], 'the local half is ISO-8601, like the engine formatter'


# --- the discriminator the Kraken collector's reconnect counter asked for ---------------------------

@pytest.mark.parametrize('tcp_ok, dns_ok, icmp_ok, state', [
    (False, True, True, 'connect_blocked'),    # reachable, but no NEW connection gets through
    (False, False, True, 'connect_blocked'),   # ...and the resolver's new flows fail with them
    (False, True, False, 'transport_only'),    # nothing gets through at all
    (False, False, False, 'blocked'),
    (True, True, None, 'ok'),                  # healthy: no ping is spawned, so no verdict from it
])
def test_icmp_separates_a_blocked_connection_from_a_dead_path(tcp_ok, dns_ok, icmp_ok, state):
    """The shape the Kraken collector pointed at (2026-09-08).

    Its WebSocket stood for 466 hours with **zero** reconnects, straight through eight connectivity
    episodes, while every feed poll — each of which opens a new connection — failed. An established
    flow surviving while new ones are refused is not generic packet loss; it is a different fault
    with a different owner, and ICMP needs no connection to say so.
    """
    assert netwatch._describe(tcp_ok, dns_ok, icmp_ok) == state


def test_the_ping_is_only_paid_for_when_the_connection_already_failed():
    """A process spawn per second, on a box already running MT5 and two tick collectors, for a
    question that only matters during an outage — the healthy path must not pay it."""
    source = Path(netwatch.__file__).read_text(encoding='utf-8')

    assert 'None if tcp_ok else _icmp_probe' in source, (
        'the ICMP leg must be conditional on the TCP leg having failed')


def test_a_missing_ping_binary_degrades_to_no_verdict(monkeypatch):
    """A diagnostic that raises because a tool is absent is worse than one that says less."""
    def _no_binary(*args, **kwargs):
        raise FileNotFoundError('ping')

    monkeypatch.setattr(netwatch.subprocess, 'run', _no_binary)

    assert netwatch._icmp_probe('1.1.1.1', 1.0) is None


def test_windows_ping_returning_zero_for_unreachable_is_not_read_as_success(monkeypatch):
    """`ping` on Windows exits 0 for "Destination host unreachable", which is the whole trap."""
    class _Completed:
        returncode = 0
        stdout = b'Reply from 10.0.0.1: Destination host unreachable.'

    monkeypatch.setattr(netwatch.subprocess, 'run', lambda *args, **kwargs: _Completed())

    assert netwatch._icmp_probe('1.1.1.1', 1.0) is False


def test_a_real_reply_is_read_as_reachable(monkeypatch):
    class _Completed:
        returncode = 0
        stdout = b'Reply from 1.1.1.1: bytes=32 time=11ms TTL=57'

    monkeypatch.setattr(netwatch.subprocess, 'run', lambda *args, **kwargs: _Completed())

    assert netwatch._icmp_probe('1.1.1.1', 1.0) is True
