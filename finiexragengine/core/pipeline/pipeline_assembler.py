"""Pipeline assembler — builds the per-pipeline object graph behind a PipelineRunner (ISSUE_7)."""
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.llm.openai_provider import OpenAIProvider
from finiexragengine.core.llm.prompt_builder import PromptBuilder
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
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig


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

    def get_cost_recorder(self) -> CostRecorder:
        return self._recorder

    def build_runner(self, config: PipelineConfig) -> PipelineRunner:
        """Assemble one pipeline's full graph; billing sections per paid caller."""
        pipeline_id = config.pipeline_id
        # Two embedder instances over the same model: article ingest and query embedding
        # bill under their own sections (ingest_news / ingest_query) — the cost report
        # separates corpus building from retrieval overhead.
        news_embedder = OpenAIEmbedder(self._cfg.embedding, cost_recorder=self._recorder,
                                       section='ingest_news', pipeline_id=pipeline_id)
        query_embedder = OpenAIEmbedder(self._cfg.embedding, cost_recorder=self._recorder,
                                        section='ingest_query', pipeline_id=pipeline_id)
        store = PgVectorStore(self._cfg.vector_store, self._database_url,
                              dimensions=self._cfg.embedding.dimensions)
        cache = QueryVectorCache(query_embedder, self._database_url,
                                 model=self._cfg.embedding.model,
                                 dimensions=self._cfg.embedding.dimensions)
        retriever = Retriever(cache, store, config.retrieval)
        prompt_builder = PromptBuilder(self._app.get_prompts_dir())
        provider = OpenAIProvider(self._cfg.llm, cost_recorder=self._recorder,
                                  section='llm_eval', pipeline_id=pipeline_id)
        evaluator = SymbolEvaluator(retriever, prompt_builder, provider,
                                    prompt_name=config.prompt.name,
                                    prompt_version=config.prompt.version,
                                    breaking_threshold=config.breaking.urgency_threshold)
        ingestor = Ingestor([build_source(source) for source in config.sources],
                            news_embedder, store)
        # The prompt fingerprint is resolved once here (ISSUE_33) — the runner stamps it
        # on every envelope, valid even for a pass where all evals fail.
        prompt_metadata = prompt_builder.metadata(config.prompt.name, config.prompt.version)
        return PipelineRunner(config, ingestor, evaluator, prompt_metadata,
                              llm_model=self._cfg.llm.model, cost_recorder=self._recorder)

    def attach_all(self, registry: PipelineRegistry) -> None:
        """Give every registered pipeline its real runner (replaces the scaffold mock)."""
        for pipeline in registry.list_pipelines():
            pipeline.set_runner(self.build_runner(pipeline.get_config()))
