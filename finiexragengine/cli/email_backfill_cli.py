"""CLI entry point: mails received before the mailbox existed, from a folder of `.eml` files (#138).

    python -m finiexragengine.cli.email_backfill_cli --source <source_id> --dir <folder>
        --authserv-id <provider> [--dry-run]

Writes raw mails only; the running e-mail source extracts them at `extract.max_mails_per_pass`.
`--authserv-id` names the provider that received the files (its own `Authentication-Results` header
is the only one read). Delete the folder afterwards: the files are subscriber content.
"""
import argparse
import os
from pathlib import Path

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.mail.email_backfill import backfill
from finiexragengine.core.mail.mail_store import MailStore
from finiexragengine.core.mail.sender_gate import SenderGate
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    use_utf8_output()
    parser = argparse.ArgumentParser(description='Backfill an e-mail source from .eml files (raw mails only)')
    parser.add_argument('--source', required=True, help='the e-mail source id (a key of mail.mailboxes)')
    parser.add_argument('--dir', required=True, type=Path, help='folder holding the .eml files')
    parser.add_argument('--authserv-id', required=True,
                        help="the receiving provider's authserv-id in the files' Authentication-Results")
    parser.add_argument('--dry-run', action='store_true', help='count and judge, write nothing')
    args = parser.parse_args()
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')
    mailbox = AppConfigManager().get_config().mail.mailboxes.get(args.source)
    if mailbox is None:
        parser.error(f'no mail.mailboxes.{args.source} block — the allowlist comes from there')
    if not args.dir.is_dir():
        parser.error(f'{args.dir} is not a folder')
    gate = SenderGate(mailbox.senders, args.authserv_id, mailbox.max_mail_bytes)
    census = backfill(args.dir, args.source, gate, MailStore(database_url), dry_run=args.dry_run)
    print(census.line(args.dry_run))
    for entry in census.unreadable:
        print(f'  unreadable: {entry}')


if __name__ == '__main__':
    main()
