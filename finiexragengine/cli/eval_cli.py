"""CLI: evaluate one symbol end-to-end and print the signal + prompt excerpt + timings.

Paid — one retrieval + one LLM call. A visual, single-symbol preview of the eval flow
(the full-envelope twin is `run_cli.py`). Wiring goes through the PipelineAssembler, so
the model-governance gate (allowed_models) applies here exactly as in the API.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.cost_recorder import derive_usd
from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.core.pipeline.symbol_evaluator import format_symbol_eval
from finiexragengine.exceptions.ragengine_errors import (
    BudgetExceededError,
    PipelineNotFoundError,
)


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

    # One wiring point (the assembler) for CLI and API alike — including the
    # allowed_models gate and the shared cost recorder behind every paid caller.
    assembler = PipelineAssembler(app, database_url)
    evaluator = assembler.build_evaluator(pipeline)

    query = pipeline.symbol_queries.get(args.symbol, args.symbol)
    # The eval CLI calls the evaluator directly (not via the runner, which degrades to HOLD), so
    # it handles the circuit-breaker suspend itself (ISSUE_47) — a clean message, not a traceback.
    try:
        ev = evaluator.evaluate(args.symbol, query)
    except BudgetExceededError as exc:
        print(f'⏸ paid evaluation suspended — {exc}')
        return
    usd = derive_usd(cfg.pricing, pipeline.llm.model,
                     ev.usage.prompt_tokens, ev.usage.completion_tokens)
    print(format_symbol_eval(ev, args.pipeline, usd, model=pipeline.llm.model,
                             prompt_cols=args.prompt_cols, prompt_lines=args.prompt_lines,
                             full_prompt=args.full_prompt))
    if args.json:
        print('\n' + ev.result.model_dump_json(indent=2))


if __name__ == '__main__':
    main()
