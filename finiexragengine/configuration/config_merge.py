"""Deep-merge for hierarchical config overrides — tracked base ← gitignored user override.

The one place the override semantics live, shared by the app-config and the per-pipeline
override paths:

- nested **dicts** merge recursively;
- plain **lists** (e.g. `symbols`, `keywords`) and scalars **replace** wholesale;
- **lists of objects with a known id key** (declared in `list_keys`, e.g. `models` →
  `sub_pipeline_id`, `sources` → `source_id`) **merge by that id** — a matching item is patched
  in place (so an override can flip one variant's `enabled` without restating the whole array),
  a new id is appended, and untouched base items are kept.
"""
from typing import Any, Dict, List, Optional


def deep_merge(base: Dict[str, Any], override: Dict[str, Any],
               list_keys: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Recursively merge `override` onto `base`.

    `list_keys` maps a key name to the id field its list items are merged by (patch-by-id);
    keys not listed replace wholesale (the default when `list_keys` is None).
    """
    list_keys = list_keys or {}
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value, list_keys)
        elif (key in list_keys and isinstance(result.get(key), list)
              and isinstance(value, list)):
            result[key] = _merge_keyed_list(result[key], value, list_keys[key], list_keys)
        else:
            result[key] = value
    return result


def _merge_keyed_list(base_list: List[Any], override_list: List[Any], id_field: str,
                      list_keys: Dict[str, str]) -> List[Any]:
    """Merge two lists of objects by `id_field`: patch matches, append new, keep the rest."""
    merged = [dict(item) if isinstance(item, dict) else item for item in base_list]
    index = {item[id_field]: i for i, item in enumerate(merged)
             if isinstance(item, dict) and id_field in item}
    for item in override_list:
        if isinstance(item, dict) and item.get(id_field) in index:
            position = index[item[id_field]]
            merged[position] = deep_merge(merged[position], item, list_keys)   # patch by id
        else:
            merged.append(dict(item) if isinstance(item, dict) else item)
    return merged
