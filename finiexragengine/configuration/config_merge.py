"""Deep-merge for hierarchical config overrides — tracked base ← gitignored user override.

The one place the override semantics live, shared by the app-config and the per-pipeline
override paths: nested dicts merge recursively; scalars and **lists replace** (so overriding
`symbols` or `llm.models` sets them wholesale, which is the intent).
"""
from typing import Any, Dict


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge `override` onto `base` (nested dicts merge, scalars/lists replace)."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
