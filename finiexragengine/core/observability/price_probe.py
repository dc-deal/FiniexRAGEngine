"""Pricing probe (ISSUE_67) — read the vendor's published page, compare, notify, write nothing.

Every USD figure this engine reports is derived from a hand-maintained table (`pricing.models`), and
the vendor publishes no pricing API. So a stale price skews the cost warehouse silently, and the only
thing that has ever caught it is somebody remembering to look.

**The number is grounded, not recalled.** The probe fetches the vendor's price page and has a model
extract the table *from that page*. Asking a model what prices are would be asking for exactly the
figure models hallucinate — training-cutoff stale, confidently wrong. Reading a fetched page fails
the other way: a layout change yields "could not read", never a plausible wrong price.

**And it never writes.** A wrong price applied automatically corrupts every cost figure downstream,
invisibly and in money; a missed notification costs one week of staleness. The asymmetry decides the
design: this unit returns findings, `price_cli --apply` is a human confirming them.
"""
import html as html_module
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from finiexragengine.core.llm.abstract_llm_provider import AbstractLLMProvider
from finiexragengine.types.config_types.app_config_types import PricingConfig
from finiexragengine.types.pricing_types import PriceDrift, ProbedPrice, ProbeResult

logger = logging.getLogger(__name__)

# The page is prose around a table; asking for the models we actually price keeps the answer short
# and the comparison honest — a model we do not price is not our business, and one we price that the
# page omits is a coverage gap the caller reports rather than a drift.
# The bare schema: the provider names and wraps it (`response_format.json_schema`), so handing it a
# pre-wrapped one is how the first live run failed.
_SCHEMA: Dict[str, Any] = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['prices'],
    'properties': {
        'prices': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['model', 'input_per_1m_usd', 'cached_input_per_1m_usd',
                             'output_per_1m_usd', 'found'],
                'properties': {
                    'model': {'type': 'string'},
                    # Per MILLION on the wire, because that is the unit the page prints — the
                    # conversion to the config's per-1K happens here, once, instead of asking a
                    # model to do arithmetic it has no reason to get right.
                    'input_per_1m_usd': {'type': 'number'},
                    # Asked for and then ignored, deliberately. The vendor's rows are
                    # (input, cached input, output) and the first live run mapped the CACHED value
                    # onto output — 1.25 instead of 10.00 for gpt-4o. Giving every number its own
                    # slot leaves the middle one nowhere else to go.
                    'cached_input_per_1m_usd': {'type': 'number'},
                    'output_per_1m_usd': {'type': 'number'},
                    'found': {'type': 'boolean'},
                },
            },
        },
    },
}

_PROMPT = (
    'Below are excerpts from a model-pricing page of an LLM vendor.\n\n'
    'For each model id listed under MODELS, report the standard (non-batch, non-cached) price the '
    'page states, in USD per 1,000,000 tokens, for input and for output. An embedding model has no '
    'output price: report 0 for it.\n\n'
    'The excerpts are fragments separated by ---, and some are raw data rather than prose: there a '
    'model id is followed by its row in the table\'s own column order, which is INPUT, then CACHED '
    'input, then OUTPUT. Report all three per model — the cached figure is asked for so that each '
    'number has its own place and none is mistaken for another. A dash or null means the row has '
    'no such price: report 0 for it.\n\n'
    'Report `found: false` and zeros for a model the excerpts do not price. Never estimate, never '
    'carry a price over from another model, and never use knowledge from outside this text.\n\n'
    'MODELS:\n{models}\n\nEXCERPTS:\n{page}\n')

# A page far beyond this is a redirect, a login wall or a different document. Measured on
# 2026-09-15: the real page is ~581,000 characters of HTML, so the ceiling is a sanity bound and
# never a size the probe would actually send.
_MAX_PAGE_CHARS = 4_000_000

# What reaches the model. Measured 2026-09-15: the page is ~581,000 characters of HTML but only
# ~20,500 once the markup is gone — about 5,000 tokens, well under a cent to read. So the whole text
# is sent and the table keeps its shape; `condense` below is the fallback for the day the page grows
# past this, and it is grep-like on purpose, so a layout change moves lines rather than breaking a
# parser.
_MAX_CONTEXT_CHARS = 30_000
_WINDOW_CHARS = 300          # around each mention — enough to carry the row, not the table's cousins
_MAX_SPANS_PER_MODEL = 8     # per model, never global: `gpt-4o` is a substring of half the
                             # catalogue, so one shared budget is one model eating it


