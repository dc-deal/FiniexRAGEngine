# Detailed Ingest Stage & Retrieval

The end-to-end path a news item takes — from the feed to the small, high-signal
article context handed to the LLM. Two paths share one corpus: the **ingest (write)
path** fills the corpus; the **retrieval (read) path** pulls a per-symbol slice out of
it. Read this before touching anything in the ingest or retrieval stage; the per-unit
detail below is the map.

Companion docs: `../pipeline_engine_architecture.md` (how the engine is wired) and
`../retrieval_policy.md` (the retrieval parameters in depth).

## Phase A — Ingest (write path)

Top-down, each new article flows through these units in order:

1. **Trigger — `core/triggers/interval_trigger.py` (`IntervalTrigger`) · built, ISSUE_10.**
   Ingest is **pull, not push**: the engine fetches on its own schedule. Nothing is
   pushed to us. (The only push path in the system is the future live breaking
   channel, ISSUE_11 — a separate concern.) The trigger loop is overlap-free (the
   next tick waits for the pass) and fires immediately on start; the **ingest worker**
   (`core/pipeline/ingest_worker.py`) clocks one **source-set**
   (`configs/source_sets/<id>.json` — feeds + ingest cadence, default 300s; declared
   once, referenced by constellations via `source_set`). Acquisition runs faster than
   eval deliberately: RSS windows slide, a missed article is gone forever — and this
   path never touches the LLM, so frequent is cheap. One worker feeds every pipeline
   referencing the set (1× fetch, N× read).

   **The catalogue vs. what runs.** A set's `sources` is the *declared catalogue*;
   `SourceSetConfig.active_sources()` is what the engine actually builds and polls — the
   single definition read both by the ingestor and by `SourceReach`, which takes the
   envelope's reach census over it, so the set that runs and the set that is reported cannot
   drift apart. Two per-source fields drive it:

   - **`enabled: false`** — declared but switched off: never built, never polled, no health
     event, and invisible downstream (in neither envelope reach number, nor in `errors`) —
     switching a feed off is a decision, not a degradation, so it must not be reported as a
     source the run failed to reach. Same idiom as a disabled model variant (ISSUE_42).
   - **`comment`** — editorial knowledge about the feed. JSON has no comments, so this is
     the sanctioned place to record what was learned ("high-trust FX source"; "behind
     Cloudflare from datacenter IPs").

   **Where to switch a feed off matters.** Reachability is often an *environment* fact, not
   a property of the feed: a source behind bot-management answers a clean egress IP with
   `200` and a datacenter IP with `403` (a JS challenge no feed reader can pass). Deleting
   it from the tracked set would throw away the knowledge and lie about the world. So the
   tracked `configs/source_sets/` stays the canonical catalogue, and the *machine-specific*
   switch lives in the gitignored **`user_configs/source_sets/<id>.json`**, deep-merged at
   load. Because `sources` merges **by `source_id`**, the override names only the feed it
   changes:

   ```jsonc
   // user_configs/source_sets/forex_news.json — this machine only
   { "source_set_id": "forex_news",
     "sources": [ { "source_id": "fxstreet", "enabled": false,
                    "comment": "Cf-Mitigated: challenge from this egress IP." } ] }
   ```

   The other feeds are inherited untouched. A switched-off feed is **marked, never hidden**, on
   every operator-facing surface: `feed_doctor` deliberately still probes it (`OK [disabled]`) —
   it is the tool that answers "can I turn this back on yet?" — and the Sources health report
   appends the same `[disabled]` marker to its verdict. The marker is appended rather than
   substituted, because the health record is *how the feed behaved while it was polled*, which is
   exactly what the decision to re-enable rests on. Note what this costs: `enabled` is a config
   fact and `source_health` has no column for it, so the report has to be *told* by its CLI.
   Without the marker a disabled feed's frozen last poll reads `ok` forever — stale history
   dressed as a live verdict. (Downstream is the exception: the envelope never sees a disabled
   source at all. Operators get the truth; consumers get the contract.)

   **Weight is a tiered portfolio, not a free scalar (ISSUE_107).** `SourceConfig.weight` is read
   by the detector's keyword fast-path (`keyword_source_weight`, default `0.9`), so the bands are a
   policy rather than a preference:

   | band | what belongs in it | what the band buys |
   |---|---|---|
   | **1.0** — primary / established desk | central-bank press feeds; the trusted market desks | the fast-path fires from here: a keyword hit alone reaches HIGH without waiting for a cluster to build |
   | **0.8** — secondary press | general business press, independent second-tier desks, regulator feeds whose house style trips the vocabulary | corroboration volume and corpus depth, deliberately *below* the fast-path gate |
   | **0.5–0.6** — high-volume or promotional | syndication feeds, exchange announcement channels, price commentary | recall only, normally with a `poll_interval_seconds` floor so the fast loop does not pay for them every pass |

   Two consequences worth stating, because both were measured rather than assumed. A weight is
   also a statement about *duplicates*: two feeds from the same publisher corroborate nothing —
   their near-duplicates are intra-publisher — which is why `cnbc_economy` sits at 0.6 next to
   `cnbc_forex` at 1.0. And a primary source can be *unusable* at its own tier: `sec_press` is the
   origin of half the crypto vocabulary, but the list contains the bare token `SEC` and every SEC
   press-release title opens with it, so at ≥ 0.9 the fast-path would fire on every item it ever
   publishes (25 of 25, measured 2026-08-25). It is held at 0.8 until ISSUE_46 narrows the keyword
   to phrases. The band is chosen against the vocabulary, not against the institution's prestige.

   **`enabled: false` has a second, deliberate use: a parked candidate (ISSUE_107).** The first is
   the per-machine switch-off above. The second is a feed that has been probed and is *not yet*
   trusted to run: it lives in the tracked catalogue with its weight, its `comment` carrying the
   probe evidence, and `enabled: false`. That is what makes a candidate reviewable in a diff and
   probeable on the machine that has to reach it — `feed_doctor_cli` diagnoses it there in the same
   command as everything else — while `active_sources()` keeps it out of every count, every health
   event and every envelope until someone flips one word. Promotion is that flip; nothing else
   changes.

   **The fingerprint hashes what runs, not what is declared.** `configuration/config_fingerprint.py`
   takes its feed list from `active_sources()`. So parking a candidate adds no provenance noise —
   a feed the engine never built cannot have changed what was ingested — while switching a
   *running* feed off still moves the hash, which is the case the field exists for. The asymmetry
   is the point; see `docs/development/user_configs_overrides.md`.

   **Parsing is not delivering — the two delivery checks (ISSUE_107).** Every number the health
   surfaces carried until now measured the *poll*. A feed can answer `200` on all 102,136 of them,
   parse without a complaint, and put nothing in the corpus — measured on real candidates while
   every surface called them healthy: `blockworks` (50 entries, newest 5,520 h old),
   `dlnews` (2,637 h), and binance's announcement RSS (`202` with an empty body). Two checks close
   that, on two surfaces, answering two different questions:

   | verdict | surface | rule | needs a threshold? |
   |---|---|---|---|
   | `EMPTY` | `feed_doctor` (live probe) | a 2xx that parses to zero entries | no |
   | `STALE` | `feed_doctor` (live probe) | newest item older than the feed is allowed to be | **yes** — 168 h default, or the feed's own `expected_max_age_hours` |
   | `SILENT` | `source_health` (store) | polls succeed, 0 articles stored in `reports.source_health.silence_days` | no |

   `feed_doctor` probes the feed *now* and needs the network; `source_health` reads what the feed
   *delivered* and needs only the corpus (`articles.source_id` is the same config id, which is why
   the join exists). Neither replaces the other, and the report says which one answered.

   **Three design rules these follow, because a barrier nobody can check is not a barrier:**

   - **Threshold-free where possible.** `EMPTY` and `SILENT` need no policy, and between them they
     catch every case measured above. `STALE` is the only one that needs a number — a single global
     age would be wrong for half the catalogue, since `boc_press` at 25 days is a healthy
     press-release feed while a news feed at 25 days is dead. So a feed *declares* its own
     expectation and is judged against it; a feed that declares nothing gets the default. The report
     names which of the two applied (`STALE (> 168h · default)` vs `· declared`).
   - **The census always renders.** `39 probed · 35 OK · 1 EMPTY · 1 STALE · 12 disabled` plus the
     gate line, whether or not anything tripped — otherwise "nothing was reported" is
     indistinguishable from "nothing was checked".
   - **A legend for the states present, not for all of them.** The console explains exactly the
     verdicts a run produced; a healthy fleet gets no legend at all. The full taxonomy is this
     table, which is why it lives here rather than in every render.

   **`SILENT` never stacks.** A disabled, quarantined or currently-failing feed delivers nothing
   *for a known reason* and keeps that reason as its verdict — reporting silence as well would
   report one fault twice and bury the cause. And where the corpus cannot be read at all the rule
   reports `NOT APPLIED` with a `?` in the delivery column, never `0`: on a fresh database
   "not measured" and "delivered nothing" are different answers, and treating them alike would flag
   the entire fleet at once.

   **Every source is accounted for — the pass reports all of them.** A pass records exactly one
   `SourcePoll` per source it considers (`ok`, `failed`, `quarantined`, `floor_skipped`,
   `suspended`, `host_backoff`), appended in config order; `IngestResult.polls` is the single record and the
   dict views (`per_source`, `failed_sources`, `quarantined_skips`, `floor_skips`) are derived
   from it. This matters because it was once otherwise: each fate went into its own collection,
   the CLI iterated two of them, and a feed in **quarantine therefore vanished from the output
   entirely** — a permanent HTTP 403 rendered as a clean run. `reports/ingest_report.py` renders
   against the *declared catalogue*, so a switched-off feed and one the pass never reached
   (a mid-pass budget suspend) also get a labelled line instead of silence:

   ```
   sources: 8 declared · 6 polled · 1 quarantined · 1 disabled
   fxstreet         disabled            —         —     —     —  Disabled on this machine …
   forexlive        ok                 25         0     0    25
   boe_news         QUARANTINED         —         —     —     —  3h left
   ```

   The `IngestWorker` cannot use the same table (a quarantine outlives thousands of passes on a
   15s cadence), so it carries the skip count on the pass line it logs anyway — and in the
   `WorkerState` the API serves — while the per-skip line stays DEBUG. Entering quarantine still
   WARNs once.

   **The pass is bracketed, because one source's fate depends on the others (ISSUE_84).** The loop
   runs inside `SourceHealthStore.pass_scope(source_set)`. A failure still writes its counters,
   streak and event ring the moment it happens — a pass that dies mid-way must lose no accounting —
   but the **quarantine decision is withheld** until the scope closes, because the question "is this
   feed broken, or is our connectivity gone?" cannot be answered while the loop is still running.
   At scope exit the store compares failures against the sources the pass actually attempted
   (`failed + succeeded`, accumulated rather than handed in, so no second code path can disagree
   with the loop):

   - **≥ 85 % of them failed** (and at least 3 were attempted) → a connectivity event. Every
     withheld flag is discarded, no rung advances, the whole set backs off for five minutes, and one
     `[HOST]` line + one Telegram alert replace what used to be twelve identical feed warnings per
     pass. The sources render as `host_backoff`, never `quarantined` — the feed did nothing wrong,
     and the word that names it sends the operator to the wrong place.
   - **otherwise** → the withheld flags apply, each resolving its own rung from the episode history
     and from the failure's type *and measured duration*. That duration is why `record_failure`
     takes `duration_ms` and the source's `get_fetch_deadline_ms()`: `UNREACHABLE` covers both a
     DNS refusal (~44 ms) and a feed that went quiet (~20.9 s, twice the deadline because the fetch
     retries once), and only the duration tells them apart.

   Letting the streak advance during a connectivity event is deliberate: only the *flag* is
   withheld. When the outage lifts partially — ten feeds answer, two stay dead — the ratio drops
   below the threshold, the event closes, and the two genuinely dead feeds are flagged at once,
   carrying the streak they built during the outage.

   **The fetch deadline — why a feed must be *able* to fail (ISSUE_73).** Everything above only
   engages when a fetch **returns**. On 2026-08-01 one did not: `cryptonews.com` accepted the TCP
   connection and never completed the TLS handshake, and `feedparser.parse()` passes no timeout to
   `urllib`, so the socket inherited `socket.getdefaulttimeout()` — `None`, meaning *wait forever*.
   The thread blocked inside `ssl.do_handshake()` while holding the lock shared by all four
   workers, and the engine produced nothing for nine days without a single error line. The
   quarantine machinery never fired because **nothing ever failed**. TCP itself offers no rescue:
   an established connection over which nothing flows is a perfectly normal state, and Python
   sockets do not enable keepalive.

   So every fetch carries a deadline. `feedparser` exposes no `timeout=`, but it does forward
   extra urllib handlers, and `OpenerDirector.open()` assigns `req.timeout` *before* running the
   request processors — so a `*_request` processor (`_TimeoutHandler` in `rss_source.py`) is the
   one seam that can set it without giving up feedparser's conditional GET, gzip and encoding
   handling. Configured as an acquisition concern, next to the cadence: `fetch_timeout_seconds`
   on the source-set (default **10s**), overridable per feed via `SourceConfig.timeout_seconds`.

   From there **no new error handling was needed** — the timeout re-enters the chain above:

   ```
   ssl.do_handshake() → TimeoutError ⊂ OSError
     → urllib do_open: except OSError → URLError
     → feedparser.parse(): except URLError → bozo_exception
     → _fetch_parsed: transient (URLError ⊂ OSError) → one retry → SourceFetchError(UNREACHABLE)
     → record_failure → 5 consecutive → quarantine 24h → `5/6 ok · cryptonews quarantined`
   ```

   Two properties worth keeping in mind. **The value is deliberately generous**: a hang is
   *infinite*, so 10s catches it exactly as reliably as 3s while never quarantining a merely slow
   feed (measured healthy profile: 0.5s handshake, 1.8s full parse) — the number trades only
   against false positives, never against effectiveness. And **a socket timeout bounds each
   blocking operation, not the whole fetch**: a feed that drips one byte at a time never trips it.
   That gap is closed one level up, by ISSUE_74's **pass deadline** (`pass_timeout_seconds`,
   default 300): a fetch that drips forever still ends the pass, and the worker resumes next tick.
   A process-wide `socket.setdefaulttimeout()` at server boot is the backstop under any *other*
   un-timeouted socket; the feed path does not depend on it.

   **Measuring the deadline instead of guessing it (ISSUE_76).** The 10s above was hand-picked, and
   for nine months there was nothing to judge it by — because a fetch that *fails* left no timing
   behind. `StageTimer.time()` records only when the stage returns, so exactly the polls worth
   studying vanished. When `ecb_press` timed out on 2026-08-15 the engine could not say whether the
   feed had been slow (a longer deadline would have worked) or dead (it would not have).

   The fetch is therefore timed by hand — `perf_counter()` **around** the `try`, so the duration
   survives the exception — and every attempt is appended to the **poll journal**
   (`source_poll_log`), the unpaid twin of `cost_log`:

   ```
   fetch attempt ─┬─ returns → PollSample(ok,     duration_ms, articles=N)
                  └─ raises  → PollSample(failed, duration_ms, error_type)   ← the new half
                        both → StageTimer.record('fetch', started, duration_ms)
   ```

   Skips (`quarantined`, `floor_skipped`) are deliberately **not** journaled — they never reached
   the feed, and at the 15s tick they would add ~70k rows/day. An outage is read instead as a *gap*
   in a feed's poll series against its own median cadence, which additionally catches a dead worker
   or a config change. `sources_cli` renders both halves: latency percentiles with a
   `timeout`-vs-`refused` verdict on the failures, and the gaps with the polls they cost. A journal
   write never fails a pass — see [diagnostics.md](../../development/diagnostics.md).

   **A slow feed also no longer holds up anyone else (ISSUE_74).** Passes were once serialized by
   a single lock shared across all workers, so a fetch sitting out its full timeout stalled the
   eval workers too — and a fetch that never returned stalled them forever. They now run
   independently; `pipeline_engine_architecture.md` covers what that cost, since the two
   invariants the lock carried had to be rehomed first.

   **Normalisation sits between the fetch and everything that reads the text (ISSUE_112).**
   Until 2026-08-29 an article travelled from `feedparser` to the embedder, the eval prompt and the
   breaking keyword matcher with a `.strip()` and nothing else. Measured over 1,966 dev articles:
   50.5 % carried HTML markup, 21.2 % HTML entities, 3.2 % zero-width characters or a BOM — and
   **36.7 % of every token the engine paid for was markup**.

   It was not only a cost. The keyword fast path (`_has_keyword`, over `title + summary`) matched
   *inside* markup, so 6 of 99 keyword hits were a CDN's stock-image filenames
   (`…courtroom-court-lawsuit-justice-breaking-news.png`) on a weight-1.0 source — and that path
   flags an article HIGH on its own, with no cluster and no corroboration. Production shows why
   that matters: over a 7-day window, **32 of 32 attributed detection flags came from the keyword
   path and the cluster path fired zero times**, so the contaminated gate was the only gate running.

   The treatment is one class, `core/sources/article_normalizer.py`, applied where an `Article` is
   built:

   ```
   AbstractSource.fetch()          concrete — normalises and stamps
     └─ _fetch_articles()          abstract — what the feed served
   ```

   `fetch` is deliberately no longer overridable. Normalisation has to happen for every source
   type, and a step each implementation must remember is the step the next implementation forgets —
   the accretion that left 32 of 34 `psycopg.connect` calls unbounded (ISSUE_117). A new source type
   implements `_fetch_articles` and inherits the treatment without knowing it exists.

   Seven ordered steps, stdlib only: drop `<script>`/`<style>` **bodies** → strip tags → unescape
   entities → strip again (unescaping can *reveal* an encoded tag) → drop Unicode `Cc`/`Cf`
   (zero-width, BOM, bidi overrides, Unicode Tags) → NFC → collapse whitespace. No sanitiser
   library, and that is a decision rather than an omission: `bleach`, `lxml` and BeautifulSoup would
   each make a *library version* part of what defines the signal series without appearing anywhere
   in `config_fingerprint` — the ISSUE_109 vector-space lesson in a different column.

   Three properties are load-bearing:

   - **The tag pattern is length-bounded** (`<[a-zA-Z/!][^>]{0,400}?>`). Over 11,994 measured tags
     the p99 is 238 characters and the max 2,880, so 400 covers all but one outlier — and the
     *direction* of the miss is the point. An unbounded `[^>]*` meeting a stray `<` consumes prose
     to the end of the field; bounded, the worst case is a surviving tag instead of a deleted
     article. The first attempt at measuring this problem shipped a character class that deleted
     almost all text while still producing a plausible token count.
   - **The fetched bytes are kept, not discarded.** `title_raw`/`summary_raw` are written only where
     normalisation changed the field (NULL = arrived clean), so the ingest rule holds exactly:
     markup leaves what the model *reads*, never what the engine *holds*. Re-applying the treatment
     never overwrites an existing raw value — the text is idempotent under it, and without that
     guard the provenance would not be.
   - **It is stamped, because it moves the vector space.** `ingest.text_normalizer` is a declared
     `config_fingerprint` leaf and `articles.text_normalizer` records the profile per row. A text
     treatment that changes the embeddings while every provenance field stays byte-identical is
     precisely the unattributable series ISSUE_109 exists to prevent. Profiles move forward only: a
     corrected treatment is the next profile, never an edit to one that archived rows carry.

   Forward-only by construction, not by policy: the corpus upserts
   `ON CONFLICT (article_id) DO NOTHING` and the ingestor skips ids it already holds, so existing
   rows keep their text and their vectors and nothing is re-embedded. The mixing is bounded —
   `recency_window_minutes` is 1440/2880, so ordinary retrieval is entirely normalised within two
   days. The named tail is the deep tier (`importance >= 2`), which can reach older rows.

   The pass reports what it removed (`normalised 71 of 185 fetched (13,353 chars dropped)`), for the
   same reason every paid stage echoes its spend: a silent 36.7 % is how this survived unnoticed for
   the project's whole life.

   **And the durable half is a report, because an echo is not an observation.** The pass line lives
   until the next pass overwrites it, so "is the treatment still working, and how far has the corpus
   turned over" was answerable only at a SQL prompt on the production box — by an operator with a
   shell, and by nobody else. `corpus_text` (`corpus_text_cli`, `/v1/reports/corpus_text`) answers
   four things at once:

   - **the stock** — articles per treatment, so the forward-only transition is watchable;
   - **the proof** — carriers surviving *per treatment*. A stamped row holding markup is the
     normaliser failing, and the report says so rather than printing a reassuring tick. That is the
     line that makes it a check instead of a decoration;
   - **the removal** — measured WITHIN each row against its own kept original, so a drift in article
     length between two periods cannot pass for a change in markup;
   - **the keyword fast path** — hits that exist only inside markup, per feed, against the gate that
     decides whether such a hit alone raises an article to HIGH.

   The keyword half runs the **real** `ArticleNormalizer` over the rows that match as served, never
   a SQL imitation of it: a report that approximates the treatment it audits can only measure its
   own approximation. Only matching rows are transferred (99 of 1,966 in dev), so the corpus is
   never pulled into memory.

   **The embed stage got the same treatment (ISSUE_79) — one stage later, one lesson identical.**
   The fetch was hardened first because it was the stage that hung. The stage after it could still
   take the pass down, and did: an article exceeding the embedding model's 8192-token input limit
   made the whole batch return HTTP 400, which propagated out of `Ingestor.run` (its `try` caught
   only `BudgetExceededError`) and failed the entire pass. Because nothing was stored, the
   offender stayed "new" and returned every pass — 376 failures over 30 hours, ending only when
   the feed dropped it. And it was **silent**: `source_health` records the *poll*, which had
   succeeded, so the feed read healthy the whole time while every article batched with the poison
   item was never ingested.

   Two layers now, and the second is the load-bearing one:

   - **Fit before sending.** `core/rag/token_budget.py` counts exactly with tiktoken — OpenAI's own
     tokenizer and vocabulary, so a local count equals what the API counts — and trims in *token*
     space, decoding back afterwards so a cut can never land inside a token. The limit lives in
     `embedding.max_input_tokens` next to `dimensions`: model-bound, and not discoverable from the
     API (`/v1/models` returns only id/created/object/owned_by).
   - **Survive a rejection anyway.** There will always be a limit we failed to predict, so a
     `BadRequestError` on a batch is *bisected* — halve, retry, recurse — until the offending
     input is alone and can be recorded as rejected while every other article still embeds. The
     trigger is deliberately narrow: quota errors keep raising `BudgetExceededError`, transient
     failures stay with the SDK's retry. Bisecting those would multiply an outage instead of
     isolating a defect.

   What the trim leaves behind is deliberately reversible. The embedded string is `title. summary`,
   built per pass and **never stored** — so `articles.title`/`summary` remain the row's own text
   (normalised, per the step above, and with the fetched bytes beside it in `title_raw`/
   `summary_raw`), and two nullable columns describe only the embedding input:
   `embed_input_tokens` (what was sent) and `embed_truncated_tokens` (what was cut; NULL = nothing).
   Their sum is the original length, and the full text is still in the row, so the question "how
   far did the trim move the signal?" stays answerable later from stored data alone.

   **The tokenizer is resolved and warmed at boot** (`verify_embedding_tokenizer`), for two
   reasons. tiktoken raises for a model it cannot map, and finding that out inside a worker thread
   means every pass dies. And on a cold cache it downloads its vocabulary with `requests.get()` and
   **no timeout** — measured: the process-wide `socket.setdefaulttimeout()` from ISSUE_73 does *not*
   bound it, because urllib3 uses its own sentinel rather than falling back to the socket default.
   Forcing that download at startup puts the one hang it can cause in front of the operator, once.

   Finally, the embedder itself got the deadline the LLM provider always had
   (`embedding.timeout_seconds`, default 60). Without it the SDK default applied — 600s read × 2
   retries ≈ 30 minutes — which made it the last un-timeouted network call in the ingest path.

   **Reach — the envelope's two source numbers.** `core/observability/source_reach.py`
   (`SourceReach.census`) is the one place a set's config and its feed health are combined, and
   the only source of `metadata.sources_configured` / `sources_reached`:

   - `configured` = `len(active_sources())` — a **disabled** feed is in neither number. Switching
     a feed off is a *decision, not a degradation*; reporting it as unreached would claim a
     contribution that never existed.
   - `reached` = of those, the ones `source_health` says are delivering (not in cool-off, last
     poll succeeded). Read **live** per run, never from the store's in-memory quarantine cache:
     the reader is usually a different instance from the writer.

   This replaced `sources_configured - len(failed_sources)`, which derived one number from the
   other and so could only ever differ by a *failed fetch*. Everything else — a quarantined feed,
   an aborted pass — counted as reached; and in **worker mode the runner has no pass at all**
   (acquisition is the ingest worker's clock), so the field was `configured` on every single run:
   a full reach the run never attempted, in the one mode that ships. Reading health works in both
   modes because acquisition records every poll there regardless of who ran it (CLAUDE.md —
   *capture at the call, report from the store*).

   A source within its **poll floor** needs no special case: a floor skip deliberately records no
   health, so the feed keeps its last real verdict — correct, since its articles are in the corpus
   either way.

   **A gap degrades the run — in both modes.** Every entry in `census.unreached` raises a
   `SOURCE_UNREACHABLE` carrying *why* (`quarantined until 07-18 14:39 UTC (5 consecutive
   failures, last HTTP_ERROR 403)`, `never polled`, `last poll failed (…)`), so the run reports
   `partial` rather than a clean `success` over incomplete data. Two things this fixes:

   - **The mode no longer decides the status.** Source errors used to come only from the runner's
     own fetch loop — which worker mode does not have. The identical missing feed degraded an
     inline run and passed a worker run. The mode is a deployment detail, not a fact about the
     world.
   - **The cause is preserved, not just the gap.** The Sources report shows *now*; the envelope
     is what survives to a replay tomorrow (the outcome store is the metrics warehouse). A reason
     that lives only in a live health row is gone by then.

   A source that failed its fetch *this* pass is reported once, from `ingest.failed_sources` —
   its message says more than the census could, and the census entry for it is deduplicated away.
   A `disabled` feed still raises nothing: it is not in `configured`, so it is never in
   `unreached`. Consequences accepted deliberately: a quarantined feed means `partial` for the
   full cool-off (the run *is* continuously incomplete), and a cold start whose eval fires before
   the first ingest reports every source as `never polled` — honest, and it heals on the next
   ingest pass.

   **The pass has three phases, and only the middle one is concurrent (ISSUE_107).**
   `Ingestor._run_pass` is a plan, a fetch and an accounting phase rather than one loop:

   1. **plan** (`_plan_pass`) — walks the declared catalogue and decides who is polled at all,
      recording the `SourcePoll` of everyone who is not. Sequential by necessity: it reads the
      shared quarantine state and hands out at most one half-open probe per source.
   2. **fetch** (`_fetch_all`) — pulls every due source, pooled when `fetch_workers > 1`. The only
      phase that runs concurrently, and the split is drawn here for a reason: a fetch touches the
      network and its own source object, and nothing else.
   3. **account** — walks the plan again in declared order and does everything that costs money or
      mutates state: health, journal, embed, upsert, detection, timings.

   So `fetch_workers` changes *when* feeds are pulled and never what the pass concluded — the
   result object is identical either way, asserted directly
   (`test_pooled_fetch_produces_the_same_result_object_as_the_sequential_pass`).

   **Why breadth needs this.** Fetching sequentially, a pass costs up to
   `len(active_sources()) × fetch_timeout_seconds` in the worst case — 11 feeds × 10s = 110s — and
   `IntervalTrigger` is overlap-free, so the pass duration is added to the poll cadence one for
   one. Pooled it is `ceil(n / workers) × fetch_timeout_seconds`. Measured 2026-08-25 on the live
   forex set: **11 feeds, 3,294 ms sequential → 445 ms at 8 workers (7.4×)**, p50 219 ms per feed.
   The gain scales with the feed count, which is exactly what ISSUE_107 moves.

   **The worker core is thread-safe, and this is the record of it — it has been re-derived more
   than once.** `SourceHealthStore` may be called concurrently by one pass, and that is a property
   of how it is built, not luck:

   - **`_connect()` opens a fresh psycopg connection per call.** No shared cursor, so the usual
     "psycopg connections are not thread-safe" blocker does not apply.
   - **`_PassState` accumulates into `Set[str]`, not integer counters** (`failed`, `succeeded`) —
     `set.add()` is atomic under the GIL *and* idempotent per `source_id`, so a lost update is not
     possible in either direction. This is what keeps the correlated-failure denominator honest.
   - **The policy decision is deferred to `_resolve_pass`**, which runs single-threaded after
     `pass_scope` closes. Quarantine, ladder rung and host back-off are decided there, never inside
     the loop.
   - **Per-source keys are disjoint** — one thread only ever touches one `source_id`.

   Verified rather than reasoned about, 2026-08-25, against Postgres on cloned tables: 25 rounds ×
   12 concurrent recorders inside one `pass_scope` produced no accumulator mismatch and persisted
   counters that summed correctly; the **correlated-failure guard** (all 12 feeds failing at once →
   0 quarantined, one `correlated` episode, host back-off engaged) and the **flag ladder** (one feed
   failing while 11 answer → 1 quarantined, next pass skips exactly it) both behaved as designed.
   Nothing in the accounting phase depends on this today — it is sequential on purpose — but the
   next person who wants to widen the concurrency should not have to re-establish it.

   **One hazard pre-fetching introduces, and it is not obvious.** A successful fetch advances the
   feed's `ETag` / `Last-Modified`. The budget-suspend branch abandons every source after the one
   that ran out of quota, and the invariant it rests on is *"the un-embedded articles reappear next
   pass"* — which pre-fetching would silently break: an abandoned source would answer `304` next
   pass and its articles would be gone for good, a loss the sequential form could not produce
   because it never reached them. Hence `AbstractSource.reset_conditional_get()`: the suspend path
   rewinds the validators of every source it fetched but never accounted for, so the next pass
   re-pulls them for real.

2. **Fetch — `core/sources/rss_source.py` (`RssSource.fetch`).**
   Actively pulls the RSS feed, maps each entry to an `Article` (title + summary only),
   assigns an **idempotent** `article_id` from the entry guid/link, stamps `fetched_at`
   as real-time UTC, and carries the configured `source_weight` onto every article. An
   entry with no stable identity is skipped rather than allowed to poison the corpus.
   **Conditional GET (ISSUE_11):** the long-lived source keeps each feed's `ETag` /
   `Last-Modified` and sends them back, so an unchanged feed answers `304` with no body —
   this is what lets the ingest clock run near-continuous (~15s, for flash-crash latency)
   while staying polite; the binding constraint at speed is feed etiquette, not OpenAI.
   An optional per-source `poll_interval_seconds` lets a slow feed opt out of the fast
   loop (central-bank feeds are deliberately *not* slowed — they are prime breaking
   sources; 304 keeps them fast and polite). **Status-aware + health-tracked (ISSUE_11):**
   the fetch classifies every outcome into a typed `SourceFetchError`
   (`RATE_LIMITED` on HTTP 429, `HTTP_ERROR`, `UNREACHABLE` with one retry, `PARSE_ERROR`)
   instead of parsing a non-feed error body, and every poll — success or failure — is
   recorded into `source_health`; a feed that keeps failing is flagged and quarantined so
   the loop backs off. See [`source_health_and_logging.md`](../source_health_and_logging.md).

3. **Embed — `core/rag/openai_embedder.py` (`OpenAIEmbedder.embed`).**
   Sends the article text to OpenAI and gets back a 1536-dimension vector — a point
   in "meaning space", where direction encodes meaning. OpenAI returns the vectors
   **L2-normalized** (unit length), which is what lets retrieval treat a dot product
   as cosine similarity later (no separate normalization step). The output width is
   pinned to the configured `dimensions`, so a config change can never desync the
   pgvector column.

4. **Store — `core/rag/pgvector_store.py` (`PgVectorStore.upsert`).**
   Writes the vector **and the full raw article** into the shared pgvector corpus,
   **idempotent** on `article_id` (`ON CONFLICT DO NOTHING`). Keeping the raw text is
   deliberate: it is what makes a later re-embed possible (e.g. an embedding-model
   change, ISSUE_16). The `importance` / `breaking_candidate` / `flagged_at` columns are
   populated by the breaking detector in step 5 (ISSUE_11).
   **Corpus guard (built, ISSUE_16):** on first creation the store stamps the corpus
   with its embedding model + dimensions in a `corpus_meta` row; booting against a
   mismatched stamp raises hard, naming both sides — vectors from different models
   must never mix, and a config edit can never silently poison the corpus (a model
   change is a deliberate re-embed migration, ISSUE_14).

5. **Breaking detection — `core/pipeline/breaking_detector.py` (`BreakingDetector`) · built, ISSUE_11.**
   After upsert, an **LLM-free** pass flags breaking candidates over the articles just stored:
   cluster-burst (near-duplicate count via `count_neighbors`) + a keyword fast-path on high-trust
   sources → writes an `importance` tier + `breaking_candidate` + `flagged_at` onto the corpus rows
   (`flag_candidates`). The highest tier drives the eval **wake** (the `BreakingBus`), so a flash
   crash is evaluated in seconds instead of up to a full eval interval. Full detail — the two-
   parameter split, the reaction-time anchors, continuous-ingest etiquette — in
   `../breaking_detection.md`.

**Store everything, filter later.** Ingest never decides relevance — it embeds and
upserts *every* article. Relevance is per-query and belongs to retrieval.

**Running it.** The write path runs as one pass (`core/pipeline/ingestor.py` — `Ingestor`:
fetch → embed → upsert) with three drivers: the **ingest worker** on its source-set cadence
(`server_cli --workers`, ISSUE_10 — the live mode), the manual
`finiexragengine/cli/ingest_cli.py --source-set <id>` pass, and — only when the server runs
*without* workers — inline as `Pipeline.run`'s first stage (the self-contained manual run).
Cheap to re-run: the store is asked which article ids it already holds (`existing_ids`), so only
genuinely new items are embedded — the pass reports `embedded N` (the paid count), so a re-run
over an unchanged feed window pays nothing. Article text is embedded as `title. summary` (the
title carries signal when the RSS summary is thin).

## Phase B — Retrieval (read path)

Retrieval runs **per symbol**. Top-down, one symbol's query flows through:

1. **Symbol → query text — `core/rag/symbol_query_map.py` (`SymbolQueryMap.query_for`).**
   A raw ticker ("BTCUSD") embeds poorly, so each constellation maps it to
   retrieval-friendly text ("Bitcoin BTC"). Resolution: configured alias → derived
   base currency → the symbol itself.

2. **Resolve the query vector — `core/rag/query_vector_cache.py` (`QueryVectorCache`)
   via `core/rag/retriever.py` (`Retriever.retrieve`).**
   The retrieval queries are a fixed, small set (`symbol_queries`), so they are embedded
   **once** and cached in the `query_vectors` table (ISSUE_19); later retrievals reuse the
   stored vector instead of re-calling the API. Embedded with the **same model** as the
   articles — vectors from different models live on different maps and are not comparable
   (the invariant ISSUE_16 guards; the cache key is `(query_text, model, dimensions)`, so a
   text or model change re-embeds only what changed). This is the reference direction
   everything is compared against; because the vectors are in the DB, the ranking can also
   be reproduced by hand in SQL (see `../development/database_inspection.md`).

3. **Candidate search in the DB — `core/rag/pgvector_store.py` (`PgVectorStore.query`).**
   One SQL round-trip does three things at once: the **recency filter**
   (`published_at >= since`), the **distance ranking** (`embedding <=> query`, pgvector's
   cosine-distance operator — `0.0` = identical direction, ascending = best first), and
   the **fetch cap** (`ORDER BY distance LIMIT`). The store returns each match's stored
   embedding too, so the next step needs no re-embedding.

4. **Relevance floor — `core/rag/retriever.py` (`Retriever.retrieve`) · ISSUE_24.**
   Before dedup, candidates whose query↔article distance exceeds `floor_distance`
   (crypto constellation 0.68, forex 0.55 — the cut is query-length dependent, see
   `../retrieval_policy.md`) are dropped — nearest is not the same as *near*, and an
   off-topic article must never reach the prompt. An **empty** survivor set is a result:
   the evaluator answers it mechanically (`HOLD`, `basis='no_data'`, no LLM call).

5. **Squeeze — `core/rag/retriever.py` (`Retriever._squeeze`).**
   Walks candidates in rank order and collapses near-duplicates (the same story
   syndicated across feeds) via pairwise cosine ≥ `dedup_similarity`, then caps at
   `top_k`. **Dedup runs before the cap** so duplicates never consume a slot; each tier
   over-fetches (`_OVERFETCH`) so dedup cannot starve the cap. Result: at most `top_k`
   distinct, recent, on-topic articles.

The retrieval parameters (`top_k`, `recency_window_minutes`, `dedup_similarity`, the
optional two-tier `deep_tier`) and the ranking tie-breaks are documented in
`../retrieval_policy.md`.

## What leaves retrieval — and what does not

The comparison numbers (distance / cosine) are **ephemeral**: computed to rank, used to
select, then dropped. `Retriever.retrieve` returns a `RetrievedContext` — the selected
`Article`s plus the **funnel counters** (in-window / floor-dropped / dedup collapses /
kept and the pre-floor `best_distance`; see `../retrieval_policy.md`) — never the scored
wrappers: per-article scores do not travel into the prompt, the DB, or the envelope.
What survives is the **decision** (which articles were selected) and the funnel that
explains it; the raw vectors stay in the corpus, the raw text stays with them.
Downstream, the selected articles become the LLM
prompt (ISSUE_6) whose structured output is persisted as the outcome envelope — that path
continues in `02_analysis_and_outcome.md`.
