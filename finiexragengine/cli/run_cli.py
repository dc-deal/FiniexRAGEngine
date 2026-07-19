"""CLI entry point: run one full pipeline pass and print the envelope (ISSUE_7).

Paid — one ingest pass (embeds new articles) plus one LLM call per configured symbol.
The console twin of `POST /v1/pipelines/{id}/run`; ends with the run-metrics footer.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.envelope_report import format_envelope_run
from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.exceptions.ragengine_errors import PipelineNotFoundError


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Run one full pipeline pass (ingest -> eval all symbols -> envelope)')
    parser.add_argument('--pipeline', default='crypto_sentiment',
                        help='pipeline id under configs/pipelines/')
    parser.add_argument('--json', action='store_true', help='also print the envelope JSON')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    # Same wiring the API uses: registry validates the config, the assembler builds
    # the graph — the CLI only receives parameters and prints.
    app = AppConfigManager()
    registry = app.build_pipeline_registry()
    try:
        config = registry.get(args.pipeline).get_config()
    except PipelineNotFoundError as exc:
        parser.error(str(exc))

    runner = PipelineAssembler(app, database_url).build_runner(config)
    envelope = runner.run()
    print(format_envelope_run(envelope))
    if args.json:
        print('\n' + envelope.model_dump_json(indent=2))


if __name__ == '__main__':
    main()
