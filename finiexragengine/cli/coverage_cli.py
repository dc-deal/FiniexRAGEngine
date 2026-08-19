"""CLI entry point: corpus coverage (which symbols the corpus covers) and the floor profile.

Two questions about the same distances, deliberately in one place because they share every
input — the symbol queries, the cached query vectors and the active floor. Coverage asks
*"does the corpus cover this symbol at all"*; `--floor-profile` asks the one underneath it
(ISSUE_55): *"is `retrieval.floor_distance` discriminating for this query, or is it starving
the symbol / waving everything through"*.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.cost_recorder import CostRecorder
from finiexragengine.core.observability.reports.coverage_report import (
    COVERAGE_FLOOR,
    build_coverage_report,
    format_coverage_report,
)
from finiexragengine.core.observability.reports.floor_profile_report import (
    build_floor_profile_report,
    format_floor_profile_report,
)
from finiexragengine.core.observability.reports.no_data_report import build_no_data_report
from finiexragengine.core.rag.openai_embedder import OpenAIEmbedder
from finiexragengine.core.rag.query_vector_cache import QueryVectorCache
from finiexragengine.exceptions.ragengine_errors import PipelineNotFoundError
from finiexragengine.utils.console_encoding import use_utf8_output
from finiexragengine.utils.report_window import parse_since


def main() -> None:
    # Reports carry `→`, `⚠`, `—`; a piped run would die on a cp1252 stdout.
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Corpus coverage per symbol query, or the retrieval floor profile '
                    '(--floor-profile): is the cut discriminating for this query')
    parser.add_argument('--pipeline', default='crypto_sentiment',
                        help='pipeline id under configs/pipelines/')
    parser.add_argument('--floor', type=float, default=None,
                        help='floor override for tuning experiments; default = the '
                             "pipeline's retrieval.floor_distance")
    parser.add_argument('--floor-profile', action='store_true',
                        help='render the retrieval floor profile instead: per-query distance '
                             'distribution, the knee, and the archive no-data share — is the '
                             'floor discriminating for this query at all (ISSUE_55 groundwork)')
    parser.add_argument('--archive-since', default='7d',
                        help='with --floor-profile: archive window for the mech.HOLD column')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    # Wiring only: app config (embedding model/dims) + the pipeline's symbol->query map
    # via the Pydantic-validated registry; report logic lives in
    # core.observability.reports.coverage_report.
    app = AppConfigManager()
    cfg = app.get_config()
    registry = app.build_pipeline_registry()
    try:
        pipeline = registry.get(args.pipeline).get_config()
    except PipelineNotFoundError as exc:
        parser.error(str(exc))
    # Honest header: say when the measured config diverges from the tracked file.
    config_label = f'configs/pipelines/{args.pipeline}.json'
    if registry.is_overridden(args.pipeline):
        config_label += ' (+ user override)'

    # The report measures against the *active* floor (retrieval.floor_distance) so its
    # n≤f column predicts real retrieval; --floor overrides for what-if tuning runs.
    floor = args.floor if args.floor is not None else (
        pipeline.retrieval.floor_distance or COVERAGE_FLOOR)

    recorder = CostRecorder(database_url, cfg.pricing)
    embedder = OpenAIEmbedder(cfg.embedding, cost_recorder=recorder, section='ingest_query')
    cache = QueryVectorCache(embedder, database_url, model=cfg.embedding.model,
                             dimensions=cfg.embedding.dimensions)
    if args.floor_profile:
        # The profile needs two things coverage does not: the pipeline's own feed ids (the corpus
        # is shared, so 'foreign' is only meaningful against the set this pipeline declares) and
        # the archive's no-data shares, which come from the report that already computes them.
        source_set = app.build_source_set_registry().get(pipeline.source_set)
        own_source_ids = {source.source_id for source in source_set.active_sources()}
        since, since_label = parse_since(args.archive_since)
        no_data = build_no_data_report(database_url, since, since_label=since_label)
        profile = build_floor_profile_report(
            database_url, pipeline.symbol_query_map(), cache,
            pipeline_id=args.pipeline, config_file=config_label,
            model=cfg.embedding.model,
            window_minutes=pipeline.retrieval.recency_window_minutes, floor=floor,
            own_source_ids=own_source_ids, no_data_rows=no_data.rows,
            archive_label=since_label)
        print(format_floor_profile_report(profile))
        return

    report = build_coverage_report(
        pipeline.symbol_query_map(), cache, database_url,
        pipeline_id=args.pipeline, config_file=config_label,
        model=cfg.embedding.model,
        window_minutes=pipeline.retrieval.recency_window_minutes, floor=floor)
    print(format_coverage_report(report))


if __name__ == '__main__':
    main()