def fetch_page(url: str, timeout_seconds: float = 20.0,
               client: Optional[httpx.Client] = None) -> Optional[str]:
    """The vendor's page as text, or None when it cannot be read.

    No retries: this runs weekly and an unreachable page is a finding, not an emergency. None is
    returned rather than raised because "unreadable" is a state this guard must record and report —
    a probe that has been failing quietly for a month is the failure it exists to prevent.
    """
    try:
        owned = client is None
        http = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True)
        try:
            response = http.get(url, headers={'accept': 'text/html,text/plain'})
            response.raise_for_status()
            text = response.text
        finally:
            if owned:
                http.close()
    except (httpx.HTTPError, OSError) as exc:
        logger.warning('price page %s could not be read: %s', url, exc)
        return None
    if not text or len(text) > _MAX_PAGE_CHARS:
        logger.warning('price page %s is %d chars — not the document this probe expects',
                       url, len(text or ''))
        return None
    return text


def condense(page: str, models: Sequence[str]) -> str:
    """The neighbourhood of every mention of a model we price, taken from the raw page.

    Three things were learned the expensive way on 2026-09-15, and each is why a line of this
    function looks the way it does:

    - the rendered document does not contain the price table at all — the numbers live in a data
      payload as `"gpt-5-nano",0.05,0.005,0.4`, HTML-escaped;
    - that payload sits **inside an attribute**, so stripping tags the way a reader would deletes
      exactly the part that carries the prices (a first version did, and the probe honestly reported
      "not on the page" — a true answer about the wrong half of the file);
    - the payload is one enormous line, so a line-based cut returns either nothing or everything.

    Hence: unescape, then take ± `_WINDOW_CHARS` around every mention of a model we price. That
    keeps each id together with the numbers beside it and drops the other 99 % of the document,
    without a parser that a layout change could break.

    Returns '' when no model id appears at all — a page this probe does not understand, which the
    caller reports as unreadable rather than as "everything is absent".
    """
    text = html_module.unescape(page)
    lowered = text.lower()
    spans: List[Tuple[int, int]] = []
    for model in models:
        needle = model.lower()
        start = lowered.find(needle)
        found = 0
        while start != -1 and found < _MAX_SPANS_PER_MODEL:
            spans.append((max(start - _WINDOW_CHARS, 0),
                          min(start + len(needle) + _WINDOW_CHARS, len(text))))
            found += 1
            start = lowered.find(needle, start + len(needle))
    if not spans:
        return ''
    # Merge overlapping windows so one dense table is one fragment rather than fifty copies.
    merged: List[Tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    condensed = '\n---\n'.join(text[start:end] for start, end in merged)
    return condensed[:_MAX_CONTEXT_CHARS]


def probe_prices(pricing: PricingConfig, provider: AbstractLLMProvider, *, source_url: str,
                 probe_model: str, epsilon_pct: float = 1.0,
                 page: Optional[str] = None) -> ProbeResult:
    """Compare the page against the running table. Returns findings; writes nothing, ever.

    `page` is injectable so the comparison can be tested without a network and without a paid call —
    the no-write property below is the one this whole design rests on, and it has to be assertable.
    """
    result = ProbeResult(source_url=source_url, probe_model=probe_model)
    text = page if page is not None else fetch_page(source_url)
    if text is None:
        result.readable = False
        return result

    models = sorted(pricing.models)
    # Strip the markup, then check the text is the document we think it is BEFORE paying for a
    # call: a page naming none of the models we price is our own blindness, and reporting it as
    # "every model is absent" would dress that up as a vendor decision.
    context = condense(text, models)
    if not context:
        logger.warning('price page %s mentions none of the %d priced models — not the document '
                       'this probe expects', source_url, len(models))
        result.readable = False
        return result
    completion = provider.complete_structured(
        _PROMPT.format(models='\n'.join(f'- {model}' for model in models),
                     page=context), _SCHEMA)
    result.usd = _usd_of(pricing, probe_model, completion)
    prices, missing = _read(completion.data, models)
    result.prices = tuple(prices)
    result.missing = tuple(missing)
    result.drifts = tuple(_drifts(pricing, prices, epsilon_pct))
    return result


def _usd_of(pricing: PricingConfig, probe_model: str, completion: Any) -> float:
    """What this probe cost, derived from the same table it is checking.

    Circular only in appearance: the durable figure is the `cost_log` row the provider's recorder
    writes, and this is the at-the-call echo for the printed line. A probe model the table does not
    price reports 0.0 rather than guessing — and that omission is itself a finding the run prints.
    """
    price = pricing.models.get(probe_model)
    usage = getattr(completion, 'usage', None)
    if price is None or usage is None:
        return 0.0
    return (usage.prompt_tokens / 1000.0 * price.input_per_1k
            + usage.completion_tokens / 1000.0 * price.output_per_1k)


def _read(data: Dict[str, Any], models: List[str]) -> Tuple[List[ProbedPrice], List[str]]:
    """The extraction, converted to the config's unit and split into found and absent."""
    by_model = {entry.get('model'): entry for entry in (data.get('prices') or [])
                if isinstance(entry, dict)}
    prices: List[ProbedPrice] = []
    missing: List[str] = []
    for model in models:
        entry = by_model.get(model)
        if not entry or not entry.get('found'):
            missing.append(model)
            prices.append(ProbedPrice(model=model, status='absent'))
            continue
        prices.append(ProbedPrice(
            model=model,
            input_per_1k=float(entry.get('input_per_1m_usd') or 0.0) / 1000.0,
            output_per_1k=float(entry.get('output_per_1m_usd') or 0.0) / 1000.0))
    return prices, missing


def _drifts(pricing: PricingConfig, prices: List[ProbedPrice],
            epsilon_pct: float) -> List[PriceDrift]:
    """One finding per leaf that moved beyond the epsilon — never a whole model at once.

    Per leaf because that is how a vendor changes prices and how the operator applies them: an
    output price that moved while the input did not is one number to review, not two.
    """
    findings: List[PriceDrift] = []
    for probed in prices:
        if probed.status != 'ok':
            continue                              # a gap in coverage is not a disagreement
        current = pricing.models.get(probed.model)
        if current is None:
            continue                              # the page knows a model we do not price
        for field, table_value, probed_value in (
                ('input_per_1k', current.input_per_1k, probed.input_per_1k),
                ('output_per_1k', current.output_per_1k, probed.output_per_1k)):
            if probed_value is None:
                continue
            drift = PriceDrift(model=probed.model, field=field, table_value=table_value,
                               probed_value=probed_value)
            # The epsilon is on the percentage where one exists, and on the raw values where the
            # table says zero — otherwise a model priced at 0.0 could never report a drift, which
            # is the one case where a drift matters most.
            moved = abs(drift.pct) > epsilon_pct if table_value else probed_value != table_value
            if moved:
                findings.append(drift)
    return findings


# --- rendering ------------------------------------------------------------------------------

def _price(value: Optional[float]) -> str:
    return '—' if value is None else f'{value:.6g}'


def format_probe_result(result: ProbeResult, pricing: PricingConfig,
                        epsilon_pct: float = 1.0, width: int = 100) -> str:
    """The shared console pattern: title + source line + `----` dividers + aligned columns."""
    divider = '-' * max(width - 1, 70)
    lines = [f'price probe · {result.source_url} · read by {result.probe_model} · '
             f'${result.usd:.4f}', divider]
    if not result.readable:
        lines.append('the page could not be read — NO price was claimed for any model, and the '
                     'table is untouched. A probe failing quietly is what this guard exists to '
                     'prevent, so this line is the finding.')
        return '\n'.join(lines)

    by_model = {drift.model: [] for drift in result.drifts}
    for drift in result.drifts:
        by_model[drift.model].append(drift)
    lines.append(f'{"model":<26} {"table in/out per 1K":<26} {"probed in/out":<24} drift')
    for probed in result.prices:
        current = pricing.models.get(probed.model)
        table = (f'{_price(current.input_per_1k)} / {_price(current.output_per_1k)}'
                 if current else '—')
        if probed.status != 'ok':
            found = 'not on the page'
        else:
            found = f'{_price(probed.input_per_1k)} / {_price(probed.output_per_1k)}'
        drift = ' · '.join(f'{item.field.split("_")[0]} {item.pct:+.1f} %'
                           for item in by_model.get(probed.model, ())) or '—'
        lines.append(f'{probed.model[:26]:<26} {table:<26} {found:<24} {drift}')
    lines.append(divider)
    lines.append(f'{len(result.drifts)} drift(s) beyond ±{epsilon_pct:.1f} % · nothing was '
                 f'written · apply with `price_cli --apply`')
    if result.missing:
        lines.append(f'not on the page, so not comparable: {", ".join(result.missing)}')
    return '\n'.join(lines)


def drift_notice(result: ProbeResult) -> str:
    """One Telegram line. Sent on drift AND on an unreadable page — a guard that has quietly
    stopped working is exactly the state nobody notices on their own."""
    if not result.readable:
        return (f'⚠️ price probe could not read {result.source_url} — no prices checked this week, '
                f'the table is untouched')
    parts = [f'{drift.model} {drift.field.split("_")[0]} {drift.table_value:.6g} → '
             f'{drift.probed_value:.6g} ({drift.pct:+.1f} %)' for drift in result.drifts]
    return ('⚠️ price drift: ' + ' · '.join(parts)
            + ' · table unchanged · apply with `price_cli --apply`')
