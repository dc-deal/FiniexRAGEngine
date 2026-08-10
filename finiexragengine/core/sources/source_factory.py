"""Builds a concrete input source from its SourceConfig (declared `type` -> class)."""
from finiexragengine.core.sources.abstract_source import AbstractSource
from finiexragengine.core.sources.rss_source import RssSource
from finiexragengine.exceptions.ragengine_errors import SourceFetchError
from finiexragengine.types.config_types.source_set_types import SourceConfig


def build_source(config: SourceConfig, default_timeout_seconds: int = 10) -> AbstractSource:
    """Instantiate the source implementation for a SourceConfig's `type`.

    The schema already allows `blog`/`socket`/`api`, but only `rss` is implemented;
    an unimplemented type fails loudly here rather than silently ingesting nothing.

    `default_timeout_seconds` is the source-set's `fetch_timeout_seconds` (ISSUE_73), applied
    unless the source overrides it — a network deadline every future source type needs too, so it
    belongs on the factory seam rather than at one implementation's call site.
    """
    if config.type == 'rss':
        return RssSource(config, default_timeout_seconds)
    raise SourceFetchError(
        f"source '{config.source_id}': type '{config.type}' is not implemented yet")
