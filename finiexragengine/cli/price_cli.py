"""CLI entry point: the pricing probe and its only applier (ISSUE_67).

    python -m finiexragengine.cli.price_cli --probe    # read the page, compare, write nothing
    python -m finiexragengine.cli.price_cli --apply    # write the confirmed leaves into the overlay

**The guard never writes `pricing`; this command does, and only after a human confirms.** A price is
an external fact the engine cannot verify for itself, and a plausible-but-wrong one corrupts every
USD figure downstream invisibly. A missed notice costs a week of staleness; a wrong auto-applied
price costs the warehouse. The asymmetry is why applying is a keystroke rather than a schedule.

`--apply` writes **only the leaves that differ**, into gitignored `user_configs/app_config.json`
(deep-merged at load, reported at the next boot as `[OVERRIDE]`), and stamps `pricing.checked` —
that date means "held against the vendor's published rates", which is exactly what confirming here
is. The candidate is validated against the config model *before* the file is touched: the overlay
sits outside the Pydantic gate, so an out-of-range number would otherwise land in it and only be
refused at the next load.
"""
import argparse
import json
import os
from pathlib import Path

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.configuration.price_overlay import apply_drifts
from finiexragengine.core.llm.provider_factory import build_provider
from finiexragengine.core.observability.cost_recorder import CostRecorder
from finiexragengine.core.observability.price_probe import (
    format_probe_result,
    probe_prices,
)
from finiexragengine.core.observability.price_probe_store import PriceProbeStore
from finiexragengine.types.pricing_types import ProbeResult
from finiexragengine.utils.console_encoding import use_utf8_output

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_OVERLAY = _PROJECT_ROOT / 'user_configs' / 'app_config.json'


def _run_probe(manager: AppConfigManager, database_url: str) -> ProbeResult:
    """One probe run: a paid call, recorded under `section='calibration'` like every other."""
    config = manager.get_config()
    probe_cfg = config.pricing.probe
    provider = build_provider(config.llm, probe_cfg.model,
                              cost_recorder=CostRecorder(database_url, config.pricing),
                              section='calibration')
    result = probe_prices(config.pricing, provider, source_url=probe_cfg.source_url,
                          probe_model=probe_cfg.model, epsilon_pct=probe_cfg.epsilon_pct)
    rows = PriceProbeStore(database_url).record(
        config.pricing, result.prices, source_url=probe_cfg.source_url,
        probe_model=probe_cfg.model, readable=result.readable)
    print(format_probe_result(result, config.pricing, epsilon_pct=probe_cfg.epsilon_pct))
    print(f'probes recorded: {rows} row(s) in price_probes')
    return result


def main() -> None:
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Pricing probe (ISSUE_67): read the vendor page, compare, never auto-apply')
    parser.add_argument('--probe', action='store_true',
                        help='run the probe and print the comparison; writes nothing')
    parser.add_argument('--apply', action='store_true',
                        help='run the probe, then write the drifting leaves into user_configs '
                             'after a confirmation')
    args = parser.parse_args()
    if not (args.probe or args.apply):
        parser.error('choose --probe (read only) or --apply (read, then write on confirmation)')

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (the probe is recorded, and the call is billed)')

    manager = AppConfigManager()
    result = _run_probe(manager, database_url)
    if not args.apply:
        return
    if not result.drifts:
        print('nothing to apply — the table matches the page')
        return
    print()
    for drift in result.drifts:
        print(f'  {drift.model} {drift.field}: {drift.table_value:.6g} → '
              f'{drift.probed_value:.6g} ({drift.pct:+.1f} %)')
    if input(f'\nwrite these {len(result.drifts)} leaf/leaves to '
             f'user_configs/app_config.json? [y/N] ').strip().lower() != 'y':
        print('nothing written')
        return
    written = apply_drifts(result.drifts, _OVERLAY)
    print(f'written: {json.dumps(written)} · pricing.checked stamped today · '
          f'the next boot reports it as [OVERRIDE]')


if __name__ == '__main__':
    main()
