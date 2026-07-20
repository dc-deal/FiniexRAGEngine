"""User-override visibility — WHAT a gitignored override changes, once per process.

A `(+ user override)` marker says *that* a config diverges; this unit says *what* — as
**one line per override file**, so the startup stays scannable however many overrides a
machine carries:

    [OVERRIDE] pipelines/crypto_sentiment.json · symbols 8→2 · models[4o_enhanced].enabled true→false · floor_distance 0.7→0.65

Two failure modes it exists for:

- a **forgotten override** silently steering a run (a test floor left behind changes
  what the live pipeline retrieves for a week);
- a **typo'd key**: the Pydantic configs ignore unknown keys, so `floor_distanze`
  silently does nothing — flagged here (`⚠ floor_distanze?`), checked against the
  *validated* config (not the base file), so a legitimately added key with a schema
  default (`enabled`) renders as `→value`, never as a typo.

Display compression: paths keep only what the file context does not already say (the
leaf key; from the last `[id]` segment on for patch-by-id lists; a bare `sources[x]`
collapses to `x` — a source-set file is nothing but sources). Two leaves that would
collapse to the same label (`telegram.enabled` / `weekly_report.enabled`) keep their
full path instead. String values are never quoted, just `~changed`/`~added` — the full
text lives in the override file. More than six leaves collapse to `+N more`.

Spam guard: `emit_override_report` logs each file's line at most once per process —
worker/API boot and every CLI say it exactly once (console + rotating file via the
standard logging setup). Collection is a pure function; gating (the
`logging.warn_on_override` flag) is the AppConfigManager's job — this unit never reads
config.
"""
import json
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# File labels already reported in this process — the once-per-startup spam guard.
_REPORTED: set = set()

# Sentinel: the tracked base file has no such key (the '(added)' case) — distinct from
# an explicit JSON null.
_ABSENT: Any = object()

_MAX_LEAVES = 6   # per-file cap; beyond it the line ends in '+N more'


@dataclass(frozen=True)
class OverrideEntry:
    """One touched leaf: full `path` provenance plus the raw values (rendered on format)."""
    path: str
    override_value: Any
    base_value: Any = _ABSENT    # _ABSENT = key missing in the tracked file
    unknown: bool = False        # not in the validated config -> ignored by Pydantic (typo?)


def collect_overrides(base: Dict[str, Any], override: Dict[str, Any],
                      validated: Dict[str, Any],
                      list_keys: Optional[Dict[str, str]] = None,
                      _prefix: str = '') -> List[OverrideEntry]:
    """Walk the override against the tracked base and the validated merged config.

    Args:
        base: The tracked file's raw dict (source of the `old` values).
        override: The user override's raw dict (what to walk).
        validated: `model_dump()` of the validated merged config — the schema truth:
            a path missing here was silently dropped by Pydantic (typo candidate).
        list_keys: Id-keyed lists (same map the deep merge uses), for `key[id].` paths.

    Returns:
        One entry per touched leaf, in override-file order; same-value leaves are
        skipped (stating the base value again is not a divergence).
    """
    list_keys = list_keys or {}
    entries: List[OverrideEntry] = []
    for key, value in override.items():
        path = f'{_prefix}{key}'
        if not (isinstance(validated, dict) and key in validated):
            # Pydantic dropped it: the merged config never saw this key — a typo, or a
            # key the schema genuinely does not know. Either way: it did nothing.
            entries.append(OverrideEntry(path, value, unknown=True))
            continue
        base_value = base.get(key, _ABSENT) if isinstance(base, dict) else _ABSENT
        if isinstance(value, dict) and isinstance(validated[key], dict):
            entries += collect_overrides(
                base_value if isinstance(base_value, dict) else {},
                value, validated[key], list_keys, path + '.')
        elif (key in list_keys and isinstance(value, list)
              and isinstance(validated[key], list)):
            entries += _collect_keyed_list(
                base_value if isinstance(base_value, list) else [],
                value, validated[key], list_keys[key], list_keys, path)
        elif base_value is _ABSENT:
            entries.append(OverrideEntry(path, value))
        elif base_value != value:
            entries.append(OverrideEntry(path, value, base_value))
    return entries


