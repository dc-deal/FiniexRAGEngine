"""Shapes the report catalog exchanges with its callers (ISSUE_104).

The catalog lives in `core/observability/reports/`; the API router and the CLIs both call it, so the
parameter and listing shapes cross a seam and live here. The reports' own row/section dataclasses do
NOT — they stay with the report that builds them, because nothing outside it constructs one.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class ReportParams:
    """Everything a caller can narrow a report by — transport-agnostic.

    Deliberately one flat shape rather than one per report: the router builds it from query
    parameters, a CLI from `argparse`, and the catalog's specs declare which of the fields they
    actually accept. A per-report parameter class would put the same three fields in four places.

    `since` is already resolved (and, on the HTTP path, already clamped to the configured ceiling) —
    the catalog receives a window, never a window *expression*, so nothing downstream has to know
    how `7d` is spelled.
    """
    since: Optional[datetime] = None
    window_label: Optional[str] = None      # what to render as the window line, e.g. '7d'
    source_id: Optional[str] = None
    episode_start: Optional[datetime] = None
    symbol: Optional[str] = None            # narrows a per-symbol series; empty = every symbol
    # Report-specific resolved values (caps, window sets) — each builder reads the keys it declared
    # defaults for. Generic on purpose: the resolution machinery is one code path for every report,
    # and what a value *means* stays with the report that asked for it.
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReportListingEntry:
    """One catalog entry as `GET /v1/reports` presents it — what it is and how to narrow it.

    `defaults` carries the CONFIGURED values, not the code's, so the listing and a call can never
    advertise different things.
    """
    name: str
    summary: str
    params: List[str]
    required: List[str]
    defaults: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AppliedParam:
    """One parameter as it was actually applied, and where it came from (ISSUE_104).

    The reason this exists at all: precedence is only safe when it is announced. This codebase has
    now been bitten three times by a value that meant something other than its name suggested — a
    warn-only line read as a spend cap, an exemption switch that removed a rate limit instead of
    adding a token, a day accumulator that resets on restart. Each time the cure was the same:
    state what actually happened. So every report answer says which value it used and whether that
    came from the configuration or from the call.
    """
    value: Any
    source: str                 # 'config' | 'request'
    clamped: bool = False       # true when a bound shortened what was asked for


@dataclass
class ResolvedReport:
    """What a call resolved to: the builder's inputs, plus the provenance to echo back."""
    params: ReportParams
    applied: Dict[str, AppliedParam] = field(default_factory=dict)
