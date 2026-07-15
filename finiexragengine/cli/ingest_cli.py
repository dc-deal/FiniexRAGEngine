"""CLI entry point: run one ingest pass (fetch -> embed -> upsert) for a source-set.

Spends OpenAI budget only on **new** articles (known ids are skipped before embedding),
and reports how many were embedded so the cost is never silent. Fenced under the paid
launch group. Acquisition is per source-set (ISSUE_10) — the shared corpus a set feeds
serves every pipeline referencing it.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.run_footer import RunFooter
from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.exceptions.ragengine_errors import ConfigurationError


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Run one ingest pass (fetch -> embed -> upsert) for a source-set')
    parser.add_argument('--source-set', default='crypto_news',
                        help='source-set id under configs/source_sets/')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    # The assembler is the one place the object graph is built — the CLI reuses its
    # wiring (embedder, store incl. corpus guard, shared cost recorder).
    app = AppConfigManager()
    cfg = app.get_config()
    assembler = PipelineAssembler(app, database_url)
    try:
        ingestor = assembler.build_ingestor(args.source_set)
    except ConfigurationError as exc:
        parser.error(str(exc))
    result = ingestor.run()

    # Cost line first: `embedded` is what was actually paid for this pass.
    print(f"ingest '{args.source_set}': fetched {result.fetched}, "
          f'embedded {result.embedded} (paid), stored {result.stored} new, '
          f'{result.duplicates} duplicates')
    # The circuit-breaker may have suspended the paid embedding mid-pass (ISSUE_47).
    if result.suspended:
        print('  ⏸ paid work suspended (provider quota) — embedding skipped this pass')
    for source_id, entry in result.per_source.items():
        print(f'  {source_id:14} fetched {entry.fetched:3}   embedded {entry.embedded:3}   '
              f'new {entry.stored:3}   dup {entry.duplicates:3}')
    for source_id, error in result.failed_sources.items():
        print(f'  {source_id:14} FAILED: {error}')
    # The shared metrics block (ISSUE_32): per-stage times (summed across sources) and
    # what this pass actually spent — read off the recorder's session accumulator.
    recorder = assembler.get_cost_recorder()
    footer = RunFooter(timings=result.stage_timings,
                       tokens_label=f'{recorder.session_tokens:,} embedding',
                       usd=recorder.session_usd, section='ingest_news',
                       model_label=cfg.embedding.model, aggregate=True)
    print()
    print(footer.render())


if __name__ == '__main__':
    main()
