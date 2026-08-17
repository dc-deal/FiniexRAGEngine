"""Console output encoding — make the reports printable wherever they run.

Every console surface in this project uses typographic characters: `→` in the funnels, `·` as the
field separator, `—` for an empty cell, `⚠` on a warning row, box drawing in the live display.
Twenty-seven of them have **no mapping in cp1252**, and `→` alone appears in 34 files.

That is invisible until stdout is not a terminal. Python picks the stream encoding from the console
when it has one, but falls back to the locale's preferred encoding — cp1252 on a German/Western
Windows — with `errors='strict'` the moment output is **piped or redirected**. So the same command
that renders correctly in the window dies with

    UnicodeEncodeError: 'charmap' codec can't encode character '\\u26a0'

as soon as it is piped into `Select-Object`, `findstr`, or a file. Observed on 2026-08-17 with the
source report; `→` would have done it just as well in a dozen other CLIs.

Chasing the characters would be the wrong fix — it constrains every future report to one legacy
codepage, forever, to serve a case (redirected output) that has no display problem at all. The
boundary is the right place: **UTF-8 out, and never raise.** `errors='replace'` is the safety net
for a console that genuinely cannot render a glyph — a `?` in one cell beats losing the report.

Not Windows-specific despite the usual trigger: a Linux container with `LANG=C` behaves the same.
"""
import sys
from typing import IO, Optional


def use_utf8_output() -> None:
    """Switch stdout/stderr to UTF-8 with replacement, so a report can never fail to print.

    Call once at the top of a CLI's `main()`. Idempotent, and a no-op on a stream that does not
    support reconfiguration (a captured buffer under pytest, for instance).
    """
    _reconfigure(sys.stdout)
    _reconfigure(sys.stderr)


def _reconfigure(stream: Optional[IO[str]]) -> None:
    """Best-effort switch of one stream; every failure mode here is benign."""
    reconfigure = getattr(stream, 'reconfigure', None)
    if reconfigure is None:
        return          # not a TextIOWrapper — a test capture or a custom sink
    try:
        reconfigure(encoding='utf-8', errors='replace')
    except (ValueError, OSError):
        # Detached or already-closed stream. Printing is about to fail for reasons this unit
        # cannot fix, and raising here would replace a readable error with a confusing one.
        pass
