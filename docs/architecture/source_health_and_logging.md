# Source Health & Logging (ISSUE_11)

Two operational concerns that surfaced from the first overnight worker run: the console-only log
scrolled away before it could be read, and a feed (`cryptoslate`) failed on *every* pass without
anyone noticing *why*. This page documents both fixes — rotating file logging, and per-source
health tracking with a debugging-ready Sources report — and the feed root cause that motivated them.

## The root cause: a masked HTTP 429

`cryptoslate` logged `not well-formed (invalid token)` on every crypto ingest pass. It is **not a
broken feed**: under a fast continuous loop the host answers **HTTP 429 (Too Many Requests)** with
an HTML error page, and feedparser then tried to parse that HTML as XML → the SAX error. The old
`RssSource` only special-cased `status == 304` and parsed every other body, so a 429 surfaced as a
bogus parse error. `cryptoslate` also does **not** honour conditional GET (never returns 304), so
every poll was a full GET — which is what earned the rate-limit.

The `feed_doctor` CLI reproduces and classifies this in one command (raw GET status + feedparser
parse + byte scan); the fix is status-awareness + a per-source poll floor + quarantine (below).

## Status-aware fetch (`RssSource`)

`RssSource._fetch_parsed` now classifies every outcome into the `SourceFetchError` taxonomy
(`error_type` + optional `status`) instead of choking on a non-feed body:

