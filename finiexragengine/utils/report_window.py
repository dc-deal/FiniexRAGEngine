"""Report-window parsing — pure date arithmetic, no engine dependencies.

Every store-backed report CLI takes the same `--since 7d | 30d | all` window. The parser lived as
a private copy in `perf_cli` and `breaking_cli`; `sources_cli` (ISSUE_76) would have made it three,
so it moved here — the same reasoning that put `normalize_host` in `utils/url.py`. One definition
means one meaning of "7d" across every surface.
"""
from datetime import datetime, timedelta, timezone
from typing import Tuple

# 'all' has to start somewhere; the Unix epoch predates any row this engine will ever hold.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_since(value: str) -> Tuple[datetime, str]:
    """`'7d'` / `'30d'` / `'14'` -> `(since_datetime, label)`; `'all'` -> from the epoch."""
    if value == 'all':
        return _EPOCH, 'all-time'
    days = int(value[:-1] if value.endswith('d') else value)
    return datetime.now(timezone.utc) - timedelta(days=days), f'{days}d'
