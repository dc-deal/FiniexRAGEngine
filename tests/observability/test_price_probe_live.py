"""Live (paid) test — the pricing probe against the vendor's real page (ISSUE_67).

Fenced behind the `paid` marker (excluded from default runs). Run deliberately:

    pytest -m paid tests/observability/test_price_probe_live.py -v

Needs OPENAI_API_KEY and reaches the network twice: the page fetch and one cheap extraction call —
fractions of a cent. What it proves is the half no synthetic test can: that the page is still
fetchable, still small enough, and still yields the prices the table is compared against. If the
vendor rewrites the page, this is the test that says so — and it asserts a *shape*, never a price,
because a price that changes is the finding, not a failure.
"""
import os

import pytest

pytest.importorskip('openai')

from finiexragengine.configuration.app_config_manager import AppConfigManager  # noqa: E402
from finiexragengine.core.llm.provider_factory import build_provider  # noqa: E402
from finiexragengine.core.observability.price_probe import fetch_page, probe_prices  # noqa: E402

pytestmark = [
    pytest.mark.paid,
    pytest.mark.skipif(not os.environ.get('OPENAI_API_KEY'), reason='needs OPENAI_API_KEY'),
]


def test_the_vendor_page_is_still_readable_and_still_carries_our_models():
    config = AppConfigManager().get_config()
    probe_cfg = config.pricing.probe

    page = fetch_page(probe_cfg.source_url)
    assert page, 'the price page could not be fetched — the guard would report `unreadable`'

    result = probe_prices(config.pricing, build_provider(config.llm, probe_cfg.model),
                          source_url=probe_cfg.source_url, probe_model=probe_cfg.model,
                          epsilon_pct=probe_cfg.epsilon_pct, page=page)

    # Shape, never value: a drift here is exactly what the probe exists to report.
    assert result.readable
    assert len(result.prices) == len(config.pricing.models)
    assert not result.missing, f'the page no longer mentions: {", ".join(result.missing)}'
    assert all(price.input_per_1k and price.input_per_1k > 0
               for price in result.prices if price.model.startswith('gpt'))