| Outcome | `error_type` | Notes |
|---|---|---|
| HTTP 304 | — (returns `None`) | unchanged, no body (conditional GET) |
| HTTP 429 | `RATE_LIMITED` | body is an error page — never parsed as XML |
| other 4xx/5xx | `HTTP_ERROR` | carries `status` |
| DNS / TLS / transport (`OSError`) | `UNREACHABLE` | **retried once** (transient TLS EOFs — e.g. central-bank feeds — are common) |
| malformed body, no entries | `PARSE_ERROR` | not retried (a bad body won't fix itself) |

A `bozo` feed that still yielded entries is tolerated (feedparser is lenient). The **per-source
`poll_interval_seconds`** is the min-poll floor: a feed that ignores conditional GET (like
`cryptoslate`, set to 120 s) opts out of the fast loop so it is never rate-limited in the first
place. Unset ⇒ our continuous tempo applies.

## Source health (`source_health` table)

Every poll — success *and* failure — is captured into one rolling row per feed (CLAUDE.md: *capture
at the call, report from the store*). Identity is the config `source_id` (joins to
`articles.source_id`; one row = one poller); a normalized `host` rides along so the report can group
the same feed appearing under different source-sets.

Per row: poll/success/failure counters, `consecutive_failures`, `last_success_at` / `last_failure_at`,
`last_status`, `last_error_type`, the flag/quarantine state, and a **capped `recent_events` ring**
(the last `recent_events_kept` = 10 warnings/errors, each `{ts, level, type, status, message}`) so a
row is debugging-ready on its own. `level` splits transient throttling (`RATE_LIMITED` / `UNREACHABLE`
= *warning*, we back off and retry) from a broken body / hard status (`PARSE_ERROR` / `HTTP_ERROR` =
*error*).

### Flag + quarantine (the self-healing policy)

After `flag_after_consecutive_failures` (5) straight failures a source is **flagged and quarantined**
for `quarantine_hours` (24 h): the ingestor skips it entirely (`should_poll` is an in-memory check —
no DB hit on the hot path, and it survives a worker restart by re-loading from the row). After the
cool-off it is retried once; a success **clears the flag and resets the streak** (recovery, logged
once), a failure re-flags it. This both stops the log flood and stops us hammering a feed that is
rate-limiting us.

### Log denoise

The ingest worker picks a log level from the health outcome so repeats don't flood the file: **WARN**
the first failure of a streak, **DEBUG** the repeats, **WARN once** on flag+quarantine, **INFO** on
recovery. `httpx`/`httpcore` are pinned to WARNING (they log every OpenAI call at INFO). The full
detail always persists in `source_health` regardless of console level — the report reads it there.

## The poll journal (`source_poll_log`, ISSUE_76)

Health answers **"may we poll this feed"**. It cannot answer **"how has this feed been behaving"** —
its row holds counters, not measurements. On 2026-08-15 `ecb_press` (58,227 polls, 99.97% success)
hit TLS handshake timeouts and was quarantined for 24 h after 3 m 42 s of consecutive failure, and
neither *"was it slow or dead?"* nor *"was that proportionate?"* could be answered from anything
stored. `StageTimer` keeps nothing for a stage that raises, so the timed-out polls — the ones worth
studying — left no trace at all.

The journal is `cost_log`'s shape applied to the **unpaid** calls: one row per poll attempt with
`ts · source_id · source_set · outcome · duration_ms · error_type · status · articles`, read back as
a windowed aggregate with native `percentile_cont`. A raw journal rather than pre-aggregated
buckets, because at ~26k rows/day it also answers the questions nobody has asked yet.

Two design points carry the unit:

- **The duration survives the exception.** `Ingestor` takes `perf_counter()` *around* the try, so a
  failed fetch is measured like a successful one, and `StageTimer.record()` (the manual counterpart
  to `time()`) keeps it in the pass's stage timings too.
- **Skips are not journaled — absence is the signal.** A floor-skip or quarantine skip never reached
  the feed and has nothing to time; at the worker's 15 s tick they would add ~70k rows/day of noise.
  An outage is instead read as a **gap** in a feed's poll series, measured against that feed's own
  median cadence — which also catches a dead worker, a config change or a raised poll floor.

Unlike `source_health`, a journal write **never fails a pass**: every DB error is logged and
swallowed. Diagnostics must not become a new cause of the outages they exist to explain. Retention
is `diagnostics.poll_log_retention_days` (30), pruned by the writer once per UTC day.

## Reports & CLIs

- **`sources_cli`** → the Sources report (shared pattern table): per-feed polls / success-rate /
  consecutive / last-ok / status, a capped **recent-problems** list, and an **orphan notice** for a
  `source_id` still in the store but no longer in any config (*may be deleted* — migration leaves old
  heads in place, flagged). Read-only, free. Since ISSUE_76 it also renders the journal's two
  sections: **latency** (p50/p95/p99/max over successful polls, failures kept separate with a
  `timeout` vs `refused` verdict and a ⚠ when p99 nears the configured deadline) and **poll gaps**
  (outages per feed with the polls they cost). `--since` scopes the journal window; the health rows
  stay lifetime. See [diagnostics.md](../development/diagnostics.md) for how to read them.
- **`feed_doctor_cli`** → raw output + parse diagnosis per feed (HTTP status, bytes, entries, verdict,
  and on `PARSE_ERROR` a byte scan for the offending token). Touches the feeds' network (that is the
  diagnosis) but never the LLM/embeddings — no spend.

The same aggregation feeds the **weekly report (#27)**: a source-health block lists currently-flagged
/ recently-failing feeds with their last errors (a per-pipeline/per-source problem section).

## Rotating file logging

`configure_logging(config)` (called once at server boot) wires the root logger to a **console handler
*and* a daily-rotating file** (`TimedRotatingFileHandler`, UTC midnight rollover, `backup_count` = 14
days, `logs/finiex.log`, gitignored). The console stays on for live liveness; the file is what
survives the scrollback and stays grep-able the morning after. Re-configuration (uvicorn reload) is
idempotent — our handlers are tagged and replaced, never stacked. Size-based rotation is available via
`logging.rotation = "size"` + `max_bytes`. Level is the shared `log_level`.

Config lives in `app_config.json` (`logging`, `source_health`, `diagnostics` blocks) and mirrors the
Pydantic defaults exactly.
