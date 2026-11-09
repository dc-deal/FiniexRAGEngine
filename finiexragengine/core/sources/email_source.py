"""The e-mail source (#138): a mailbox read over IMAP, raw mails first, stories second.

Each ingest pass of its (collect) set does three things, in this order and for this reason:

1. **Intake** — new mails are read from the folder (read-only), judged by the sender gate and stored
   as raw mails *before* any LLM sees them. A mailbox or database failure raises the typed
   `SourceFetchError` and the source walks the quarantine ladder like any feed.
2. **Extract** — at most `extract.max_mails_per_pass` stored mails without an extract at the current
   version are turned into stories. This step never raises: a provider outage leaves a backlog for
   the next pass and is recorded on the extract row, so intake keeps running (decision B, 2026-11-03).
3. **Deliver** — stories not yet stored become `Article`s for the ingestor, which normalises, embeds
   and stores them like any feed's articles. Delivery is confirmed at the start of the next pass;
   `reset_conditional_get` (a pass that fetched but did not store) makes them be handed out again.
"""
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from finiexragengine.core.mail.imap_mailbox import ImapMailbox
from finiexragengine.core.mail.mail_extractor import MailExtractor
from finiexragengine.core.mail.mail_message import parse_mail
from finiexragengine.core.mail.mail_store import MailStore, start_of_utc_day, start_of_utc_week
from finiexragengine.core.mail.sender_gate import SenderGate
from finiexragengine.core.mail.story_articles import story_articles
from finiexragengine.core.sources.abstract_source import AbstractSource
from finiexragengine.core.sources.article_normalizer import ArticleNormalizer
from finiexragengine.exceptions.ragengine_errors import (
    FiniexRagError,
    SourceFetchError,
    VectorStoreError,
)
from finiexragengine.types.article_types import Article
from finiexragengine.types.config_types.app_config_types import MailboxConfig
from finiexragengine.types.config_types.source_set_types import SourceConfig
from finiexragengine.types.mail_types import MailIntakeCensus, MailSourceStatus, ParsedMail

logger = logging.getLogger(__name__)


def parse_mailbox_url(url: str) -> Tuple[str, int, str]:
    """`imaps://host:993/INBOX` → (host, port, folder). Only implicit TLS is accepted."""
    parsed = urlparse(url)
    if parsed.scheme != 'imaps' or not parsed.hostname:
        raise ValueError(f"mail source url must be imaps://host[:port]/folder, got '{url}'")
    return parsed.hostname, parsed.port or 993, unquote(parsed.path.lstrip('/')) or 'INBOX'


