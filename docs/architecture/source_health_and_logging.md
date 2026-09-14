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

### Flag + quarantine — the graduated policy (ISSUE_84)

After `flag_after_consecutive_failures` (5) straight failures a source is **flagged and
quarantined**: the ingestor skips it entirely (`should_poll` is an in-memory check — no DB hit on
the hot path, and it survives a worker restart by re-loading from the row).

*How long* it is skipped is the part ISSUE_84 rewrote. The original policy was a flat 24 hours for
any five failures, from any feed, for any reason. Two production incidents priced that — see
[The two incidents](#the-two-incidents-that-rewrote-the-policy) below — and the replacement is the
shape `BudgetGuard` (#47) already implements for paid calls: **suspend → cool-off → one probe →
resume**, with the cool-off growing across repeats.

**1. A ladder, not a constant.**

```json
"quarantine_hours": [1, 6, 24]
```

The first episode costs an hour, a repeat within `ladder_reset_hours` (168 h) six, the third and
beyond a day. A full window with no new episode drops the feed back to the first rung. A bare
integer is still valid and means a one-rung ladder (`24` → `[24]`), so an existing `user_configs`
override keeps working.

**2. The failure picks the starting rung — by type *and* duration.**

`UNREACHABLE` is an overloaded bucket: a DNS failure, a refused connection, a TLS handshake timeout
and a read timeout all land in it. The *type* cannot separate "the feed went quiet" from "the feed
said no" — but the duration can, by three orders of magnitude. Measured on the live host: refusals
return in ~44 ms, timeouts in ~20,857 ms (2× the 10 s deadline, because `_fetch_parsed` retries
once). The cut sits at `deadline_ratio` (0.7) of the single deadline, in the empty band between.

| Failure | Reading | Rung |
|---|---|---|
| `UNREACHABLE`, duration ≥ 0.7 × deadline | went quiet — usually transient | shortest |
| `UNREACHABLE`, duration < 0.7 × deadline | DNS / refused — durable | longest |
| `RATE_LIMITED` (429) | alive, we are polling too fast | middle |
| `HTTP_ERROR` 5xx | their outage, usually short | shortest |
| `HTTP_ERROR` 4xx | refused at us (the `fxstreet` 403 case) | longest |
| `PARSE_ERROR` | broken body, will not fix itself | longest |

The final rung is `max(episodes in the reset window, rung from the failure)` — history can only
make it worse, never better. The taxonomy is deliberately **not** split for this: `error_type` is
stamped into `source_health.last_error_type` and every `source_poll_log` row, so a new `DNS_ERROR`
would cut the existing series for a distinction the duration already carries.

**3. A half-open probe at cool-off expiry.** Exactly one poll is allowed through. Success clears
the flag, resets the streak and the rung, and closes the episode as `probe_ok`. Failure escalates
**one rung immediately** rather than waiting for five fresh failures.

**4. A correlated-failure guard.** When at least `correlated_failure_ratio` (0.85) of a pass's
pollable sources fail at once — and the pass had at least `correlated_min_pollable` (3) of them —
**no feed is quarantined and no rung advances**. Twelve of twelve feeds failing in the same minutes
is evidence that the feeds are not the problem. Instead the whole set backs off for
`correlated_backoff_minutes` (5) and the condition is logged and alerted as what it is. Its second
job is protecting the ladder: without it one connectivity event escalates every healthy feed a rung,
so the *next* single-feed wobble starts on a longer cool-off than it deserves.

The decision is taken at the **end** of the pass (`SourceHealthStore.pass_scope`), because the
ratio is not knowable while the loop is still running. Only the decision is deferred — counters,
streak and the event ring are written per failure, so a pass that dies mid-way loses no accounting.

### The episode history (`source_quarantine_log`, ISSUE_84)

Every decision lands as a row: quarantines (`kind='quarantine'`) and connectivity events
(`kind='correlated'`, no `source_id` — they belong to the set). **The history is the state**: the
rung is derived from a `COUNT` over this table inside the reset window, never from a stored counter
that could drift from the rows that explain it.

Correlated events sit in the same table on purpose. They explain a *gap* in an individual feed's
history — without them 2026-07-29 reads as "failed five times, nothing happened", and nobody can
tell whether the policy worked or was deliberately suppressed.

The row carries the decision *and its evidence*: rung, cool-off, the trigger's type/status/duration,
the streak, and a `timeline` snapshot of the poll lines that led there. That snapshot is why the
table earns its bytes — `source_poll_log` keeps 14 days, an episode series is read for months, and
the minutes that triggered a decision are exactly the ones worth outliving the retention.

No retention window: ~1 KB per episode at a handful of episodes per week is ~1 MB/year, about one
thousandth of the journal's 11 MB/day.

### The two incidents that rewrote the policy

Kept here because they are the only reason the numbers above are not guesses.

**2026-08-15, `ecb_press`** — 58,227 polls at 99.97 % success. Ten of its sixteen lifetime failures
fell inside one twelve-minute window; the final five spanned **3 m 42 s**. The flat policy answered
with **24 hours**: ~1,900 polls never made, and 19 `forex_macro_sentiment` envelopes marked
`partial` because one feed had a four-minute bad patch. Under the ladder the same event costs one
hour and ~80 polls.

**2026-07-29/30** — a ~5 h host outage took all twelve feeds, the OpenAI API and the database at
once. Every feed crossed five consecutive failures within the same minutes, so every feed got its
own 24 h. The engine then ran and **paid** normally for ~18 further hours on a corpus draining from
71 to 33 relevant articles per pass: a five-hour outage became a **~25 hour** blackout, ~20 of them
self-inflicted. It only avoided collapsing into mechanical `no_data` HOLDs because
`recency_window_minutes` (1440) and the old `quarantine_hours` (24) happened to be the same length.

The ladder alone would have recovered most of that (rung 1 → rung 2 → resume at ~22:00 instead of
the next afternoon). The correlated guard removes the rest *and* keeps the fleet off the ladder for
an event none of the feeds caused.

### Log denoise

The ingest worker picks a log level from the health outcome so repeats don't flood the file. The
level tracks how much the event *asks of the operator* — and the frequency matters as much as the
level: the 2026-07-29 log holds 144 identical lines per feed, which is how a fleet-wide outage
managed to look like twelve separate feed problems.

| Event | Level | Telegram | How often |
|---|---|---|---|
| failure below the threshold | `recent_events` only | — | ~56k/day — never logged |
| first failure of a streak | WARN | — | once |
| repeat failures | DEBUG | — | per pass |
| quarantine, first rung | WARN (names the rung) | — | once per episode |
| escalation to a higher rung | WARN | — | once per episode |
| escalation to the **top** rung | ERROR | — | rare — the feed is effectively lost |
| probe ok / recovery | INFO | — | once |
| probe failed → escalate | WARN | — | once per cool-off |
| **connectivity event: start** | ERROR | **yes** | once |
| **connectivity event: continues** | WARN | — | once per back-off cycle |
| **connectivity event: recovered** | WARN (with duration) | **yes** | once |

The connectivity event needs no rate limiter: while the back-off holds, `should_poll` skips every
source, so the pass polls nothing and produces no event. One line per cycle falls out of the
mechanism itself.

It is also the one condition **the stall watchdog (#75) cannot see** — during a connectivity outage
every ingest pass still *completes*, it just fails every poll. Hence its own alert, riding the
watchdog's existing `AlertCallback` seam (`types/alert_types.py`), wired in `api_app` via
`WorkerSupervisor.set_host_alert`. The health store itself never learns that Telegram exists: the
event travels out on `IngestResult` and the worker announces it.

### The back-off is not also a blind spot (2026-09-08)

The guard stops a set polling for `correlated_backoff_minutes`, which is right — eleven feeds
failing together is not eleven feed problems. The cost is that the engine then stops *looking*, so
the closing event can only report the back-off's own length. On 2026-09-08 all eight episodes were
reported as "recovered after 5m" while the neighbouring set — under the ratio, still polling — was
fetching 265 articles again **21 seconds** after the same failure. Every episode looked identical
and none of them was measured.

So while a back-off holds, each pass runs one **connectivity probe** instead of nothing: resolve a
name, open a socket to a literal address, log both with their durations.

```
[HOST] forex_news probe · dns cloudflare.com FAIL (24800ms) · tcp 1.1.1.1:53 FAIL (3001ms) · blocked
[HOST] forex_news probe · dns cloudflare.com ok (31ms) · tcp 1.1.1.1:53 ok (12ms) · ok
```

Four decisions in that shape, each from what 2026-09-08 got wrong:

- **The name is one the engine does not poll.** A feed fetched every 15 s stays in the OS resolver
  cache and answers happily through an outage. That cache is why the same event produced
  `timed out` from the fast-polled crypto feeds and `getaddrinfo failed` from the slower
  central-bank ones — one cause wearing two symptoms, sorted by poll cadence, which read as two
  different faults for most of a day.
- **The socket goes to a literal address**, so the transport is tested with no name lookup in front
  of it. DNS failing while the socket opens is a resolver fault; both failing is the path.
- **The DNS half is timed, not bounded.** `getaddrinfo` takes no timeout — the OS resolver's retry
  schedule decides, and that is why a failing ingest pass took **55 seconds** against a 10 s
  per-feed deadline. Capping the probe would hide the one number that explains it.
- **It runs only during a back-off**, where the engine is otherwise silent and no feed is being
  touched. Two syscalls per pass, and the first probe that says `ok` dates the end of the outage.

`diagnostics.connectivity_probe_*` configures it; `connectivity_probe_enabled: false` switches it
off, because a diagnostic is worth paying for and not worth being unable to stop.

### The alert survives the outage it reports (2026-09-08)

The connectivity alert travels over the network it is *about*, so it is the message most likely to
be lost exactly when it matters. On 2026-09-08 two opening alarms were lost that way while the
recovery five minutes later went through — leaving a lone *"host connectivity recovered after 5m"*
in the operator's inbox with nothing before it, which reads as noise rather than as an incident.

An undelivered alert is now **held with the moment it was raised** (a bounded in-memory deque per
worker, 8 deep) and the queue is drained ahead of anything newer, so the incident is read in the
order it happened rather than the order it arrived. A delayed line says so — `[delayed 5m] host
connectivity — …` — because an alarm arriving after its own all-clear would otherwise look like a
second outage. A failure *during* a flush re-queues from that message onward, so the half that got
through is never replayed.

Two deliberate limits, stated rather than discovered later:

- **It drains on the next alert, not on a timer.** An incident whose last message failed waits for
  the next one. The trade buys no retry loop and no scheduler; the message worth keeping is the one
  that *opens* an outage, and an outage is always followed by its recovery.
- **A restart drops it.** The durable record is the log and the `correlated` rows in
  `source_quarantine_log` — an alert is a notification, not a ledger, and persisting it would mean a
  table for a nice-to-have.

### What the cross-set line may claim (corrected 2026-09-08)

The alert names the fleet, not just the set, because 12/12 across two independently-configured sets
says *the host* while 5/5 in one set says *one upstream provider* — and the two send the operator to
different places.

**The two halves are different measures, so each says which** (2026-09-08):

```
[HOST] host connectivity — forex_news 11/11 unreachable this pass
                         + crypto_news 12/12 known failing in 5m; no quarantine applied, retry …
```

The set's own number is `failed/pollable` **in this pass**. The other set's is `failed/known` over
the lookback, where *known* is every row `source_health` holds for it — including a feed disabled in
the configuration, because `enabled` lives in the config and this store deliberately never reads it.
That is why two sets with eleven pollable feeds and one disabled feed each report `11/11` for
themselves and `12/12` for the other: two correct numbers that looked like one inconsistent measure
until they were labelled. Making them agree would mean teaching the store about configuration it has
no business knowing.

That count used to be `consecutive_failures > 0`, which is the other set's state **at the instant
this pass ends**. It undercounts precisely during the flapping a host failure produces: a feed that
failed seconds ago and then answered once has a streak of zero. On 2026-09-08 it reported
`crypto_news 1/12` for a set that was losing 8 of 11, sending the reader to eleven healthy feeds. It
now counts feeds whose **`last_failure_at` falls inside the correlated window** instead.

What no query can see is a set that has not polled *yet* — its worker is seconds away on its own
clock. So the wording carries what the count cannot: with nothing to report the line reads
*"no failure reported by the other sets yet"*, never *"other sets healthy"*.

`httpx`/`httpcore` are pinned to WARNING (they log every OpenAI call at INFO). The full detail
always persists in `source_health` regardless of console level — the report reads it there.

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
buckets, because at ~56k rows/day (measured on the server, ~11 MB/day including indexes) it also
answers the questions nobody has asked yet.

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
is `diagnostics.poll_log_retention_days` (14 — the same window the rotating file log keeps),
pruned by the writer once per UTC day.

## Reports & CLIs

- **`sources_cli`** → the Sources report (shared pattern table): per-feed polls / success-rate /
  consecutive / last-ok / status, a capped **recent-problems** list, and an **orphan notice** for a
  `source_id` still in the store but no longer in any config (*may be deleted* — migration leaves old
  heads in place, flagged). Read-only, free. Since ISSUE_76 it also renders the journal's two
  sections: **latency** (p50/p95/p99/max over successful polls, failures kept separate with a
  `timeout` vs `refused` verdict and a ⚠ when p99 nears the configured deadline) and **poll gaps**
  (outages per feed with the polls they cost). `--since` scopes the journal window; the health rows
  stay lifetime. See [diagnostics.md](../development/diagnostics.md) for how to read them.
- **`sources_cli --history <source_id>`** (ISSUE_84) → one feed's quarantine episodes: when each
  started, which rung it reached, what triggered it (with the duration that picked the rung), how it
  ended, and what it cost in that feed's own polls. The summary line separates **polls missed to
  policy** from **polls missed to the outage** — two numbers that were indistinguishable before, and
  the pair the whole change is judged on. `--history <id> --episode <ISO ts>` drills into one
  decision poll by poll, printing the decision next to its evidence (including why the correlated
  guard did *not* fire). Read-only, free.
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

**Two clocks in one file, and it matters downstream.** The handler rolls over at **UTC** midnight,
so `finiex.log.2026-09-07` covers UTC 2026-09-07 — but the formatter stamps the *local* clock with
its offset (`2026-09-08T11:05:03.750+02:00`), deliberately, because the operator reads this file next
to a wall clock. Every other surface in the engine is UTC. Anything that filters this file by time
therefore has to parse the offset and compare in UTC; `core/observability/log_reader.py` does, and
`GET /v1/logs/engine` serves the result — see
[`connect_contract.md`](connect_contract.md) and the
[diagnostics runbook](../development/diagnostics.md).

Config lives in `app_config.json` (`logging`, `source_health`, `diagnostics` blocks) and mirrors the
Pydantic defaults exactly.
