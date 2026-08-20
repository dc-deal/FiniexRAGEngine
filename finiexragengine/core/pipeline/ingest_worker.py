"""Ingest worker — clocks one source-set's acquisition (ISSUE_10)."""
import asyncio
import logging
from contextlib import ExitStack
from datetime import datetime, timezone
from time import perf_counter
from typing import Callable, Dict, List, Optional, Set

from finiexragengine.core.observability.cost_recorder import CostRecorder, PassSpend
from finiexragengine.core.pipeline.ingestor import Ingestor
from finiexragengine.core.triggers.abstract_trigger import AbstractTrigger
from finiexragengine.core.ui.engine_stats import (
    EngineStats,
    IngestSnapshot,
    SourcesSnapshot,
)
from finiexragengine.types.alert_types import AlertCallback
from finiexragengine.types.config_types.source_set_types import SourceSetConfig
from finiexragengine.types.ingest_types import HostEvent, IngestResult, SourcePoll
from finiexragengine.types.trigger_types import TriggerReason
from finiexragengine.types.worker_types import WorkerState

logger = logging.getLogger(__name__)

# A feed polled less than this many times its expected cadence reads as stuck, not merely slow.
_OVERDUE_FACTOR = 2.0


def _overdue_feeds(last_ok: Dict[str, datetime], expected: Dict[str, int],
                   now: datetime, skip: Set[str]) -> List[str]:
    """Feeds whose last successful poll is overdue vs their expected cadence — 'is it still alive?'.

    A healthy slow feed (its own `poll_interval_seconds`, politeness) cycles ok → floor_skip → ok,
    so its `last_ok` stays within its interval; only a feed that stopped polling for more than
    `_OVERDUE_FACTOR`× its expected gap is flagged. A feed already named this pass (quarantined /
    failed) is skipped to avoid a double marker; a feed never yet polled is normal at startup.
    """
    overdue: List[str] = []
    for source_id, interval in expected.items():
        if source_id in skip:
            continue
        last = last_ok.get(source_id)
        if last is None:
            continue
        overdue_s = (now - last).total_seconds()
        if overdue_s > interval * _OVERDUE_FACTOR:
            overdue.append(f'{source_id} overdue {int(overdue_s / 60)}m')
    return overdue


def _quarantine_chip(poll: SourcePoll, now: datetime) -> str:
    """`ecb_press q 42m (2/3)` — how long the cool-off still runs, and which rung it is on.

    The rung is the part that carries information (ISSUE_84): "wait an hour" and "this feed is
    effectively gone" were the same word before. Falls back to the bare marker when the ingestor
    could not resolve a rung (no episode row — e.g. a quarantine written before ISSUE_84).
    """
    left = ''
    if poll.until is not None:
        left = f' {_format_age((poll.until - now).total_seconds())}'
    if poll.rung is None:
        return f'{poll.source_id} quarantined{left}'
    return f'{poll.source_id} q{left} ({poll.rung[0] + 1}/{poll.rung[1]})'


