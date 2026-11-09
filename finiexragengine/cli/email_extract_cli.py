"""CLI entry point: one extract batch for an e-mail source, outside the service (#138 addendum). 💸

    python -m finiexragengine.cli.email_extract_cli --source <source_id> --limit 10 [--dry-run]

The oldest pending mails first. Refused while the source's `extract.mode` is `live`: the service would
be extracting the same backlog, and a mail would be paid for twice. The service delivers the stories on
its next pass. Spend is recorded under `mail_extract`, like the service's own extracts.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.mail.extract_batch import run_batch
from finiexragengine.core.mail.mail_store import MailStore
from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    use_utf8_output()
    parser = argparse.ArgumentParser(description='Extract one batch of stored mails, oldest first (paid)')
    parser.add_argument('--source', required=True, help='the e-mail source id (a key of mail.mailboxes)')
    parser.add_argument('--limit', type=int, required=True, help='how many mails this batch takes')
    parser.add_argument('--dry-run', action='store_true', help='show the batch, extract nothing')
    args = parser.parse_args()
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')
    if args.limit < 1:
        parser.error('--limit must be at least 1')
    manager = AppConfigManager()
    mailbox = manager.get_config().mail.mailboxes.get(args.source)
    if mailbox is None:
        parser.error(f'no mail.mailboxes.{args.source} block')
    if mailbox.extract.mode != 'manual':
        parser.error(f"mail.mailboxes.{args.source}.extract.mode is '{mailbox.extract.mode}' — the service "
                     "extracts this backlog itself; set it to 'manual' for a separate run")
    assembler = PipelineAssembler(manager, database_url)
    recorder = assembler.get_cost_recorder()
    extractor = assembler.build_mail_extractor(args.source)
    with recorder.pass_scope('manual'):
        census = run_batch(MailStore(database_url), extractor, args.source, args.limit, dry_run=args.dry_run)
    for line in census.lines(args.dry_run):
        print(line)


if __name__ == '__main__':
    main()
