"""CLI entry point: remove one mail, its extracts and its articles together (#138).

    python -m finiexragengine.cli.email_purge_cli --message-id '<id@host>' [--dry-run]

Manual only, for a mail that should not be in the archive (a sender let through by mistake, a
publisher's request). Nothing in the engine deletes mails on its own.
"""
import argparse
import os

from finiexragengine.core.mail.mail_store import MailStore
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    use_utf8_output()
    parser = argparse.ArgumentParser(description='Remove one mail with its extracts and articles')
    parser.add_argument('--message-id', required=True, help="the mail's Message-ID, angle brackets included")
    parser.add_argument('--dry-run', action='store_true', help='count what would go, remove nothing')
    args = parser.parse_args()
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')
    result = MailStore(database_url).purge(args.message_id, dry_run=args.dry_run)
    if not result.mails:
        print(f'no stored mail with Message-ID {args.message_id}')
        return
    print(f"{'would remove' if args.dry_run else 'removed'}: 1 mail · {result.extracts} extract(s) · "
          f'{result.articles} article(s)')


if __name__ == '__main__':
    main()
