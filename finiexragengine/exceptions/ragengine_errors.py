"""Custom exceptions for FiniexRAGEngine.

All errors root at FiniexRagError. These subclasses back the envelope's
RunError.type taxonomy — ISSUE_7 maps each to its fixed taxonomy string
(SOURCE_UNREACHABLE, LLM_TIMEOUT, VECTOR_STORE_ERROR, …) so a downstream
collector can classify failures without parsing log text.
"""
from typing import Optional


class FiniexRagError(Exception):
    """Root base for all FiniexRAGEngine errors."""


class PipelineNotFoundError(FiniexRagError):
    """Raised when a requested pipeline_id is not registered."""


class ConfigurationError(FiniexRagError):
    """Raised when a config is inconsistent (e.g. a model outside allowed_models)."""


class SourceFetchError(FiniexRagError):
    """Raised when an input source cannot be fetched.

    Carries a **typed reason** so source-health (ISSUE_11) can classify a failure without
    parsing the message. `error_type` is from a small taxonomy — RATE_LIMITED (HTTP 429),
    HTTP_ERROR (other 4xx/5xx), UNREACHABLE (DNS/TLS/transport), PARSE_ERROR (malformed feed
    body) — and `status` is the HTTP status when the failure was an HTTP response, else None.
    """

    def __init__(self, message: str, *, error_type: str = 'UNREACHABLE',
                 status: Optional[int] = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.status = status


class EmbeddingError(FiniexRagError):
    """Raised when the embedding provider fails or returns an unexpected dimension."""


class LLMError(FiniexRagError):
    """Raised when the LLM provider fails or returns unparseable output."""


class LLMTimeoutError(LLMError):
    """The LLM call exceeded the configured timeout (taxonomy: LLM_TIMEOUT)."""


class LLMApiError(LLMError):
    """The LLM backend returned an error (taxonomy: LLM_API_ERROR)."""


class LLMParseError(LLMError):
    """The LLM returned output that did not parse/validate (taxonomy: LLM_PARSE_ERROR)."""


class VectorStoreError(FiniexRagError):
    """Raised on vector-store I/O failures."""
