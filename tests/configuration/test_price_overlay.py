"""The price applier (ISSUE_67) — the only path that may change a price, and its gate.

The probe reports; this writes, with a human behind it. Two properties matter and both are here:
only the leaves that drifted are written (an applier that rewrote the block would freeze today's
values for models nobody reviewed), and the candidate is validated BEFORE the file is touched,
because the overlay sits outside the load-time Pydantic gate.
"""
import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from finiexragengine.configuration.price_overlay import apply_drifts
from finiexragengine.types.pricing_types import PriceDrift


def _drift(model: str = 'gpt-5-nano', field: str = 'input_per_1k',
           table_value: float = 0.00005, probed_value: float = 0.00004) -> PriceDrift:
    return PriceDrift(model=model, field=field, table_value=table_value,
                      probed_value=probed_value)


def test_only_the_drifting_leaf_is_written(tmp_path: Path):
    overlay = tmp_path / 'app_config.json'

    written = apply_drifts([_drift()], overlay, checked=date(2026, 9, 15))

    data = json.loads(overlay.read_text(encoding='utf-8'))
    assert written == {'gpt-5-nano': {'input_per_1k': 0.00004}}
    assert data['pricing']['models'] == {'gpt-5-nano': {'input_per_1k': 0.00004}}
    assert data['pricing']['checked'] == '2026-09-15'
    # The untouched half of the model is NOT written: the overlay says what differs from the
    # tracked file, and an output price nobody reviewed must keep coming from there.
    assert 'output_per_1k' not in data['pricing']['models']['gpt-5-nano']


def test_an_existing_overlay_keeps_everything_it_already_held(tmp_path: Path):
    overlay = tmp_path / 'app_config.json'
    overlay.write_text(json.dumps({'telegram': {'enabled': True},
                                   'pricing': {'models': {'gpt-4o': {'input_per_1k': 0.002}}}}),
                       encoding='utf-8')

    apply_drifts([_drift()], overlay, checked=date(2026, 9, 15))

    data = json.loads(overlay.read_text(encoding='utf-8'))
    assert data['telegram'] == {'enabled': True}                       # untouched neighbour
    assert data['pricing']['models']['gpt-4o'] == {'input_per_1k': 0.002}
    assert data['pricing']['models']['gpt-5-nano'] == {'input_per_1k': 0.00004}


def test_an_impossible_price_is_refused_before_the_file_is_touched(tmp_path: Path):
    """The gate runs first, so a refusal leaves the running configuration exactly as it was."""
    overlay = tmp_path / 'app_config.json'
    overlay.write_text('{"pricing": {"models": {}}}', encoding='utf-8')
    before = overlay.read_bytes()

    with pytest.raises(ValidationError):
        apply_drifts([_drift(probed_value='not-a-price')], overlay)

    assert overlay.read_bytes() == before


def test_stamping_the_date_is_what_confirming_means(tmp_path: Path):
    """`checked` says the table was held against the vendor's published rates — which is what a
    human confirming a fetched page has just done. The automatic probe never reaches here."""
    overlay = tmp_path / 'app_config.json'

    apply_drifts([], overlay, checked=date(2026, 9, 15))

    assert json.loads(overlay.read_text(encoding='utf-8'))['pricing']['checked'] == '2026-09-15'
