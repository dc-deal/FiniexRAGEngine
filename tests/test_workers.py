"""Two-worker model (ISSUE_10) — trigger loop, worker resilience, supervisor build.
No DB, no API: fakes sit at the Ingestor/Pipeline seams, intervals are milliseconds.
"""
import asyncio
import json
from datetime import datetime, timezone
from typing import List

import pytest

from finiexragengine.core.pipeline.eval_worker import EvalWorker
from finiexragengine.core.pipeline.ingest_worker import IngestWorker
from finiexragengine.core.triggers.interval_trigger import IntervalTrigger
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.source_set_types import SourceSetConfig
from finiexragengine.types.ingest_types import IngestResult
from finiexragengine.types.outcome_types import RunMetadata, SentimentEnvelope

_SET = SourceSetConfig(
    source_set_id='crypto_news',
    sources=[{'source_id': 's1', 'url': 'https://example.test'}])


def _run(coro):
    return asyncio.run(coro)


# --- interval trigger -----------------------------------------------------------------

def test_trigger_fires_immediately_then_on_interval():
    calls: List[float] = []

    async def _scenario():
        trigger = IntervalTrigger(interval_seconds=0.01)

        async def tick():
            calls.append(asyncio.get_event_loop().time())
            if len(calls) >= 3:
                await trigger.stop()

        await trigger.start(tick)

    _run(_scenario())
    assert len(calls) == 3                     # first run immediate, then per interval


def test_trigger_stop_interrupts_the_wait():
    async def _scenario():
        trigger = IntervalTrigger(interval_seconds=60)   # would block a minute

        async def tick():
            pass

        task = asyncio.create_task(trigger.start(tick))
        await asyncio.sleep(0.01)              # first (immediate) run happened
        await trigger.stop()
        await asyncio.wait_for(task, timeout=1.0)   # returns promptly, not after 60s

    _run(_scenario())


# --- workers: pass logging, resilience ------------------------------------------------

class _FakeIngestor:
    def __init__(self, exc=None):
        self.runs = 0
        self._exc = exc

    def run(self) -> IngestResult:
        self.runs += 1
        if self._exc is not None:
            raise self._exc
        return IngestResult(fetched=10, embedded=2, stored=2)


def _ingest_worker(ingestor, interval=0.005) -> IngestWorker:
    return IngestWorker(_SET, ingestor, IntervalTrigger(interval), asyncio.Lock())


def test_ingest_worker_records_state():
    ingestor = _FakeIngestor()

    async def _scenario():
        worker = _ingest_worker(ingestor)
        task = asyncio.create_task(worker.start())
        await asyncio.sleep(0.02)
        await worker.stop()
        await task
        return worker.get_state()

    state = _run(_scenario())
    assert state.name == 'ingest:crypto_news' and state.kind == 'ingest'
    assert state.runs >= 2 and state.last_status == 'ok'
    assert 'fetched 10' in state.last_detail


def test_failing_pass_never_kills_the_loop():
    ingestor = _FakeIngestor(exc=RuntimeError('feed exploded'))

    async def _scenario():
        worker = _ingest_worker(ingestor)
        task = asyncio.create_task(worker.start())
        await asyncio.sleep(0.02)
        await worker.stop()
        await task
        return worker.get_state()

    state = _run(_scenario())
    assert ingestor.runs >= 2                  # kept ticking after the failure
    assert state.last_status == 'error' and 'feed exploded' in state.last_detail


class _FakePipeline:
    def __init__(self):
        self.runs = 0

    def get_config(self):
        from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig
        return PipelineConfig(
            pipeline_id='p', outcome_type='sentiment_fear_greed', market='crypto',
            symbols=[{'key': 'BTCUSD', 'base': 'BTC', 'quote': 'USD'}],
            llm={'model': 'gpt-4o-mini'}, source_set='crypto_news',
            trigger={'type': 'interval', 'timeframe': 'M10'})

    def run(self) -> SentimentEnvelope:
        self.runs += 1
        return SentimentEnvelope(
            pipeline_id='p', outcome_type='sentiment_fear_greed', prompt_version='2',
            timestamp=datetime.now(timezone.utc),
            status='success', result=[], metadata=RunMetadata(model='gpt-4o-mini'))


def test_eval_worker_runs_pipeline_and_tracks_state():
    pipeline = _FakePipeline()

    async def _scenario():
        worker = EvalWorker(pipeline, IntervalTrigger(0.005), asyncio.Lock())
        task = asyncio.create_task(worker.start())
        await asyncio.sleep(0.02)
        await worker.stop()
        await task
        return worker.get_state()

    state = _run(_scenario())
    assert state.name == 'eval:p' and pipeline.runs >= 2
    assert state.last_status == 'ok' and 'success' in state.last_detail


# --- supervisor build (uses the real registries over tmp configs) ---------------------

