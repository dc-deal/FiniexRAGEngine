"""Abstract base for an input source (RSS, blog, socket, API)."""
from abc import ABC, abstractmethod
from typing import List, Optional

from finiexragengine.core.sources.article_normalizer import ArticleNormalizer
from finiexragengine.types.article_types import Article
from finiexragengine.types.config_types.source_set_types import SourceConfig


class AbstractSource(ABC):
    """Contract for a pluggable input source.

    A source fetches raw articles. The trigger axis (interval-pull vs event-push)
    is handled by the Trigger layer, not here.
    """

    def __init__(self, config: SourceConfig,
                 normalizer: Optional[ArticleNormalizer] = None) -> None:
        self._config = config
        # Defaulted rather than required (ISSUE_112): a source built without one still normalises,
        # so the safe behaviour is what you get for free and the configured profile is what the
        # factory passes. A None default meaning "no normalisation" would make markup the reward
        # for forgetting an argument.
        self._normalizer: ArticleNormalizer = normalizer or ArticleNormalizer()

    def get_source_id(self) -> str:
        return self._config.source_id

    def get_url(self) -> str:
        """The feed URL — used to derive the health-store `host` (ISSUE_11)."""
        return self._config.url

    def get_fetch_deadline_ms(self) -> Optional[float]:
        """This source's effective fetch deadline in milliseconds, when it has one (ISSUE_84).

        The quarantine ladder reads a failure's duration *against its deadline*: a failure that
        burned the deadline is a feed that went quiet (transient, short cool-off), one that came
        back in milliseconds is a refusal (durable, long cool-off). Without the deadline that
        split cannot be made, because both arrive as `UNREACHABLE`.

        None means "this source type has no deadline to compare against" — the policy then reads
        the failure conservatively rather than guessing.
        """
        return None

    def due_for_fetch(self) -> bool:
        """Whether this source should be polled this pass (ISSUE_11).

        The default source is always due; a source with a poll floor (e.g. a feed that ignores
        conditional GET) overrides this. The Ingestor gates on it *before* fetch, so a within-floor
        pass is a local no-op — not a health event (a floor skip must not reset a failure streak)."""
        return True

    def reset_conditional_get(self) -> None:
        """Forget any "I have already seen this" state, so the next fetch re-pulls in full.

        Exists for one caller and one situation (ISSUE_107): a pass that fetched a source and then
        abandoned it without storing its articles. A source that remembers validators across polls
        (`RssSource`'s ETag / Last-Modified) would answer the next poll with `304` and those
        articles would be lost for good — the ingest contract is that anything a pass did not store
        is seen again. A stateless source has nothing to rewind, hence the no-op default.
        """
        return None

    def fetch(self) -> List[Article]:
        """Fetch this source's current articles, normalised (ISSUE_112).

        Concrete on purpose, and the reason is structural rather than stylistic: normalisation has
        to happen for *every* source type, and a step each implementation must remember is a step
        the next implementation forgets — the accretion that left 32 of 34 `psycopg.connect` calls
        unbounded (ISSUE_117). A new source type implements `_fetch_articles` and inherits the
        treatment without knowing it exists.

        The trade accepted here: `fetch` is no longer overridable. A source needing different
        acquisition mechanics overrides `_fetch_articles`; one needing a different *text* treatment
        is asking for a second profile, which is a config change, not a subclass.

        Returns:
            Articles with their idempotent article_id already assigned (ISSUE_3), their text
            normalised, and `text_normalizer` stamped.
        """
        return [self._normalizer.apply(article) for article in self._fetch_articles()]

    @abstractmethod
    def _fetch_articles(self) -> List[Article]:
        """Fetch the current set of articles from this source, as the feed serves them.

        Returns:
            Articles with their idempotent article_id already assigned (ISSUE_3). Text is
            un-normalised here — `fetch` applies the declared profile.
        """
        ...
