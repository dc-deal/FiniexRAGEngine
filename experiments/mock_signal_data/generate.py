"""Mock signal-data generator for early FiniexTestingIDE (#141 / #429) testing.

Emits JSONL that mimics the FiniexDataCollector archive of FiniexRAGEngine sentiment
envelopes. NOT for correctness — plausible, schema-valid mock data only; the format may still
change. Each JSONL line = one AnalysisEnvelope per ~10-min snapshot, plus a top-level
`collected_msc` (epoch milliseconds, UTC) = the collector's receive time = the IDE merge key
(nearest snapshot with `collected_msc <= tick.collected_msc`; no look-ahead).

The default window sits inside the IDE's kraken_spot tick coverage so a backtest binds. Use
`--pipeline-id` to stamp a non-crypto batch (e.g. a forex mock) with its own data-source id.

Variant fan-out (ISSUE_42): `--variants` renders one constellation through N mock models as
separate streams — format A, as confirmed by the IDE: the first (default) variant keeps the
bare `pipeline_id`, the others get `<pipeline_id>_<sub_id>`; every stream carries the grouping
hints `metadata.variant_group` / `metadata.variant`. One JSONL file per stream lands in
`--out` (a directory in this mode). All variants share one news walk (same corpus, same
sources, same source-side failures); only the model reading differs — correlated scores with
genuine disagreement near the signal thresholds, per-model latency/cost, per-model timeout
cycles. That is the comparison dataset the IDE validates the format against.

Archive rotation (ISSUE_13): `--rotate daily|weekly` emits the collector's bucketed
file layout instead of one file per stream — `<out>/<stream_id>/<bucket>.jsonl`, buckets
named from each line's `collected_msc` via `finiexragengine.utils.archive_layout` (the
shared naming contract). This is the IDE's material for smoke-testing the multi-file
range read (#141) before the real collector exists.

Run from the repo root:
    python experiments/mock_signal_data/generate.py            # 5-cycle fixture sample
    python experiments/mock_signal_data/generate.py --cycles 1008 --out data/mock_signals/full_week.jsonl
    python experiments/mock_signal_data/generate.py --cycles 1008 \
        --variants "mini=gpt-4o-mini,4o_enhanced=gpt-4o" --out data/mock_signals/variant_week
    python experiments/mock_signal_data/generate.py --cycles 1008 --rotate daily \
        --out data/mock_signals/rotated_week
"""
import argparse
import json
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Dict, Set

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finiexragengine.types.outcome_types import (
    AnalysisEnvelope,
    ArticleRef,
    RunError,
    RunMetadata,
    SentimentResult,
    StageTiming,
)
from finiexragengine.utils.archive_layout import bucket_path

# The eight crypto_sentiment symbols; the IDE has kraken_spot tick data for all of them.
DEFAULT_SYMBOLS = ['BTCUSD', 'ETHUSD', 'SOLUSD', 'ADAUSD', 'XRPUSD', 'DASHUSD', 'LTCUSD', 'ETHEUR']
NAME = {
    'BTCUSD': 'Bitcoin', 'ETHUSD': 'Ethereum', 'SOLUSD': 'Solana', 'ADAUSD': 'Cardano',
    'XRPUSD': 'XRP', 'DASHUSD': 'Dash', 'LTCUSD': 'Litecoin', 'ETHEUR': 'Ethereum (EUR)',
    'EURUSD': 'Euro', 'GBPUSD': 'British Pound', 'USDJPY': 'Japanese Yen',
}
DEFAULT_PIPELINE_ID = 'crypto_sentiment'
OUTCOME_TYPE = 'sentiment_fear_greed'
SCHEMA_VERSION = '1.0'
# Prompt provenance (ISSUE_33) — identical across variants by design: the anchor that
# attributes any score difference to the model. Mirrors prompts/sentiment_v2.md.
PROMPT_ID = 'sentiment-crypto'
PROMPT_VERSION = '2'
PROMPT_HASH = '1c86eac137d8'
INTERVAL = timedelta(minutes=10)
# Default start: inside the IDE's tick coverage; 1008 cycles = 7 days, 4032 = 28 days.
DEFAULT_START = '2026-04-27T00:00:00Z'
DEFAULT_OUT = 'tests/fixtures/signals/crypto_sentiment_sample.jsonl'

