"""CLI entry point: run one ingest pass (fetch -> embed -> upsert) for a pipeline.

Spends OpenAI budget only on **new** articles (known ids are skipped before embedding),
and reports how many were embedded so the cost is never silent. Fenced under the paid
launch group.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.cost_recorder import CostRecorder
from finiexragengine.core.pipeline.ingestor import Ingestor
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.core.rag.openai_embedder import OpenAIEmbedder
from finiexragengine.core.rag.pgvector_store import PgVectorStore
from finiexragengine.core.sources.source_factory import build_source
from finiexragengine.exceptions.ragengine_errors import PipelineNotFoundError


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Run one ingest pass (fetch -> embed -> upsert) for a pipeline')
    parser.add_argument('--pipeline', default='crypto_sentiment',
                        help='pipeline id under configs/pipelines/')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    # Config loading goes through the Pydantic gate: app config + the pipeline via the
    # registry (validated on load; a bad --pipeline fails cleanly, not with a traceback).
    app = AppConfigManager()
    cfg = app.get_config()
    registry = PipelineRegistry(app.get_pipelines_dir())
    registry.load()
    try:
        pipeline = registry.get(args.pipeline).get_config()
    except PipelineNotFoundError as exc:
        parser.error(str(exc))

    sources = [build_source(source) for source in pipeline.sources]
    recorder = CostRecorder(database_url, cfg.pricing)
    embedder = OpenAIEmbedder(cfg.embedding, cost_recorder=recorder,
                              section='ingest_news', pipeline_id=args.pipeline)
    store = PgVectorStore(cfg.vector_store, database_url, dimensions=cfg.embedding.dimensions)
    result = Ingestor(sources, embedder, store).run()

    # Cost line first: `embedded` is what was actually paid for this pass.
    print(f"ingest '{args.pipeline}': fetched {result.fetched}, "
          f'embedded {result.embedded} (paid), stored {result.stored} new, '
          f'{result.duplicates} duplicates')
    for source_id, entry in result.per_source.items():
        print(f'  {source_id:14} fetched {entry.fetched:3}   embedded {entry.embedded:3}   '
              f'new {entry.stored:3}   dup {entry.duplicates:3}')
    for source_id, error in result.failed_sources.items():
        print(f'  {source_id:14} FAILED: {error}')


if __name__ == '__main__':
    main()
