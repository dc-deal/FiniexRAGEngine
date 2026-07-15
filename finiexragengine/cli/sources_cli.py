"""CLI entry point: source-health report (ISSUE_11) — feed reliability + the problem log.

Reads the health captured by the ingest workers and marks orphans against the currently
configured sources (a feed still in the store but removed from every source-set).
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.configuration.source_set_registry import SourceSetRegistry
from finiexragengine.core.observability.reports.source_health_report import (
    build_source_health_report,
    format_source_health_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Source-health report: feed reliability, flags/quarantine, recent problems')
    parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    # Currently-configured source ids across every set — anything in the store but not here is
    # an orphan (a removed feed) the report flags as safe-to-delete.
    manager = AppConfigManager()
    registry = SourceSetRegistry(manager.get_source_sets_dir())
    registry.load()
    configured_ids = {source.source_id
                      for source_set in registry.list_sets()
                      for source in source_set.sources}

    report = build_source_health_report(database_url, configured_ids)
    print(format_source_health_report(report))


if __name__ == '__main__':
    main()
