-- 020_mail_raw_and_extracts — the e-mail source's own record, raw first, extract second (#138).
--
-- A newsletter arrives as one mail holding about ten stories. The engine keeps the mail before it
-- asks an LLM anything about it (`mail_raw`), and keeps every extract the LLM made of it
-- (`mail_extracts`), one per extract version. Two reasons, both about what cannot be had again:
-- the mailbox may be gone one day, and a better extract instruction will come — a re-extraction then
-- runs over these rows, not over a mailbox. Stories become ordinary `articles` rows; each one's id is
-- derived from the mail's Message-ID, its index and the extract version, so a story points at its mail.
--
-- What is never stored: the raw .eml, the recipient's address, unsubscribe tokens, personalised links.
-- `text` is the visible text after those were stripped. A rejected mail keeps its verdict and sender
-- but no text — countable without keeping unvetted content.
--
-- Retention: none. These rows are the archive the source exists for (like `articles`). A single mail
-- is removed only by hand (`email_purge_cli`), together with its extracts and its articles.

CREATE TABLE mail_raw (
    mail_id        TEXT PRIMARY KEY,              -- sha256(Message-ID)[:32]
    source_id      TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    folder         TEXT NOT NULL,                 -- the IMAP folder it was read from; 'backfill' for files
    uidvalidity    BIGINT,                        -- NULL for backfilled files
    imap_uid       BIGINT,                        -- the folder's UID; the intake cursor is its maximum
    sender         TEXT NOT NULL,
    subject        TEXT NOT NULL DEFAULT '',
    sent_at        TIMESTAMPTZ NOT NULL,          -- the mail's own Date, in UTC — the stories' published_at
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    verdict        TEXT NOT NULL,                 -- 'accepted' | 'rejected'
    reject_reason  TEXT,                          -- 'sender' | 'dmarc' | 'size'; NULL when accepted
    text           TEXT,                          -- NULL when rejected
    text_chars     INTEGER NOT NULL DEFAULT 0,
    size_bytes     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX mail_raw_cursor ON mail_raw (source_id, folder, uidvalidity, imap_uid);
CREATE INDEX mail_raw_sent ON mail_raw (source_id, sent_at);

CREATE TABLE mail_extracts (
    mail_id            TEXT NOT NULL REFERENCES mail_raw (mail_id) ON DELETE CASCADE,
    extract_version    INTEGER NOT NULL,
    model              TEXT NOT NULL,
    prompt_hash        TEXT NOT NULL,             -- the instruction's body hash, like an envelope's prompt_hash
    stories            JSONB,                     -- [{"headline", "facts", "topic"}]; NULL while it failed
    dropped            JSONB,
    prompt_tokens      INTEGER NOT NULL DEFAULT 0,
    completion_tokens  INTEGER NOT NULL DEFAULT 0,
    usd                DOUBLE PRECISION,          -- from the price table; NULL when the model has no price
    duration_ms        INTEGER NOT NULL DEFAULT 0,
    extracted_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    error              TEXT,                      -- the last failure's class and reason; NULL on success
    attempts           INTEGER NOT NULL DEFAULT 1,
    delivered_at       TIMESTAMPTZ,               -- its stories were stored as articles
    PRIMARY KEY (mail_id, extract_version)
);
