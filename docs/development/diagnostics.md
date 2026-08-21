# Diagnostics — answering questions about the running engine

Indexed by **question**, not by subsystem. When something looks wrong, the entry here names the
instrument that answers it and how to read the answer — so an investigation reads a measurement
instead of reasoning from indirect evidence.

The other docs describe how the engine is *built*; this one describes how to *interrogate* it.
For raw SQL access and the corpus-side queries, see [database inspection](database_inspection.md).

Every instrument below is free (no API spend) and reads the store. None of them needs the engine
to be stopped.

---

## Is a feed slow, or is it dead?

```bash
python -m finiexragengine.cli.sources_cli --since 7d
```

The **latency** section keeps successful and failed polls apart on purpose — averaging them
together would drag `p99` to the timeout value and make a fast feed look like it is failing.

```
latency (last 7d) — successful polls; failures kept separate
----------------------------------------------------------------------------------------
source               polls     p50     p95     p99     max  fails   fail p50  why
----------------------------------------------------------------------------------------
cryptonews           14021    0.4s    1.1s    2.3s    9.8s      0          —
ecb_press            11455    0.5s    1.2s    1.8s    2.1s     16      10.0s  timeout
cryptoslate              5       —       —       —       —      5       0.1s  refused
----------------------------------------------------------------------------------------
```

Read the **`why`** column first — it is the answer:

- **`timeout`** — the failures burned the full deadline. The feed accepted the connection and then
  went quiet. A longer `timeout_seconds` might have worked; this is the feed to consider raising.
- **`refused`** — the failures returned in milliseconds. The feed said no (403, DNS, connection
  refused). A longer timeout would change nothing; the problem is the feed or the credential.
- **`⚠`** — `p99` sits within `diagnostics.timeout_warn_ratio` of the configured deadline. Nothing
  has failed yet, but there is no headroom left for a slow day.

Note what the two columns say together: `ecb_press` above has a **fast p99 (1.8s)** and failures
that sat at **10.0s**. Those are not the same feed being slow — that is a feed that is normally
quick and occasionally stops answering entirely. A raised timeout would only make the outage
longer, not fix it.

The deadline each feed is judged against is its own `timeout_seconds` if set, otherwise its set's
`fetch_timeout_seconds` (`configs/source_sets/*.json`).

## What did an outage actually cost?

The **poll gaps** section of the same command. A gap in a feed's poll series *is* an outage, and
it is measured against that feed's **own** cadence — so a feed polled every 40s and one polled
every 10 minutes are both judged by their own normal.

```
poll gaps (last 7d) — outages measured against each feed's own cadence
----------------------------------------------------------------------------------------
source             cadence   gaps    longest   polls missed
----------------------------------------------------------------------------------------
ecb_press              40s      1     23h58m           2014
----------------------------------------------------------------------------------------
```

`polls missed` is the price in the unit that matters. It is what turns *"quarantined for 24h after
3m42s of failure"* into a number you can weigh against `source_health.quarantine_hours`.

Because it reads gaps rather than quarantine records, it also catches a feed that stopped being
polled for reasons that have nothing to do with quarantine — a dead worker, a config change, a
raised poll floor.

## Was that quarantine proportionate? Did we do it to ourselves?

`sources_cli --history <source_id>` (ISSUE_84). The gaps section above says a feed was gone; this
says **why, for how long, on whose decision, and what it cost**.

```
quarantine history — ecb_press (30d, forex_news)
------------------------------------------------------------------------------------------------
started (UTC)        rung  cool-off  trigger                      ended (UTC)          outcome   missed
------------------------------------------------------------------------------------------------
2026-07-29 15:04:51   —    5m        ⚠ host 12/12                 2026-07-29 20:54:31  resumed    4310
                                     no quarantine, no rung advance
2026-08-15 05:04:04  1/3   1h        UNREACHABLE 10.0s            2026-08-15 06:04:07  probe ok      79
------------------------------------------------------------------------------------------------
2 events (1 quarantines, 1 host) · rung now 1/3, resets 2026-08-22 05:04 · escalations to max: 0
polls missed:  79 to policy · 4310 to the outage
```

The last line is the one to read first: **`to policy` is what our own reaction cost, `to the
outage` is what the world cost.** Before ISSUE_84 both were the same undifferentiated "the feed was
gone", which is precisely why a 3m42s wobble could cost a day of ingest for three weeks before
anyone noticed.

