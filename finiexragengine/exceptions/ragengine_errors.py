"""Custom exceptions for FiniexRAGEngine.

All errors root at FiniexRagError. These subclasses back the envelope's
RunError.type taxonomy — ISSUE_7 maps each to its fixed taxonomy string
(SOURCE_UNREACHABLE, LLM_TIMEOUT, VECTOR_STORE_ERROR, …) so a downstream
collector can classify failures without parsing log text.
"""


class FiniexRagError(Exception):
    """Root base for all FiniexRAGEngine errors."""


class PipelineNotFoundError(FiniexRagError):
    """Raised when a requested pipeline_id is not registered."""


class SourceFetchError(FiniexRagError):
    """Raised when an input source cannot be fetched."""


class EmbeddingError(FiniexRagError):
    """Raised when the embedding provider fails or returns an unexpected dimension."""


class LLMError(FiniexRagError):
    """Raised when the LLM provider fails or returns unparseable output."""


class VectorStoreError(FiniexRagError):
    """Raised on vector-store I/O failures."""
