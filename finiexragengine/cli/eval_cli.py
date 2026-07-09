"""CLI: evaluate one symbol end-to-end and print the signal + prompt excerpt + timings.

Paid — one retrieval + one LLM call. A visual, single-symbol preview of the eval flow
(ISSUE_7 orchestrates this over all symbols into the envelope).
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.llm.openai_provider import OpenAIProvider
from finiexragengine.core.llm.prompt_builder import PromptBuilder
from finiexragengine.core.observability.cost_recorder import CostRecorder, derive_usd
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.core.pipeline.symbol_evaluator import (
    SymbolEvaluator,
    format_symbol_eval,
)
from finiexragengine.core.rag.openai_embedder import OpenAIEmbedder
from finiexragengine.core.rag.pgvector_store import PgVectorStore
from finiexragengine.core.rag.query_vector_cache import QueryVectorCache
from finiexragengine.core.rag.retriever import Retriever
from finiexragengine.exceptions.ragengine_errors import PipelineNotFoundError


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Evaluate one symbol (retrieve -> prompt -> LLM) and print the signal')
    parser.add_argument('--pipeline', default='crypto_sentiment')
    parser.add_argument('--symbol', default='BTCUSD')
    parser.add_argument('--prompt-cols', type=int, default=60)
    parser.add_argument('--prompt-lines', type=int, default=4)
    parser.add_argument('--full-prompt', action='store_true', help='dump the whole prompt')
    parser.add_argument('--json', action='store_true', help='also print the SentimentResult JSON')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    app = AppConfigManager()
    cfg = app.get_config()
    registry = PipelineRegistry(app.get_pipelines_dir())
    registry.load()
    try:
        pipeline = registry.get(args.pipeline).get_config()
    except PipelineNotFoundError as exc:
        parser.error(str(exc))

    # Wiring: cost recorder threads through both paid callers (query embed + LLM).
    recorder = CostRecorder(database_url, cfg.pricing)
    embedder = OpenAIEmbedder(cfg.embedding, cost_recorder=recorder, section='ingest_query')
    cache = QueryVectorCache(embedder, database_url, model=cfg.embedding.model,
                             dimensions=cfg.embedding.dimensions)
    store = PgVectorStore(cfg.vector_store, database_url, dimensions=cfg.embedding.dimensions)
    retriever = Retriever(cache, store, pipeline.retrieval)
    provider = OpenAIProvider(cfg.llm, cost_recorder=recorder, section='llm_eval',
                              pipeline_id=args.pipeline)
    evaluator = SymbolEvaluator(retriever, PromptBuilder(app.get_prompts_dir()), provider,
                                prompt_name=pipeline.prompt.name,
                                prompt_version=pipeline.prompt.version,
                                breaking_threshold=pipeline.breaking.urgency_threshold)

    query = pipeline.symbol_queries.get(args.symbol, args.symbol)
    ev = evaluator.evaluate(args.symbol, query)
    usd = derive_usd(cfg.pricing, cfg.llm.model,
                     ev.usage.prompt_tokens, ev.usage.completion_tokens)
    print(format_symbol_eval(ev, args.pipeline, usd,
                             prompt_cols=args.prompt_cols, prompt_lines=args.prompt_lines,
                             full_prompt=args.full_prompt))
    if args.json:
        print('\n' + ev.result.model_dump_json(indent=2))


if __name__ == '__main__':
    main()
