"""The probe's own record (ISSUE_67) — what the page said, week after week.

One row per probed model per run, append-only. The point is not the single reading: it is the
history that says, months from now, whether this probe is reliable enough to ever be trusted beyond
shadow mode. A guard nobody can audit is a guard nobody should promote.

**A failed write is logged and swallowed**, like the other provenance stores here: losing a row
costs an explanation, and a weekly job that died because its bookkeeping failed would be the worse
trade. The notification has already gone out by then.
"""
import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

import psycopg

from finiexragengine.types.config_types.app_config_types import PricingConfig
from finiexragengine.types.pricing_types import ProbedPrice

logger = logging.getLogger(__name__)


class PriceProbeStore:
    """Appends probe rows into `price_probes`."""

    def __init__(self, database_url: str, table: str = 'price_probes') -> None:
        self._database_url = database_url
        self._TABLE = table

    def record(self, pricing: PricingConfig, prices: Sequence[ProbedPrice], *, source_url: str,
               probe_model: str, readable: bool = True, at: Optional[datetime] = None) -> int:
        """Persist one run; returns the number of rows written (0 on a swallowed failure).

        An unreadable page still writes one row per configured model, with `status='unreadable'` and
        no probed price. That is deliberate: the absence of rows and a failing probe would otherwise
        look the same in the history, and telling them apart is the whole reason `status` exists.
        """
        stamp = at or datetime.now(timezone.utc)
        rows = []
        for model, price in sorted(pricing.models.items()):
            probed = next((entry for entry in prices if entry.model == model), None)
            status = 'unreadable' if not readable else (probed.status if probed else 'absent')
            probed_in = probed.input_per_1k if (probed and status == 'ok') else None
            probed_out = probed.output_per_1k if (probed and status == 'ok') else None
            rows.append((stamp, model, source_url, status, probed_in, probed_out,
                         price.input_per_1k, price.output_per_1k,
                         _delta(price.input_per_1k, probed_in),
                         _delta(price.output_per_1k, probed_out), probe_model))
        try:
            with psycopg.connect(self._database_url) as conn, conn.cursor() as cur:
                cur.executemany(
                    f'INSERT INTO {self._TABLE} (ts, model, source_url, status, '
                    'probed_input_per_1k, probed_output_per_1k, table_input_per_1k, '
                    'table_output_per_1k, delta_input_pct, delta_output_pct, probe_model) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)', rows)
            return len(rows)
        except psycopg.Error as exc:
            logger.warning('price probe not recorded (provenance only, the notice already went '
                           'out): %s', exc)
            return 0


def _delta(table_value: float, probed_value: Optional[float]) -> Optional[float]:
    """Signed percentage against the table; None where there is nothing to compare."""
    if probed_value is None or not table_value:
        return None
    return 100.0 * (probed_value - table_value) / table_value