class EmailSource(AbstractSource):
    """A mailbox as a source: intake → extract → deliver, one pass at a time."""

    def __init__(self, config: SourceConfig, mailbox_config: MailboxConfig, mailbox: ImapMailbox,
                 folder: str, store: MailStore, gate: SenderGate, extractor: MailExtractor,
                 normalizer: Optional[ArticleNormalizer] = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        super().__init__(config, normalizer)
        self._mailbox_config = mailbox_config
        self._mailbox = mailbox
        self._folder = folder
        self._store = store
        self._gate = gate
        self._extractor = extractor
        self._clock = clock
        # Extracts handed to the ingestor by the last call, confirmed at the start of the next one.
        self._handed_out: List[Tuple[str, int]] = []
        self._status = MailSourceStatus(source_id=config.source_id, state='waiting',
                                        account=mailbox_config.account,
                                        extract_mode=mailbox_config.extract.mode)
        self.last_census: Optional[MailIntakeCensus] = None

    def get_fetch_deadline_ms(self) -> Optional[float]:
        return self._mailbox_config.timeout_seconds * 1000

    def reset_conditional_get(self) -> None:
        """The pass that took the last stories did not store them: hand them out again."""
        self._handed_out = []

    def mail_status(self) -> MailSourceStatus:
        return self._status

    def _fetch_articles(self) -> List[Article]:
        now = self._clock()
        census = MailIntakeCensus()
        try:
            if self._handed_out:
                self._store.confirm_delivered(self.get_source_id(), self._handed_out)
                self._handed_out = []
            self._intake(census)
        except SourceFetchError as exc:
            self._failed(now, str(exc))
            raise
        except VectorStoreError as exc:
            self._failed(now, 'mail store unavailable')
            raise SourceFetchError(f'mail store unavailable: {exc}', error_type='HTTP_ERROR',
                                   status=503) from exc
        self._extract(census)
        articles = self._deliver(census, now)
        self.last_census = census
        self._succeeded(now, census)
        return articles

    # --- the three steps ------------------------------------------------------------------------

    def _intake(self, census: MailIntakeCensus) -> None:
        source_id = self.get_source_id()
        uidvalidity, last_uid = self._store.cursor(source_id, self._folder)
        current, fetched = self._mailbox.read_new(uidvalidity, last_uid,
                                                  self._mailbox_config.max_mail_bytes)
        for mail in fetched:
            raw = mail.raw if mail.raw is not None else (mail.header_only or b'')
            parsed = parse_mail(raw, fallback_id=f'<uid-{current}-{mail.uid}@{self._folder}>',
                                with_text=mail.raw is not None)
            if mail.raw is None:
                parsed = _with_size(parsed, mail.size_bytes)
            verdict = self._gate.check(parsed)
            if self._store.store_mail(source_id, self._folder, current, mail.uid, parsed, verdict):
                census.seen += 1
                if verdict.accepted:
                    census.accepted += 1
                else:
                    census.rejected[verdict.reason or 'unknown'] = \
                        census.rejected.get(verdict.reason or 'unknown', 0) + 1

    def _extract(self, census: MailIntakeCensus) -> None:
        """Never raises: a failure is written to the extract row and retried on a later pass.

        In `manual` mode it does nothing: extraction runs outside the service (`email_extract_cli`),
        and the stories that run writes are delivered by `_deliver` like any others.
        """
        if self._mailbox_config.extract.mode == 'manual':
            return
        source_id = self.get_source_id()
        try:
            pending = self._store.pending(source_id, self._extractor.version,
                                          self._mailbox_config.extract.max_mails_per_pass)
        except VectorStoreError as exc:
            logger.warning('[MAIL] %s · extract skipped this pass — %s', source_id, exc)
            return
        for mail in pending:
            try:
                outcome = self._extractor.extract(mail)
                self._store.record_extract(
                    mail.mail_id, self._extractor.version, model=outcome.served_model or self._extractor.model,
                    prompt_hash=self._extractor.prompt_hash, stories=outcome.stories,
                    dropped=outcome.dropped, prompt_tokens=outcome.prompt_tokens,
                    completion_tokens=outcome.completion_tokens, usd=outcome.usd,
                    duration_ms=outcome.duration_ms)
                census.extracted += 1
                census.usd += outcome.usd or 0.0
            except FiniexRagError as exc:
                census.extract_failed += 1
                reason = f'{type(exc).__name__}: {exc}'
                logger.warning('[MAIL] %s · extract failed for one mail, kept for the next pass — %s',
                               source_id, reason[:200])
                try:
                    self._store.record_failure(mail.mail_id, self._extractor.version,
                                               model=self._extractor.model,
                                               prompt_hash=self._extractor.prompt_hash, error=reason)
                except VectorStoreError:
                    pass

    def _deliver(self, census: MailIntakeCensus, now: datetime) -> List[Article]:
        source_id = self.get_source_id()
        try:
            extracts = self._store.undelivered(source_id, self._extractor.version)
        except VectorStoreError as exc:
            logger.warning('[MAIL] %s · delivery skipped this pass — %s', source_id, exc)
            return []
        articles: List[Article] = []
        for extract in extracts:
            articles.extend(story_articles(self._config, extract.message_id, extract.sent_at,
                                           extract.extract_version, extract.stories, now))
            self._handed_out.append((extract.mail_id, extract.extract_version))
        census.stories_delivered = len(articles)
        return articles

    # --- status ---------------------------------------------------------------------------------

    def _succeeded(self, now: datetime, census: MailIntakeCensus) -> None:
        source_id = self.get_source_id()
        try:
            today = self._store.window_counts(source_id, start_of_utc_day(now))
            week = self._store.window_counts(source_id, start_of_utc_week(now))
            backlog = self._store.backlog(source_id, self._extractor.version)
        except VectorStoreError:
            today, week, backlog = self._status.today, self._status.week, self._status.extract_backlog
        self._status = MailSourceStatus(
            source_id=source_id, state='ok', account=self._mailbox_config.account,
            last_poll_at=now, last_success_at=now, extract_mode=self._mailbox_config.extract.mode,
            today=today, week=week, extract_backlog=backlog)
        if census.seen or census.extracted or census.extract_failed or census.stories_delivered:
            rejected = sum(census.rejected.values())
            logger.info('[MAIL] %s · %d new · %d accepted · %d rejected%s · %d extracted ($%.4f)%s · '
                        '%d stories handed out · backlog %d', source_id, census.seen, census.accepted,
                        rejected, f" ({', '.join(f'{k} {v}' for k, v in sorted(census.rejected.items()))})"
                        if rejected else '', census.extracted, census.usd,
                        f' · {census.extract_failed} failed' if census.extract_failed else '',
                        census.stories_delivered, backlog)

    def _failed(self, now: datetime, reason: str) -> None:
        self._status = MailSourceStatus(
            source_id=self.get_source_id(), state='failing', account=self._mailbox_config.account,
            last_poll_at=now, last_success_at=self._status.last_success_at,
            failing_since=self._status.failing_since or now, failure=reason[:200],
            extract_mode=self._mailbox_config.extract.mode, today=self._status.today, week=self._status.week,
            extract_backlog=self._status.extract_backlog)


def _with_size(parsed: ParsedMail, size_bytes: int) -> ParsedMail:
    """A size-refused mail is parsed from its header only; its real size is the server's."""
    return replace(parsed, size_bytes=size_bytes)
