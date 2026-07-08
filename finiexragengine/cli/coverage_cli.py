"""CLI entry point: corpus coverage report (which symbols the corpus actually covers)."""
import argparse
import json
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.rag.coverage_report import (
    COVERAGE_FLOOR,
    build_coverage_report,
    format_coverage_report,
)
from finiexragengine.core.rag.openai_embedder import OpenAIEmbedder
from finiexragengine.core.rag.query_vector_cache import QueryVectorCache
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Corpus coverage report per symbol query')
    parser.add_argument('--pipeline', default='crypto_sentiment',
                        help='pipeline id under configs/pipelines/')
    parser.add_argument('--floor', type=float, default=COVERAGE_FLOOR,
                        help='best-distance beyond which coverage counts as generic fallback')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    # Wiring only: app config (embedding model/dims, corpus table) + the pipeline's
    # symbol->query map; the report logic lives in core.rag.coverage_report.
    app = AppConfigManager()
    cfg = app.get_config()
    pipeline = PipelineConfig(**json.loads(
        (app.get_pipelines_dir() / f'{args.pipeline}.json').read_text(encoding='utf-8')))

    embedder = OpenAIEmbedder(cfg.embedding)
    cache = QueryVectorCache(embedder, database_url, model=cfg.embedding.model,
                             dimensions=cfg.embedding.dimensions)
    report = build_coverage_report(
        pipeline.symbol_queries, cache, database_url,
        pipeline_id=args.pipeline, model=cfg.embedding.model,
        window_minutes=pipeline.retrieval.recency_window_minutes,
        article_table=cfg.vector_store.table, floor=args.floor)
    print(format_coverage_report(report))


if __name__ == '__main__':
    main()
