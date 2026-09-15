"""Pricing probe (ISSUE_67) — the guard that must never write, tested where it could.

Every USD figure this engine reports comes from a hand-maintained table, and the probe's whole
design rests on one property: it reads the vendor's page, reports what differs, and **changes
nothing**. A plausible-but-wrong price applied automatically would corrupt the cost warehouse
invisibly and in money; a missed notice costs a week of staleness.

So the first test is the property, asserted rather than trusted. The rest are the ways a comparison
can be wrong while looking right: a coverage gap read as a drift, an unreadable page read as "no
change", and a per-model verdict where the vendor moved one leaf.
"""
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from finiexragengine.core.observability import price_probe
from finiexragengine.core.observability.price_probe import (
    condense,
    drift_notice,
    format_probe_result,
    probe_prices,
)
from finiexragengine.types.config_types.app_config_types import ModelPrice, PricingConfig
from finiexragengine.types.llm_types import LlmCompletion, LlmUsage
from finiexragengine.types.pricing_types import PROBE_STATUSES

_PAGE = 'gpt-4o-mini $0.15 / $0.60 · gpt-5-nano $0.05 / $0.40 (per 1M tokens)'


class _Provider:
    """Answers with a fixed extraction — the page text never reaches a network here."""

    def __init__(self, prices: Dict[str, Any], usage: LlmUsage = None) -> None:
        self._prices = prices
        self._usage = usage or LlmUsage(prompt_tokens=8000, completion_tokens=120)
        self.prompts: list = []

    def complete_structured(self, prompt: str, json_schema: Dict[str, Any]) -> LlmCompletion:
        self.prompts.append(prompt)
        return LlmCompletion(data={'prices': self._prices}, usage=self._usage,
                             model='gpt-4o-mini-2024-07-18')


def _pricing(**models: ModelPrice) -> PricingConfig:
    return PricingConfig(models=models or {
        'gpt-4o-mini': ModelPrice(input_per_1k=0.00015, output_per_1k=0.0006),
        'gpt-5-nano': ModelPrice(input_per_1k=0.00005, output_per_1k=0.0004)})


def _entry(model: str, inp: float, out: float, found: bool = True,
           cached: float = 0.0) -> Dict[str, Any]:
    """One extraction row, in the unit the page prints: USD per MILLION tokens.

    `cached` is carried because the schema asks for it: the vendor's rows are (input, cached,
    output), and the first live run mapped the middle value onto output. Every number having its
    own slot is what stopped that.
    """
    return {'model': model, 'input_per_1m_usd': inp, 'cached_input_per_1m_usd': cached,
            'output_per_1m_usd': out, 'found': found}


def test_the_guard_reports_and_writes_nothing(tmp_path: Path):
    """The property the whole shadow-mode design rests on — asserted, not trusted.

    A price the engine applied to itself would be wrong in the one direction nobody notices: every
    past figure stays frozen, every future one silently shifts.
    """
    pricing = _pricing()
    before = pricing.model_dump()
    tracked = Path('configs/app_config.json').read_bytes()
    provider = _Provider([_entry('gpt-4o-mini', 0.15, 0.60), _entry('gpt-5-nano', 0.04, 0.40)])

    result = probe_prices(pricing, provider, source_url='https://x.test/pricing',
                          probe_model='gpt-4o-mini', page=_PAGE)

    assert result.drifts and not result.clean
    assert pricing.model_dump() == before                     # the running table is untouched
    assert Path('configs/app_config.json').read_bytes() == tracked   # and so is the file


def test_a_drift_is_per_leaf_and_signed():
    """A vendor moves one number at a time, and so does the finding: an output price that moved
    while the input did not is one thing to review, not two."""
    provider = _Provider([_entry('gpt-4o-mini', 0.15, 0.30), _entry('gpt-5-nano', 0.05, 0.40)])

    result = probe_prices(_pricing(), provider, source_url='https://x.test/pricing',
                          probe_model='gpt-4o-mini', page=_PAGE)

    assert [(d.model, d.field, round(d.pct, 1)) for d in result.drifts] == [
        ('gpt-4o-mini', 'output_per_1k', -50.0)]
    assert 'gpt-4o-mini output 0.0006 → 0.0003 (-50.0 %)' in drift_notice(result)


def test_a_move_inside_the_epsilon_is_not_a_finding():
    """A page that renders 0.150 one week and 0.15 the next must not produce a notice."""
    provider = _Provider([_entry('gpt-4o-mini', 0.1501, 0.60), _entry('gpt-5-nano', 0.05, 0.40)])

    result = probe_prices(_pricing(), provider, source_url='https://x.test/pricing',
                          probe_model='gpt-4o-mini', epsilon_pct=1.0, page=_PAGE)

    assert result.clean and not result.drifts


