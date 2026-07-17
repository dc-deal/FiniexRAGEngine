"""CLI entry point: run one ingest pass (fetch -> embed -> upsert) for a source-set.

Spends OpenAI budget only on **new** articles (known ids are skipped before embedding),
and reports how many were embedded so the cost is never silent. Fenced under the paid
launch group. Acquisition is per source-set (ISSUE_10) — the shared corpus a set feeds
serves every pipeline referencing it.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.ingest_report import (
    build_ingest_report,
    format_ingest_report,
)
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
        # The declared catalogue (including the switched-off feeds the ingestor is never given)
        # — the report renders against it so no declared source can be missing from the table.
        source_set = assembler.get_source_sets().get(args.source_set)
    except ConfigurationError as exc:
        parser.error(str(exc))
    result = ingestor.run()

    # Cost line first (inside the report's headline): `embedded` is what this pass paid for.
    print(format_ingest_report(build_ingest_report(args.source_set, result, source_set)))
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
