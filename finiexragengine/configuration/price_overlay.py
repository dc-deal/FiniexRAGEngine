"""The only writer of prices (ISSUE_67) — confirmed drifts into the gitignored overlay.

The probe never writes; this does, and only with a human behind it. It lives in `configuration/`
rather than beside the guard because what it touches is the config layer: `user_configs/
app_config.json`, the gitignored overlay that deep-merges over the tracked file at load and is
reported leaf by leaf at the next boot.

**Validated before the file is touched.** The overlay sits outside the Pydantic gate — it is just a
JSON file until something loads it — so an out-of-range price would otherwise land on disk and only
be refused at the next boot, which is the worst moment to find out. Building the candidate model
first means a refusal leaves the running configuration exactly as it was.
"""
import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from finiexragengine.types.config_types.app_config_types import PricingConfig
from finiexragengine.types.pricing_types import PriceDrift


def apply_drifts(drifts: Sequence[PriceDrift], overlay_path: Path,
                 checked: Optional[date] = None) -> Dict[str, Any]:
    """Write only the drifting leaves into the overlay; returns what was written.

    Only the leaves: an applier that rewrote the whole `pricing` block would freeze today's values
    for models nobody reviewed, and the overlay's job is to say what differs from the tracked file —
    not to become a second copy of it.

    `checked` is stamped as well, because that date means "held against the vendor's published
    rates" and confirming here is exactly that. The automatic probe never reaches this function.
    """
    data: Dict[str, Any] = {}
    if overlay_path.exists():
        data = json.loads(overlay_path.read_text(encoding='utf-8'))
    pricing = data.setdefault('pricing', {})
    models = pricing.setdefault('models', {})
    written: Dict[str, Any] = {}
    for drift in drifts:
        models.setdefault(drift.model, {})[drift.field] = drift.probed_value
        written.setdefault(drift.model, {})[drift.field] = drift.probed_value
    pricing['checked'] = (checked or date.today()).isoformat()
    PricingConfig(**pricing)          # the gate, before the write and not after
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    return written