class IngestWorker:
    """Runs fetch -> embed-only-new -> upsert for ONE source-set on its own cadence.

    Cheap and time-critical by design: RSS windows slide, a missed article is gone
    forever — so this clocks faster than eval and never touches the LLM. One worker
    feeds every pipeline referencing the set (1x fetch, Nx read). A failing pass is
    logged and the loop continues — the corpus is append-only, the next tick heals.
    """

    def __init__(self, source_set: SourceSetConfig, ingestor: Ingestor,
                 trigger: AbstractTrigger, pass_timeout_seconds: int = 300,
                 cost_recorder: Optional[CostRecorder] = None,
                 on_candidates: Optional[Callable[[int], None]] = None,
                 engine_stats: Optional[EngineStats] = None,
                 on_host_event: Optional[AlertCallback] = None) -> None:
        self._ingestor = ingestor
        self._trigger = trigger
        # Wall-clock deadline for one pass (ISSUE_74). There is deliberately NO lock here any
        # more: the workers used to share one, which is what let a single hung feed hold every
        # worker hostage for nine days on 2026-08-01. Self-overlap is impossible without it —
        # the trigger awaits the pass before computing its next wait.
        self._pass_timeout_seconds = pass_timeout_seconds
        self._cost_recorder = cost_recorder
        # Optional (ISSUE_11): called with the highest importance tier flagged this pass, to
        # nudge the eval workers on this set out-of-band (the breaking bus). None = no wake.
        self._on_candidates = on_candidates
        # Optional (ISSUE_26): the live dashboard's shared state. None = no display (the
        # /health-only and CLI paths), in which case every push below is skipped — zero overhead.
        self._engine_stats = engine_stats
        # Optional (ISSUE_84): where a set-wide connectivity event is announced. Reuses the
        # watchdog's alert seam, so Telegram wiring lives in exactly one place and this worker
        # only knows "there is somewhere to say it". None = log and dashboard only.
        self._on_host_event = on_host_event
        # Per-feed expected cadence (its own poll_interval / politeness, else the set's interval)
        # + the last successful poll, so a stuck slow feed can be flagged overdue on the dashboard.
        set_interval = source_set.trigger.interval_seconds
        self._expected: Dict[str, int] = {
            source.source_id: (source.poll_interval_seconds or set_interval)
            for source in source_set.active_sources()}
        self._last_ok: Dict[str, datetime] = {}
        self._state = WorkerState(name=f'ingest:{source_set.source_set_id}',
                                  kind='ingest',
                                  interval_seconds=source_set.trigger.interval_seconds)

    def get_state(self) -> WorkerState:
        return self._state

    def set_host_alert(self, alert: Optional[AlertCallback]) -> None:
        """Give connectivity events a voice (ISSUE_84), once a channel exists.

        A setter rather than a constructor argument for the same reason the stall watchdog has
        one: the workers are assembled before the Telegram client, and detection must never wait
        on delivery being configured."""
        self._on_host_event = alert

    async def start(self) -> None:
        await self._trigger.start(self._pass)

    async def stop(self) -> None:
        await self._trigger.stop()

    async def _pass(self, reason: TriggerReason) -> None:
        with ExitStack() as stack:
            started = perf_counter()
            # This pass's own spend accumulator (ISSUE_74) — entered on the event loop, so the
            # context copy `asyncio.to_thread` makes carries it into the worker thread. Replaces a
            # session delta against the shared recorder, which was only correct while every pass
            # was serialized. Without a recorder there is nothing to account. The scope also binds
            # WHY the pass runs (ISSUE_87), so every embed row it writes carries it — the ingest
            # path has no envelope to carry the fact.
            spend = (stack.enter_context(self._cost_recorder.pass_scope(reason))
                     if self._cost_recorder else PassSpend())
            self._state.last_run_at = datetime.now(timezone.utc)
            try:
                # The pass body is synchronous (feeds, OpenAI, psycopg) — run it in a
                # thread so the event loop keeps serving the API while we work. The deadline
                # abandons the *await*, not the thread (a blocked thread cannot be cancelled):
                # the worker resumes next tick instead of staying dead until a restart.
                result = await asyncio.wait_for(asyncio.to_thread(self._ingestor.run),
                                                timeout=self._pass_timeout_seconds)
            # Every branch opens its detail with the pass's reason (ISSUE_87) — the ingest twin of
            # the eval worker's line, and the only place an ingest pass can show it at all.
            except asyncio.TimeoutError:
                self._state.last_status = 'error'
                self._state.last_detail = (f'{reason} · pass exceeded '
                                           f'{self._pass_timeout_seconds}s deadline')
                logger.warning('[%s] %s pass exceeded %ds deadline — abandoned, next tick continues',
                               self._state.name, reason, self._pass_timeout_seconds)
            except Exception as exc:   # noqa: BLE001 — a pass must never kill the loop
                self._state.last_status = 'error'
                self._state.last_detail = f'{reason} · {exc}'
                logger.exception('[%s] %s pass failed — next tick continues',
                                 self._state.name, reason)
            else:
                usd = spend.usd
                self._state.last_status = 'ok'
                # Prefix a suspended pass (provider quota, ISSUE_47) so it is visible, not silent.
                prefix = f'{reason} · ' + ('suspended (quota) · ' if result.suspended else '')
                # Tokens belong in `last_detail`, not only in the log call: this one string is
                # what the log line, the activity stream AND /health all render (ISSUE_79). Split
                # across two places it produced three different versions of the same pass.
                self._state.last_detail = (f'{prefix}fetched {result.fetched} · '
                                           f'embedded {result.embedded} · '
                                           f'stored {result.stored} · '
                                           f'{result.embed_tokens} tok')
                # Embed-stage deviations ride the line only when non-zero (ISSUE_79) — the same
                # exception-density idiom the quarantine count uses below.
                if result.truncated:
                    self._state.last_detail += f' · {result.truncated} truncated'
                if result.rejected:
                    self._state.last_detail += f' · {result.rejected} rejected'
                # Surface breaking candidates in the pass line when any were flagged (ISSUE_11).
                if result.candidates:
                    self._state.last_detail += f' · flagged {result.candidates} breaking'
                # Sources the pass did not poll ride along on the pass line rather than getting
                # their own log entries: a quarantine lasts hours, so on a 15s cadence a per-skip
                # line would emit thousands of identical repeats. Here the count is visible on a
                # line that prints anyway — and on the worker state the API serves.
                if result.quarantined_skips:
                    self._state.last_detail += (f' · {len(result.quarantined_skips)} quarantined '
                                                f'({", ".join(result.quarantined_skips)})')
                # A quiet pass (nothing new, nothing flagged, $0 — the common case once the
                # corpus is warm and conditional GET is 304ing) logs at DEBUG so an overnight
                # run's log stays readable; a pass that stored, flagged or spent logs at INFO —
                # so spend is still never silent (a paid pass always has stored > 0). The eval
                # workers' INFO passes remain the regular liveness heartbeat either way.
                eventful = (result.stored or result.candidates or usd
                            or result.failed_sources or result.suspended
                            or result.truncated or result.rejected)
                duration_ms = (perf_counter() - started) * 1000.0
                logger.log(logging.INFO if eventful else logging.DEBUG,
                           '[%s] %s · $%.6f · %.0fms', self._state.name,
                           self._state.last_detail, usd, duration_ms)
                self._log_source_health(result)
                if result.host_event is not None:
                    await self._report_host_event(result.host_event)
                # Feed the live dashboard from the same structured pass (ISSUE_26) — next to the
                # log call, never parsed back from it. Skipped entirely without a display.
                self._push_stats(result, usd, duration_ms, eventful)
                # Nudge the eval workers on this set out-of-band (ISSUE_11) — in the event
                # loop thread, after the sync pass returned. A missed nudge is harmless: the
                # candidate is already persisted, the eval worker still catches it next interval.
                if self._on_candidates is not None and result.max_tier > 0:
                    self._on_candidates(result.max_tier)
            self._state.runs += 1
            self._state.last_duration_ms = (perf_counter() - started) * 1000.0

    def _push_stats(self, result: IngestResult, usd: float, duration_ms: float,
                    eventful: bool) -> None:
        """Push this pass into the live dashboard's shared state (ISSUE_26); a no-op without one."""
        stats = self._engine_stats
        if stats is None:
            return
        now = datetime.now(timezone.utc)
        source_set_id = self._set_name()          # this worker's key — one SOURCES/INGEST row per set
        # Track each feed's last successful poll, then flag one that stopped polling vs its expected
        # cadence — 'is my slow (politeness) feed still alive?' A healthy slow feed cycles within its
        # interval and is not flagged; a quarantined/failed feed is already named, so it is skipped.
        for poll in result.polls:
            if poll.status == 'ok':
                self._last_ok[poll.source_id] = now
        already: Set[str] = set(result.quarantined_skips) | set(result.failed_sources)
        # SOURCES row: healthy collapses to `N/N ok`; only failed/quarantined/overdue feeds named.
        ok = sum(1 for poll in result.polls if poll.status == 'ok')
        deviations = ([_quarantine_chip(poll, now) for poll in result.polls
                       if poll.status == 'quarantined']
                      + [f'{source_id} failed' for source_id in result.failed_sources]
                      + _overdue_feeds(self._last_ok, self._expected, now, already))
        # A set-wide back-off replaces the per-feed story rather than adding to it (ISSUE_84):
        # naming twelve blameless feeds is exactly the noise the guard exists to remove.
        backoff = next((poll.until for poll in result.polls if poll.status == 'host_backoff'), None)
        event = result.host_event
        stats.set_sources(source_set_id, SourcesSnapshot(
            last=now, ok=ok, total=len(result.polls),
            deviations=[] if backoff else deviations,
            host_backoff_until=backoff or (event.backoff_until if event
                                           and not event.resumed else None),
            host_detail=event.fleet if event and not event.resumed else ''))
        # INGEST row + the activity line (only an eventful pass streams — mirrors the log level so
        # a warm 304-ing corpus does not flood the stream).
        stats.set_ingest(source_set_id, IngestSnapshot(last=now, fetched=result.fetched,
                                                       new=result.stored, cost_usd=usd,
                                                       duration_ms=duration_ms,
                                                       suspended=result.suspended,
                                                       tokens=result.embed_tokens,
                                                       truncated=result.truncated))
        if eventful:
            stats.push_event('INGEST', f'{source_set_id} {self._state.last_detail}')
        if result.suspended:
            stats.push_event('BUDGET', 'embedding suspended — provider quota')
        # BREAKING (detected side): cumulative HIGH-tier candidates flagged by ingest (ISSUE_11).
        if result.candidates:
            stats.add_breaking_detected(result.candidates, at=now)
        # A feed crossing into flagged+quarantined this pass gets its own red activity line.
        for source_id, note in result.health_notes.items():
            if note.just_flagged:
                stats.push_event('SOURCE', f'{source_id} flagged + quarantined')

    def _set_name(self) -> str:
        # 'ingest:crypto_news' -> 'crypto_news' for the compact stream line.
        return self._state.name.split(':', 1)[-1]

    async def _report_host_event(self, event: HostEvent) -> None:
        """One line — and, on the edges, one alert — for a set-wide connectivity failure (ISSUE_84).

        Nothing else would speak for this condition: the stall watchdog watches for passes that
        stop *completing*, and during a connectivity outage every pass completes perfectly while
        failing every poll. Before ISSUE_84 the operator's only signal was twelve identical
        "feed unreachable" warnings per pass, which read like a feed problem and buried the one
        fact that mattered.

        Rate limiting needs no extra machinery: while the back-off holds, `should_poll` skips
        every source, so a pass polls nothing and produces no event. One line per back-off cycle
        falls out of the mechanism itself.
        """
        if event.resumed:
            message = (f'host connectivity recovered after {_format_age(event.duration_seconds)} '
                       f'— normal polling resumed ({self._set_name()})')
            logger.warning('[HOST] %s', message)
        elif event.opened:
            message = (f'host connectivity — {event.fleet} unreachable in one pass, '
                       f'no quarantine applied, retry '
                       f'{event.backoff_until.strftime("%H:%M:%S")} UTC')
            logger.error('[HOST] %s', message)
        else:
            # A continuation is loud enough in the log and would only repeat an alert the
            # operator already has.
            logger.warning('[HOST] still down — %s/%s unreachable in %s, next retry %s',
                           event.failed, event.pollable, self._set_name(),
                           event.backoff_until.strftime('%H:%M:%S'))
            return
        if self._engine_stats is not None:
            self._engine_stats.push_event('SOURCE', message)
        if self._on_host_event is None:
            return
        try:
            await self._on_host_event(message)
        except Exception:   # noqa: BLE001 — an undelivered alert must not fail the pass
            logger.exception('[HOST] alert delivery failed')

    def _log_source_health(self, result: IngestResult) -> None:
        """Emit source-failure lines at a level that denoises repeats (ISSUE_11).

        A feed that fails every pass (e.g. cryptoslate rate-limiting a fast loop) would otherwise
        flood the log. So: WARN the first failure of a streak, DEBUG the repeats, WARN once when it
        crosses into flagged+quarantined, and INFO a recovery. The full detail always persists in
        source_health regardless of the console level — the Sources report reads it from there."""
        for source_id in result.recovered_sources:
            logger.info('[%s] source %s recovered', self._state.name, source_id)
        # A skipped source is traceable at DEBUG only: entering quarantine already WARNed once
        # (`just_flagged` below), and the steady state is carried by the pass line + the Sources
        # report. Repeating it per pass would drown the signal it is meant to raise.
        for source_id in result.quarantined_skips:
            logger.debug('[%s] source %s skipped — quarantined', self._state.name, source_id)
        for source_id, message in result.failed_sources.items():
            note = result.health_notes.get(source_id)
            if note is not None and note.just_flagged:
                # The rung is the news, not the fact of a quarantine (ISSUE_84): "1/3, one hour"
                # and "3/3, a day" are the difference between a wobble and a lost feed, and the
                # top rung is an ERROR because nothing shorter will get it looked at.
                rung = f'{(note.rung or 0) + 1}/{note.rungs_total}'
                top = note.rungs_total > 1 and note.rung == note.rungs_total - 1
                logger.log(
                    logging.ERROR if top else logging.WARNING,
                    '[%s] source %s quarantined until %s — rung %s%s (%d consecutive): %s',
                    self._state.name, source_id,
                    note.quarantined_until.isoformat() if note.quarantined_until else '?',
                    rung, ' after a failed probe' if note.probe else '',
                    note.consecutive_failures, message)
            elif note is not None and note.suppressed:
                # The threshold was crossed but the correlated guard ruled it a local problem.
                # DEBUG, because the host event itself is the line worth reading — repeating it
                # per feed is exactly the 144-lines-per-feed noise ISSUE_84 set out to remove.
                logger.debug('[%s] source %s failed during a connectivity event — no quarantine',
                             self._state.name, source_id)
            elif note is None or note.consecutive_failures <= 1:
                logger.warning('[%s] source %s failed: %s', self._state.name, source_id, message)
            else:
                logger.debug('[%s] source %s still failing (%dx): %s', self._state.name,
                             source_id, note.consecutive_failures, message)