def _collect_keyed_list(base_list: List[Any], override_list: List[Any],
                        validated_list: List[Any], id_field: str,
                        list_keys: Dict[str, str], path: str) -> List[OverrideEntry]:
    """Patch-by-id lists: recurse into the matched item, `[id]` in the path."""
    base_index = {item[id_field]: item for item in base_list
                  if isinstance(item, dict) and id_field in item}
    valid_index = {item[id_field]: item for item in validated_list
                   if isinstance(item, dict) and id_field in item}
    entries: List[OverrideEntry] = []
    for item in override_list:
        item_id = item.get(id_field) if isinstance(item, dict) else None
        if item_id is None or item_id not in valid_index:
            # Id-less (or validation-dropped) item — nothing to anchor a path on.
            entries.append(OverrideEntry(f'{path}[+]', item))
            continue
        if item_id not in base_index:
            # Known to the validated config but absent in the tracked file: the merge
            # appended it as a genuinely new item.
            entries.append(OverrideEntry(f'{path}[{item_id}]', item))
            continue
        patch = {k: v for k, v in item.items() if k != id_field}
        entries += collect_overrides(base_index[item_id], patch,
                                     valid_index[item_id], list_keys,
                                     f'{path}[{item_id}].')
    return entries


def format_override_report(file_label: str, entries: List[OverrideEntry]) -> str:
    """One line: `[OVERRIDE] <file> · <leaf> <old>→<new> · …` (typos as `⚠ key?`)."""
    # Compression must stay unambiguous: when two leaves collapse to the same label
    # (`telegram.enabled` and `weekly_report.enabled` both → `enabled`), those keep
    # their full path — a reader must never have to guess which section changed.
    shorts = [_short_path(entry.path) for entry in entries]
    counts = Counter(shorts)
    parts: List[str] = []
    for entry, short in zip(entries, shorts):
        path = entry.path if counts[short] > 1 else short
        if entry.unknown:
            parts.append(f'⚠ {path}?')
        elif isinstance(entry.override_value, str) or isinstance(entry.base_value, str):
            # Strings are prose (comments, urls) — the diff itself carries no signal.
            parts.append(f'{path} ~changed' if entry.base_value is not _ABSENT
                         else f'{path} ~added')
        elif entry.base_value is _ABSENT:
            parts.append(f'{path} →{_compact(entry.override_value)}')
        else:
            parts.append(f'{path} {_compact(entry.base_value)}'
                         f'→{_compact(entry.override_value)}')
    if len(parts) > _MAX_LEAVES:
        parts = parts[:_MAX_LEAVES - 1] + [f'+{len(parts) - (_MAX_LEAVES - 1)} more']
    label = file_label.removeprefix('user_configs/')
    return f'[OVERRIDE] {label} · ' + ' · '.join(parts)


def emit_override_report(file_label: str, entries: List[OverrideEntry]) -> None:
    """Log the line for `file_label` — at most once per process, nothing if empty."""
    if not entries or file_label in _REPORTED:
        return
    _REPORTED.add(file_label)
    logger.warning('%s', format_override_report(file_label, entries))


def _short_path(path: str) -> str:
    """Keep only what the file context does not already say.

    `llm.models[4o_enhanced].enabled` → `models[4o_enhanced].enabled`;
    `sources[fxstreet].enabled` → `fxstreet.enabled` (a source-set file is nothing but
    sources); `retrieval.floor_distance` → `floor_distance`. Ids are `[a-z0-9_]`, so
    splitting on dots is safe.
    """
    segments = path.split('.')
    bracketed = [i for i, segment in enumerate(segments) if '[' in segment]
    if not bracketed:
        return segments[-1]
    start = bracketed[-1]
    head = segments[start]
    if head.startswith('sources['):
        head = head[len('sources['):].rstrip(']')
    return '.'.join([head] + segments[start + 1:])


def _compact(value: Any) -> str:
    """Inline value: JSON literals for scalars, `len` for lists, `{…}` for dicts."""
    if isinstance(value, list):
        return str(len(value))
    if isinstance(value, dict):
        return '{…}'
    return json.dumps(value, ensure_ascii=False)
