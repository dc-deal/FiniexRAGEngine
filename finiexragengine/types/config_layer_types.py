"""Shapes of the layered configuration load — tracked reference, private layer, machine overlay."""
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class AppliedOverlay:
    """One overlay merged onto a document: where it came from and what it was merged onto.

    `base` is the document as it stood *before* this overlay — the tracked file for the first
    overlay, the already-merged result for the next — so the override report can render
    `old → new` per touched leaf for each layer on its own.
    """
    label: str                    # e.g. 'configs_private/pipelines/x.json', 'user_configs/…/x.json'
    base: Dict[str, Any]
    overlay: Dict[str, Any]


@dataclass
class LayeredDocument:
    """One configuration document resolved across every layer that defines or patches it."""
    name: str                     # the file name shared by every layer, e.g. 'x.json'
    data: Dict[str, Any]          # the merged result, ready for validation
    private: bool                 # its complete definition lives in the private layer
    overlays: List[AppliedOverlay] = field(default_factory=list)
