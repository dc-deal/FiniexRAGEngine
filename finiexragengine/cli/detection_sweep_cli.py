"""CLI entry point: detection sweep (ISSUE_106) — what would each detector have flagged?

Read-only replay over the stored corpus: no LLM, no embedding calls, no writes. Answers the question
the live cluster path cannot — whether a different measure or a looser similarity would detect
corroboration, or merely admit one feed's daily template first.

Not on the report catalog, deliberately: it is a self-join over embeddings, far heavier than every
catalogued read. `source_contribution_cli` is the standing precedent for "its own command until it
earns an HTTP entry".
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.detection_sweep_report import (
    DEFAULT_SIMILARITIES,
    build_detection_sweep_report,
    format_detection_sweep_report,
)
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.utils.console_encoding import use_utf8_output
from finiexragengine.utils.report_window import parse_since


def main() -> None:
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Detection sweep: what each candidate detector would have flagged, replayed '
                    'from the corpus')
    parser.add_argument('source_set_id', nargs='?', default='',
                        help='the set to sweep; omitted, every configured set')
    parser.add_argument('--since', default='7d', help='window: 7d, 30d, or all')
    parser.add_argument('--sample', type=int, default=400,
                        help='how many recent articles to score as seeds (default 400)')
    parser.add_argument('--similarity', type=float, action='append', dest='similarities',
                        help='override the grid; repeatable (default: '
                             + ', '.join(f'{s:.2f}' for s in DEFAULT_SIMILARITIES) + ')')
    # ISSUE_112 stamps the text treatment that produced the stored text. Two articles sharing a
    # feed's HTML boilerplate are similar because of the markup, so a grid read off a mixed sample
    # measures the wrong thing — this is how you compare like with like.
    parser.add_argument('--normalizer', default=None,
                        help="restrict the sample to one text treatment: 'v1' for normalised rows, "
                             "'' for the raw pre-ISSUE_112 ones; omitted, whatever is there (the "
                             'report then says the mix out loud)')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    manager = AppConfigManager()
    sets = manager.build_source_set_registry()
    since, since_label = parse_since(args.since)
    wanted = [args.source_set_id] if args.source_set_id else [
        source_set.source_set_id for source_set in sets.list_sets()]
    similarities = tuple(sorted(args.similarities, reverse=True)) if args.similarities \
        else DEFAULT_SIMILARITIES

    for index, source_set_id in enumerate(wanted):
        try:
            source_set = sets.get(source_set_id)
        except ConfigurationError as exc:
            parser.error(str(exc))          # an unknown id is a usage error, not a crash
        detection = source_set.detection
        report = build_detection_sweep_report(
            database_url, since, source_set_id=source_set_id,
            # The ACTIVE feeds, not the declared catalogue: a parked candidate contributes no
            # articles, so counting it would flatter the neighbourhood it never joined.
            source_ids={source.source_id for source in source_set.active_sources()},
            window_minutes=detection.cluster_window_minutes,
            mid_cluster_size=detection.mid_cluster_size,
            high_cluster_size=detection.high_cluster_size,
            live_similarity=detection.cluster_similarity,
            since_label=since_label, sample=args.sample, similarities=similarities,
            normalizer=args.normalizer)
        if index:
            print()
        print(format_detection_sweep_report(report))


if __name__ == '__main__':
    main()
