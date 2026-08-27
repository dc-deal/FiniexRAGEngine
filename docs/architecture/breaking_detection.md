# Breaking Detection (ISSUE_11)

How the engine catches a **flash crash** fast — detect a breaking story cheaply at ingest,
fast-path it through evaluation, and (later) push the confirmed signal live. This is the last core
piece of the v1.0 live channel.

Companion docs: `application_flow/01_ingest_and_retrieval.md` (where detection runs) ·
`application_flow/02_analysis_and_outcome.md` (where the confirm gate lives).

## The problem it closes

Before #11, both background workers ran on fixed clocks. A story that broke 30s after an interval
eval waited up to a full eval cadence (10 min) to be seen — the exact flash-crash blind spot. And
the corpus's `importance` / `breaking_candidate` columns (created empty for this) were never
written, so the opt-in deep retrieval tier (`retrieval.deep_tier`, reads `importance >= 2`) had
nothing to pull.

## The pipeline (one line)

**Continuous cheap ingest → detect a burst in seconds (no LLM) → wake eval immediately → confirm
(`urgency >= threshold`) → (push).**

```
ingest worker (every ~15s, conditional GET)         eval worker (interval OR breaking-wake)
  fetch → embed only new → upsert                     retrieve → LLM → assemble → persist
       └─ BreakingDetector (no LLM) ── flags ──┐            ▲
          importance tier + breaking_candidate │            │ wake (tier ≥ min_importance)
          on the corpus rows                   └── BreakingBus ┘
```

## Stage 1 — detection (ingest, no LLM)

`core/pipeline/breaking_detector.py` runs *after* upsert, over the articles just stored (so
cross-feed copies count):

- **Primary signal — cluster-burst.** The same story hitting many feeds in a short window forms a
  tight embedding cluster; the cluster size *is* the signal. `count_neighbors(vector, since,
  max_distance)` is one `COUNT(*)` over the recency window with a cosine-distance filter
  (`max_distance = 1 − cluster_similarity`) — pure vector math in the DB, **no LLM, ever**.
- **Secondary fast-path — keyword.** A breaking keyword (word-boundary match, so "SEC" never fires
  on "seconds") on a high-trust source (`source_weight ≥ keyword_source_weight`) flags HIGH on its
  own, without waiting for the cluster to build.
- **Tiers written to the corpus** (`flag_candidates` sets `importance` + `breaking_candidate` +
  `flagged_at`): `cluster ≥ high_cluster_size` **or** the keyword fast-path → **HIGH (3)** +
  `breaking_candidate = TRUE`; `cluster ≥ mid_cluster_size` → **MID (2)**; else routine (untagged).
- **Byproduct:** flagged MID+/HIGH articles populate `importance`, so the previously-dead
  `retrieval.deep_tier` becomes live — detection feeds retention for free.

Config lives on the **source-set** (`detection` block) because the keyword vocabulary is
market-specific and the thresholds are read next to the feeds they are judged against — but note
what the cluster size counts: **near-duplicate articles, corpus-wide**, not distinct feeds and not
one set's slice. See *The detection trigger* and `DetectionConfig` for why that distinction matters
and which half of it is still open.

```json
"detection": {
  "cluster_similarity": 0.85, "cluster_window_minutes": 60,
  "mid_cluster_size": 3, "high_cluster_size": 5, "keyword_source_weight": 0.9,
  "keywords": ["hack", "exploit", "halt", "SEC", "collapse"]
}
```

> The static `keywords` list is the **seam** an LLM-refreshed buzzword flow (ISSUE_46) later fills
> automatically — the detector reads the same field, so hand-seeding now is zero rework.

## The detection trigger — which path fired (ISSUE_106, migration 011)

`flag_candidates` wrote `importance`, `breaking_candidate` and `flagged_at`, but never *which of the
two paths* raised the tier. The decision was known inside `_tier` and discarded one line later, so
the one number the report offered — `flagged_candidates` — has only ever been the **sum of two
near-independent channels**. "Is the cluster path still alive?" could not be answered by any query,
report or log line after the fact, only by re-deriving it from the vectors and the window. **A
threshold whose effect nobody can observe is a threshold nobody can tune.**

`articles.detection_trigger` closes it. Closed vocabulary (`types/ingest_types.py`):

| value | meaning |
|---|---|
| `cluster` | `cluster_size` crossed `mid_` / `high_cluster_size` |
| `keyword` | a keyword on a source at/above `keyword_source_weight` — the fast path |
| `NULL` | flagged before the column existed. **Not a category**, and never backfilled: the decision depended on the corpus state at that instant and is irreconstructable |

Three properties worth knowing before someone changes one of them:

- **An overlap is attributed to the cluster.** When a real burst also contains a keyword, both
  branches would fire. The burst wins, for two reasons: it is the tier's primary meaning and the
  value `high_cluster_size` is calibrated on, and the fast path's entire justification is that it
  fires *before* a cluster exists — crediting it with bursts would flatter its hit rate and make the
  measurement useless for the question it was built to answer.
- **An empty trigger writes nothing.** `flag_candidates(..., trigger='')` leaves the column
  untouched rather than storing `''`, so NULL keeps meaning "not recorded". A caller that does not
  know must not erase what another pass recorded.
- **Surfaces report NULL as its own bucket.** The `breaking` report renders
  `flagged by path: 31 cluster · 15 keyword` — and where old rows are in the window,
  `42 unrecorded` beside them with the reason. Folding them into either path would invent evidence.

