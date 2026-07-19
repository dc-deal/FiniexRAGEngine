"""CLI entry point: verify configured models against the provider's live list (ISSUE_40).

Free — `models.list` costs no tokens. The manual twin of the soft check the server runs
at boot: staged like the run itself — the ingest section (the corpus-binding embedding
model) and the llm stage section (every allowed eval model) — with whether the provider
actually serves each id right now, and who uses it.
"""
import argparse

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.llm.model_catalog import check_configured_models, format_model_check
from finiexragengine.exceptions.ragengine_errors import LLMApiError


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Check configured models against the provider (free, no tokens)')
    parser.parse_args()

    app = AppConfigManager()
    cfg = app.get_config()
    registry = app.build_pipeline_registry()

    # The 'used by' column: the embedding model backs the shared corpus + query vectors
    # for every pipeline; each eval model lists the pipelines that declare it.
    used_by = {cfg.embedding.model: ['shared corpus + query embedding (all pipelines)']}
    for pipeline in registry.list_pipelines():
        config = pipeline.get_config()
        used_by.setdefault(config.llm.model, []).append(config.pipeline_id)

    try:
        sections = check_configured_models(cfg)
    except LLMApiError as exc:
        parser.error(str(exc))
    # With a custom llm endpoint the two sections are checked against different hosts.
    default_endpoint = 'api.openai.com (default)'
    endpoint = (f'llm {cfg.llm.base_url} · embedding {default_endpoint}'
                if cfg.llm.base_url else default_endpoint)
    print(format_model_check(sections, used_by, endpoint))


if __name__ == '__main__':
    main()
