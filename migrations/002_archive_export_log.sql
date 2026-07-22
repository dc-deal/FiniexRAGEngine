-- 002_archive_export_log — the "already handed over" flag for the outcome archive export (ISSUE_13).
--
-- The export CLI / weekly auto-export write closed buckets (one day = one bucket) to the rotated
-- JSONL layout. As the history grows, re-writing every closed day on each run is wasteful, so the
-- incremental mode needs to know which buckets were already exported. That truth lives here — one
-- row per exported (stream, bucket, boundary) — not on the filesystem (which can be deleted or
-- moved independently). `--incremental` reads it (only unflagged buckets); every mode writes it.
-- Open (still-growing) buckets are never flagged: they are not a finished handover yet.

CREATE TABLE archive_export_log (
    stream_id    TEXT NOT NULL,          -- pipeline_id / variant stream (the per-stream dir)
    bucket       TEXT NOT NULL,          -- '2026-07-14' (daily) | '2026-W28' (weekly)
    boundary     TEXT NOT NULL,          -- daily | weekly — tracked independently
    exported_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    lines        INTEGER NOT NULL,       -- lines written the last time this bucket was exported
    PRIMARY KEY (stream_id, bucket, boundary)
);
