"""Resolve configuration documents across the three layers — one rule for every registry.

Order, lowest first:

1. the tracked reference (`configs/<kind>/`) — public, what a contributor gets out of the box;
2. the private layer (`configs_private/<kind>/`) — ours, a private repository identical on every
   machine: a file with the name of a tracked one is an **overlay** onto it, a file with a new
   name is a **complete** private definition;
3. the machine overlay (`user_configs/<kind>/`) — per machine, unversioned, patches whatever the
   two layers below produced.

Each overlay is merged with `deep_merge` (id-keyed lists patched by id), in that order, and recorded
with the document it was merged onto, so the override report shows each layer's own change. Two
*files* claiming one id are not resolved here — that is the registry's duplicate-id refusal, which
covers a private definition colliding with a tracked one as well.
"""
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from finiexragengine.configuration.config_merge import deep_merge
from finiexragengine.configuration.override_report import OverrideEntry
from finiexragengine.types.config_layer_types import AppliedOverlay, LayeredDocument

# A hook a registry may run after every merge step — the pipeline registry's llm-form guard needs
# to see each overlay on its own, because the form an overlay picks must win over the one below it.
MergeStep = Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]


def layer_label(directory: Path) -> str:
    """`user_configs/pipelines` for `…/user_configs/pipelines` — the part a reader needs."""
    return f'{directory.parent.name}/{directory.name}'


def load_layered_documents(tracked_dir: Path, private_dir: Optional[Path],
                           user_dir: Optional[Path], list_keys: Dict[str, str],
                           merge_step: Optional[MergeStep] = None) -> List[LayeredDocument]:
    """Every document the three layers define, merged, tracked ones first.

    Args:
        tracked_dir: The public reference directory (must exist).
        private_dir: The private layer's directory of the same kind; None or absent = no layer.
        user_dir: The machine overlay directory; None or absent = no overlays.
        list_keys: Lists merged by id (see `deep_merge`).
        merge_step: Optional hook applied after each overlay, given (merged, overlay).

    Returns:
        One `LayeredDocument` per tracked file, then one per private file without a tracked
        counterpart, each in file-name order.
    """
    tracked = sorted(tracked_dir.glob('*.json'))
    tracked_names = {path.name for path in tracked}
    private = sorted(private_dir.glob('*.json')) if _exists(private_dir) else []
    documents: List[LayeredDocument] = []
    for path in tracked:
        # A tracked document: the private layer may patch it, then the machine.
        overlays = [d for d in (private_dir, user_dir) if _exists(d)]
        documents.append(_resolve(path, overlays, private=False, list_keys=list_keys,
                                  merge_step=merge_step))
    for path in private:
        if path.name in tracked_names:
            continue   # an overlay onto a tracked document — applied above, not a definition
        # A complete private definition: only the machine may patch it.
        overlays = [d for d in (user_dir,) if _exists(d)]
        documents.append(_resolve(path, overlays, private=True, list_keys=list_keys,
                                  merge_step=merge_step))
    return documents


def _resolve(path: Path, overlay_dirs: List[Path], *, private: bool, list_keys: Dict[str, str],
             merge_step: Optional[MergeStep]) -> LayeredDocument:
    data = _read(path)
    document = LayeredDocument(name=path.name, data=data, private=private)
    for directory in overlay_dirs:
        overlay_path = directory / path.name
        if not overlay_path.exists():
            continue
        overlay = _read(overlay_path)
        merged = deep_merge(document.data, overlay, list_keys)
        if merge_step is not None:
            merged = merge_step(merged, overlay)
        document.overlays.append(AppliedOverlay(
            label=f'{layer_label(directory)}/{path.name}', base=document.data, overlay=overlay))
        document.data = merged
    return document


def _read(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def _exists(directory: Optional[Path]) -> bool:
    return directory is not None and directory.is_dir()


def group_entries_by_stem(entries: Dict[str, List[OverrideEntry]]
                          ) -> Dict[str, List[OverrideEntry]]:
    """Layer-labelled override entries regrouped by document (file stem), in layer order.

    The registries key what each overlay moved by its label (`configs_private/pipelines/x.json`,
    `user_configs/pipelines/x.json`); a config view serves one entry list per document, so both
    layers of `x` meet under `x` — the private layer's first, because it was applied first.
    """
    grouped: Dict[str, List[OverrideEntry]] = {}
    for label, items in entries.items():
        grouped.setdefault(Path(label).stem, []).extend(items)
    return grouped
