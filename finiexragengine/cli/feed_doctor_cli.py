"""CLI entry point: feed doctor (ISSUE_11) — raw output + parse diagnosis for the feeds.

Touches the network (that is the diagnosis) but never the LLM/embeddings — no spend. Resolves
feed URLs from the source-set configs; `--source <id>` narrows to one, otherwise all are probed.

Probes concurrently (ISSUE_107): the catalogue went from 14 feeds to 39, each costing two requests,
and a walled host burns its full timeout on both — sequentially that is minutes of apparent hang.
A probe touches only the network, so pooling it is safe by construction; the render is sorted by
source id, never by completion order, so the output is identical whatever the pool does.
"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from typing import Tuple

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.sources.feed_doctor import (
    FeedDiagnosis,
    diagnose_feed,
    format_diagnoses,
)
from finiexragengine.types.config_types.source_set_types import SourceConfig
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    # Reports carry `→`, `⚠`, `—`; a piped run would die on a cp1252 stdout.
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Feed doctor: pull each feed\'s raw output and diagnose parse/HTTP failures')
    parser.add_argument('--source', help='diagnose only this source_id (default: all)')
    parser.add_argument('--workers', type=int, default=8,
                        help='how many feeds to probe at once (default 8; 1 = one after another)')
    parser.add_argument('--quiet', action='store_true',
                        help='suppress the per-feed progress lines (they go to stderr)')
    args = parser.parse_args()

    manager = AppConfigManager()
    registry = manager.build_source_set_registry()
    # Every rss source across every set — de-duplicated on source_id. Disabled feeds are kept
    # deliberately: the doctor is how the operator checks whether a switched-off feed is
    # reachable again (it is marked `[disabled]` in the report, never silently skipped).
    feeds = {source.source_id: source
             for source_set in registry.list_sets()
             for source in source_set.sources
             if source.type == 'rss'}
    if args.source:
        if args.source not in feeds:
            parser.error(f'unknown source_id {args.source!r} — known: {sorted(feeds)}')
        feeds = {args.source: feeds[args.source]}

    ordered = sorted(feeds.items())
    workers = max(1, min(args.workers, len(ordered)))
    done = 0

    def probe(item: Tuple[str, SourceConfig]) -> FeedDiagnosis:
        source_id, source = item
        # The feed's own staleness expectation rides along where it declares one (ISSUE_107) — the
        # verdict has to be judged against the number the config actually holds, not a global guess.
        diagnosis = diagnose_feed(source_id, source.url, disabled=not source.enabled,
                                  expected_max_age_hours=source.expected_max_age_hours)
        # Progress goes to stderr, so a piped run still gets a clean table on stdout. It is the
        # difference between "this is working through 39 feeds" and "this has hung".
        if not args.quiet:
            nonlocal done
            done += 1
            print(f'  [{done:>2}/{len(ordered)}] {source_id:18.18} {diagnosis.verdict}',
                  file=sys.stderr, flush=True)
        return diagnosis

    if not args.quiet:
        print(f'probing {len(ordered)} feed(s), {workers} at a time '
              f'(2 requests each — raw GET + feedparser)…', file=sys.stderr, flush=True)
    started = perf_counter()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='doctor') as pool:
        diagnoses = list(pool.map(probe, ordered))
    elapsed = perf_counter() - started
    # Sorted by source id, not by completion — the pool must not be able to change the output.
    diagnoses.sort(key=lambda diagnosis: diagnosis.source_id)
    print(format_diagnoses(diagnoses, elapsed_seconds=elapsed, workers=workers))


if __name__ == '__main__':
    main()