def test_a_model_the_page_does_not_mention_is_a_gap_never_a_drift():
    """Coverage and disagreement are different findings, and only one of them is the vendor's."""
    provider = _Provider([_entry('gpt-4o-mini', 0.15, 0.60),
                          _entry('gpt-5-nano', 0.0, 0.0, found=False)])

    result = probe_prices(_pricing(), provider, source_url='https://x.test/pricing',
                          probe_model='gpt-4o-mini', page=_PAGE)

    assert result.drifts == () and result.missing == ('gpt-5-nano',)
    assert 'not on the page' in format_probe_result(result, _pricing(), width=100)


def test_an_unreadable_page_claims_no_price_at_all(monkeypatch):
    """`fetch_page` returning None must not read as "nothing changed" — that is how a broken guard
    stays invisible for a month."""
    provider = _Provider([])
    monkeypatch.setattr(price_probe, 'fetch_page', lambda *args, **kwargs: None)

    result = probe_prices(_pricing(), provider, source_url='https://x.test/pricing',
                          probe_model='gpt-4o-mini')

    assert not result.readable and result.prices == () and result.drifts == ()
    assert 'could not read' in drift_notice(result)
    assert provider.prompts == []                    # and no paid call was made


def test_the_prompt_names_the_models_and_carries_the_page():
    """Grounded, not recalled: the page text is IN the prompt, and only our priced models are asked
    about — a model we do not price is not our business."""
    provider = _Provider([_entry('gpt-4o-mini', 0.15, 0.60), _entry('gpt-5-nano', 0.05, 0.40)])

    probe_prices(_pricing(), provider, source_url='https://x.test/pricing',
                 probe_model='gpt-4o-mini', page=_PAGE)

    prompt = provider.prompts[0]
    assert _PAGE in prompt and '- gpt-4o-mini' in prompt and '- gpt-5-nano' in prompt
    assert 'never use knowledge from outside this text' in prompt


def test_the_run_reports_what_it_cost_from_the_table_it_checks():
    """Provenance for the printed line; the durable figure is the cost_log row the provider writes."""
    provider = _Provider([_entry('gpt-4o-mini', 0.15, 0.60), _entry('gpt-5-nano', 0.05, 0.40)],
                         usage=LlmUsage(prompt_tokens=10000, completion_tokens=200))

    result = probe_prices(_pricing(), provider, source_url='https://x.test/pricing',
                          probe_model='gpt-4o-mini', page=_PAGE)

    assert result.usd == pytest.approx(10000 / 1000 * 0.00015 + 200 / 1000 * 0.0006)


def test_the_status_vocabulary_is_declared():
    assert PROBE_STATUSES == ('ok', 'unreadable', 'absent')


def test_the_cached_column_is_read_and_then_ignored():
    """The trap the first live run fell into: gpt-4o is 2.50 input, 1.25 cached, 10.00 output, and
    an extraction without a slot for the middle number reported 1.25 as the output price.

    The schema now asks for all three; only two are compared. A probe that silently priced output at
    the cached rate would have looked like an 87 % price cut — a drift an operator might well have
    applied.
    """
    provider = _Provider([_entry('gpt-4o-mini', 0.15, 0.60, cached=0.075),
                          _entry('gpt-5-nano', 0.05, 0.40, cached=0.005)])

    result = probe_prices(_pricing(), provider, source_url='https://x.test/pricing',
                          probe_model='gpt-4o-mini', page=_PAGE)

    assert result.clean
    assert [(p.model, p.input_per_1k, p.output_per_1k) for p in result.prices] == [
        ('gpt-4o-mini', 0.00015, 0.0006), ('gpt-5-nano', 0.00005, 0.0004)]


def test_the_prices_survive_being_inside_an_attribute():
    """The page's numbers are not in its rendered text: they sit in an escaped data payload INSIDE
    an attribute, so anything that strips tags the way a reader would deletes exactly them.

    Measured against the real page on 2026-09-15 — a version that stripped tags produced a document
    naming none of our models, and the probe honestly reported "not on the page".
    """
    page = ('<main><p>Pricing per 1M tokens.</p>'
            '<pricing-table data-rows="[[0,&quot;gpt-5-nano&quot;],[0,0.05],[0,0.005],[0,0.4]]">'
            '</pricing-table></main>')

    context = condense(page, ['gpt-5-nano'])

    assert 'gpt-5-nano' in context and '0.05' in context and '0.4' in context


def test_a_page_naming_none_of_our_models_condenses_to_nothing():
    """Which the caller turns into `unreadable` — never into "every model is absent"."""
    assert condense('<p>welcome to our documentation</p>', ['gpt-5-nano']) == ''


def test_one_model_id_cannot_eat_the_whole_window_budget():
    """`gpt-4o` is a substring of half the catalogue, so a shared budget is one model eating it —
    measured against the real page, where it left nothing for the embeddings."""
    page = ' '.join(f'"gpt-4o-variant-{n}",0.1,0.01,0.2' for n in range(40)) + ' "gpt-5-nano",0.05,0.005,0.4'

    context = condense(page, ['gpt-4o', 'gpt-5-nano'])

    assert 'gpt-5-nano' in context