# Mock-only plausibility tables per model: served snapshot (alias → dated, ISSUE_40),
# $/1M-token prices (in, out), and the llm stage pace (a bigger model is slower).
SNAPSHOT = {'gpt-4o-mini': 'gpt-4o-mini-2024-07-18', 'gpt-4o': 'gpt-4o-2024-11-20'}
PRICE_PER_1M = {'gpt-4o-mini': (0.15, 0.60), 'gpt-4o': (2.50, 10.00)}
LLM_BASE_MS = {'gpt-4o-mini': 900.0, 'gpt-4o': 1400.0}


@dataclass
class _Variant:
    """One model variant = one output stream (ISSUE_42)."""
    sub_id: str            # '' in single-stream mode (no hints emitted)
    model: str
    stream_id: str         # bare pipeline_id for the default, else pipeline_id_sub_id
    group: str             # variant_group hint (= the constellation id); '' single-stream
    bias: float            # fixed reading offset — one model leans a touch more bullish
    rng: random.Random     # per-variant noise: scores, confidence, latency, tokens
    error_cycles: Set[int]  # LLM timeouts are model-side → per-variant cycles


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
    # Deterministic from symbol+time+index — with a shared k per cycle, every variant
    # cites the exact same articles (identical retrieval context).
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


def _timings(start: datetime, llm_ms: float):
    # fetch/embed/retrieve are corpus-side and comparable across variants; the llm stage
    # duration is the model-dependent one.
    durations = {'fetch': 380.0, 'embed': 240.0, 'retrieve': 160.0,
                 'llm': round(llm_ms, 1), 'parse': 50.0}
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
        prompt_id=PROMPT_ID,
        prompt_hash=PROMPT_HASH,
        timestamp=timestamp,
        status=status,
        result=results,
        metadata=metadata,
        errors=errors,
    )


def _cycle_facts(rng, scores, symbols, i, short, no_news_by_cycle, partial_cycles,
                 breaking_by_cycle, no_news_p) -> dict:
    """Shared per-cycle script — everything the corpus/ingest side decides.

    Every variant evaluates the SAME news state: base sentiment walk, no-news symbols
    (retrieval is shared), source outages (partial), the breaking symbol, and which
    articles are cited. Only the LLM reading differs per variant.
    """
    for sym in symbols:
        scores[sym] = max(-1.0, min(1.0, scores[sym] + rng.uniform(-0.15, 0.15)))
    partial = i in partial_cycles or (not short and rng.random() < 0.025)
    breaking = breaking_by_cycle.get(i) or (
        rng.choice(symbols) if (not short and rng.random() < 0.015) else None)
    no_news = set(no_news_by_cycle.get(i, set()))
    if not short:
        no_news |= {sym for sym in symbols if sym != breaking and rng.random() < no_news_p}
    no_news -= {breaking}
    return {
        'partial': partial,
        'breaking': breaking,
        'no_news': no_news,
        'articles_found': rng.randint(40, 90),
        'articles_relevant': rng.randint(15, 30),
        'base_scores': {sym: scores[sym] for sym in symbols},
        'source_k': {sym: rng.randint(1, 3) for sym in symbols if sym not in no_news},
    }


