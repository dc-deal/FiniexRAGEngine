"""Model catalog — verifies configured models against the provider's live list (ISSUE_40)."""
import logging
from typing import Dict, Iterable, List, Optional, Set, Tuple

from openai import OpenAI, OpenAIError

from finiexragengine.exceptions.ragengine_errors import LLMApiError
from finiexragengine.types.config_types.app_config_types import AppConfig

logger = logging.getLogger(__name__)

# Section title + model dict — one entry per stage in `check_configured_models`.
CheckSection = Tuple[str, Dict[str, bool]]


class ModelCatalog:
    """Queries one provider endpoint's available models and checks configured ids.

    **Endpoint-scoped, not stage-scoped:** the LLM stage may point at an OpenAI-compatible
    `base_url` (vLLM, Ollama — there `models.list` reflects the locally served models)
    while embeddings stay on the OpenAI default — each endpoint gets its own catalog.
    `models.list` is a **free** call (no tokens); the result is memoized per catalog so
    several `check()`s share one fetch. Account-owned fine-tunes appear as `ft:...` ids.
    Used two ways: **softly at server boot** (warn, never block — the allowlist stays the
    hard gate, and a transient provider outage must not stop `/latest`) and **manually
    via the models CLI**.
    """

    def __init__(self, base_url: Optional[str] = None,
                 client: Optional[OpenAI] = None) -> None:
        self._base_url = base_url
        self._client = client   # built lazily from OPENAI_API_KEY if not injected
        self._available: Optional[Set[str]] = None

    def available_ids(self) -> Set[str]:
        """Every model id the provider serves for this key/endpoint (free, memoized)."""
        if self._available is None:
            try:
                if self._client is None:
                    self._client = OpenAI(base_url=self._base_url)
                self._available = {model.id for model in self._client.models.list()}
            except OpenAIError as exc:
                raise LLMApiError(f'model list unavailable: {exc}') from exc
        return self._available

    def check(self, models: Iterable[str]) -> Dict[str, bool]:
        """model -> is it actually available at the provider right now."""
        available = self.available_ids()
        return {model: model in available for model in models}


def check_configured_models(config: AppConfig) -> List[CheckSection]:
    """Both report sections, staged like the run itself: ingest first, then llm stage.

    The embedding model is corpus-binding (#16): if it disappears at the provider, both
    ingest AND query embedding fail, and unlike the eval model there is no substitute
    without re-embedding the whole corpus — so it is checked with the same weight.
    One `models.list` per distinct endpoint: with `llm.base_url` unset both sections
    share a single catalog (one call); with it set, the embedding model is still checked
    against the OpenAI default — a self-hosted LLM endpoint does not serve
    `text-embedding-*`, and a false MISSING would cry wolf at every boot.
    """
    llm_catalog = ModelCatalog(base_url=config.llm.base_url)
    embed_catalog = ModelCatalog() if config.llm.base_url else llm_catalog
    return [
        ('ingest — embedding model', embed_catalog.check([config.embedding.model])),
        ('llm stage — eval models (allowed_models)',
         llm_catalog.check(config.llm.allowed_models)),
    ]


def verify_embedding_tokenizer(config: AppConfig) -> bool:
    """Boot-time check that the embedding model's tokenizer resolves AND loads (ISSUE_79).

    Two failures this moves from mid-pass to startup:

    - **An unknown model.** tiktoken maps model → encoding from a table it maintains (the OpenAI
      API cannot answer this — `/v1/models` carries no encoding field), and raises for a model it
      has not seen. Discovering that inside a worker thread means every ingest pass dies.
    - **A cold vocabulary cache.** On first use tiktoken downloads its BPE table with
      `requests.get()` and **no timeout**. Measured: the process-wide `socket.setdefaulttimeout()`
      from ISSUE_73 does *not* bound it — urllib3 uses its own sentinel rather than falling back to
      the socket default, so a stalled response is waited out in full. Forcing the download here
      means a hang happens at startup, in front of the operator, once — instead of inside a pass.

    Soft like its sibling above: it warns and returns False rather than blocking the boot. A run
    without a tokenizer still fails per pass, but loudly and with this line already in the log.
    """
    from finiexragengine.core.rag.token_budget import TokenBudget   # local: avoids a rag↔llm cycle
    try:
        budget = TokenBudget(config.embedding)
        budget.fit('warm the vocabulary')     # forces the cache load, not just the mapping
    except Exception as exc:   # noqa: BLE001 — a boot check never blocks the boot
        logger.warning('embedding tokenizer unavailable for %s: %s — inputs cannot be fitted to '
                       'the model limit, so every ingest pass will fail on an over-long article',
                       config.embedding.model, exc)
        return False
    return True


def verify_configured_models(config: AppConfig) -> bool:
    """Boot-time soft check: warn per unavailable configured model; never raise/block.

    A typo'd model, a retired snapshot or a deleted fine-tune gets a loud line *before*
    it costs a failed run — but the check itself failing (network, missing key) only
    logs and moves on. Returns False when the check could not run (rich/yellow: #25).
    """
    # The tokenizer is checked alongside (ISSUE_79) — same question, one boot: can this engine
    # actually use the embedding model it was configured with?
    verify_embedding_tokenizer(config)
    try:
        sections = check_configured_models(config)
    except LLMApiError as exc:
        logger.warning('startup model check skipped: %s', exc)
        return False
    for title, checked in sections:
        for model, available in checked.items():
            if not available:
                logger.warning("model '%s' (%s) is not available at the provider "
                               '(typo, retired snapshot, or deleted fine-tune?)',
                               model, title)
    return True


def format_model_check(sections: List[CheckSection], used_by: Dict[str, List[str]],
                       endpoint: str) -> str:
    """Render the staged check as the console pattern table (models CLI)."""
    divider = '-' * 78
    lines = [
        'Model Check',
        f'endpoint: {endpoint}',
        divider,
        f'{"model":44} {"available":>9}  used by',
        divider,
    ]
    total = 0
    missing: List[str] = []
    for title, checked in sections:
        lines.append(title)
        for model, available in checked.items():
            total += 1
            if not available:
                missing.append(model)
            status = 'yes' if available else 'MISSING'
            pipelines = ', '.join(used_by.get(model, [])) or '-'
            lines.append(f'  {model:42} {status:>9}  {pipelines}')
    lines.append(divider)
    lines.append(f'{total} models checked · '
                 + (f'{len(missing)} MISSING: {", ".join(missing)}' if missing else 'all available'))
    return '\n'.join(lines)