def test_supervisor_builds_one_ingest_per_referenced_set_and_one_eval_per_stream(
        tmp_path, monkeypatch):
    # Two fan variants over ONE source-set -> 1 ingest worker + 2 eval workers.
    (tmp_path / 'sets').mkdir()
    (tmp_path / 'sets' / 'crypto_news.json').write_text(json.dumps({
        'source_set_id': 'crypto_news',
        'sources': [{'source_id': 's1', 'url': 'https://example.test'}]}))
    (tmp_path / 'pipes').mkdir()
    (tmp_path / 'pipes' / 'crypto.json').write_text(json.dumps({
        'pipeline_id': 'crypto_sentiment', 'outcome_type': 'sentiment_fear_greed',
        'market': 'crypto', 'symbols': [{'key': 'BTCUSD', 'base': 'BTC', 'quote': 'USD'}],
        'source_set': 'crypto_news',
        'trigger': {'type': 'interval', 'timeframe': 'M10'},
        'llm': {'models': [
            {'name': 'gpt-4o-mini', 'sub_pipeline_id': 'mini', 'default': True},
            {'name': 'gpt-4o', 'sub_pipeline_id': '4o_enhanced'}]}}))

    from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
    from finiexragengine.core.pipeline.worker_supervisor import WorkerSupervisor

    registry = PipelineRegistry(tmp_path / 'pipes')
    registry.load()

    class _FakeSets:
        def get(self, source_set_id):
            assert source_set_id == 'crypto_news'
            return SourceSetConfig(**json.loads(
                (tmp_path / 'sets' / 'crypto_news.json').read_text()))

    class _FakeAssembler:
        def get_source_sets(self):
            return _FakeSets()

        def build_ingestor(self, source_set_id, billing_label=''):
            return _FakeIngestor()

        def get_cost_recorder(self):
            return None

    async def _scenario():
        return WorkerSupervisor(_FakeAssembler(), registry).states()

    states = _run(_scenario())
    names = sorted(s.name for s in states)
    assert names == ['eval:crypto_sentiment', 'eval:crypto_sentiment_4o_enhanced',
                     'ingest:crypto_news']
    by_name = {s.name: s for s in states}
    assert by_name['ingest:crypto_news'].interval_seconds == 300   # set's own cadence
    assert by_name['ingest:crypto_news'].timeframe is None         # ingest is relative, no bar
    # Eval cadence is a bar-close timeframe; interval_seconds is derived (M10 = 600s).
    assert by_name['eval:crypto_sentiment'].timeframe == 'M10'
    assert by_name['eval:crypto_sentiment'].interval_seconds == 600


def test_supervisor_refuses_unknown_trigger_type():
    from finiexragengine.core.pipeline.worker_supervisor import WorkerSupervisor
    from finiexragengine.types.config_types.pipeline_config_types import TriggerConfig
    with pytest.raises(ConfigurationError, match='event'):
        WorkerSupervisor._interval_trigger(
            TriggerConfig(type='event'), 'pipeline p')


def test_eval_trigger_requires_a_timeframe():
    # ISSUE_timeframe: an eval worker without a bar-close frame is a config error, not a default.
    from finiexragengine.core.pipeline.worker_supervisor import WorkerSupervisor
    from finiexragengine.types.config_types.pipeline_config_types import TriggerConfig
    with pytest.raises(ConfigurationError, match='timeframe'):
        WorkerSupervisor._eval_trigger(TriggerConfig(), None, 'pipeline p')


def test_breaking_confirmation_log_reports_reaction_time():
    # ISSUE_11: a confirmed breaking episode is logged inline with its reaction time (edge-triggered).
    from datetime import timedelta

    from finiexragengine.core.pipeline.breaking_episode import BreakingEpisodeTracker
    from finiexragengine.core.pipeline.eval_worker import _breaking_line
    from finiexragengine.types.outcome_types import ArticleRef, SentimentResult

    t3 = datetime(2026, 7, 13, 14, 0, 54, tzinfo=timezone.utc)
    envelope = SentimentEnvelope(
        pipeline_id='crypto_sentiment', outcome_type='sentiment_fear_greed', prompt_version='2',
        timestamp=t3, status='success', metadata=RunMetadata(model='gpt-4o-mini'),
        result=[
            SentimentResult(
                symbol='BTCUSD', signal='SELL', sentiment_score=-0.5, confidence=0.8,
                reasoning='hack', urgency=0.92, is_breaking=True,
                sources=[ArticleRef(article_id='a', url='u', title='t',
                                    published_at=t3 - timedelta(seconds=49),
                                    fetched_at=t3 - timedelta(seconds=42))]),
            SentimentResult(symbol='ETHUSD', signal='HOLD', sentiment_score=0.0, confidence=0.5,
                            reasoning='calm', urgency=0.1, is_breaking=False),
        ])
    episodes = BreakingEpisodeTracker().new_episodes(envelope)
    assert len(episodes) == 1                               # only the breaking row = one episode
    line = _breaking_line('crypto_sentiment', episodes[0])
    assert 'BTCUSD' in line
    assert 'engine 42s' in line and 'e2e 49s' in line       # published≠fetched → real e2e


def test_overdue_feeds_flags_a_stalled_slow_feed():
    # ISSUE_26: a slow (politeness) feed that stopped polling reads as overdue; a healthy one not.
    from datetime import timedelta

    from finiexragengine.core.pipeline.ingest_worker import _overdue_feeds

    now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    expected = {'fxstreet': 300, 'boe_news': 3600}          # fast 5m + slow 1h (politeness)
    last_ok = {'fxstreet': now - timedelta(seconds=200),    # within its 5m — fine
               'boe_news': now - timedelta(hours=3)}        # 3h vs 1h × 2 → overdue
    assert _overdue_feeds(last_ok, expected, now, skip=set()) == ['boe_news overdue 180m']


def test_overdue_skips_already_flagged_and_never_polled():
    from datetime import timedelta

    from finiexragengine.core.pipeline.ingest_worker import _overdue_feeds

    now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    expected = {'a': 300, 'b': 300}
    last_ok = {'a': now - timedelta(hours=1)}               # 'a' overdue but quarantined this pass
    assert _overdue_feeds(last_ok, expected, now, skip={'a'}) == []   # skipped + 'b' never polled
