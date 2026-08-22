"""ISSUE_96 step 2 — calibrate `story_similarity` against the 2026-08-18 hand count.

Read-only, no API spend. Run on the server, paste the output back.

    python calibrate_stories.py

Sweeps the threshold over the window the hand count was taken from (2026-08-11 → 08-18, which
predates the 08-20 ingest outage) and reports, per analysis unit, how many stories each threshold
produces against the number counted by eye. Delete this file once the threshold is decided — it is
a calibration instrument, not a shipped surface.
"""
import os
from datetime import datetime, timedelta, timezone

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.breaking_report import build_breaking_report
from finiexragengine.core.pipeline.breaking_episode_rule import groupings_from_configs
from finiexragengine.core.pipeline.breaking_story_rule import (
    StoryCandidate,
    StoryGrouping,
    assign_stories,
)

# The hand count from ISSUE_82's follow-up notes: 29 episodes over the 7 days to 2026-08-18,
# read into ~17 stories. Per unit, this is what the measure has to reproduce.
HAND_COUNT = {'BTCUSD': 4, 'ETHUSD': 4, 'SOLUSD': 5, 'XRPUSD': 2, 'GBPUSD': 1, 'EURGBP': 1}

WINDOW_END = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
THRESHOLDS = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.60]


def main() -> None:
    database_url = os.environ['DATABASE_URL']
    registry = AppConfigManager().build_pipeline_registry()
    configs = [pipeline.get_config() for pipeline in registry.list_pipelines()]
    rules = groupings_from_configs(configs)

    since = WINDOW_END - timedelta(days=7)
    report = build_breaking_report(database_url, since, since_label='7d (hand-count window)',
                                  rules=rules)
    episodes = [episode for episode in report.episodes if episode.started <= WINDOW_END]
    print(f'window {since:%Y-%m-%d %H:%M} → {WINDOW_END:%Y-%m-%d %H:%M} UTC · '
          f'{len(episodes)} episodes (hand count: 29 → ~17 stories)\n')

    if not episodes:
        print('NO EPISODES IN THE WINDOW — the archive may not reach back this far.')
        return

    # Key on the symbol: the hand count was taken per symbol, and the report row carries it.
    candidates = [StoryCandidate(key=episode.symbol, started=episode.started,
                                 reason=episode.reason) for episode in episodes]

    units = sorted({episode.symbol for episode in episodes})
    header = f'{"threshold":>9} ' + ' '.join(f'{unit:>8}' for unit in units) + f' {"total":>7}'
    print(header)
    print(f'{"hand":>9} ' + ' '.join(f'{HAND_COUNT.get(u, "?")!s:>8}' for u in units)
          + f' {sum(HAND_COUNT.values()):>7}')
    print('-' * len(header))

    for threshold in THRESHOLDS:
        ids = assign_stories(candidates, StoryGrouping(similarity=threshold,
                                                       window=timedelta(hours=72)))
        per_unit = {}
        for candidate, story_id in zip(candidates, ids):
            per_unit.setdefault(candidate.key, set()).add(story_id)
        cells = ' '.join(f'{len(per_unit.get(unit, ())):>8}' for unit in units)
        print(f'{threshold:>9.2f} {cells} {len(set(ids)):>7}')

    print('\nepisodes per unit (what the grouping starts from):')
    counts = {}
    for episode in episodes:
        counts[episode.symbol] = counts.get(episode.symbol, 0) + 1
    for unit in units:
        print(f'  {unit:>8}  {counts[unit]:>2} episodes → hand count {HAND_COUNT.get(unit, "?")}')

    # The table says how many; this says WHICH — the only way to judge whether a merge the measure
    # made is right, or whether the hand count was. A threshold cannot be signed off from counts
    # alone: two units can be wrong in opposite directions and still total correctly.
    show = float(os.environ.get('SHOW_AT', '0') or 0)
    if not show:
        print('\n(set SHOW_AT=0.45 to print the groupings at that threshold, with their reasons)')
        return
    print(f'\n--- groupings at similarity {show:.2f} ---')
    ids = assign_stories(candidates, StoryGrouping(similarity=show, window=timedelta(hours=72)))
    grouped: dict = {}
    for episode, story_id in zip(episodes, ids):
        grouped.setdefault((episode.symbol, story_id), []).append(episode)
    for (symbol, story_id), members in sorted(grouped.items()):
        mark = '  MERGED' if len(members) > 1 else ''
        print(f'\n{symbol} story {story_id} · {len(members)} episode(s){mark}')
        for episode in members:
            print(f'    {episode.started:%m-%d %H:%M}  {episode.signal:4} {episode.reason[:150]}')


if __name__ == '__main__':
    main()
