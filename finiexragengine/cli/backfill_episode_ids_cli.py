"""CLI entry point: backfill episode identity into the archived series (ISSUE_108).

A dry run by default. Writing needs `--apply`, and `--apply` refuses when the self-check found any
disagreement between the replay and the identities the engine already served — a one-shot rewrite of
an archive another project reads has no `--force`.

`--since`/`--until` take absolute dates rather than the shared `parse_since` window expressions,
which are all wall-clock-relative. A backfill covers a *named* historical range: `41d` would mean a
different range tomorrow, and a re-runnable job whose boundaries move is not idempotent.
"""
import argparse
import os
from datetime import datetime, timedelta, timezone

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.outcome.episode_backfill import (
    EpisodeBackfill,
    format_backfill_plan,
)
from finiexragengine.core.pipeline.breaking_episode_rule import groupings_from_configs
from finiexragengine.utils.console_encoding import use_utf8_output


def _day(value: str) -> datetime:
    """`2026-07-16` or a full ISO stamp -> an aware UTC datetime."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def main() -> None:
    # The report carries `→` and `—`; a piped run would die on a cp1252 stdout.
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Backfill breaking_episode_id/_start into archived envelopes. Dry run unless '
                    '--apply is given.')
    parser.add_argument('--since', required=True, type=_day,
                        help='range start, inclusive (e.g. 2026-07-16)')
    parser.add_argument('--until', required=True, type=_day,
                        help='range end, EXCLUSIVE. Take it past the ISSUE_65 deploy '
                             '(2026-08-24) even though nothing after it needs writing: rows that '
                             'already carry an identity are what the self-check compares against, '
                             'and a range ending before it reports carried=0 and validates nothing')
    parser.add_argument('--apply', action='store_true',
                        help='write both sinks; omitted, nothing is written')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')
    if args.until <= args.since:
        parser.error('--until must be after --since')

    manager = AppConfigManager()
    configs = [pipeline.get_config()
               for pipeline in manager.build_pipeline_registry().list_pipelines()]
    groupings = groupings_from_configs(configs)
    # The prologue replays state before the range so an episode that opened earlier keeps its own
    # identity instead of being re-minted from a clipped start. Same formula the boot seed uses
    # (`pipeline_assembler`), widest across the pipelines, because one read serves them all.
    prologue = max((max(2 * groupings[config.pipeline_id].rule.get_gap(),
                        timedelta(hours=config.breaking.episode_seed_hours))
                    for config in configs), default=timedelta(hours=72))

    backfill = EpisodeBackfill(database_url, groupings, prologue=prologue)
    plan = (backfill.apply(args.since, args.until) if args.apply
            else backfill.plan(args.since, args.until))
    print(format_backfill_plan(plan))


if __name__ == '__main__':
    main()
