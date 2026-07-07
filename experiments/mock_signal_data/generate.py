"""Mock signal-data generator for early FiniexTestingIDE (#141 / #429) testing.

Emits JSONL that mimics the FiniexDataCollector archive of FiniexRAGEngine sentiment
envelopes. NOT for correctness — plausible, schema-valid mock data only; the format may still
change. Each JSONL line = one AnalysisEnvelope per ~10-min snapshot, plus a top-level
`collected_msc` (epoch milliseconds, UTC) = the collector's receive time = the IDE merge key
(nearest snapshot with `collected_msc <= tick.collected_msc`; no look-ahead).

The default window sits inside the IDE's kraken_spot tick coverage so a backtest binds. Use
`--pipeline-id` to stamp a non-crypto batch (e.g. a forex mock) with its own data-source id.

Run from the repo root:
    python experiments/mock_signal_data/generate.py            # 5-cycle fixture sample
    python experiments/mock_signal_data/generate.py --cycles 1008 --out data/mock_signals/full_week.jsonl
    python experiments/mock_signal_data/generate.py --pipeline-id forex_macro_sentiment \
        --symbols EURUSD,GBPUSD --start 2026-04-06T00:00:00Z --cycles 4032 --out data/mock_signals/forex.jsonl
"""
import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finiexragengine.types.outcome_types import (
    AnalysisEnvelope,
    ArticleRef,
    RunError,
    RunMetadata,
    SentimentResult,
    StageTiming,
)

# The eight crypto_sentiment symbols; the IDE has kraken_spot tick data for all of them.
DEFAULT_SYMBOLS = ['BTCUSD', 'ETHUSD', 'SOLUSD', 'ADAUSD', 'XRPUSD', 'DASHUSD', 'LTCUSD', 'ETHEUR']
NAME = {
    'BTCUSD': 'Bitcoin', 'ETHUSD': 'Ethereum', 'SOLUSD': 'Solana', 'ADAUSD': 'Cardano',
    'XRPUSD': 'XRP', 'DASHUSD': 'Dash', 'LTCUSD': 'Litecoin', 'ETHEUR': 'Ethereum (EUR)',
    'EURUSD': 'Euro', 'GBPUSD': 'British Pound', 'USDJPY': 'Japanese Yen',
}
DEFAULT_PIPELINE_ID = 'crypto_sentiment'
OUTCOME_TYPE = 'sentiment_fear_greed'
PROMPT_VERSION = '1'
SCHEMA_VERSION = '1.0'
INTERVAL = timedelta(minutes=10)
# Default start: inside the IDE's tick coverage; 1008 cycles = 7 days, 4032 = 28 days.
DEFAULT_START = '2026-04-27T00:00:00Z'


def _signal(score: float) -> str:
    if score > 0.2:
        return 'BUY'
    if score < -0.2:
        return 'SELL'
    return 'HOLD'


def _reasoning(name: str, score: float, breaking: bool) -> str:
    if breaking:
        return f'Breaking: major {name} headline cluster; sharp sentiment move.'
    mood = 'bullish' if score > 0.2 else 'bearish' if score < -0.2 else 'mixed'
    return f'{mood.capitalize()} {name} coverage; net news tone {score:+.2f}.'


def _sources(symbol: str, when: datetime, k: int) -> list:
    refs = []
    name = NAME.get(symbol, symbol)
    for i in range(k):
        digest = sha256(f'{symbol}-{when.isoformat()}-{i}'.encode('utf-8')).hexdigest()[:32]
        refs.append(ArticleRef(
            article_id=digest,
            url=f'https://example.test/{symbol.lower()}/{digest[:8]}',
            title=f'{name} mock headline {i + 1}',
            published_at=when - timedelta(minutes=15 * (i + 1)),
        ))
    return refs


def _timings(start: datetime):
    durations = {'fetch': 380.0, 'embed': 240.0, 'retrieve': 160.0, 'llm': 900.0, 'parse': 50.0}
    timings = []
    cursor = start
    for stage, ms in durations.items():
        end = cursor + timedelta(milliseconds=ms)
        timings.append(StageTiming(stage=stage, started_at=cursor, ended_at=end, duration_ms=ms))
        cursor = end
    return timings, sum(durations.values())


def _make_envelope(pipeline_id, status, timestamp, results, metadata, errors):
    return AnalysisEnvelope[SentimentResult](
        schema_version=SCHEMA_VERSION,
        pipeline_id=pipeline_id,
        outcome_type=OUTCOME_TYPE,
        prompt_version=PROMPT_VERSION,
        timestamp=timestamp,
        status=status,
        result=results,
        metadata=metadata,
        errors=errors,
    )


