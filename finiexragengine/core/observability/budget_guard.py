"""Cost circuit-breaker guard (ISSUE_47) — react to the provider's own quota limit.

The hard stop is the provider: at the account ceiling it refuses the call. Re-implementing a
dollar-accumulator to *predict* that ceiling would be redundant and imprecise (our price table is
an estimate; the real limit lives at the provider). So this guard reacts to the authoritative
signal instead — the source-health quarantine pattern (#49) applied to the paid seam: on a quota
signal, suspend paid work; back off through a cool-off; re-probe once; auto-resume on the first
success. Shared across the embedders + LLM provider; in-memory only (no DB on the hot path).

Deliberately **provider-agnostic**: the guard never inspects a vendor's error vocabulary — the
concrete provider classifies its own failure and calls `on_quota_error` (see
`core/llm/openai_quota.is_quota_exceeded`). A second provider brings its own classifier and
leaves this file untouched.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from finiexragengine.types.config_types.app_config_types import CircuitBreakerConfig

logger = logging.getLogger(__name__)


def _fmt(seconds: int) -> str:
    return f'{seconds // 60}m' if seconds >= 60 else f'{seconds}s'


class BudgetGuard:
    """Suspends paid work when the provider reports quota exhausted, backs off, and re-probes.

    Call sequence at a paid seam: `should_attempt()` before the call; `on_quota_error()` if the
    call returns the quota signal; `record_spend(usd)` after a successful call (clears the suspend
    → auto-resume, and accumulates the warn-only day spend).
    """

    def __init__(self, config: CircuitBreakerConfig, day_spend_start: float = 0.0) -> None:
        self._enabled = config.enabled
        self._reprobe = max(1, config.reprobe_interval_seconds)
        self._soft_daily = config.soft_daily_usd
        self._suspended = False
        self._reason: Optional[str] = None
        self._retry_at: Optional[datetime] = None
        # Warn-only day accumulator (reset at UTC midnight), seeded from the billing log at boot.
        self._day = datetime.now(timezone.utc).date()
        self._day_spend = day_spend_start
        self._soft_warned = False

    def should_attempt(self) -> bool:
        """False while suspended within the cool-off; True (with an immediate re-arm) for the
        single probe once the cool-off elapses — so exactly one doomed call per interval."""
        if not self._enabled or not self._suspended:
            return True
        now = datetime.now(timezone.utc)
        if self._retry_at is not None and now < self._retry_at:
            return False
        self._retry_at = now + timedelta(seconds=self._reprobe)   # probe now, re-arm the window
        return True

    def on_quota_error(self, reason: str = 'quota') -> None:
        """A paid call reported provider quota exhausted — arm (or renew) the suspend.

        `reason` is the provider's actual error code (the seam passes it through), so the log and
        /health name what really came back rather than a hard-coded string. The default stays
        vendor-neutral: naming one provider's code here would leak its vocabulary back into this
        deliberately provider-agnostic unit."""
        newly = not self._suspended
        self._suspended = True
        self._reason = reason
        self._retry_at = datetime.now(timezone.utc) + timedelta(seconds=self._reprobe)
        # Loud once on entry; a failed re-probe is its own quieter line (the 10-min recheck anchor).
        if newly:
            logger.warning('[BUDGET] provider quota reached (%s) — paid work suspended, '
                           're-probe in %s', reason, _fmt(self._reprobe))
        else:
            logger.info('[BUDGET] re-probe failed (%s) — still suspended, next in %s',
                        reason, _fmt(self._reprobe))

    def record_spend(self, usd: float = 0.0) -> None:
        """A paid call SUCCEEDED — quota is available, so clear any suspend (auto-resume) and
        accumulate the warn-only day spend."""
        if self._suspended:
            self._suspended = False
            self._reason = None
            self._retry_at = None
            logger.info('[BUDGET] re-probe ok — paid work resumed')
        self._accumulate(usd)

    def _accumulate(self, usd: float) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._day:                      # new UTC day — reset the warn accumulator
            self._day, self._day_spend, self._soft_warned = today, 0.0, False
        self._day_spend += usd
        if self._soft_daily > 0 and not self._soft_warned and self._day_spend >= self._soft_daily:
            self._soft_warned = True
            logger.warning('[BUDGET] soft daily $%.2f reached (day spend $%.2f) — provider still '
                           'allows spend; watching', self._soft_daily, self._day_spend)

    @property
    def suspended(self) -> bool:
        return self._suspended

    def status(self) -> dict:
        """Guard state for /health (ISSUE_47)."""
        return {'enabled': self._enabled, 'suspended': self._suspended, 'reason': self._reason,
                'retry_at': self._retry_at.isoformat() if self._retry_at else None,
                'day_spend_usd': round(self._day_spend, 6), 'soft_daily_usd': self._soft_daily}