def _render_variant(variant: _Variant, facts: dict, symbols, collected_at, force_error):
    """One variant's envelope for one cycle — the model-side reading of the shared facts."""
    rng = variant.rng
    analysis_at = collected_at - timedelta(seconds=2)
    model = variant.model

    if force_error:
        # Contract: status 'error' -> empty result, populated errors (nothing produced).
        # No response served -> no snapshot captured, no tokens spent.
        metadata = RunMetadata(
            model=model, sources_configured=3, sources_reached=3,
            articles_found=facts['articles_found'], articles_relevant=facts['articles_relevant'],
            processing_time_ms=30010.0, stage_timings=[],
        )
        errors = [RunError(type='LLM_TIMEOUT', message='LLM did not respond within 30s',
                           timestamp=analysis_at)]
        return _make_envelope(variant.stream_id, 'error', analysis_at, [], metadata, errors)

    timings, total_ms = _timings(analysis_at, LLM_BASE_MS.get(model, 900.0) * rng.uniform(0.85, 1.25))
    price_in, price_out = PRICE_PER_1M.get(model, PRICE_PER_1M['gpt-4o-mini'])
    results = []
    per_symbol_tokens: Dict[str, int] = {}
    prompt_tokens = completion_tokens = 0
    for sym in symbols:
        if sym in facts['no_news']:
            # Empty context after the floor -> mechanical HOLD, no LLM call (ISSUE_24).
            per_symbol_tokens[sym] = 0
            results.append(SentimentResult(
                symbol=sym, signal='HOLD', sentiment_score=0.0, confidence=0.0,
                reasoning='No relevant news found', urgency=0.0, is_breaking=False,
                sources=[], basis='no_data',
            ))
            continue
        # Correlated disagreement: shared base walk + per-variant bias/noise = the same
        # news read by a different model. Signals mostly agree, diverge near thresholds.
        score = round(max(-1.0, min(1.0,
            facts['base_scores'][sym] + variant.bias + rng.uniform(-0.08, 0.08))), 3)
        breaking = sym == facts['breaking']
        urgency = round(rng.uniform(0.8, 0.95) if breaking else rng.uniform(0.0, 0.4), 3)
        p, c = rng.randint(550, 950), rng.randint(90, 140)
        prompt_tokens += p
        completion_tokens += c
        per_symbol_tokens[sym] = p + c
        results.append(SentimentResult(
            symbol=sym,
            signal=_signal(score),
            sentiment_score=score,
            confidence=round(rng.uniform(0.45, 0.9), 3),
            reasoning=_reasoning(NAME.get(sym, sym), score, breaking),
            urgency=urgency,
            is_breaking=urgency >= 0.8,
            sources=_sources(sym, analysis_at, facts['source_k'][sym]),
        ))
    errors = []
    if facts['partial']:
        # Source-side outage — shared across variants (one ingest, N readings).
        errors.append(RunError(
            type='SOURCE_UNREACHABLE',
            message='Failed to fetch https://cryptonews.com/rss: connection timeout after 10s',
            timestamp=analysis_at,
        ))
    metadata = RunMetadata(
        model=model, model_snapshot=SNAPSHOT.get(model, model),
        sources_configured=3, sources_reached=2 if facts['partial'] else 3,
        articles_found=facts['articles_found'], articles_relevant=facts['articles_relevant'],
        processing_time_ms=total_ms, stage_timings=timings,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        cost_usd=round((prompt_tokens * price_in + completion_tokens * price_out) / 1e6, 6),
        per_symbol_tokens=per_symbol_tokens,
        # Grouping hints (ISSUE_42) — real RunMetadata fields since the fan-out landed;
        # the model omits the keys entirely when unset (single-stream mode).
        variant_group=variant.group or None, variant=variant.sub_id or None,
    )
    return _make_envelope(variant.stream_id, 'partial' if facts['partial'] else 'success',
                          analysis_at, results, metadata, errors)


