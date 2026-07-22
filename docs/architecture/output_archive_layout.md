# Output Archive — File Layout & Rotation

The shared contract for the rotated sentiment archive (ISSUE_13). Three parties touch
it: the **engine defines** the layout (this doc + the reference code), the **collector
writes** it (FiniexDataCollector, ISSUE_9), the **TestingIDE reads** it (their #141).
It is locked **before** live collection starts — a populated archive cannot be
re-bucketed cheaply.

Reference implementation: `finiexragengine/utils/archive_layout.py`
(`bucket_name` / `bucket_path` / `buckets_for_range` — pure functions, pinned by
`tests/test_archive_layout.py`). The mock generator emits exactly this layout
(`--rotate`, see `experiments/mock_signal_data/`); the collector mirrors the functions.

## Layout

```
<archive root>/
  crypto_sentiment/                    # one directory per stream (pipeline_id,
    2026-04-27.jsonl                   #  incl. variant streams like
    2026-04-28.jsonl                   #  crypto_sentiment_4o_enhanced — ISSUE_42)
    …
  forex_macro_sentiment/
    2026-W18.jsonl                     # weekly boundary: ISO week
```

- **Bucket names** — daily: the UTC calendar date (`2026-04-27`); weekly: the ISO week
  (`2026-W18`, ISO year + zero-padded ISO week, Monday start). Both sort
  lexicographically = chronologically. ISO-year edge: 2027-01-01 buckets as `2026-W53`.
- **Boundary configurable, default daily** — a knob of the *writer* (the collector's
  config; the mock's `--rotate`). One stream keeps **one boundary for its whole
  history**: switching is a deliberate re-bucketing migration, never a config flip.

## Rotation semantics

- **A line lands in the bucket of its `collected_msc`** (the collector's receive time,
  UTC) — consistent with the no-look-ahead merge model (ISSUE_9): the analysis
  `timestamp` is informational, collection time owns the ordering *and* the bucketing.
- **Closed buckets are immutable.** When the boundary passes, the writer closes the file
  and never appends again; late lines cannot happen because `collected_msc` is assigned
  at receive time, monotonically.
- The line format itself is unchanged (ISSUE_9): one JSONL line =
  `{ collected_msc: <int epoch-ms>, ...AnalysisEnvelope }`.

## Reader contract (TestingIDE #141)

For a query range `[start, end]`:

1. compute the overlapping buckets — reference: `buckets_for_range(start, end, boundary)`;
2. load **only** those files, **concatenate in bucket order** (lines inside a bucket are
   already `collected_msc`-ordered);
3. merge by `collected_msc <= tick.collected_msc` as before — rotation changes *where*
   lines live, never what they mean.

`buckets_for_range(2026-04-28 06:00, 2026-04-30 01:00, 'daily')`
→ `['2026-04-28', '2026-04-29', '2026-04-30']`.

## Ownership (the ISSUE_13 decision, settled)

The **collector owns the durable rotated JSONL** — it writes the history. The engine's
`OutcomeStore` stays a **database table** (`/latest` + replay + metrics warehouse; see
`core/outcome/outcome_store.py`) — nothing in the engine rotates files during a live run.
The engine owns the *format*: envelope schema, line shape, and this layout.

## Manual export from the DB (the handover path)

Until the collector runs — or for a backfill — the engine can write the same layout from
its own journal: `export_cli` (→ `core/outcome/outcome_exporter.py`) reads `outcomes` and
writes `<out>/<stream_id>/<bucket>.jsonl`.

**Exactly one scope selector is required — no default, so a bare run is an error** (it prevents
silently re-writing the whole history as it grows):

| Flag | Selects | Reads the flag? | Sets the flag? |
|---|---|---|---|
| `--incremental` | closed days **not yet exported** | **yes** (only unflagged) | yes |
| `--since <date\|week>` | whole buckets on/after the date (`week` = current ISO week) | no | yes |
| `--all` | every closed day (all-time) — the deliberate full re-export | no | yes |
| `--day <date>` | the single bucket a `YYYY-MM-DD` falls into | no | yes |

Modifier (any scope): `--include-open` also writes the current, still-growing bucket — a
throwaway peek, never flagged and not handover-safe (a later run rewrites the then-closed day).

```bash
python -m finiexragengine.cli.export_cli --out data/signal_export --incremental   # the regular run
python -m finiexragengine.cli.export_cli --out data/signal_export --since week
python -m finiexragengine.cli.export_cli --out data/signal_export --all
python -m finiexragengine.cli.export_cli --day 2026-07-21
```

Two properties keep the handover redundancy-free — the whole reason it exists:

- **The DB flag = the "already handed over" record.** `archive_export_log` (migration `002`)
  holds one row per exported `(stream_id, bucket, boundary)`. `--incremental` reads it and writes
  only the not-yet-flagged days; the explicit scopes ignore it for selection but still set it, so
  a re-export stays possible on demand. Open buckets are never flagged. The flag lives in the DB,
  not the filesystem — a deleted/moved export dir does not confuse it, and a `--all`/`--since`
  rebuilds regardless.
- **Whole closed buckets only, idempotent.** Each bucket file is rewritten in full from the
  journal, ordered by `(ts, id)`. A closed day never gains rows, so any re-export is
  byte-identical — no append, no partial days (a time-window cut would split a bucket and break
  this).

In a DB export, `collected_msc` is the envelope's analysis `timestamp` in epoch-ms (there
is no collector receive-time to stamp; this matches the validated mock). When the live
collector runs, it stamps its own receive time instead — the durable archive is still the
collector's, this is the manual twin.

**Auto-export with the weekly report.** The same export runs automatically alongside the
weekly report (`weekly_report.export_outcomes`, default `true`) — both the scheduled Telegram
weekly and every `report_cli` run — writing to `weekly_report.export_dir`
(default `data/signal_export`). It runs in **`--incremental`** mode
(`auto_export_weekly` → `OutcomeArchiveExporter.export`), so a weekly run only writes the days
that closed since the last export — never a full-history rebuild. `report_cli --no-export` skips
it for one run.
