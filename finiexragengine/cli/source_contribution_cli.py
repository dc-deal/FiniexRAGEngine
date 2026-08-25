"""CLI entry point: per-feed contribution for one source-set (ISSUE_82 finding 9).

Its own command (ISSUE_104): health and latency describe the pipe, this describes what came through
it — articles produced against articles that actually reached a prompt, next to the weight the feed
was given by hand. *Is a feed worth its weight* is a different question from *is it delivering*, and
a flag that swaps one for the other hides that.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.source_contribution_report import (
    build_source_contribution_report,
    format_source_contribution_report,
)
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.utils.console_encoding import use_utf8_output
from finiexragengine.utils.report_window import parse_since


def main() -> None:
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='What each feed of a source-set contributed: produced vs actually cited')
    parser.add_argument('source_set_id', nargs='?', default='',
                        help='the set to read; omitted, every configured set')
    parser.add_argument('--since', default='7d', help='window: 7d, 30d, or all')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    # Not on the report catalog yet (ISSUE_104's first cut): it is resolved per source-set rather
    # than once per engine, and it has no HTTP entry. It moves onto the catalog when it gains one.
    manager = AppConfigManager()
    sets = manager.build_source_set_registry()
    pipelines = manager.build_pipeline_registry()
    since, since_label = parse_since(args.since)
    wanted = [args.source_set_id] if args.source_set_id else [
        source_set.source_set_id for source_set in sets.list_sets()]
    for index, source_set_id in enumerate(wanted):
        try:
            source_set = sets.get(source_set_id)
        except ConfigurationError as exc:
            parser.error(str(exc))    # an unknown id is a usage error, not a crash
        # Only pipelines reading this set can cite its articles; envelopes from another set would
        # count nothing and cost the walk.
        pipeline_ids = {pipeline.get_config().pipeline_id
                        for pipeline in pipelines.list_pipelines()
                        if pipeline.get_config().source_set == source_set_id}
        report = build_source_contribution_report(
            database_url, since, source_set_id=source_set_id, pipeline_ids=pipeline_ids,
            weights={source.source_id: source.weight for source in source_set.sources},
            disabled_ids={source.source_id for source in source_set.sources
                          if not source.enabled},
            since_label=since_label)
        if index:
            print()
        print(format_source_contribution_report(report))


if __name__ == '__main__':
    main()