The other columns answer the follow-ups without a second query:

- **rung** — `1/3` is an hour, `3/3` is a day. A feed climbing the ladder is getting worse; one
  sitting at `1/3` is having bad afternoons.
- **trigger** — the failure *and its duration*. `UNREACHABLE 10.0s` burned the deadline (went
  quiet → short rung); `HTTP_ERROR 403 0.04s` was refused (durable → long rung). Same taxonomy,
  different verdict, and this column is where that becomes visible.
- **a `⚠ host` row** — the correlated guard fired: the whole set failed at once, so *nothing* was
  quarantined. It appears in a feed's history because it explains a gap the feed did not cause.
- **recurrence** — the summary's reset date plus the interval between rows. Two episodes 41 hours
  apart are noise; two ninety minutes apart are a feed on its way out. Same count, opposite
  diagnosis.

For one decision in full, add `--episode`:

```
sources_cli --history ecb_press --episode 2026-08-15T05:04:04
```

That prints the run-up poll by poll with the decision next to its evidence — including why the
correlated guard did *not* fire — plus what the old flat policy would have charged. Poll detail
comes from `source_poll_log` while inside its 14-day window; past that it falls back to the
snapshot frozen into the episode when the decision was taken, which is the reason that snapshot
exists.

## Is the relevance floor cutting real news — or cutting nothing?

`coverage_cli --floor-profile` (ISSUE_55 groundwork). `retrieval.floor_distance` is **one absolute
cut applied to per-query distance distributions that are not comparable**, so the same number can
starve one symbol and wave everything through for another. Both failures are silent: a starved
symbol produces a mechanical `no_data` HOLD that reads exactly like "no news".

```
Retrieval Floor Profile — crypto_sentiment · floor 0.700 · window 1440min/24h · 312 articles
--------------------------------------------------------------------------------------------
query                   nearest     p10  median  <=floor  foreign    knee  mech.HOLD  symbols
--------------------------------------------------------------------------------------------
Bitcoin BTC               0.483   0.559   0.702      128       11   0.612      0.0 %  BTCUSD
Dash cryptocurrency       0.612   0.633   0.688       47       19   0.530 ⚠    0.0 %  DASHUSD
Cardano ADA               0.706   0.741   0.798        0        0   0.712 ✗   38.1 %  ADAUSD
--------------------------------------------------------------------------------------------
```

Read the two markers, both threshold-free:

- **`✗` starved** — the *nearest* article in the window is already beyond the floor, so the query
  is scored on nothing. Measured 2026-08-19: ADAUSD lost **410 of 1075 passes** this way, with its
  nearest candidate at 0.706 against a floor of 0.700 — six thousandths.
- **`⚠` indiscriminate** — the window *median* is inside the floor, i.e. more than half the corpus
  counts as relevant to one symbol. For a symbol-specific query that is implausible, and it is the
  direction ISSUE_55 does not consider: a floor too *low* for a generic query feeds a symbol
  articles that are not about it, which is what ISSUE_24 exists to prevent.