def _envelope(rng, scores, symbols, pipeline_id, collected_at, force_partial, force_error,
              breaking_symbol, no_news_symbols, no_news_p):
    analysis_at = collected_at - timedelta(seconds=2)

    if force_error:
        # Contract: status 'error' -> empty result, populated errors (nothing produced).
        metadata = RunMetadata(
            model='gpt-4o-mini', sources_configured=3, sources_reached=3,
            articles_found=rng.randint(40, 90), articles_relevant=rng.randint(15, 30),
            processing_time_ms=30010.0, stage_timings=[],
        )
        errors = [RunError(type='LLM_TIMEOUT', message='LLM did not respond within 30s',
                           timestamp=analysis_at)]
        return _make_envelope(pipeline_id, 'error', analysis_at, [], metadata, errors)

    timings, total_ms = _timings(analysis_at)
    results = []
    for sym in symbols:
        scores[sym] = max(-1.0, min(1.0, scores[sym] + rng.uniform(-0.15, 0.15)))
        score = round(scores[sym], 3)
        breaking = sym == breaking_symbol
        no_news = (not breaking) and (sym in no_news_symbols or rng.random() < no_news_p)
        if no_news:
            results.append(SentimentResult(
                symbol=sym, signal='HOLD', sentiment_score=0.0, confidence=0.0,
                reasoning='No relevant news found', urgency=0.0, is_breaking=False, sources=[],
            ))
            continue
        urgency = round(rng.uniform(0.8, 0.95) if breaking else rng.uniform(0.0, 0.4), 3)
        results.append(SentimentResult(
            symbol=sym,
            signal=_signal(score),
            sentiment_score=score,
            confidence=round(rng.uniform(0.45, 0.9), 3),
            reasoning=_reasoning(NAME.get(sym, sym), score, breaking),
            urgency=urgency,
            is_breaking=urgency >= 0.8,
            sources=_sources(sym, analysis_at, rng.randint(1, 3)),
        ))
    reached = 2 if force_partial else 3
    errors = []
    if force_partial:
        errors.append(RunError(
            type='SOURCE_UNREACHABLE',
            message='Failed to fetch https://cryptonews.com/rss: connection timeout after 10s',
            timestamp=analysis_at,
        ))
    metadata = RunMetadata(
        model='gpt-4o-mini', sources_configured=3, sources_reached=reached,
        articles_found=rng.randint(40, 90), articles_relevant=rng.randint(15, 30),
        processing_time_ms=total_ms, stage_timings=timings,
    )
    return _make_envelope(pipeline_id, 'partial' if force_partial else 'success', analysis_at,
                          results, metadata, errors)


def main() -> None:
    parser = argparse.ArgumentParser(description='Generate mock sentiment JSONL for #141 / #429.')
    parser.add_argument('--cycles', type=int, default=5)
    parser.add_argument('--start', default=DEFAULT_START, help='ISO8601 UTC start')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--symbols', default=','.join(DEFAULT_SYMBOLS))
    parser.add_argument('--pipeline-id', default=DEFAULT_PIPELINE_ID,
                        help='stamps envelope.pipeline_id (IDE data-source id, #429)')
    parser.add_argument('--out', default='tests/fixtures/signals/crypto_sentiment_sample.jsonl')
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(',') if s.strip()]
    pipeline_id = args.pipeline_id
    rng = random.Random(args.seed)
    start = datetime.fromisoformat(args.start.replace('Z', '+00:00'))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)

    scores = {sym: rng.uniform(-0.3, 0.3) for sym in symbols}
    short = args.cycles <= 8
    if short:
        no_news_by_cycle = {0: {symbols[-1]}}
        partial_cycles = {2}
        breaking_by_cycle = {3: 'BTCUSD' if 'BTCUSD' in symbols else symbols[0]}
        error_cycles = {args.cycles - 1}
        no_news_p = 0.0
    else:
        # week+: deterministic at-least-one-of-each early, plus random sprinkle for variety
        no_news_by_cycle = {}
        partial_cycles = {12}
        breaking_by_cycle = {18: 'BTCUSD' if 'BTCUSD' in symbols else symbols[0]}
        error_cycles = {24}
        no_news_p = 0.05

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open('w') as handle:
        for i in range(args.cycles):
            collected_at = start + i * INTERVAL
            force_error = i in error_cycles or (not short and rng.random() < 0.008)
            force_partial = (i in partial_cycles or (not short and rng.random() < 0.025)) and not force_error
            breaking = None
            if not force_error:
                breaking = breaking_by_cycle.get(i) or (
                    rng.choice(symbols) if (not short and rng.random() < 0.015) else None)
            envelope = _envelope(rng, scores, symbols, pipeline_id, collected_at, force_partial,
                                 force_error, breaking, no_news_by_cycle.get(i, set()), no_news_p)
            collected = collected_at.isoformat().replace('+00:00', 'Z')
            line = {'collected_msc': int(collected_at.timestamp() * 1000),
                    **json.loads(envelope.model_dump_json())}
            handle.write(json.dumps(line) + '\n')
    print(f'wrote {args.cycles} snapshots ({len(symbols)} symbols, pipeline_id={pipeline_id}) → {out_path}')


if __name__ == '__main__':
    main()
