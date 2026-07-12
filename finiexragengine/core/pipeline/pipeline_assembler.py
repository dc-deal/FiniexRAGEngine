"""Pipeline assembler — builds the per-pipeline object graph behind a PipelineRunner (ISSUE_7)."""
import logging

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.llm.prompt_builder import PromptBuilder
from finiexragengine.core.llm.provider_factory import build_provider
from finiexragengine.core.observability.cost_recorder import CostRecorder
from finiexragengine.core.pipeline.ingestor import Ingestor
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.core.pipeline.pipeline_runner import PipelineRunner
from finiexragengine.core.pipeline.symbol_evaluator import SymbolEvaluator
from finiexragengine.core.rag.openai_embedder import OpenAIEmbedder
from finiexragengine.core.rag.pgvector_store import PgVectorStore
from finiexragengine.core.rag.query_vector_cache import QueryVectorCache
from finiexragengine.core.rag.retriever import Retriever
from finiexragengine.core.sources.source_factory import build_source
from finiexragengine.core.store.outcome_store import OutcomeStore
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig

logger = logging.getLogger(__name__)


class PipelineAssembler:
    """Wires config into runnable units — the one place the object graph is built.

    Construction and execution stay separated: the registry loads/validates configs,
    this assembler turns a config into a `PipelineRunner` (sources, embedders, store,
    retriever, prompt builder, LLM provider, evaluator, ingestor), and the runner only
    executes. CLIs and the API share this wiring instead of each re-plumbing it.

    One `CostRecorder` is shared across everything the assembler builds, so every paid
    call of a process lands in the same billing log and the runner can read its own
    run's spend as a session delta.
    """

    def __init__(self, app: AppConfigManager, database_url: str) -> None:
        self._app = app
        self._cfg = app.get_config()
        self._database_url = database_url
        self._recorder = CostRecorder(database_url, self._cfg.pricing)
        # One store for all pipelines (ISSUE_8): every runner persists into it, the
        # API's /latest reads from it — the shared source of truth, like the recorder.
        self._outcome_store = OutcomeStore(database_url)

    def get_cost_recorder(self) -> CostRecorder:
        return self._recorder

    def get_outcome_store(self) -> OutcomeStore:
        return self._outcome_store

    def resolve_model(self, config: PipelineConfig) -> str:
        """The pipeline's declared eval model, validated against the governance allowlist.

        Fails fast at assembly — before any spend — so a typo or an unapproved model
        never reaches the API. An allowed model without a pricing entry still runs but
        is warned about: its calls would be billed as $0 (the recorder repeats the
        warning per call).
        """
        model = config.llm.model
        if model not in self._cfg.llm.allowed_models:
            raise ConfigurationError(
                f"pipeline '{config.pipeline_id}' declares model '{model}' which is not "
                f'in llm.allowed_models {self._cfg.llm.allowed_models} — extend the '
                'allowlist (user_configs/app_config.json) or fix the constellation')
        if model not in self._cfg.pricing.models:
            logger.warning("model '%s' has no pricing entry — its cost will record as "
                           '0.0 (add it to pricing.models)', model)
        return model

    def build_evaluator(self, config: PipelineConfig) -> SymbolEvaluator:
        """Assemble the per-symbol eval unit (retriever -> prompt -> LLM) for one pipeline."""
        model = self.resolve_model(config)
        query_embedder = OpenAIEmbedder(self._cfg.embedding, cost_recorder=self._recorder,
                                        section='ingest_query',
                                        pipeline_id=config.pipeline_id)
        store = PgVectorStore(self._cfg.vector_store, self._database_url,
                              dimensions=self._cfg.embedding.dimensions)
        cache = QueryVectorCache(query_embedder, self._database_url,
                                 model=self._cfg.embedding.model,
                                 dimensions=self._cfg.embedding.dimensions)
        retriever = Retriever(cache, store, config.retrieval)
        prompt_builder = PromptBuilder(self._app.get_prompts_dir())
        # Provider seam: `llm.provider` names the implementation, the factory resolves
        # it — the assembler never hard-codes a provider class.
        provider = build_provider(self._cfg.llm, model, cost_recorder=self._recorder,
                                  section='llm_eval', pipeline_id=config.pipeline_id)
        return SymbolEvaluator(retriever, prompt_builder, provider,
                               prompt_name=config.prompt.name,
                               prompt_version=config.prompt.version,
                               breaking_threshold=config.breaking.urgency_threshold)

    def build_runner(self, config: PipelineConfig) -> PipelineRunner:
        """Assemble one pipeline's full graph; billing sections per paid caller."""
        pipeline_id = config.pipeline_id
        # The news embedder bills under its own section (ingest_news) — the cost report
        # separates corpus building from retrieval overhead (ingest_query, in the evaluator).
        news_embedder = OpenAIEmbedder(self._cfg.embedding, cost_recorder=self._recorder,
                                       section='ingest_news', pipeline_id=pipeline_id)
        store = PgVectorStore(self._cfg.vector_store, self._database_url,
                              dimensions=self._cfg.embedding.dimensions)
        evaluator = self.build_evaluator(config)
        ingestor = Ingestor([build_source(source) for source in config.sources],
                            news_embedder, store)
        # The prompt fingerprint is resolved once here (ISSUE_33) — the runner stamps it
        # on every envelope, valid even for a pass where all evals fail.
        prompt_builder = PromptBuilder(self._app.get_prompts_dir())
        prompt_metadata = prompt_builder.metadata(config.prompt.name, config.prompt.version)
        return PipelineRunner(config, ingestor, evaluator, prompt_metadata,
                              llm_model=config.llm.model, cost_recorder=self._recorder,
                              outcome_store=self._outcome_store)

    def attach_all(self, registry: PipelineRegistry) -> None:
        """Give every registered pipeline its real runner (replaces the scaffold mock)."""
        for pipeline in registry.list_pipelines():
            pipeline.set_runner(self.build_runner(pipeline.get_config()))
