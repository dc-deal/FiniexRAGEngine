"""CLI entry point: signal packages — check the setup, or pack by hand (free; no API cost).

    python -m finiexragengine.cli.package_cli check
    python -m finiexragengine.cli.package_cli pack --source all [--cutoff YYYY-MM-DD]
    python -m finiexragengine.cli.package_cli pack --source <pipeline_id> --cutoff 2026-11-17

`pack` is also what the engine starts as a child process for a scheduled run (with `--json`, which
adds one machine-readable line on stdout for it). By hand it is the first package of a new
installation and the step the go-live order calls "a hand pack". It lowers its own priority first:
a pass of the signal pipelines is never queued behind it.

The sources are the pipelines this installation runs, private ones included — each its own chain of
packages. Without `--cutoff` a run packs to today's 00:00 UTC.

Exit codes: 0 when every source packed or had nothing new; 1 when a source failed or was refused;
2 when the configuration refuses (no DATABASE_URL, packages not enabled, an unknown source).
"""
import argparse
import json
import os
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NoReturn

import psutil

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.build_info import sample_build_info
from finiexragengine.core.outcome.outcome_exporter import OutcomeArchiveExporter
from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.packages.day_packer import PackContext, default_cutoff, pack_source
from finiexragengine.core.packages.package_ledger import PackageLedger
from finiexragengine.core.packages.package_store import PackageStore
from finiexragengine.types.package_types import PACKER_PROGRAM, SourceRun
from finiexragengine.utils.console_encoding import use_utf8_output

EXIT_CONFIGURATION = 2


def lower_priority() -> str:
    """Below-normal CPU priority for this process — what a package run must never compete with is a
    pass of the signal pipelines. Returns what was set, for the census line."""
    process = psutil.Process()
    try:
        if os.name == 'nt':
            process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            return 'below normal'
        process.nice(10)
        return 'nice 10'
    except (psutil.AccessDenied, OSError) as exc:
        return f'unchanged ({exc.__class__.__name__})'


def main() -> None:
    use_utf8_output()
    parser = argparse.ArgumentParser(description='Signal packages: check the setup, or pack by hand')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('check', help='the mint, the folder, the disk, the sources and their last runs')
    pack = commands.add_parser('pack', help='pack closed days to a cutoff (default: today 00:00 UTC)')
    pack.add_argument('--source', required=True, help="a pipeline id, or 'all'")
    pack.add_argument('--cutoff', default=None, help='a UTC day YYYY-MM-DD; days before it are packed')
    pack.add_argument('--json', action='store_true',
                      help='also print one JSON line on stdout (what the engine reads); the census goes to stderr')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        _refuse('DATABASE_URL is not set (point it at the pgvector Postgres)')
    manager = AppConfigManager()
    config = manager.get_config().packages
    sources = sorted(pipeline.get_config().pipeline_id
                     for pipeline in manager.build_pipeline_registry().list_pipelines())
    root = Path(config.output_dir)
    store, ledger = PackageStore(root), PackageLedger(root)

    if args.command == 'check':
        _check(database_url, config.min_free_percent, root, store, sources, config.enabled)
        return

    if not config.enabled:
        _refuse('packages.enabled is false — this installation makes no packages')
    selected = sources if args.source == 'all' else [args.source]
    unknown = [name for name in selected if name not in sources]
    if unknown:
        _refuse(f'no pipeline named {", ".join(unknown)} (known: {", ".join(sources)})')
    now = datetime.now(timezone.utc)
    cutoff = default_cutoff(now)
    if args.cutoff:
        try:
            day = date.fromisoformat(args.cutoff)
        except ValueError:
            _refuse(f'--cutoff {args.cutoff!r} is not a day YYYY-MM-DD')
        cutoff = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)

    priority = lower_priority()
    build = sample_build_info(manager.get_config().version)
    context = PackContext(store=store, ledger=ledger, exporter=OutcomeArchiveExporter(database_url),
                          database_url=database_url, mint=OutcomeStore(database_url).instance_identity(),
                          packer={'program': PACKER_PROGRAM, 'version': build.version,
                                  'commit': build.commit, 'dirty': build.dirty},
                          min_free_percent=config.min_free_percent)
    out = sys.stderr if args.json else sys.stdout
    print(f'[PACK] cutoff {cutoff:%Y-%m-%d} 00:00 UTC · {len(selected)} source(s) · priority {priority} '
          f'· {root}', file=out)
    started = datetime.now(timezone.utc)
    runs: List[SourceRun] = [pack_source(name, context, cutoff) for name in selected]
    width = max(len(name) for name in selected)
    for run in runs:
        print(_census_line(run, width), file=out)
    counts = {result: sum(1 for run in runs if run.result == result)
              for result in ('packed', 'nothing_new', 'refused', 'failed')}
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"{len(runs)} source(s) · {counts['packed']} packed · {counts['nothing_new']} nothing new · "
          f"{counts['refused']} refused · {counts['failed']} failed · {elapsed:.0f} s", file=out)
    if args.json:
        print(json.dumps({'runs': [run.to_record() for run in runs]}))
    if counts['failed'] or counts['refused']:
        sys.exit(1)


def _census_line(run: SourceRun, width: int) -> str:
    if run.result == 'packed':
        detail = (f'{run.package_id}  {run.member_count} days · {run.record_count:,} records · '
                  f'{run.zip_size_bytes / 1e6:.1f} MB · {run.duration_seconds:.0f} s')
    else:
        detail = run.reason or ''
    excluded = f' · {len(run.excluded)} excluded' if run.excluded else ''
    return f'[PACK] {run.source:<{width}}  {run.result:<11} {detail}{excluded}'


def _check(database_url: str, min_free_percent: float, root: Path, store: PackageStore,
           sources: List[str], enabled: bool) -> None:
    """Everything a run depends on, each with its value — read only, nothing written."""
    mint = OutcomeStore(database_url).instance_identity()
    probe_root = root if root.exists() else Path('.')
    usage = shutil.disk_usage(probe_root)
    free = usage.free / usage.total * 100 if usage.total else 0.0
    last: Dict[str, Any] = store.last_runs()
    print(f'enabled     {enabled}')
    print(f"mint        {mint or 'NONE — no package can be made (migration 017)'}")
    print(f'folder      {root.resolve()} ({"exists" if root.exists() else "created on the first run"})')
    print(f'disk        {free:.1f} % free · refuses below {min_free_percent:g} % (packages.min_free_percent)')
    print(f'sources     {len(sources)}')
    for name in sources:
        manifests = store.manifests(name)
        record = last.get(name)
        state = f"last run {record['result']} {record['at'][:16]}" if record else 'never run'
        print(f'  {name:<28} {len(manifests)} package(s) · {state}')


def _refuse(message: str) -> NoReturn:
    print(f'❌ {message}', file=sys.stderr)
    sys.exit(EXIT_CONFIGURATION)


if __name__ == '__main__':
    main()
