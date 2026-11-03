"""The e-mail source's record (#138): raw mails, their extracts, and what became of them.

The only SQL over `mail_raw` and `mail_extracts` (migration 020). Raw first: a mail is stored the
moment it passes or fails the gate, before any LLM call, so a provider outage never loses intake.
An extract is stored per (mail, version); its stories become articles through the ordinary ingest
path, and `delivered_at` records that they arrived. Every connect is bounded, like every store here.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg

from finiexragengine.core.mail.mail_message import mail_id as mail_key
from finiexragengine.core.mail.story_articles import story_article_id
from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.types.mail_types import (
    DeliverableExtract,
    GateVerdict,
    MailStory,
    MailWindowCounts,
    ParsedMail,
    PendingMail,
)

_CONNECT_TIMEOUT_SECONDS = 5
# A mail whose extract failed this often is left alone until the version changes — one instruction
# that cannot handle one mail must not cost money on every pass forever.
MAX_EXTRACT_ATTEMPTS = 3


@dataclass(frozen=True)
class PurgeResult:
    mails: int
    extracts: int
    articles: int


class MailStore:
    """Reads and writes the mail source's two tables."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._database_url, connect_timeout=_CONNECT_TIMEOUT_SECONDS)

    # --- intake -------------------------------------------------------------------------------

    def cursor(self, source_id: str, folder: str) -> Tuple[Optional[int], int]:
        """(UIDVALIDITY, highest UID) last read from this folder; (None, 0) before the first read."""
        row = self._one('SELECT uidvalidity, max(imap_uid) FROM mail_raw '
                        'WHERE source_id = %s AND folder = %s AND uidvalidity IS NOT NULL '
                        'GROUP BY uidvalidity ORDER BY max(fetched_at) DESC LIMIT 1',
                        (source_id, folder))
        return (int(row[0]), int(row[1] or 0)) if row else (None, 0)

    def store_mail(self, source_id: str, folder: str, uidvalidity: Optional[int],
                   uid: Optional[int], mail: ParsedMail, verdict: GateVerdict) -> bool:
        """Record one mail with its verdict; False when its Message-ID is already stored.

        A rejected mail keeps no text — it is counted, not kept.
        """
        text = mail.text if verdict.accepted else None
        return bool(self._execute(
            'INSERT INTO mail_raw (mail_id, source_id, message_id, folder, uidvalidity, imap_uid, '
            'sender, subject, sent_at, verdict, reject_reason, text, text_chars, size_bytes) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) '
            'ON CONFLICT (mail_id) DO NOTHING',
            (mail_key(mail.message_id), source_id, mail.message_id, folder, uidvalidity, uid,
             mail.sender, mail.subject, mail.sent_at, 'accepted' if verdict.accepted else 'rejected',
             verdict.reason, text, len(text or ''), mail.size_bytes)))

    # --- extract ------------------------------------------------------------------------------

    def pending(self, source_id: str, version: int, limit: int) -> List[PendingMail]:
        """Accepted mails without a usable extract at `version`, oldest first."""
        rows = self._all(
            'SELECT r.mail_id, r.source_id, r.message_id, r.sent_at, r.text FROM mail_raw r '
            'LEFT JOIN mail_extracts e ON e.mail_id = r.mail_id AND e.extract_version = %s '
            "WHERE r.source_id = %s AND r.verdict = 'accepted' "
            'AND (e.mail_id IS NULL OR (e.stories IS NULL AND e.attempts < %s)) '
            'ORDER BY r.sent_at LIMIT %s',
            (version, source_id, MAX_EXTRACT_ATTEMPTS, limit))
        return [PendingMail(*row) for row in rows]

    def backlog(self, source_id: str, version: int) -> int:
        row = self._one(
            'SELECT count(*) FROM mail_raw r '
            'LEFT JOIN mail_extracts e ON e.mail_id = r.mail_id AND e.extract_version = %s '
            "WHERE r.source_id = %s AND r.verdict = 'accepted' "
            'AND (e.mail_id IS NULL OR (e.stories IS NULL AND e.attempts < %s))',
            (version, source_id, MAX_EXTRACT_ATTEMPTS))
        return int(row[0]) if row else 0

    def record_extract(self, mail_id: str, version: int, *, model: str, prompt_hash: str,
                       stories: Sequence[MailStory], dropped: Sequence[str], prompt_tokens: int,
                       completion_tokens: int, usd: Optional[float], duration_ms: int) -> None:
        self._execute(
            'INSERT INTO mail_extracts (mail_id, extract_version, model, prompt_hash, stories, '
            'dropped, prompt_tokens, completion_tokens, usd, duration_ms, error) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL) '
            'ON CONFLICT (mail_id, extract_version) DO UPDATE SET model = EXCLUDED.model, '
            'prompt_hash = EXCLUDED.prompt_hash, stories = EXCLUDED.stories, dropped = EXCLUDED.dropped, '
            'prompt_tokens = EXCLUDED.prompt_tokens, completion_tokens = EXCLUDED.completion_tokens, '
            'usd = EXCLUDED.usd, duration_ms = EXCLUDED.duration_ms, extracted_at = now(), error = NULL, '
            'attempts = mail_extracts.attempts + 1',
            (mail_id, version, model, prompt_hash,
             json.dumps([story.model_dump() for story in stories], ensure_ascii=False),
             json.dumps(list(dropped), ensure_ascii=False), prompt_tokens, completion_tokens, usd,
             duration_ms))

    def record_failure(self, mail_id: str, version: int, *, model: str, prompt_hash: str,
                       error: str) -> None:
        self._execute(
            'INSERT INTO mail_extracts (mail_id, extract_version, model, prompt_hash, error) '
            'VALUES (%s, %s, %s, %s, %s) '
            'ON CONFLICT (mail_id, extract_version) DO UPDATE SET error = EXCLUDED.error, '
            'extracted_at = now(), attempts = mail_extracts.attempts + 1',
            (mail_id, version, model, prompt_hash, error[:500]))

    # --- delivery -----------------------------------------------------------------------------

    def undelivered(self, source_id: str, version: int) -> List[DeliverableExtract]:
        rows = self._all(
            'SELECT e.mail_id, r.message_id, r.sent_at, e.extract_version, e.stories '
            'FROM mail_extracts e JOIN mail_raw r ON r.mail_id = e.mail_id '
            'WHERE r.source_id = %s AND e.extract_version = %s AND e.stories IS NOT NULL '
            'AND e.delivered_at IS NULL ORDER BY r.sent_at',
            (source_id, version))
        return [DeliverableExtract(mail_id=row[0], message_id=row[1], sent_at=row[2],
                                   extract_version=row[3],
                                   stories=[MailStory(**story) for story in (row[4] or [])])
                for row in rows]

    def confirm_delivered(self, source_id: str, keys: Sequence[Tuple[str, int]]) -> int:
        """Stamp `delivered_at` and retire each mail's articles of older extract versions.

        Without the retirement a re-extraction would put every story into the corpus twice, which
        would double its weight on the day a pipeline reads the set. The older extracts stay.
        Returns the number of articles retired.
        """
        retired = 0
        if not keys:
            return 0
        try:
            with self._connect() as conn, conn.cursor() as cur:
                for key_mail_id, version in keys:
                    cur.execute('UPDATE mail_extracts SET delivered_at = now() '
                                'WHERE mail_id = %s AND extract_version = %s', (key_mail_id, version))
                    cur.execute('SELECT r.message_id, e.extract_version, jsonb_array_length(e.stories) '
                                'FROM mail_extracts e JOIN mail_raw r ON r.mail_id = e.mail_id '
                                'WHERE e.mail_id = %s AND e.extract_version < %s '
                                'AND e.stories IS NOT NULL', (key_mail_id, version))
                    old_ids = [story_article_id(source_id, message_id, index, old_version)
                               for message_id, old_version, count in cur.fetchall()
                               for index in range(count or 0)]
                    if old_ids:
                        cur.execute('DELETE FROM articles WHERE article_id = ANY(%s)', (old_ids,))
                        retired += cur.rowcount
        except psycopg.Error as exc:
            raise VectorStoreError(f'mail store: delivery not recorded: {exc}') from exc
        return retired

    # --- status, report, purge ------------------------------------------------------------------

    def window_counts(self, source_id: str, since: datetime) -> MailWindowCounts:
        row = self._one(
            "SELECT count(*) FILTER (WHERE r.verdict = 'accepted'), "
            "count(*) FILTER (WHERE r.verdict = 'rejected'), "
            'coalesce(sum(jsonb_array_length(e.stories)) FILTER (WHERE e.delivered_at IS NOT NULL), 0) '
            'FROM mail_raw r LEFT JOIN LATERAL (SELECT stories, delivered_at FROM mail_extracts x '
            'WHERE x.mail_id = r.mail_id ORDER BY x.extract_version DESC LIMIT 1) e ON true '
            'WHERE r.source_id = %s AND r.fetched_at >= %s', (source_id, since))
        return MailWindowCounts(mails=int(row[0]), rejected=int(row[1]), items=int(row[2])) if row \
            else MailWindowCounts()

    def intake_rows(self, since: datetime) -> List[Dict[str, Any]]:
        """Per UTC day of intake and source: seen, accepted, rejected by reason, stories, spend."""
        rows = self._all(
            "SELECT r.source_id, date_trunc('day', r.fetched_at AT TIME ZONE 'UTC')::date AS day, "
            "count(*), count(*) FILTER (WHERE r.verdict = 'accepted'), "
            "count(*) FILTER (WHERE r.reject_reason = 'sender'), "
            "count(*) FILTER (WHERE r.reject_reason = 'dmarc'), "
            "count(*) FILTER (WHERE r.reject_reason = 'size'), "
            'coalesce(sum(jsonb_array_length(e.stories)), 0), coalesce(sum(e.usd), 0), '
            'coalesce(sum(e.duration_ms), 0) '
            'FROM mail_raw r LEFT JOIN mail_extracts e ON e.mail_id = r.mail_id '
            'WHERE r.fetched_at >= %s GROUP BY 1, 2 ORDER BY 2, 1', (since,))
        return [{'source_id': row[0], 'day': row[1].isoformat(), 'seen': int(row[2]),
                 'accepted': int(row[3]), 'rejected_sender': int(row[4]),
                 'rejected_dmarc': int(row[5]), 'rejected_size': int(row[6]), 'stories': int(row[7]),
                 'usd': round(float(row[8]), 6), 'extract_seconds': round(int(row[9]) / 1000, 1)}
                for row in rows]

    def tables_exist(self) -> bool:
        row = self._one("SELECT count(*) FROM information_schema.tables "
                        "WHERE table_name IN ('mail_raw', 'mail_extracts')", ())
        return bool(row and int(row[0]) == 2)

    def purge(self, message_id: str, *, dry_run: bool) -> PurgeResult:
        """Remove one mail, its extracts and its articles together; `dry_run` only counts."""
        key = mail_key(message_id)
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute('SELECT source_id FROM mail_raw WHERE mail_id = %s', (key,))
                found = cur.fetchone()
                if not found:
                    return PurgeResult(0, 0, 0)
                cur.execute('SELECT extract_version, coalesce(jsonb_array_length(stories), 0) '
                            'FROM mail_extracts WHERE mail_id = %s', (key,))
                extracts = cur.fetchall()
                ids = [story_article_id(found[0], message_id, index, version)
                       for version, count in extracts for index in range(count)]
                cur.execute('SELECT count(*) FROM articles WHERE article_id = ANY(%s)', (ids,))
                articles = int(cur.fetchone()[0])
                if not dry_run:
                    cur.execute('DELETE FROM articles WHERE article_id = ANY(%s)', (ids,))
                    cur.execute('DELETE FROM mail_raw WHERE mail_id = %s', (key,))   # extracts cascade
                return PurgeResult(1, len(extracts), articles)
        except psycopg.Error as exc:
            raise VectorStoreError(f'mail store: purge failed: {exc}') from exc

    # --- plumbing -----------------------------------------------------------------------------

    def _execute(self, sql: str, params: Tuple) -> int:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.rowcount
        except psycopg.Error as exc:
            raise VectorStoreError(f'mail store: {exc}') from exc

    def _one(self, sql: str, params: Tuple) -> Optional[Tuple]:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        except psycopg.Error as exc:
            raise VectorStoreError(f'mail store: {exc}') from exc

    def _all(self, sql: str, params: Tuple) -> List[Tuple]:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        except psycopg.Error as exc:
            raise VectorStoreError(f'mail store: {exc}') from exc


def start_of_utc_day(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def start_of_utc_week(now: Optional[datetime] = None) -> datetime:
    """Monday 00:00 UTC of the current week."""
    day = start_of_utc_day(now)
    return day - timedelta(days=day.weekday())
