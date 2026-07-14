"""Abstract base for an input source (RSS, blog, socket, API)."""
from abc import ABC, abstractmethod
from typing import List

from finiexragengine.types.article_types import Article
from finiexragengine.types.config_types.source_set_types import SourceConfig


class AbstractSource(ABC):
    """Contract for a pluggable input source.

    A source fetches raw articles. The trigger axis (interval-pull vs event-push)
    is handled by the Trigger layer, not here.
    """

    def __init__(self, config: SourceConfig) -> None:
        self._config = config

    def get_source_id(self) -> str:
        return self._config.source_id

    def get_url(self) -> str:
        """The feed URL — used to derive the health-store `host` (ISSUE_11)."""
        return self._config.url

    def due_for_fetch(self) -> bool:
        """Whether this source should be polled this pass (ISSUE_11).

        The default source is always due; a source with a poll floor (e.g. a feed that ignores
        conditional GET) overrides this. The Ingestor gates on it *before* fetch, so a within-floor
        pass is a local no-op — not a health event (a floor skip must not reset a failure streak)."""
        return True

    @abstractmethod
    def fetch(self) -> List[Article]:
        """Fetch the current set of articles from this source.

        Returns:
            Articles with their idempotent article_id already assigned (ISSUE_3).
        """
        ...