def _parse_variants(spec: str):
    """'mini=gpt-4o-mini,4o_enhanced=gpt-4o' → [(sub_id, model), ...]; first = default."""
    pairs = []
    for part in spec.split(','):
        sub_id, _, model = part.strip().partition('=')
        if not model or not re.fullmatch(r'[a-z0-9_]+', sub_id):
            raise SystemExit(f"bad variant spec '{part}' — want sub_id=model, sub_id in [a-z0-9_]")
        pairs.append((sub_id, model))
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description='Generate mock sentiment JSONL for #141 / #429.')
    parser.add_argument('--cycles', type=int, default=5)
    parser.add_argument('--start', default=DEFAULT_START, help='ISO8601 UTC start')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--symbols', default=','.join(DEFAULT_SYMBOLS))
    parser.add_argument('--pipeline-id', default=DEFAULT_PIPELINE_ID,
                        help='stamps envelope.pipeline_id (IDE data-source id, #429)')
    parser.add_argument('--variants', default=None,
                        help="variant fan-out (ISSUE_42): 'sub_id=model,sub_id=model'; "
                             'first entry = default stream (keeps the bare pipeline id); '
                             '--out becomes a directory, one JSONL per stream')
    parser.add_argument('--rotate', choices=['daily', 'weekly'], default=None,
                        help='bucketed archive layout (ISSUE_13): --out becomes a directory '
                             'root, files land at <out>/<stream_id>/<bucket>.jsonl')
    parser.add_argument('--out', default=DEFAULT_OUT)
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
        no_news_p = 0.0
    else:
        # week+: deterministic at-least-one-of-each early, plus random sprinkle for variety
        no_news_by_cycle = {}
        partial_cycles = {12}
        breaking_by_cycle = {18: 'BTCUSD' if 'BTCUSD' in symbols else symbols[0]}
        no_news_p = 0.05

    # Build the variant list. Without --variants: one anonymous default = today's
    # single-stream behavior (no hints). With it: format A — default keeps the bare id.
    multi = args.variants is not None
    pairs = _parse_variants(args.variants) if multi else [('', 'gpt-4o-mini')]
    base_error = args.cycles - 1 if short else 24
    variants = []
    for idx, (sub_id, model) in enumerate(pairs):
        vrng = random.Random(f'{args.seed}:{sub_id or "default"}')
        variants.append(_Variant(
            sub_id=sub_id,
            model=model,
            stream_id=pipeline_id if idx == 0 else f'{pipeline_id}_{sub_id}',
            group=pipeline_id if multi else '',
            bias=0.0 if idx == 0 else vrng.uniform(-0.06, 0.06),
            rng=vrng,
            # The deterministic timeout cycle is offset per stream — per-variant gaps are
            # the norm. The random sprinkle may still rarely coincide across streams
            # (a provider-wide hiccup), which is a realistic case the IDE should survive.
            error_cycles={(base_error + idx * 96) % args.cycles},
        ))

    # Output: single mode writes one file (unchanged); variant mode writes a directory
    # with one JSONL per stream — mirroring the collector's per-stream archives.
    # --rotate (ISSUE_13) switches either mode to the bucketed layout
    # <out>/<stream_id>/<bucket>.jsonl, buckets named from each line's collected_msc.
    paths = {}
    handles = {}
    counts = {}
    if args.rotate:
        out_root = Path(f'data/mock_signals/rotated_{args.rotate}'
                        if args.out == DEFAULT_OUT else args.out)
    elif multi:
        out_dir = Path('data/mock_signals/variant_week' if args.out == DEFAULT_OUT else args.out)
        for variant in variants:
            paths[variant.stream_id] = out_dir / f'{variant.stream_id}.jsonl'
    else:
        paths[variants[0].stream_id] = Path(args.out)

    def _sink(stream_id, collected_at):
        # One lazily-opened handle per target file. When rotating, the line's collection
        # time picks the bucket — each bucket file is written exactly once per run
        # (closed buckets immutable by construction).
        key = stream_id
        if args.rotate:
            rel = bucket_path(stream_id, collected_at, args.rotate)
            key = str(rel)
            paths.setdefault(key, out_root / rel)
        if key not in handles:
            paths[key].parent.mkdir(parents=True, exist_ok=True)
            handles[key] = paths[key].open('w')
        counts[key] = counts.get(key, 0) + 1
        return handles[key]

    for i in range(args.cycles):
        collected_at = start + i * INTERVAL
        facts = _cycle_facts(rng, scores, symbols, i, short, no_news_by_cycle,
                             partial_cycles, breaking_by_cycle, no_news_p)
        for variant in variants:
            force_error = i in variant.error_cycles or (not short and variant.rng.random() < 0.008)
            envelope = _render_variant(variant, facts, symbols, collected_at, force_error)
            line = {'collected_msc': int(collected_at.timestamp() * 1000),
                    **json.loads(envelope.model_dump_json())}
            _sink(variant.stream_id, collected_at).write(json.dumps(line) + '\n')
    for handle in handles.values():
        handle.close()
    for key in sorted(paths):
        print(f'wrote {counts[key]} snapshots ({len(symbols)} symbols, {key}) → {paths[key]}')


if __name__ == '__main__':
    main()