`foreign` is the corroboration: the corpus is shared across pipelines and `pgvector_store.query`
applies no source-set filter, so this counts in-floor articles coming from another pipeline's
feeds. `knee` is the largest gap in the below-median part of the distance curve — the floor the
corpus itself suggests, computed without an LLM (ISSUE_55's cross-check, step 7).

`--floor 0.72` re-reads the whole profile at a candidate value without touching config, and
`--archive-since 30d` widens the mech.HOLD column.

## Is a feed worth the weight it was given?

`sources_cli --contribution [source_set_id]` (ISSUE_82 finding 9). Health says a feed is reachable
and latency says it is quick; neither says whether what it publishes ever reaches a prompt.

```
Source Contribution — crypto_news · last 7d
--------------------------------------------------------------------------------------------
source              weight  articles   cited  citation%  breaking  note
--------------------------------------------------------------------------------------------
cryptonews            0.80       136      20     14.7 %         7
cointelegraph         1.00       234       9      3.8 %         2  ⚠ high volume, rarely cited
decrypt               1.00       174       2      1.1 %         0  ⚠ high volume, rarely cited
--------------------------------------------------------------------------------------------
```

`cited` counts **distinct** articles that appeared in at least one envelope's `sources[]` — the
question is whether an article reached a prompt at all, not how often it was reused, or a feed's
rank would become a function of pass cadence.

The point of the table is the first two columns read together: `source_weight` has two hand-set
levels with 10 of 14 feeds at 1.0, and it has never been checked against anything. This is that
check. It **ranks**, it does not judge — a low citation rate can mean a feed is off-topic for these
symbols, syndicated (dedup drops it), or genuinely less useful, and the table does not distinguish
them.

## Why is a pipeline `partial` instead of `success`?

`partial` means the analysis ran but not every configured source was reachable — the engine
preferred a degraded answer over no answer (the envelope contract). The count is in the envelope:

```sql
SELECT pipeline_id, status,
       (envelope->'metadata'->>'sources_configured')::int AS configured,
       (envelope->'metadata'->>'sources_reached')::int    AS reached,
       count(*)
FROM outcomes WHERE ts > now() - interval '2 days'
GROUP BY 1,2,3,4 ORDER BY 1,2;
```

`reached < configured` names the degradation; `sources_cli` then names *which* feed and why.

Worth knowing for consumers: a quarantined feed marks every envelope of its pipeline `partial` for
as long as the quarantine lasts. A downstream filter on `status = 'success'` would silently drop
those signals even though the analysis itself was sound.

## Where did the time go in a pass?

```bash
python -m finiexragengine.cli.perf_cli --since 7d
```

Per-section API latency (avg / p95 / max / summed) from the billing log — the *paid* calls. Its
unpaid twin is the latency section above: `perf_cli` covers OpenAI, `sources_cli` covers the feeds.

## Did we lose articles?

Three separate questions, three instruments:

- **Never fetched** — a gap in `sources_cli`'s poll-gap section: nobody asked the feed.
- **Fetched but not stored** — the ingest pass line (`fetched N · embedded N · stored N`); a
  `rejected` count means the embedding provider refused an article outright.
- **Stored but trimmed** — the per-article token columns (ISSUE_79):

```sql
SELECT source_id, count(*) AS articles,
       round(avg(embed_input_tokens))                               AS avg_tok,
       max(embed_input_tokens + coalesce(embed_truncated_tokens,0)) AS longest_original,
       count(embed_truncated_tokens)                                AS truncated
FROM articles WHERE embed_input_tokens IS NOT NULL
GROUP BY source_id ORDER BY longest_original DESC;
```

## Is the process growing?

The question that had no answer on 2026-08-01. The frozen process showed **5 sockets in
`CLOSE_WAIT`** and **1,191 MB resident memory**, both read off a live process with `py-spy` and
`netstat`, both recorded nowhere — and the restart took the measurement with it.

Nothing is leaking that we know of (the one real leak, `report_cli --send` never closing its
Telegram client, is fixed; the server path was always correct). The point is the design target:
an unattended run of **weeks**. 5 sockets in nine days is nothing; 5 per day for six weeks is a
file-descriptor ceiling whose failure mode looks exactly like twelve unreachable feeds.

**Right now** — `GET /health`, served from the gauge's live sample, never from the table:

```json
"resources": {"enabled": true, "rss_mb": 412.3, "open_sockets": 24, "threads": 31,
              "sampled_at": "2026-08-19T10:23:04+00:00", "ceiling_mb": 0, "over_ceiling": false}
```

**Over time** — the weekly report's Storage section:

```
resources — rss 412 MB (min 388 · max 447) · sockets 24 · threads 31 · vs last week +6 MB rss
```

**Read the delta, not the value.** A single reading never meant anything — 1,191 MB was alarming
only because there was nothing to compare it to. A steadily climbing weekly mean is the signal.
On the first week the line says `first week` rather than inventing a delta against zero, which
would read as exactly the leak it is meant to detect.

Three ways the line can say it has nothing:

- `not sampled in this window` — the gauge is off (`diagnostics.resource_gauge_enabled`), `psutil`
  is missing, or migration 008 has not been applied. The gauge **disables itself** rather than
  blocking boot when `psutil` is absent, so a `git pull` without `pip install` degrades quietly:
  look for `[RESOURCE] gauge disabled` in the log.
- `sockets n/a` — the platform refused the count. `psutil.Process().net_connections()` needs
  privileges Windows does not always grant, and the live host is Windows. Memory and threads still
  report; `n/a` and `0` are deliberately different.
- `first week` — no prior window to compare against yet.

Raw series, when a week is not the right window:

```sql
SELECT date_trunc('hour', ts) AS hour, round(avg(rss_mb)) AS rss_mb,
       max(open_sockets) AS sockets, max(threads) AS threads
  FROM resource_samples WHERE ts > now() - interval '3 days'
 GROUP BY 1 ORDER BY 1;
```

Sampled on the stall-watchdog tick (60s) — ~1.4k rows/day, retention
`diagnostics.resource_retention_days` (14), pruned once per UTC day by the writer.
`diagnostics.resource_rss_warn_mb` warns **once** when crossed and marks the live header;
it ships at `0` (off) on purpose — the weekly line is what produces a real number to set it from.

## Is the engine running right now — and how do I stop it?

The dev container has **no `ps`, `pgrep`, `top`, `curl`, `lsof` or `netstat`**, and `jobs` only sees
children of the current shell — so it shows nothing started from another terminal. Anyone who
backgrounds a server without noting the PID cannot find it again without the handles below. That is
the real reason a background start is a bad idea here, not tidiness.

**What is running?** `/proc` is always there:

```bash
for p in /proc/[0-9]*; do
  cmd=$(tr '\0' ' ' < $p/cmdline 2>/dev/null)
  # Test the FIRST argument only. A pattern like */python* also matches the shell running this
  # loop, because its own command line contains the search words.
  case "${cmd%% *}" in *python*) ;; *) continue ;; esac
  case "$cmd" in *server_cli*|*uvicorn*|*run_cli*|*ingest_cli*)
    echo "${p#/proc/}  $cmd" ;;
  esac
done
```

**Stopping it:** `pkill -f server_cli` (present, even though `pgrep` is not), or `kill <pid>` with a
PID from the list above. Then confirm the port is actually dead — a process can linger while
shutting down workers:

```bash
python3 -c "import urllib.request as u; u.urlopen('http://localhost:8100/v1/health',timeout=3); print('still answering')" \
  || echo "port 8100 dead"
```

**Is the API answering?** Without `curl`:

```bash
python3 -c "import urllib.request as u,json; print(json.load(u.urlopen('http://localhost:8100/v1/health')))"
```

The path is `/v1/health`, not `/health` — the health router sits under the same `/v1` prefix as
everything else.

**Following along:** `tail -f logs/finiex.log` (path from `logging.file`, daily rotation). The log is
written regardless of who started the server.

**Better: do not background it.** A foreground start in its own terminal is the controllable option
here — visible, stoppable with Ctrl-C, and the live display (`--live`) is only usable that way:

```bash
python finiexragengine/cli/server_cli.py --workers        # workers run ingest + eval
python finiexragengine/cli/server_cli.py                  # API only: no passes, no spend
```

**Keep the spend in view:** a running `--workers` server pays per pass (M10, two pipelines: roughly
$0.03/hour). `/v1/health` reports `budget.day_spend_usd`; if it shows `soft_daily_usd: 0.0` there is
**no cap configured** and the cost circuit breaker (ISSUE_47) will not engage.

## Is the engine still working at all?

The stall watchdog (ISSUE_75) answers this without being asked: no completed pass within
`max(factor × cadence, floor_minutes)` and it logs, alerts on Telegram and turns the worker's
dashboard row red. On 2026-08-01 the engine stood still for nine days and nothing said so; that
is the gap it closes.

To check by hand, the newest poll in the journal is the engine's pulse:

```sql
SELECT source_id, max(ts) AS last_poll FROM source_poll_log GROUP BY source_id ORDER BY 2;
```

## We restored the database — what does the consumer see?

A restore rewinds `stream_seq`, so the engine re-mints `seq` values a consumer already holds. To a
cursor-based reader that is the worst failure shape there is: every new frame sits *below* their
cursor and is silently ignored while the connection stays healthy and heartbeats keep arriving.
Nothing looks wrong; the signal simply never advances again.

`stream_epoch` is the guard. It bumps automatically when the engine can see the rewind:

```sql
-- what each stream believes about itself
SELECT pipeline_id, seq, epoch, cluster_id, updated_at FROM stream_seq ORDER BY pipeline_id;

-- what the journal remembers — a counter behind this is a rewind
SELECT pipeline_id,
       max((envelope->>'seq')::BIGINT)          AS journal_seq,
       max((envelope->>'stream_epoch')::BIGINT) AS journal_epoch
FROM outcomes GROUP BY pipeline_id;
```

Boot reconciliation bumps on either of two pieces of evidence: the counter is behind its own
journal, or `cluster_id` (`<system_identifier>/<timeline_id>`) changed — PITR and standby promotion
start a new timeline, and a restore into a fresh cluster changes the system identifier. Both log a
`series rewound` warning naming the old and new epoch. **A boot after a restore that logs nothing is
the case to look at**, not the one that shouts.

**The one shape the engine cannot see: a logical dump/restore in place.** Neither the timeline nor
the system identifier changes, and if the journal was restored along with the counter they agree
with each other. After such a restore, bump the epoch by hand *before* starting the engine:

```sql
UPDATE stream_seq
   SET epoch = GREATEST(epoch + 1, extract(epoch FROM now())::BIGINT),
       last_available_msc = NULL
 WHERE pipeline_id = '<stream>';
```

The `GREATEST` matters: a plain `epoch + 1` can reissue a number an earlier series already used, and
two series sharing an epoch collide the consumer's `(pipeline_id, stream_epoch, seq)` archive key —
they merge, silently.

If it was missed, the consumer reports it: a client presenting a cursor beyond our head receives a
`cursor_ahead` control frame. That is the after-the-fact detector for exactly this case — treat it as
evidence that one of the two stores was rewound, never as something to ignore.

## How fast did the engine really react to breaking news?

```bash
python -m finiexragengine.cli.breaking_cli --since 7d
```

**Read this number with care.** `engine react` is `envelope timestamp − the freshest *retrieved*
source`, which is a proxy for the trigger, not the trigger itself: the envelope does not record
which of its sources carried the breaking flag. Since ISSUE_81 it anchors on the freshest source
rather than the oldest (the old anchor measured the 24h retrieval window instead of any reaction),
but the proxy remains.

The ingest half of the chain is exact and can be read directly:

```sql
SELECT source_id, count(*) AS flagged,
       round(avg(EXTRACT(EPOCH FROM (flagged_at - fetched_at))))      AS flag_lag_s,
       round(avg(EXTRACT(EPOCH FROM (fetched_at - published_at)))/60) AS fetch_lag_min
FROM articles WHERE breaking_candidate AND flagged_at > now() - interval '3 days'
GROUP BY source_id ORDER BY flagged DESC;
```

Measured 2026-08-15: `fetch_lag` 1–6 min, `flag_lag` 2–7 **seconds** — so publication to flag takes
minutes, while the report showed a 107-minute median. That gap is the proxy, not the engine.

## Did the engine really break N times?

```bash
python -m finiexragengine.cli.breaking_cli --since 7d --timeline
python -m finiexragengine.cli.breaking_cli --since 7d --timeline XRPUSD    # one symbol
```

The window as a strip of cells — `#` breaking, `.` in the hold band, `_` below both, `-` the pass
ran but this symbol was not scored (`basis = no_data`), `~` no pass at all (an outage) — with the
**verdict flips** next to the **episodes**. That pairing is the whole diagnostic: flips are the
model's noise and do not change when the grouping rule is retuned; episodes are what the rule made
of them. A clean block is one story; a comb is a threshold being crossed by drift.

```
episode rule (read-time):
  crypto_sentiment       hold ≥0.70 · gap 150m
  forex_macro_sentiment  hold ≥0.70 · gap 150m
------------------------------------------------------------------------------------------
unit                 passes  mech  brk flips  epi  first → last breaking      series
crypto_sentiment
ETHUSD/ETHEUR          1076     0  133   128    4  08-11 15:10 → 08-18 05:00  ##...####.__.##...####.
SOLUSD                  827   249  210    68    5  08-11 10:50 → 08-18 10:10  ###__###.____##...------####
ADAUSD                    0   304    0     0    0  —                          ---------------------------
```

Read it in this order:

- **The rule header first.** Both breaking reports re-derive the whole archive at read time, so the
  same command over the same data gives different numbers under a different rule. The header names
  the rule per pipeline, and shows it when they differ — a `user_configs` override on one pipeline
  is otherwise invisible in the output. The *open* gate is deliberately absent — an episode opens on the `is_breaking` recorded at
  the time, possibly under a different threshold than today's config.
- **A row is an analysis unit, not a ticker.** `ETHUSD/ETHEUR` is one fanned analysis (ISSUE_70) and
  therefore one episode.
- **`passes + mech` is the envelope count.** A row of `0 / 304` means the symbol was never scored,
  which is a different statement from "never broke" — and both are different from a missing row.

If you see many episodes on a comb-shaped series, the gap is too short for that pipeline. If you
see one episode spanning days, check the hold band: at 40–50 % band occupancy an episode chains
through the lulls instead of closing.

The underlying question — how reproducible the model is at all — is answered in SQL, by comparing
passes that saw a **byte-identical** source set:

```sql
WITH p AS (
  SELECT o.id, o.ts, r->>'symbol' AS symbol,
         md5(string_agg(s->>'article_id', ',' ORDER BY s->>'article_id')) AS set_hash,
         min((r->>'urgency')::float) AS urgency, min(r->>'signal') AS signal
  FROM outcomes o,
       LATERAL jsonb_array_elements(o.envelope->'result') r,
       LATERAL jsonb_array_elements(r->'sources') s
  WHERE o.pipeline_id = 'crypto_sentiment' AND r->>'basis' = 'llm'
    AND o.ts >= now() - interval '7 days'
  GROUP BY o.id, o.ts, r->>'symbol'
), d AS (
  SELECT symbol, ts, set_hash, urgency, signal,
         lag(set_hash) OVER w AS p_hash, lag(ts) OVER w AS p_ts,
         lag(urgency)  OVER w AS p_urgency, lag(signal) OVER w AS p_signal
  FROM p WINDOW w AS (PARTITION BY symbol ORDER BY ts)
)
SELECT symbol, count(*) AS pairs,
       round(avg(abs(urgency - p_urgency))::numeric, 3) AS d_urgency,
       round(100.0 * count(*) FILTER (WHERE signal <> p_signal) / count(*), 1) AS signal_flip_pct
FROM d WHERE p_hash = set_hash AND ts - p_ts <= interval '15 minutes'
GROUP BY symbol ORDER BY d_urgency DESC;
```

Only **adjacent** passes, because the prompt carries `Current time:` and absolute article
timestamps: over hours a falling urgency is correct decay, not drift. Measured 2026-08-17 over
~6,200 pairs: mean `urgency` drift **0.032**, `signal` flips **2.8 %** of adjacent pairs (0 % on
thinly-covered symbols, 6.8 % on BTCUSD). Small everywhere — the breaking gate is simply the one
place where a third of a lattice step becomes a categorical error.

## Reference — the diagnostic stores

| Store | Holds | Lifetime |
|---|---|---|
| `source_health` | one rolling row per feed: counters, flag/quarantine, last errors | forever |
| `source_poll_log` | one row per poll attempt: duration, outcome, error type | `diagnostics.poll_log_retention_days` (14) |
| `source_quarantine_log` | one row per quarantine episode / connectivity event: rung, cool-off, trigger, frozen timeline | forever (~1 MB/year) |
| `resource_samples` | one row per watchdog tick: rss, sockets, threads | `diagnostics.resource_retention_days` (14) |
| `cost_log` | one row per paid API call: tokens, USD, duration | forever (billing) |
| `outcomes` | every produced envelope | forever |
| `articles` | the corpus + embed token counts + breaking flags | forever |

`source_health` and `source_poll_log` are deliberately separate: health answers **"may we poll this
feed"** on the hot path and must be correct, so it raises on failure; the journal answers **"how has
it been behaving"** and is allowed to lose a row rather than fail a pass.

## Config

```json
"diagnostics": {
    "poll_log_enabled": true,
    "poll_log_retention_days": 14,
    "timeout_warn_ratio": 0.7
}
```

**Measured on the server** (2026-08-17, over 44 hours): the journal writes **~56k rows/day**, and
`pg_total_relation_size` — table plus indexes — comes to **~11 MB/day**. At the 14-day default that
is ~780k rows and **~155 MB**, which is why the default is 14 and not 30: the first estimate
(~26k/day) was derived from a poll average that included the nine-day freeze, so it was low by
half, and it assumed ~60 bytes per row against an actual ~204 with indexes. Fourteen days is also
the window the rotating file log keeps, so an incident and its poll history age out together.

The journal prunes itself once per UTC day, on the first record after the date turns. `poll_log_enabled: false` switches it off entirely; the reports then say so
rather than showing an empty table.
