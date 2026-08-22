"""Compact relative age — the one vocabulary every operator-facing surface ages in.

`45s` · `15m` · `9h22m`. Byte-identical private copies lived in `stall_watchdog` and
`live_display`; `ingest_worker` needed a third and **called it without importing it** — which
stayed invisible until the first feed entered quarantine on 2026-08-20 and took the crypto ingest
worker down with a `NameError` for 37 hours. One definition, imported, is the version of this that
cannot be called without existing (ISSUE_82 follow-up).

Same reasoning that moved `parse_window` here from three CLI copies: one definition means one
meaning across every surface.
"""


def format_age(seconds: float) -> str:
    """Compact relative age: `45s` · `15m` · `9h22m`.

    Seconds below 90 so a fresh event reads exactly rather than rounding to `1m`; minutes up to an
    hour; `HhMM` beyond, zero-padded so a column of ages stays aligned.
    """
    if seconds < 90:
        return f'{seconds:.0f}s'
    if seconds < 3600:
        return f'{seconds / 60:.0f}m'
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f'{hours}h{minutes:02d}m'