The pass log carries the same split at the call (`[breaking] flagged 3 HIGH + 1 MID via cluster 3 ·
keyword 1`), the persisted column is the durable warehouse — the same division of labour as spend.

## The reachability preflight — a threshold only means something relative to the feeds (ISSUE_106)

All three thresholds above are **relative to the feeds that actually run**, and nothing validated
them against the source set. The set is declared once and then erodes at runtime, in two ways the
config never learns about: `enabled: false` (usually per machine — reachability is an environment
fact) and the quarantine ladder switching a failing feed out dynamically. Measured on the live
engine 2026-08-25: `crypto_news` asked for a cluster of **5** while running **4** feeds, and had
done for weeks without a single surface saying so.

`core/pipeline/detection_preflight.py` checks it. Reported at boot as `[DETECTION]` lines — the same
idiom as `[OVERRIDE]` and `[AUTH]` — and again in the `breaking` report, from **one** formatting
function, so the two surfaces cannot describe the same state differently.

**Warn, never refuse.** A pending migration is corruption and rightly blocks boot; an
over-ambitious threshold is a *degraded feature*, and blocking on it would take the engine down over
a quarantined feed. Warning it is the whole point — the failure mode was silence.

### The two checks are not equally strong, and the wording keeps them apart

| check | strength | why |
|---|---|---|
| `keyword_source_weight` > every active feed's weight | **proof** | `source_weight` comes from the config and nothing else can raise it. The fast-path cannot fire, period. |
| `high/mid_cluster_size` > active feed count | **indicator** | `count_neighbors` is a `COUNT(*)` over corpus *articles* with no notion of which feed each came from, so four feeds reach a cluster of five whenever one publishes near-duplicates of its own (live-blog, follow-up, syndicated re-post). |

So the cluster line says *"the cross-feed path cannot be reached by these feeds alone; only a feed
duplicating itself, or the keyword path, can still fire"* — never "unreachable", which would be
false and, worse, reassuring. Reporting an indicator as a proof is how a report loses its
credibility.

### What each state looks like, with the numbers that produced it

```
# the case that filed the issue — crypto_news as it actually ran, 2026-08-25
[DETECTION] crypto_news · 4 active feeds (6 declared, 2 out: theblock, cryptoslate)
[DETECTION] crypto_news · high_cluster_size=5 exceeds the active feed count (4) — the cross-feed
            path to HIGH cannot be reached by these feeds alone; only a feed duplicating itself,
            or the keyword path, can still fire
[DETECTION] crypto_news · keyword gate 0.9 · 3 of 4 active feeds at or above (highest 1.0)

# after ISSUE_107
[DETECTION] crypto_news · 7 active feeds (21 declared, 14 out: cryptoslate, bitcoinmagazine, +12 more)
[DETECTION] crypto_news · keyword gate 0.9 · 5 of 7 active feeds at or above (highest 1.0)
[DETECTION] crypto_news · cluster thresholds 3/5 satisfiable by 7 feeds

# the SILENT failure — every 1.0 feed walled, only the 0.8 tier survives
[DETECTION] crypto_news · keyword_source_weight=0.9 is above every active feed (highest 0.8) —
            the keyword fast-path CANNOT fire; detection is cluster-only
```

The last one is the case worth building this for: a keyword hit that never fires writes nothing,
logs nothing and flags nothing. Half the detection system switches off and every existing surface
keeps reporting a healthy fleet — because the feeds *are* healthy. Two Cloudflare walls have already
landed on this catalogue in 2026 (`cryptoslate` in July, `ambcrypto` in August), so it is not a
thought experiment.

### The gate distribution, not a boolean

The line reports `keyword gate 0.9 · N of M active feeds at or above` rather than a yes/no.
ISSUE_82 measured `keyword_source_weight: 0.9` as *"a binary switch separating 10 feeds from 4"* and
shelved the question as **observation, no lever** for want of an instrument. This is the instrument:
a gate that every feed clears is a constant with extra steps, and one that 5 of 20 clear is a
different lever than the config suggests.

### What it deliberately does not do

- **Quarantine is not part of the boot check.** It is dynamic — a boot-time verdict would be stale
  within the hour. So the two surfaces report two different populations, and each names its own:

  ```
  # boot, from config alone
  [DETECTION] crypto_news · 7 active feeds (21 declared, …) · quarantine not included
              (it is dynamic — the breaking report reads it live)
  # the breaking report, at read time
  crypto_news · 5 of 7 enabled feeds pollable (21 declared, …) · 2 quarantined right now:
              coindesk, theblock
  crypto_news · high_cluster_size=5 exceeds the pollable feed count (5) — …
  ```

  `with_quarantine()` recomputes the verdict against `effective` (enabled minus currently
  quarantined) and stamps `quarantine_known`, so "none are quarantined" stays distinguishable from
  "nobody looked". Only ids belonging to the set are counted — the health store is engine-wide, and
  charging another set's cool-off to this one would invent a warning nobody can act on.
- **It does not retune anything.** Whether `high_cluster_size: 5` is *right* for the current feed
  count is a calibration question that needs the detection trigger on the row first (ISSUE_106
  part A) — the preflight makes the mismatch visible, it does not decide the value.

## Stage 2 — the two-parameter split (the wake vs the confirm)

Sensitivity is **per-pipeline** (`BreakingConfig`), because detection flagging is *one shared
write* on a corpus that many pipelines read. Two knobs gate two different questions at two stages —
they are **orthogonal on purpose**:

