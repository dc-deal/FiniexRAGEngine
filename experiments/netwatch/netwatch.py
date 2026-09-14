"""Watch the host's own connectivity from outside the engine (2026-09-08).

On 2026-09-08 the engine reported eight host-connectivity episodes in one day, having reported none
in the thirteen days before. What it could not say was how long each lasted: the correlated-failure
guard stops polling for five minutes, so every episode was reported as "recovered after 5m" — the
back-off's own length. The engine now probes during that window, but a measurement taken by the
process that is failing cannot separate "our egress is down" from "this machine's stack is broken".
This script is the second opinion, and it runs whether or not the engine does.

What it records, once per second:

- **a TCP connect to a literal address** — no name lookup in front of it, so the transport is tested
  on its own;
- **a DNS lookup of a name the engine does not poll** — a host fetched every 15 s stays in the OS
  resolver cache and answers straight through an outage, which is exactly why the same event looked
  like two different faults in the engine's log.

DNS is resolved **on a worker thread with a deadline**, which is not a detail: `getaddrinfo` takes no
timeout argument, the OS resolver's own retry schedule decides, and on 2026-09-08 that turned a
10-second per-feed deadline into a 55-second ingest pass. A sampler that called it inline would
freeze for tens of seconds during precisely the window it exists to measure.

Output is **one line per state change plus a heartbeat**, not one line per second: the transitions
are the evidence, and a day of "still fine" is 80 kB instead of 5 MB. Every line carries local time
*and* UTC, because the engine's log is stamped local while everything else it produces is UTC —
correlating the two should not require arithmetic at 3 a.m.

    python experiments/netwatch/netwatch.py                    # defaults, Ctrl-C to stop
    python experiments/netwatch/netwatch.py --log C:\\temp\\netwatch.log

Stdlib only, so it runs in any interpreter the engine itself runs in.
"""
import argparse
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

# A name the engine does NOT poll. A host we fetch every 15 s stays in the resolver cache and would
# report healthy through an outage — the effect that made 2026-09-08 read as two separate faults.
_DNS_NAME = 'cloudflare.com'
# A literal address, so the transport is measured without a lookup in front of it.
_TCP_TARGET = '1.1.1.1:53'


def _stamp(moment: datetime) -> str:
    """Local time with its offset, and the UTC instant beside it.

    Both, deliberately. The engine's log formatter stamps the OS clock while every other surface it
    produces is UTC; a diagnostic meant to be read next to that log carries the conversion already
    done, or it becomes one more thing to get wrong under pressure.
    """
    return (f'{moment.astimezone().isoformat(timespec="milliseconds")} '
            f'({moment.astimezone(timezone.utc).strftime("%H:%M:%SZ")})')


def _tcp_probe(host: str, port: int, timeout: float) -> Tuple[bool, float]:
    """Open and close a socket; report success and how long it took."""
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ok = True
    except OSError:
        ok = False
    return ok, (time.perf_counter() - started) * 1000.0


# A resolver that is alive answers a name it has never seen — with an address or with NXDOMAIN —
# in tens of milliseconds. One that is unreachable spends the OS retry schedule first. The
# *duration* is the signal, not the outcome.
# 2 s, not 1: a live resolver answers in ~100 ms, a silent one costs the OS retry schedule.
# The first (cold) query of a process was measured at 1.1 s — a 1 s line would call that an
# outage on the very first sample.
_RESOLVER_ALIVE_SECONDS = 2.0


