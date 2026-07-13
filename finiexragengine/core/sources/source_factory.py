"""Builds a concrete input source from its SourceConfig (declared `type` -> class)."""
from finiexragengine.core.sources.abstract_source import AbstractSource
from finiexragengine.core.sources.rss_source import RssSource
from finiexragengine.exceptions.ragengine_errors import SourceFetchError
from finiexragengine.types.config_types.source_set_types import SourceConfig


def build_source(config: SourceConfig) -> AbstractSource:
    """Instantiate the source implementation for a SourceConfig's `type`.

    The schema already allows `blog`/`socket`/`api`, but only `rss` is implemented;
    an unimplemented type fails loudly here rather than silently ingesting nothing.
    """
    if config.type == 'rss':
        return RssSource(config)
    raise SourceFetchError(
        f"source '{config.source_id}': type '{config.type}' is not implemented yet")
