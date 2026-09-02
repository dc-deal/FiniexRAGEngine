"""Runtime domain type for an ingested news article."""
import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional, Tuple, get_args

from finiexragengine.types.outcome_types import RetrievalFunnel

# WHICH retrieval tier surfaced an article (ISSUE_30). `retrieval_tier` rather than a bare `tier`
# because the short name is already taken twice in this codebase — `ingest_types` and
# `BreakingConfig` use "tier" for the IMPORTANCE tier (1=LOW/2=MID/3=HIGH), which is a different
# axis entirely: importance decides whether an old article may be *admitted*, this records which
# window actually *produced* it.
#
# A closed vocabulary rather than the retriever's internal 0/1: the value reaches an envelope, and
# a reader should not have to know which integer meant which window. Strict here at the producing
# seam so a typo fails where it is written; the model field that stores it is a plain `str`, so an
# archived envelope carrying a value a later version introduces still parses.
RetrievalTier = Literal['recent', 'deep']
RETRIEVAL_TIERS: Tuple[str, ...] = get_args(RetrievalTier)


@dataclass
class Article:
    """A single ingested news article (raw, source-agnostic).

    Args:
        article_id: Idempotent identity key (hash of guid/url) — dedup across feeds and polls.
        source_id: Originating source identifier.
        source_weight: Trust / weight of the source (from the constellation config).
        url: Canonical article URL.
        title: Article headline, normalised at ingest (ISSUE_112).
        summary: Short summary / excerpt (full-text scraping is out of scope), normalised at ingest.
        language: Best-effort ISO language code.
        published_at: Publication time as reported by the feed (UTC, tz-aware).
        fetched_at: Time the article was fetched into the engine (UTC, tz-aware).
    """
    article_id: str
    source_id: str
    source_weight: float
    url: str
    title: str
    summary: str
    language: str
    published_at: datetime
    fetched_at: datetime
    # What the embedding actually saw (ISSUE_79). The embedded string is `title. summary`, built
    # per pass and never stored — so these two only describe the *embedding input*: how many tokens
    # were sent, and how many were cut to fit the model's limit (None = nothing was cut). Their sum
    # is the length of the normalised text. Stored rather than recomputed so per-source analysis is
    # a SQL aggregate, and so the row records what happened rather than what today's tokenizer
    # would say.
    embed_input_tokens: Optional[int] = None
    embed_truncated_tokens: Optional[int] = None
    # The text as the feed served it, kept ONLY where normalisation changed it (ISSUE_112) —
    # None means "arrived clean", not "not measured". This is what keeps the ingest rule intact:
    # markup is removed from what the model reads, never from what the engine holds, so an
    # injection investigation gets the exact bytes instead of a URL whose feed has rolled over.
    title_raw: Optional[str] = None
    summary_raw: Optional[str] = None
    # Which declared treatment produced `title`/`summary` and therefore the vector (ISSUE_112).
    # The ISSUE_79 pattern: the row records its own provenance rather than leaving it to be
    # inferred from when it was stored. None = stored before the column existed.
    text_normalizer: Optional[str] = None

    @staticmethod
    def make_id(url: str, guid: str | None = None) -> str:
        """Build the idempotent identity key from the article's guid/url.

        Args:
            url: Canonical article URL.
            guid: Feed-provided GUID, if any (preferred when present).

        Returns:
            A stable hex digest used as the dedup key (ISSUE_3).
        """
        basis = (guid or url).strip().lower()
        return hashlib.sha256(basis.encode('utf-8')).hexdigest()[:32]


@dataclass
class ScoredArticle:
    """A vector-store match: the article plus its retrieval-time score context (ISSUE_5).

    Args:
        article: The matched article.
        distance: Cosine distance to the query vector (lower = more similar).
        embedding: The stored embedding — lets retrieval collapse near-duplicate
            stories pairwise without re-embedding.
        importance: Corpus importance tag (None until the breaking detector sets it).
    """
    article: Article
    distance: float
    embedding: list[float]
    importance: int | None = None


@dataclass
class RetrievedArticle:
    """One article that reached the prompt, and which retrieval tier surfaced it (ISSUE_30).

    The pairing exists because the tier is a fact about *this retrieval*, not about the article:
    the same corpus row is `recent` for one pipeline's 24h window and `deep` for another's week.
    Putting it on `Article` would stamp a per-query verdict onto the shared corpus shape.

    Why it has to travel at all: fear/greed is a **current-mood** measure, so a week-old article
    must be fenced rather than mixed into today's reading — and nothing downstream could tell the
    two apart once `_squeeze` returned bare `Article`s.
    """
    article: Article
    retrieval_tier: RetrievalTier


@dataclass
class RetrievedContext:
    """What retrieval handed the evaluator: the squeezed context plus its funnel.

    Args:
        retrieved: At most `top_k` relevant, deduped articles — best first — each with the
            tier that surfaced it (ISSUE_30).
        funnel: The counters of how the squeeze arrived there (ISSUE_24) — carried
            through `SymbolEval` into the envelope metadata.
    """
    retrieved: list[RetrievedArticle]
    funnel: RetrievalFunnel

    @property
    def articles(self) -> list[Article]:
        """The bare articles, best first — the shape the prompt and the pass counters read.

        Kept as a property when `retrieved` replaced it as the field (ISSUE_30), and that is
        load-bearing rather than convenient: `PromptBuilder.build` renders `articles` straight into
        the template, and every archived envelope records its template's `content_hash` as
        `prompt_hash`. A shape change here would move the rendering of a prompt version that is
        immutable by rule and make archived provenance unverifiable. Extending the result object
        additively is precisely what result objects are kept for.
        """
        return [item.article for item in self.retrieved]