class _DnsProbe:
    """A name lookup that the sampler can give up waiting for.

    `getaddrinfo` cannot be interrupted and takes no timeout, so it runs on its own thread. When the
    deadline passes the sampler records the lookup as still outstanding and moves on — the thread
    finishes in its own time and its result is simply discarded. At most one lookup is in flight, so
    a resolver stalled for a minute costs one thread rather than sixty.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._thread: Optional[threading.Thread] = None
        self._result: Optional[bool] = None
        self._started: float = 0.0

    def sample(self, deadline: float) -> Tuple[Optional[bool], float]:
        """`(ok, ms)` — `ok is None` means the resolver had not answered by the deadline."""
        if self._thread is None or not self._thread.is_alive():
            self._result = None
            self._started = time.perf_counter()
            self._thread = threading.Thread(target=self._resolve, daemon=True)
            self._thread.start()
        self._thread.join(timeout=deadline)
        elapsed = (time.perf_counter() - self._started) * 1000.0
        if self._thread.is_alive():
            return None, elapsed          # still blocked — the interesting state, not an error
        return self._result, elapsed

    def _resolve(self) -> None:
        """Ask for a name the OS cannot have cached (2026-09-09).

        Asking for the *configured* name would be answered from cache in ~1 ms — this sampler
        re-asks it every second, so it keeps its own entry warm and reports healthy straight
        through a resolver outage. The engine's own probe shipped with exactly that flaw and
        reported `ok` through two measured episodes while eleven feeds could not resolve.

        A random label under the same domain has to leave the machine. NXDOMAIN is a perfectly
        good answer: what separates a live resolver from a silent one is how long it took, which
        the sampler already measures.
        """
        started = time.perf_counter()
        try:
            socket.getaddrinfo(f'{secrets.token_hex(6)}.{self._name}', None)
        except OSError:
            # Includes Windows' `[Errno 11001] getaddrinfo failed`, the exact text the engine's
            # feed failures carried on 2026-09-08 — and the answer NXDOMAIN also arrives here.
            pass
        self._result = (time.perf_counter() - started) < _RESOLVER_ALIVE_SECONDS


def _icmp_probe(host: str, timeout: float) -> Optional[bool]:
    """Can the host be *reached* at all, without opening a connection?

    Only ever called when the TCP leg has already failed, and that restriction is the design: it
    costs a process spawn, which on a machine already running MT5 and two tick collectors is not
    something to pay 86,400 times a day for a question that only matters during an outage.

    The distinction it buys is the one the Kraken collector raised (2026-09-08): its WebSocket has
    stood for 466 hours with **zero** reconnects, straight through eight connectivity episodes. An
    established flow surviving while every new connection fails is not generic packet loss — it is
    new connections being refused, which is a different fault with a different owner. ICMP needs no
    connection, so `ping ok + tcp FAIL` says exactly that, and `ping FAIL + tcp FAIL` says the path.

    Shelling out to the OS `ping` rather than opening a raw socket: raw ICMP needs privileges and
    platform-specific packing, and a diagnostic that only runs as Administrator is one that will not
    be running when it is needed. `None` = the probe itself could not be taken.
    """
    windows = os.name == 'nt'
    command = ['ping', '-n' if windows else '-c', '1',
               '-w' if windows else '-W',
               str(int(timeout * 1000)) if windows else str(max(1, int(timeout))),
               host]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout + 2.0)
    except (OSError, subprocess.SubprocessError):
        return None
    # Windows' ping returns 0 even for "Destination host unreachable", so the body is checked too.
    if completed.returncode != 0:
        return False
    text = completed.stdout.decode('utf-8', 'replace').lower()
    return 'ttl=' in text or 'time=' in text


def _describe(tcp_ok: bool, dns_ok: Optional[bool], icmp_ok: Optional[bool] = None) -> str:
    """The state, in the words the engine's own probe uses, so both logs read alike."""
    if not tcp_ok and icmp_ok:
        # Reachable, but no new connection can be opened. The Kraken WebSocket standing through
        # every episode says this is the shape to watch for.
        return 'connect_blocked'
    if dns_ok is None:
        return 'dns_pending' if tcp_ok else 'blocked'
    if tcp_ok and dns_ok:
        return 'ok'
    if tcp_ok:
        return 'dns_only'
    if dns_ok:
        return 'transport_only'
    return 'blocked'


