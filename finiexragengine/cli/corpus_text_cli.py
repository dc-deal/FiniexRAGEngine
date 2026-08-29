"""CLI entry point: corpus text (ISSUE_112) — the treatment behind the stored text, and its effect.

Its own command, like every other report (ISSUE_104). The question is specific: *what produced the
text the model reads, is any of it still carrying markup, how much did the treatment remove, and
which keyword hits only ever existed inside a tag.*

It exists because the answer used to live nowhere the engine could show it. The normaliser's echo
(`normalised N (M chars)`) is an at-the-call line on one pass, overwritten by the next; everything
durable was a SQL prompt on the production box, which is reachable by an operator with a shell and
by nobody else. A threshold whose effect nobody can observe is a threshold nobody can tune.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.corpus_text_report import (
    format_corpus_text_report,
)
from finiexragengine.core.observability.reports.report_catalog import (
    build_report,
    format_parameter_line,
    resolve,
)
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    # The report carries `✓`, `⚠`, `…`; a piped run would die on a cp1252 stdout.
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Corpus text: treatment census, surviving carriers, what the normaliser '
                    'removed, and the keyword hits that exist only inside markup')
    # No argparse defaults (ISSUE_104): omitted, `reports.corpus_text.window` applies. The window
    # narrows the FLOW half only — the census and the phantom table are corpus-wide, because a text
    # treatment is a property of a stored row rather than of a time slice.
    parser.add_argument('--since', default=None,
                        help='window for the flow half: 7d, 30d, or all; omitted, the configured '
                             'window applies')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    manager = AppConfigManager()
    resolved = resolve('corpus_text', manager.get_config().reports, {'window': args.since})
    print(format_parameter_line(resolved.applied))
    print(format_corpus_text_report(
        build_report('corpus_text', database_url, manager, resolved.params)))


if __name__ == '__main__':
    main()
