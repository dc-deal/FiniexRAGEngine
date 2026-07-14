"""CLI entry point: feed doctor (ISSUE_11) — raw output + parse diagnosis for the feeds.

Touches the network (that is the diagnosis) but never the LLM/embeddings — no spend. Resolves
feed URLs from the source-set configs; `--source <id>` narrows to one, otherwise all are probed.
"""
import argparse

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.configuration.source_set_registry import SourceSetRegistry
from finiexragengine.core.sources.feed_doctor import diagnose_feed, format_diagnoses


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Feed doctor: pull each feed\'s raw output and diagnose parse/HTTP failures')
    parser.add_argument('--source', help='diagnose only this source_id (default: all)')
    args = parser.parse_args()

    manager = AppConfigManager()
    registry = SourceSetRegistry(manager.get_source_sets_dir())
    registry.load()
    # (source_id, url) across every set — de-duplicated on source_id.
    feeds = {source.source_id: source.url
             for source_set in registry.list_sets()
             for source in source_set.sources
             if source.type == 'rss'}
    if args.source:
        if args.source not in feeds:
            parser.error(f'unknown source_id {args.source!r} — known: {sorted(feeds)}')
        feeds = {args.source: feeds[args.source]}

    diagnoses = [diagnose_feed(source_id, url) for source_id, url in sorted(feeds.items())]
    print(format_diagnoses(diagnoses))


if __name__ == '__main__':
    main()