def watch(log_path: Path, dns_name: str, tcp_target: str, timeout: float,
          heartbeat_seconds: float, max_mb: float, echo: bool = False) -> None:
    host, _, port = tcp_target.rpartition(':')
    dns = _DnsProbe(dns_name)
    last_state = ''
    next_heartbeat = 0.0

    def emit(line: str) -> None:
        # Console output is OFF by default when writing to a file, and that is a defect fix rather
        # than a preference (2026-09-10). A Windows console in QuickEdit mode blocks the next write
        # for as long as text is selected — one stray click, and `print()` suspends this whole loop.
        # It happened: the watcher froze at 19:18 and resumed at 08:51 the next morning, leaving a
        # 13.5-hour hole that reads exactly like a quiet night and is nothing of the kind. The log
        # file is the record; the console is a convenience, and a convenience must not be able to
        # stop the measurement.
        if echo:
            print(line, flush=True)
        # Appending per line rather than holding a handle: the file stays readable (and copyable)
        # while this runs, which is how it will actually be used.
        with log_path.open('a', encoding='utf-8') as handle:
            handle.write(line + '\n')

    emit(f'{_stamp(datetime.now(timezone.utc))} netwatch start · tcp {tcp_target} · '
         f'dns {dns_name} · timeout {timeout}s · heartbeat {heartbeat_seconds:.0f}s')
    # Wall-clock of the previous sample, so a stall announces itself instead of looking like calm.
    last_sample = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            # A hole in the record is indistinguishable from an uneventful stretch — unless the
            # record says so. Anything past two heartbeats means this process was not running, and
            # reading that silence as "no outages" is the exact mistake it invites.
            stalled = now - last_sample
            if stalled > heartbeat_seconds * 2:
                emit(f'{_stamp(datetime.now(timezone.utc))} GAP             '
                     f'no samples for {stalled:.0f}s — the watcher was not running, '
                     f'this window is UNMEASURED')
                last_state = ''          # force the next sample to print, whatever it finds
            last_sample = now
            tcp_ok, tcp_ms = _tcp_probe(host, int(port), timeout)
            dns_ok, dns_ms = dns.sample(timeout)
            # Only when the connection failed — see `_icmp_probe` for why this is not paid for in
            # the healthy state.
            icmp_ok = None if tcp_ok else _icmp_probe(host, timeout)
            state = _describe(tcp_ok, dns_ok, icmp_ok)
            # A change is always written; the heartbeat only proves the watcher is still alive.
            if state != last_state or now >= next_heartbeat:
                marker = '' if state == last_state else '  <-- CHANGED'
                icmp = '' if icmp_ok is None else \
                    f' icmp={"ok" if icmp_ok else "FAIL"}'
                emit(f'{_stamp(datetime.now(timezone.utc))} {state:15} '
                     f'tcp={"ok" if tcp_ok else "FAIL"} ({tcp_ms:.0f}ms) '
                     f'dns={"ok" if dns_ok else "FAIL" if dns_ok is False else "pending"} '
                     f'({dns_ms:.0f}ms){icmp}{marker}')
                last_state = state
                next_heartbeat = now + heartbeat_seconds
            if max_mb and log_path.exists() and log_path.stat().st_size > max_mb * 1024 * 1024:
                # A cap rather than rotation: this is a diagnostic someone starts deliberately and
                # stops deliberately, and silently deleting its own evidence would be worse.
                emit(f'{_stamp(datetime.now(timezone.utc))} netwatch STOPPED — log reached '
                     f'{max_mb} MB. Copy it away and restart if the incident is still open.')
                return
            time.sleep(1.0)
    except KeyboardInterrupt:
        emit(f'{_stamp(datetime.now(timezone.utc))} netwatch stopped by operator')


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--log', default='netwatch.log', help='where to append (default: ./netwatch.log)')
    parser.add_argument('--dns', default=_DNS_NAME,
                        help='domain to ask under; a random label is prefixed so the OS cache '
                             'cannot answer for it')
    parser.add_argument('--tcp', default=_TCP_TARGET, help='literal host:port to connect to')
    parser.add_argument('--timeout', type=float, default=2.0, help='per-probe deadline in seconds')
    parser.add_argument('--heartbeat', type=float, default=60.0,
                        help='seconds between "still the same" lines (0 = every sample)')
    parser.add_argument('--max-mb', type=float, default=50.0, help='stop when the log reaches this (0 = no cap)')
    parser.add_argument('--echo', action='store_true',
                        help='also print to the console — off by default, because a Windows '
                             'console in QuickEdit mode suspends the writer on a stray click')
    args = parser.parse_args(argv)
    watch(Path(args.log), args.dns, args.tcp, args.timeout, args.heartbeat, args.max_mb,
          echo=args.echo)
    return 0


if __name__ == '__main__':
    sys.exit(main())