| Knob | Question | Anchors | Timing |
|------|----------|---------|--------|
| `breaking.min_importance` | "Is this cluster hot enough to **look now** (pay an off-cadence eval)?" | the **wake** (eval worker, via `BreakingSubscription`) | **before** any LLM spend |
| `breaking.urgency_threshold` | "Having **read** it, is it market-moving enough to **count** as breaking?" | envelope assembly (`is_breaking = urgency ≥ this`, in `SymbolEvaluator`) | **after** the LLM read it |

`min_importance` controls *how eagerly you spend to look*; `urgency_threshold` controls *what you
call breaking once you've looked*. Collapsing them into one would force "only look at what you'd
already call breaking" — which destroys the cheap look-first stage.

**Worked example — one shared corpus, two sensitivities** (crypto `min_importance=2`, forex
`min_importance=3`, both `urgency_threshold=0.80`):

| Cluster | Detector tier | crypto (eager) | forex (conservative) |
|---------|---------------|----------------|----------------------|
| ETF story, 3 feeds | MID (2) | **wakes** → LLM urgency 0.50 → *not* breaking (looked, correctly didn't push) | sleeps (2 < 3) |
| Exchange hack, 6 feeds | HIGH (3) + candidate | **wakes** → urgency 0.92 → **breaking** → (push) | wakes, but irrelevant → urgency 0.10 → not breaking |

The wake filter lives in `BreakingSubscription.notify(tier)`: the `BreakingBus` only latches a
subscription when the flagged tier reaches its `min_importance`, so the same MID cluster wakes the
eager pipeline and is ignored by the conservative one — **without a per-pipeline write to a shared
row**.

### How the wake travels (Stage B mechanics)

- `BreakingBus` (`core/pipeline/breaking_bus.py`) — in-process pub/sub keyed by `source_set_id`.
  The ingest worker `publish(source_set_id, max_tier)` once per pass if it flagged anything; each
  eval worker `subscribe(source_set_id, min_importance)`. No queue infra — the corpus is the
  durable buffer; a missed nudge just means the eval worker catches it on its next interval (the
  candidate is already persisted).
- `EventTrigger` (`core/triggers/event_trigger.py`) — the eval worker's clock: it races
  `sleep(interval)` vs the breaking wake vs `stop`, overlap-free (the pass is awaited before the
  next wait). Ingest workers stay on a pure `IntervalTrigger`.
- **Confirm gate:** a breaking wake only makes eval run *sooner*, not differently — so
  `metadata.model` / the prompt fingerprint stay envelope-consistent. Everything is persisted
  regardless (store-first, #8); the gate governs only what would *push*.

## Continuous ingest & polling etiquette (why 304, not throttling)

Ingest is cheap and duplicate-free (dedup skips known ids across *all* feeds → embedding only ever
pays for genuinely new articles), so the ingest clock runs **near-continuous** (~15s) instead of
every 5 min — dropping detection latency from up to 5 min to seconds. The expensive/dangerous thing
is *latency to the flash crash*, not the embedding.

**The binding constraint at high cadence is feed politeness, not OpenAI** — OpenAI's embedding
limits are huge and new-article volume per tick ≈ 0; hammering RSS hosts with full-body GETs every
15s is what earns a `429` / IP ban. The fix is standard:

- **Conditional GET** (`core/sources/rss_source.py`): the source keeps each feed's `ETag` /
  `Last-Modified` between polls and sends them back; an unchanged feed answers **`304 Not Modified`
  (no body)**. Poll cadence is then bounded by feed freshness, not bandwidth — cheap *and* polite.
- **All feeds stay fast; politeness comes from 304, not throttling.** Central-bank feeds
  (Fed/ECB/BoE) are *prime* flash-crash sources (rate decision, emergency intervention), so they
  are **not** down-rated. An optional per-source `poll_interval_seconds` exists for a genuinely slow
  feed, but the default is fast-for-all.

This is a deliberate, recorded decision: 304 is the mechanism serious feed readers have always used;
throttling the prime sources would defeat the breaking channel.

## Reaction time & the report (ISSUE_11 Stage E)

Reaction time = how fast the engine turns a breaking story into a confirmed signal. It is a **live
measurement, irreconstructable afterwards** (like token usage), so it is captured at the event and
reported from the store (CLAUDE.md — *capture at the call, report from the store*).

**The timeline** (a breaking is a *flow* over several ingest passes and articles, not a point):

```
t0 published_at   ─┐  published→fetched  (feed + our poll — NOT fully ours; 304 keeps it small)
t1 fetched_at     ─┤  fetched→flagged    (detection: waiting for the cluster / keyword copy)
t2 flagged_at     ─┤  flagged→confirmed  (eval / LLM)
t3 envelope ts    ─┘
   engine reaction (t3 − t1) = what WE control      end-to-end (t3 − t0) = what the consumer feels
```

- **Anchor rule:** `t1` = the *freshest* `fetched_at` across the confirming cluster — how fresh the
  evidence was when the engine decided.

  It was the **earliest** until ISSUE_81, on the reasoning that from-first-sighting is the honest
  number because a smarter detector could have flagged the first copy. That argument does not
  survive contact with retrieval: a pass retrieves context up to its `recency_window_minutes`
  back (1440 for crypto, 2880 for forex), and the oldest of those has nothing to do with the story
  that broke. The metric therefore tracked the *window*, reporting a ~22h median in production for
  a pipeline that evaluates every 10 minutes and jumps the queue on a breaking wake in seconds.

  The corrected anchor restores the metric's variance, which is where its information lives: real
  daily medians now range from under an hour to many hours, and the fastest confirmations come in
  at **0.2 minutes** — the breaking-wake path proving itself, which the old anchor could never
  show because it was pinned near the window. A high value now means something real (the engine
  confirmed on aged context) and is a *retrieval* question, not a measurement artefact.

  The precise anchor would be the article that actually triggered detection (`articles.flagged_at`),
  but the envelope does not record which of its sources was flagged — the store report could join
  it and the live path could not, and the two must agree by construction. Carrying that flag on the
  envelope rides ISSUE_64 Phase 2, which extends the envelope anyway.
- **What's captured:** `ArticleRef.fetched_at` (t1, on the envelope, additive/back-compat) +
  `published_at` (t0, already there) + envelope `timestamp` (t3). `articles.flagged_at` (t2) lives
  in the corpus; the report joins it by `article_id` for detection latency.
- **Episode de-dup (live AND store):** a hot story stays `is_breaking` across many envelopes —
  counting/logging every pass inflates "confirmed" (one lingering ADAUSD story = 89 raw hits, 2
  episodes) and lets reaction grow with the wall-clock. So a breaking *episode* is counted
  **once**, on the transition into breaking, with reaction anchored on the **first** confirming
  envelope. Where that boundary falls is `BreakingEpisodeRule`'s decision (see *The episode rule*
  below); the store report drives it in batch (`breaking_report._aggregate`, restart-robust), the
  live eval worker + dashboard drive it streaming (`BreakingEpisodeTracker`). One rule object, two
  callers — they cannot diverge. The `[BREAKING ✓]` log fires once per episode, not per pass.
- **Estimated publish dates excluded from e2e:** a date-less feed falls back `published_at :=
  fetched_at` (so recency filtering still works). Those estimated dates would collapse e2e onto
  engine, so both surfaces drop sources where `published_at == fetched_at` from the e2e sample;
  if every source is estimated, e2e is `—` (honest), not a fake number.

### Episodes — state vs. event (worked example)

`is_breaking` is a **state** (is this symbol breaking *right now*?), recomputed every eval pass —
not an **event**. A symbol with ongoing news scores `urgency ≥ threshold` pass after pass, so
counting each observation counts the *state*, not distinct breaks — like counting every second a
fire alarm rings instead of counting one fire. That is the "confirmed" inflation.

An **episode** is the fix: one continuous stretch of breaking, counted **once** on the transition
into breaking.

A real day for `ADAUSD` — **89 breaking passes → 2 episodes**:

```
09:37 │ breaking · breaking · … · breaking      ◀ EPISODE 1 (the transition into breaking)
      │ (ON every 10-min pass — the same story)
      ┊   … 101 min quiet (not breaking) …       ⇒ past the gap, the episode is over
14:41 │ breaking · breaking · … · 23:50          ◀ EPISODE 2
```

Reaction time follows the same logic: sampled once, at the episode's first confirming pass, then
frozen. Otherwise it re-anchors on the ageing oldest article every pass and grows with the
wall-clock (a lingering story drifted `863m → 873m → 883m` — a symptom, not a signal).

### The episode rule — a Schmitt trigger, not a plain gap (ISSUE_82)

A pure gap rule ("breaking, then quiet for N minutes") is not enough, because `is_breaking` is a
threshold verdict on a **quantised** score. Measured over seven days of `crypto_sentiment`, the
model emits exactly seven `urgency` values — never 0.65/0.75/0.85 — and the confirm gate at 0.8
sits on one of them, with the largest non-zero bucket (0.7) one step below. **70 % of all non-zero
scores sit on the pair straddling the threshold.** Mean pass-to-pass drift on a *byte-identical*
source set is 0.032, a third of a lattice step, and that is enough to flip the verdict: on
2026-08-17 XRPUSD crossed the threshold nine times in fifteen passes while `signal` stayed BUY
15/15 and the freshest retrieved source never moved.

The result was a **~4.7x overcount** — 394 breaking passes → 66 episodes → ~14 real stories in one
week — and a corrupted reaction metric, because every re-trigger re-samples against an ageing
article (one XRPUSD story climbed 0.5 → 130.6 min of "engine reaction" purely with the wall clock).

`BreakingEpisodeRule` (`core/pipeline/breaking_episode_rule.py`) answers that with the standard
treatment for a noisy signal crossing a threshold — **open high, hold low**:

| | condition | config |
|---|---|---|
| **opens** | the pass's recorded `is_breaking` | `breaking.urgency_threshold` (0.8) |
| **stays open** | `urgency >= exit` — or breaking again | `breaking.urgency_exit_threshold` (0.7) |
| **closes** | neither, for longer than the gap | `breaking.episode_gap_minutes` (150) |

**What an episode is keyed by:** the **retrieval query**, i.e. the analysis unit — not the ticker
and not the base currency. Fanned symbols (ETHUSD/ETHEUR, one query under ISSUE_70) are one
episode; same-base but differently-queried instruments (USDJPY/USDCAD/USDCHF) are not. Falls back
to `base_currency`, then the ticker, for a symbol no longer configured. One derivation,
`EpisodeGrouping.key_for`, shared by the live tracker and both store reports — see
`symbol_model_and_grouping.md` for why base was wrong and how it surfaced in production.

Two properties worth knowing:

- **Opening uses the recorded verdict, never a re-derivation from today's threshold.** An archived
  pass keeps the decision its pipeline actually took, so retuning `urgency_threshold` later cannot
  rewrite history when the store report re-groups it. `urgency` is read only for the hold
  condition — which also makes pre-ISSUE_6 rows degrade to the old behaviour instead of misbehaving.
- **The gap is measured, not guessed.** The first value (30 min) was the worst possible choice on a
  600 s cadence: three missed passes plus a second of jitter decided whether a story split, and
  every symbol's smallest observed gap was 30:00.6–30:22 — exactly the boundary. Measuring the
  silence between two episodes of the *same* story then gave **50–150 min**, while different
  stories sat **4 h or more** apart, and 150 reproduced a hand count of the week's stories.
  Sweeping 45 → 150 → 180 over the archive (the rule runs at read time, so this re-derives the
  whole history) gave 24 → 14 → 12 crypto episodes against ~15 hand-counted stories, and cut the
  reaction median from 116.4 to **18.4 min**. 180 was rejected: it merged an ETHUSD SELL episode
  with a separate BUY story, and **a split is recoverable downstream while a merge is lossy** —
  the absorbed story is frozen out of the episode's signal and reason entirely.

The same rule in three cases:

| passes (urgency) | episodes | why |
|---|---|---|
| 0.8 · 0.8 · 0.7 · 0.7 · 0.8 | 1 | the 0.7 passes hold — this is the ISSUE_82 case |
| 0.8 · [0.3 for 50 min] · 0.8 | 2 | below the exit gate past the gap → genuinely over |
| 0.8 · [0.3 for 20 min] · 0.8 | 1 | a dip inside the gap does not end a story |

Setting `urgency_exit_threshold` equal to `urgency_threshold` disables the hysteresis and restores
the pre-ISSUE_82 grouping — the documented escape hatch.

**Both breaking reports name the rule they used** in a header line, per pipeline. They re-derive the
whole archive at read time, so the same command over the same data yields different numbers under a
different rule; without the header a retune is invisible on the page. The *open* gate is
deliberately not printed — an episode opens on the `is_breaking` recorded at the time, which may
have been taken under a different `urgency_threshold` than the one loaded now.

**The envelope is untouched by all of this.** `is_breaking` remains the raw per-pass verdict, so a
consumer's reading of the contract does not change and no `schema_version`/`prompt_version` moves.
Consequently the two episode knobs are **excluded from the `config_fingerprint`** (the only dotted
exclusions in `_PIPELINE_EXCLUDED`): they regroup a report at read time, and two runs either side
of a retune emit byte-identical envelopes — hashing them would fork a series that did not fork.
Giving the *consumer* a debounced regime is a contract change, and belongs to `breaking_episode_id`
(#65) — see *Episode identity on the envelope* below.

**Restart robustness.** The live tracker's state is seeded at boot from the persisted envelopes
(`pipeline_assembler.build_episode_tracker` replays `max(2 × gap, episode_seed_hours)` through the
same `observe`), so a restart mid-story resumes the episode instead of re-opening it. The replayed
envelopes also carry their episode id, and the tracker **adopts** it rather than minting a new one:
the window is finite, so a story that opened before it has its start clipped, and minting from that
clipped start would give the consumer a second identity for a story it is already tracking. Before that, two of one week's
66 episodes were boot artefacts — re-confirmed 3 and 11 minutes after the previous breaking pass,
i.e. well inside any gap, which only an empty tracker can produce. Seeding is best-effort: an
unreadable store costs episode continuity across one restart and never stops the engine.

**The window has to span an episode, not the gap.** The replay can only *open* an episode on a
recorded breaking pass, while the hold band keeps one alive long after the last one — measured
2026-08-18, that tail ran 5 h (BTCUSD), 8.7 h (ETHUSD) and **33 h** (XRPUSD, which had no breaking
pass for a day and a half while its episode stayed open). A `2 × gap` window (5 h) therefore
recovered **0 of 4** open episodes, and the log caught the boundary twice: two restarts four
minutes apart reported `1 episode(s) still open` and then `0`, because one symbol's last breaking
pass fell a minute outside. `breaking.episode_seed_hours` (default 72) sets the depth, floored at
`2 × gap`.

**The remaining edge is reported, not hidden.** An episode already open at the *first* replayed
envelope may reach back further than the window, so its start is a lower bound. The seed logs it
(`N open at the window edge (SYMBOL) …`) and the dashboard renders those durations as `● ≥4h47m`,
while an episode whose opening was observed shows its real duration. The live dashboard resumes
displaying whatever the replay left open (`BreakingEpisodeTracker.seed`), so a story spanning a
restart keeps its row; the session counters beside it are not restored, because they count what
this process saw.

### The distribution under the two gates — `prompt_drift` (ISSUE_110)

The lattice above is what makes a prompt change dangerous. Both gates sit *on* lattice values, so a
prompt that shifts the score distribution by one step changes how much of the population is above
the confirm gate — without changing anything a provenance field can show. That happened: v2 → v3
went live on 2026-08-23 21:37 UTC and cut the crypto confirm rate **8.43 % → 0.47 %**, 113 breaking
rows a day down to six, and it took three days to see. `prompt_version` and `prompt_hash` labelled
every affected row correctly the whole time. A label is not a comparison.

`prompt_drift` (`core/observability/reports/prompt_drift_report.py`, `GET
/v1/reports/prompt_drift`, `prompt_drift_cli`) is that comparison: per pipeline, per prompt version,
the urgency histogram plus the confirm and hold-band shares. Three of its properties are load-bearing
rather than presentational, and each one exists because its absence produced a wrong answer.

**It never pools.** Grouping is per pipeline, always. Across both streams the v3 → v4 aggregate moved
6.67 % → 6.60 % — practically unchanged, while both distributions underneath were rebuilt. No field
in the result object holds a cross-pipeline figure and no line of the rendering prints one, so the
report has nowhere to put the number that would mislead.

**The confirm band never travels without its concentration.** Forex v3 reads healthy at 10.78 % and
collapsed at *"one analysis unit supplies 93 % of it"* — roughly 205 of its 220 confirm rows were
USDCAD alone. Every row therefore carries the number of contributing analysis units and the largest
one's share. The unit is the **episode key** (the retrieval query, so a fanned pair under ISSUE_70
counts once) and it is *displayed* by its tickers — counting by query and reading by query are two
different requirements.

**Only LLM-scored passes enter the distribution.** A result with `basis != 'llm'` is a mechanical
`no_data` HOLD: retrieval came back empty after the floor and the model never ran, yet the row
carries `urgency 0.0`. Folding those in makes a corpus outage — the 37 frozen hours of 2026-08-20 —
read as *"the new prompt got calmer"*. `mechanical` is reported beside `scored`, because an absent
number is not an answer either.

Two further readings the shape buys:

- **The hold/break ratio** separates a collapse from a calm model. v3's confirm share fell 18-fold
  while it kept parking one step below the gate, so its ratio ran to ~19 against v2's ~2.3. A bare
  confirm share cannot tell "the model stopped seeing urgency" from "the model stopped crossing the
  line", and only the second is a threshold problem.
- **The confirm count comes from each pass's recorded `is_breaking`**, never re-derived against
  today's `urgency_threshold` — the same rule `BreakingEpisodeRule` follows, so a retune cannot
  rewrite what the archive says happened. The **hold band** is a read-time derivation against the
  configured `urgency_exit_threshold`, and the report prints which of the two it applied to which
  number. Its totals are pinned against `breaking_timeline`'s by a parity test: `scored`, the
  confirm count and `mechanical` are defined in the same words on both surfaces, so they are
  asserted to agree rather than assumed to.

Buckets are the **observed** value set, not a hard-coded seven: the lattice is a measured property
of a prompt, and a version emitting 0.75 must not be folded into a neighbour silently. Past twelve
distinct values in one version the report bins to 0.1 and says so in its legend.

**A prompt bump's Definition of Done.** A prompt change is a series break by construction, so the
issue that makes one records the before/after distribution from this report, in the issue, at the
time of the bump — an artefact produced once, not a monitor somebody is supposed to watch. And it
records the *split*, never one number per version: v3 → v4 is the worked example of an aggregate
that moved 0.07 points while both streams were rebuilt underneath it.

### Episode identity on the envelope (ISSUE_65)

The rule above decides *where* an episode begins and ends. This is what a consumer receives of it.

Two additive fields on every `SentimentResult`:

| Field | Meaning |
|---|---|
| `breaking_episode_id` | `<pipeline_id>:<episode_key>:<started_at>` — anchored on the episode's **start**, so every pass of one story carries the same value |
| `breaking_episode_start` | `true` on the pass that **opened** the episode, `false` on every pass that continued it |

**Which rows carry the id, and why it is not "the breaking ones".** The id is stamped on every pass
the rule counts as *inside* the episode: the opener, a pass in the hold band (`is_breaking` false,
`urgency` at or above the exit threshold) and a dip that arrives before the gap elapses. Since the
Schmitt trigger above, an episode outlives its own boolean — and an id present only where
`is_breaking` is true would flicker exactly as often as the edge it exists to replace. The consumer
gates on episode identity precisely because that edge flips **19-21 times per episode**; an id with
holes would hand them the same problem under a new name. Rows outside any episode carry `null`.

**The key is the retrieval query, not the symbol.** It is `EpisodeGrouping.key_for` — the same
grouping the reports use. So the symbols of one fanned analysis (ETHUSD/ETHEUR, ISSUE_70) share one
episode by construction, while FX symbols that merely share a base currency do not.

**Correlation, never a dedupe key.** Two envelopes of one episode carry it by design, so it cannot
identify a transmission unit; that is `seq`'s job (ISSUE_9).

**The id therefore contains the query text, and that is accepted deliberately.** In production it
reads `forex_macro_sentiment:US Dollar Canadian Dollar USD/CAD Bank of Canada BOC:2026-08-23T20:20:14Z`
— long, and it carries a piece of configuration into a consumer-visible field. The consequence to be
aware of: retuning a retrieval query (which #55/#29 aim to do) changes the key at the next boot, so
an episode open across that boot continues under a new id. That is not an artefact of the id format
— it is the grouping itself changing, because a different query *is* a different analysis unit, and
the rule would treat it as one regardless. Hashing the key would buy nothing: a hash of a changed
query changes too, and it would cost the readability that makes an id greppable in a log line.
Decided 2026-08-24: leave it, and say so here.

**Where it is assigned.** In the run, before the envelope is persisted — the journal's JSONB column
is the exact served JSON, so a stamp applied afterwards would reach neither the archive nor the
wire. `PipelineRunner` therefore drives the rule and `Pipeline.run` returns a `PipelineRunResult`
(envelope + what the pass did to the episode state); the eval worker renders that result instead of
running the rule a second time.

**The registry** (`breaking_episodes`, migration 010) is written in the envelope's own transaction,
so a journal row can never reference an episode the registry never received. The opening pass
inserts; every later pass advances `last_seen_at` and `n_passes`. The descriptive fields — signal,
urgency, reason, and both reaction times — are frozen at the opening pass, because they describe the
edge: re-sampling a reaction against ageing evidence is the defect ISSUE_81 removed from that
metric. There is deliberately **no `ended` column**: it is `last_seen_at + gap < now()`, and the gap
is per-pipeline config, so storing it would freeze one policy value into history.

What the table is *not* for: it is not what makes assignment restart-safe. Seeding already does that
(below). What it adds is episode-level aggregates as plain SQL — count, mean reaction, duration,
passes per episode — which otherwise cost a full JSONB scan of `outcomes` and a re-grouping in
Python.

### Backfilling identity into the archive (ISSUE_108)

ISSUE_65 stamps identity in the assembly path, so every pass since its deploy carries it and nothing
before it does. `backfill_episode_ids_cli` closes that gap over a named range —
`core/outcome/episode_backfill.py`, dry run by default.

**It drives the live tracker, not a second derivation.** `_aggregate` in the funnel report never
calls `episode_id()`; the report cannot produce identities at all. Only
`BreakingEpisodeTracker.observe()` mints them, and the hard part is the bookkeeping around the rule —
the per-key id map, the adopt-an-existing-id branch, and the release that lets the next episode on a
key mint fresh. So the backfill replays the archive through the same `observe` the eval workers run,
which is what `tracker.seed()` already does at boot for the same reason. Parity is against the live
path rather than against a report.

**The range needs a prologue.** An episode that opened before the range is still open inside it, so
replaying from the range's start cold would mint an id from a *clipped* start — a second identity for
a story already running. A window before the range is therefore replayed for state only and never
written, its width `max(2 × gap, episode_seed_hours)` — the boot seed's own formula, calibrated
against measured hold-band tails of 5 h, 8.7 h and 33 h rather than guessed.

**The self-check is the reason to replay past the ISSUE_65 boundary** even though nothing past it
needs writing. The backfill keeps each served identity aside, clears it on the model so `observe`
computes instead of adopting, and compares. Agreement is evidence that the replay matches the live
path on real data; a disagreement aborts `--apply` and is printed with both values. There is no
override flag. Note that a disagreement is not automatically a defect: for an episode open across
that boundary the served id was minted by a process whose own seed window clipped the start, so a
different anchor is possible with no rule divergence — and because the write rule is *only where
absent*, such a case can never corrupt anything.

**Both sinks or neither**, in one transaction per envelope: `jsonb_set` on the two keys (never a
whole-envelope rewrite, so "nothing else can move" is provable and tested), plus the
`breaking_episodes` rows the tracker produced — deduplicated per episode id rather than per result,
so a fanned pair counts one pass and not two.

**It is a reconstruction, not a recovery.** Three read-time policy changes landed inside the
consumer's window — hysteresis (2026-08-17), `EPISODE_GAP` 45 → 150 (08-18), and the episode key
moving from base currency to the retrieval query (08-18). The last is consequential: before it,
`USDJPY`/`USDCAD`/`USDCHF` shared one `USD` key, so one symbol's story held another's episode open.
A replay under today's grouping splits them correctly, which makes the backfilled FX episodes for
that stretch better than what was served **and different from it**. For a backtest that is the right
answer — the strategy is tested against the engine that will run it — but a diff against anything
captured off the live wire in that window will disagree for FX, wholesale and correctly.

### Stories — the number the episode count is read with (ISSUE_96)

An **episode** is what the rule produces: a run of breaking passes held together by the hysteresis
and closed after the gap. A **story** is the news behind it. They are not the same, because the
model restates one headline many times: the 2026-08-17 SOLUSD Pump-Token story produced twenty
distinct phrasings of *"a significant price increase for Solana's Pump Token and a bullish chart
pattern"* over fourteen hours, and the gap rule cut them into three episodes.

The measure is a **TF-IDF cosine over the episodes' `reasoning` text**, single-link above
`breaking.story_similarity`, bounded by `breaking.story_window_hours` and never crossing an analysis
unit (`core/pipeline/breaking_story_rule.py`). Like the episode rule it runs at **read time**, so
retuning re-derives the whole archive with no migration.

Three things about it are measured, not chosen, and all three surprised the design:

- **Word overlap does not work.** Every reason opens with the same scaffolding, so raw Jaccard
  scored two entirely different stories at 0.45 and two episodes of the *same* story at 0.12. TF-IDF
  suppresses exactly those words and learns which they are from the corpus, so there is no
  hand-maintained stop list to drift.
- **IDF must be smoothed.** Unsmoothed separates better on a large corpus and collapses on a small
  one: two identical reasons alone in a window give every term `df == N`, every weight zero, and a
  cosine of **0.000** with itself.
- **`story_similarity = 0.45` is the middle of a measured plateau.** Swept over 2026-08-11..08-18 the
  output is identical from 0.35 to 0.60; at 0.30 ETHUSD fuses two different stories. Reading the
  groupings it produces, all fifteen are correct — which retired the hand count of seventeen that
  seeded this work. That count was read off *truncated* console output and over-split SOLUSD.

**What it is for.** Not correcting an overcount — the calibrated gap already did that, and in the
window above episodes and stories are near-identical (16 vs 15). It is the **guard** that makes the
divergence visible when it returns, and the invariant that lets the gap be retuned without another
hand count: between gap 45 and 150 the episode count moved −45 % while the story count moved −12 %.

### Is `confirmed` a subset of `flagged`? No.

The report used to print `N flagged → M confirmed`, which reads like a yield. It is not one, and
the arrow was removed for that reason (ISSUE_96): the line now reads
`N flagged (corpus) · M confirmed episodes over K stories`, three counts of one window rather than a
chain. A second measurement backs the same conclusion — breaking-triggered passes carry an
`is_breaking` row **no more often than ordinary scheduled ones** (30.8 % vs 40.5 % over 14 days), so
the detector's verdict is indistinguishable from the base rate. Detection
(the ingest-side flag) and confirmation (the LLM's urgency) are independent paths, and a story can
be confirmed with **no** flagged article behind it at all: on 2026-08-17 both XRPUSD episodes came
from articles with `importance = NULL`, `breaking_candidate = false`, `flagged_at = NULL`. The
out-of-band wake that ran during them was triggered by an unrelated headline. Read the two numbers
as two independent measurements of the same window, never as a ratio.

**The report** (`core/observability/breaking_report.py`, CLI `cli/breaking_cli.py`) — the shared
pattern table, windowed all-time / this week / recent, aggregated from the store; **no per-run
performance footer** (a breaking report is an aggregate over many events, not one run's stage
timings):

```
Breaking Detection — reaction & stories
window: last 7d
episode rule (read-time): crypto_sentiment hold ≥0.70 · gap 150m
story rule (read-time):   crypto_sentiment story ≥0.45 · within 72h
------------------------------------------------------------------------
pipeline                 confirmed stories     engine react       end-to-end
                          episodes                med / p90        med / p90
------------------------------------------------------------------------
crypto_sentiment                 6       5    10.3m / 14.1m   26.7m / 104.0m
------------------------------------------------------------------------
38 flagged (corpus) · 6 confirmed episodes over 5 stories · push pending (Stage C)
```

The same counts feed the live display (#26) and the weekly report (#27, a per-pipeline section).

### Why it broke — the reason + duration surfaces (ISSUE_64)

A breaking episode is the engine's most important event, so both surfaces show *why* it fired and
whether it is still live — not just that it did:

- **Live** (#26): the BREAKING section lists up to three recent episodes, one line each —
  `SYMBOL SIGNAL` · **live** (`● <running>`, a pass within the episode gap still held it open) or
  **ended** (`<age> ago`, closed by the gap rule) · **why** (see below).
- **Weekly** (`breaking_report`): a per-episode listing grouped by pipeline — `started`, `duration`
  (last pass − start), and the same reason — read from each episode's *first* confirming envelope.

**Two reason fields, and they do different jobs (ISSUE_64 Phase 2, prompt v3).**

- `reasoning` justifies the *signal*. It is present on every row, breaking or not, and it is the
  text the **story measure clusters on** (see above).
- `breaking_reason` names the *news*: purpose-built, at most ~25 words, event first
  (*"SEC sues Bitmine over its ETH treasury buys; desks flipping risk-off"*), and absent unless
  something is actually breaking. The prompt asks for it explicitly as news rather than sentiment,
  because the model's default scaffolding is what the story measure had to learn to ignore.

Surfaces read `display_reason` — `breaking_reason` when the model wrote one, `reasoning`
otherwise — so every envelope produced before prompt v3 keeps rendering. The preference is resolved
**once**, where the episode is built, never per renderer.

**`breaking_reason` must not become the story measure's input.** `story_similarity = 0.45` was
calibrated over 1,455 real `reasoning` texts, and the new field is both differently distributed and
empty on non-breaking rows — repointing the clustering at it would retire that calibration without
a visible failure. `tests/observability/reports/test_breaking_report.py` pins this: two episodes whose `breaking_reason`
lines share almost no vocabulary still count as one story because their `reasoning` does.

The prompt change itself is a **series break** — different prompts yield different scores for the
same news — so both families moved to **v3** together (`forex_sentiment` skips v2 deliberately, so
one number describes the prompt generation across pipelines) and the old template files stay
byte-identical: their `content_hash` is the `prompt_hash` recorded in every envelope they ever
produced. A `{{ symbol }}` → `{{ query }}` rename therefore lives only in the new files, with the
builder binding both keys; `tests/llm/test_prompt_builder.py` pins every shipped hash.

A follow-up rides on this: `story_similarity` should be re-measured on v3 reasons once enough exist
— asking the model for one more field shifts the others' distribution slightly, even though the
clustering field itself did not change. The sweep harness (`experiments/story_calibration/`) makes
that a re-run, not a rebuild.

The reason belongs to a persisted episode identity (`breaking_episode_id`, #65), so
live/report/stream/export share one authoritative event — shipped; see *Episode identity on the
envelope*.

## Live push channel (Stage C — deferred, IDE-accepted)

The live low-latency wire is a one-way **SSE** stream (`GET /v1/stream`) carrying the **full
cadence** — every envelope the pipeline produces, scheduled and out-of-band alike. The July design
here was a breaking-*only*, edge-triggered push; #9 withdrew it, because such a channel announces an
episode's start and never its all-clear, and because a quiet channel cannot be told from a dead
producer (#73 is the proof). `is_breaking` is a field, not a channel. It is deferred and paired with
the collector handshake (#9), where the full contract lives —
persistence guarantee (parity anchor), full envelope + `schema_version`, edge-trigger, stable
event-id dedupe, keep-alive heartbeats, Bearer auth. Persistence already gives the IDE's SIGNAL
worker breaking *for free*; push is only the live path.

## Out of scope / deferred

- **Escalation model** (a stronger model for the confirmed priority eval) — deferred to #42
  double-tracked series data ("decide on data, not taste").
- LLM-refreshed keyword vocabulary + semantic breaking-concept retrieval → **ISSUE_46**.
