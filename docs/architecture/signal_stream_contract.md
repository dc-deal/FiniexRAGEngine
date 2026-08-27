# The signal stream — the live transport contract (ISSUE_9)

**The consumer reads this as the contract.** `output_archive_layout.md` says what an archive *line*
is; this says what a *frame* is and how a connection behaves. Agreed field by field with the
FiniexTestingIDE over three rounds (2026-08-19 / 08-20) and closed on the two remaining questions on
2026-08-27.

Worked example, generated from real journal envelopes:
[`STREAM_FRAMES_SAMPLE.sse`](STREAM_FRAMES_SAMPLE.sse). It is produced by
`experiments/stream_frames_sample/generate.py` **through the same renderer the live stream uses**
(`core/outcome/stream_frames.py`), so the published sample cannot disagree with the wire. It could
before, and twice did.

---

## The address

```
GET /v1/stream/{pipeline_id}                 Authorization: Bearer <token>
GET /v1/stream/{pipeline_id}?history=N
GET /v1/stream/{pipeline_id}?since=<seq>&epoch=<n>
```

**The pipeline is a path segment, and that is a security property.** Authorization derives the grant
from the matched route's first path parameter (`pipelines:<pipeline_id>`), so a `?pipeline=` form
would be *authenticated but ungated* — reachable by any valid token, including one entitled to
nothing — and invisible to the suite's walk over identity routes, which is the guard built to make
exactly that unforgettable. No new grant surface: a stream **is** the pipeline's series, so the grant
that governs `/latest` governs this.

| Parameter | Meaning |
|---|---|
| *(none)* | `history=1` — the connect snapshot |
| `history=N` | N frames before live, bounded by `replay_window_hours`. `history=0` is live only |
| `since=<seq>&epoch=<n>` | replay ascending from **`since + 1`**, then live. Suppresses the snapshot |

**`since` is exclusive: `?since=N` serves `N+1` onward, never `N`.** So the cursor a consumer stores
is the last `seq` they *accepted*, and they resume with exactly that number. Written down because it
was load-bearing and unstated: the consumer's whole cursor arithmetic rests on it — after
`replay_truncated` they set the cursor to `oldest_available_seq - 1`, and after `epoch_changed` they
resume at `head_seq - 1` so the new epoch's newest envelope is not skipped. The failure mode of the
other reading is quiet rather than loud: one envelope duplicated per reconnect, harmless for the
series and wrong for every count. They had it right, from measurement rather than from the contract
(2026-08-27) — which is the kind of agreement that holds until someone reimplements it.

Refusals, never silent precedences:

| Condition | Answer |
|---|---|
| unknown `pipeline_id` | `404` — "exists but idle" and "does not exist" are different operator situations |
| `history` **and** `since` | `400` |
| `since` without `epoch`, or `epoch` without `since` | `400` — a cursor is `(epoch, seq)` |
| transport switched off (`stream.enabled: false`) | `503` — "not now", where `404` would say "never" |
| absent, malformed or unknown token | `401` + `WWW-Authenticate: Bearer` — **not** a transport failure |
| token lacks `pipelines:<id>` | `403`, naming what it does hold |

## The frame

```
event: signal
data: {"schema_version":"2.0","seq":1041,"stream_epoch":1,...}
<blank line>
```

- **Named events throughout** — `signal`, `heartbeat`, `control`. Never the default event.
- **One `data:` line of compact JSON**, and **a blank line terminates it**: per the SSE
  specification that blank line is what *dispatches* the event, so a frame without one is buffered
  by a conforming client indefinitely.
- **No `id:` line.** Emitting one would make a conforming client send `Last-Event-ID` on reconnect, a
  header the engine does not honour — `?since=` is the only cursor. A header sent anyway is ignored.
- **`retry: 5000`** once at stream open. A **default** for a client with no policy of its own, not
  authoritative: the consumer's own backoff bounds govern.
- **`reasoning` is model-written text and contains newlines.** They are escaped inside the JSON
  string, so a `data:` line never carries a literal one — asserted at the renderer, because a
  pretty-printer added later would split frames only under load.
- **Real frame size, measured over the live API on 2026-08-27: 38.3 kB** (`crypto_sentiment`, 9
  rows, 87 source refs) and **36.9 kB** (`forex_macro_sentiment`, 8 rows, 84 refs). That is
  **≈5.5 MB/day/stream** at M10, not the ~13.5 kB / ~1.02 MB the earlier contract text carried —
  size buffers and archive growth against the measured numbers.

### The frame is the stored envelope, verbatim

One pass → one envelope → one frame, whatever the trigger. Two symbols transitioning in the same pass
do **not** produce two frames. A projected frame would be a derived view of the stored object, i.e. a
place where live and archive can drift silently — so the frame is the stored JSON, never
re-validated on its way out (a model default would rewrite an archived line, and the parity claim
would become a claim about the model).

### Nothing is ever filtered out

Scheduled passes, out-of-band breaking passes, boot passes and `status: 'error'` envelopes are all
frames. `is_breaking` is a field, not a channel. The series is promised gapless and the consumer
treats a `seq` gap as loss **immediately, with no grace period**, so anything withheld punches a hole
indistinguishable from a dropped frame and fires their recovery for nothing. An `error` envelope is a
frame because its `seq` exists.

## `control` — one code, one handler

```
event: control
data: {"code":"live","stream_epoch":1,"head_seq":1041}
```

An out-of-band condition travels as a `code` rather than as one event name per case, so a new
condition is additive and the consumer keeps one handler.

| Code | Meaning | Terminal |
|---|---|---|
| `live` | the replay ended; everything after this is live. Emitted **once**, so it is never inferred from a pause | no |
| `replay_truncated` | `since` was older than the window; carries `oldest_available_seq` and the replay continues from there | no |
| `cursor_ahead` | `since` is beyond our head — **the consumer** rewound | **yes** |
| `epoch_changed` | the series was rewound on **our** side; carries `previous_epoch` | **yes** |
| `auth_revoked` | the credential died mid-stream | **yes** |

`epoch_changed` and `cursor_ahead` stay distinct on purpose: same remedy (reconnect), different
diagnosis and different operator alert. Both are terminal on the connect path *and* mid-stream, which
gives a consumer exactly **one** resync path — the connect path — and no second handler inside their
live loop.

**`auth_revoked` is not reachable yet.** The token registry is loaded at boot, so revocation
currently means a restart, and a restart closes every connection anyway. The code exists and is
specified; a config reload (#115) is what makes it fire.

## `heartbeat` — a connection watchdog, never a freshness claim

```
event: heartbeat
data: {"stream_epoch":1,"seq":1043,"available_msc":1787705420265,"now_msc":1787705462148}
```

- A **named event**, not a `: ping` comment: SSE comment lines are discarded by conforming clients
  per spec, so a comment cannot carry state — and this one carries `seq`, which is how a stalled
  producer is told from a healthy connection.
- **Every `stream.heartbeat_seconds` on every view**, including the ~10-minute cadence view. Without
  it a consumer's watchdog would have to exceed a pass interval, and a dead socket would go unnoticed
  for longer than a pass.
- `now_msc` is server time at emission, so `(available_msc, now_msc)` makes clock skew between the
  two hosts *measurable* rather than a matter of blame.
- On a **cold stream** `available_msc` is absent (nothing became fetchable yet) and `seq` is `0`,
  which cannot collide with a real position because the counter returns `seq + 1`.

## `stream_epoch` on every frame — with no exception

Heartbeats and control frames included. The consumer's cursor is `(stream_epoch, seq)`, and the
restore case runs *overnight*, when only heartbeats carry a number — so the epoch would be missing
from exactly the frames that would explain a backwards jump. Uniform rather than "every frame with a
`seq`", because a rule with an exception is a rule someone violates while believing they comply.

`stream_epoch: 0` means **"not known yet"** — the sequencer has no counter row for this stream — and
never "epoch zero". A consumer adopts the first real epoch it sees rather than reading the change as
a rewind.

## The numbers the engine serves, so nobody configures a second answer

`GET /v1/pipelines` carries them. Engine-wide facts at the response level, per-stream facts on the
row — a per-row copy of an engine fact would claim to be a per-stream property, and someone would
eventually set two of them differently.

```json
{ "stream": { "heartbeat_seconds": 20, "replay_window_hours": 24 },
  "pipelines": [ { "pipeline_id": "crypto_sentiment", "cadence_seconds": 600, "...": "..." } ] }
```

Both `stream` fields are **mandatory**: the consumer lets the served value govern and keeps only
their own multiple (a 3× connection watchdog), so a null would put a branch in their code for a state
the engine cannot be in. `cadence_seconds` is the same number `/v1/health`'s `eval:<pipeline_id>`
worker reports — one derivation, `TriggerConfig.cadence_seconds`, pinned by a test that walks both
surfaces.

## Delivery: one reader, forward by `seq`

Passes only commit. A single dispatcher walks the journal by `seq` — woken by `LISTEN/NOTIFY`, which
PostgreSQL delivers on COMMIT — and fans out to subscribers.

The obvious alternative (each pass enqueues its own envelope after committing) is ordered in the store
and **unordered on the wire**: two passes committing 40 ms apart can enqueue in reverse order if the
first thread is descheduled between COMMIT and the enqueue. A consumer treating a gap as immediate
loss would declare the earlier `seq` lost and drop it when it arrives below their cursor. One reader
makes wire order equal `seq` order **by construction**, so no grace period is needed anywhere.

Three properties follow:

- **the crash window closes on its own** — an envelope committed by a process that died before
  pushing is read on the next advance;
- **backpressure is isolated from the engine** — the dispatcher is not a pass thread, so a slow
  consumer cannot delay a pass. A subscriber whose bounded queue fills is **dropped**, never
  accommodated; the resulting `seq` gap is visible and recoverable with `?since=`;
- **a lost notification costs a delay, not a stall** — the dispatcher also sweeps forward every
  `stream.fallback_poll_seconds`.

The dispatcher runs whenever there is a journal, **with or without `--workers`**: the stream is a
read surface, so a dev instance can serve this contract against a journal another process writes
without making a single paid call.

## The subscribe race (RC-1)

A pass committing between the snapshot read and the subscriber's registration would reach nobody. So
the order is: **register, buffer, snapshot, then discard from the buffer everything the snapshot
already carried** (`seq <= last replayed`). Unimplementable without `seq`.

## The range endpoint — same decision, different rendering

```
GET /v1/pipelines/{pipeline_id}/envelopes?since=<seq>&epoch=<n>&limit=N
```

The collector's catch-up path, and its own address because it is its own question. `/latest` cannot
answer it: everything produced between two polls that is no longer newest at poll time is never
fetched — systematically the out-of-band breaking passes.

It shares the replay policy with the stream, so the two surfaces cannot disagree about a cursor. The
mapping rule:

| On the stream | Here |
|---|---|
| terminal control code (`epoch_changed`, `cursor_ahead`) | **`409`**, `detail.code` naming which |
| non-terminal marker (`replay_truncated`) | a **body field** (`truncated`, `oldest_available_seq`) |
| a journal that cannot be read | `503` |

`since` and `epoch` are both **required** here — there is no other way to call the route.

## Two bounds on a replay, not one

`replay_window_hours` bounds **age**. `stream.max_replay_frames` bounds **volume**, and it is not
redundant: a window that holds nothing — a quiet weekend, a stream that stopped days ago — clamps
nothing at all, so a cursor far in the past would replay the whole tail in one burst. Measured
against the dev journal: 164 envelopes at ~34 kB is 5.5 MB, and the same shape on a
production-length series is orders of magnitude worse. `max_replay_frames` defaults to **200** —
just above one M10 day (144 frames) with headroom, ≈7.7 MB at the measured frame size.

Whichever floor bites harder wins, and the caller is told through the **same** `replay_truncated`
marker either way — because the remedy is the same: fetch the span between `requested_since` and
`oldest_available_seq` from the journal export. No new field, no new code, no branch on the
consumer's side.

One interaction worth knowing: on a cadence faster than M10 the volume bound can bite *before* the
age bound (24 h at M1 is 1,440 frames). That is intended — the point is to bound a reconnect burst
regardless of cadence, and the marker makes it visible rather than surprising.

## Escalation beyond the window

| Gap | Path |
|---|---|
| ≤ `replay_window_hours` | `?since=<seq>` — the stream's replay or the range endpoint |
| larger, entirely in **closed** buckets | the journal export (#62) — complete by construction |
| larger, reaching into the **current** bucket | the export with `include_open` |

An outage longer than a day is an operator event, not something a client self-heals through.

## Field tiers — the complete row set, and why this section exists

#9 §4 draws the tier table for the envelope. It was drawn once, and two later issues added **row**
fields without assigning them a tier: `base_currency` / `quote_currency` (#70) and `breaking_reason`
(#64 Phase 2). The consumer found the gap by diffing every envelope against their declared field
names — three fields arriving that no tier covered — which is a better way to find it than the
alternative, and the alternative is a consumer deciding a field's meaning by reading it.

So the complete `result[]` row tiering, and it lives here rather than in the issue because this is
the document a consumer reads:

| Row field | Tier | Note |
|---|---|---|
| `symbol`, `signal`, `sentiment_score`, `confidence`, `reasoning`, `urgency`, `is_breaking`, `basis` | **2** | consumed at runtime |
| `evidence_as_of` | **2** | absent exactly when the row rests on no evidence, which coincides with `basis: 'no_data'` |
| `breaking_episode_id`, `breaking_episode_start` | **3** | episode identity (#65) — correlation, never a dedupe key |
| `base_currency`, `quote_currency` | **4** | the instrument's pair legs, attached by the engine from the `SymbolSpec` and never scored by the model. **Assigned Tier 3 on 2026-08-27 and corrected to 4 the same day** — see below |
| `breaking_reason` | **4** | the model's purpose-built breaking line — a display string that moves with the prompt. Free to evolve, and deliberately not part of what a decision rests on |

And the rule that keeps this complete: **a new row field is assigned a tier in the change that adds
it.** A tier table drawn once is a table that goes stale in exactly the way this one did.

### Why the pair legs are Tier 4, and the reasoning is the consumer's

They were assigned Tier 3 for a consumer's benefit — a free cross-check at parse time against the
broker's own symbol specification. The consumer declined, with a better argument than the one that
made the assignment: taking our split would be **a second answer to a question already answered
authoritatively**, and if a cross-check is ever wanted it belongs at *import* time — validate and
refuse — where it needs no runtime field at all.

So Tier 3 would have been a coordination cost on a field nobody consumes. The general form is worth
keeping, because the assignment was made in good faith and was still wrong: **a tier is earned by
what a consumer consumes, not by what might be useful to them.** Guessing upward looks generous and
buys a coordinated break for nothing.

Their own rule, recorded because it explains what will and will not be consumed in future:

> The live envelope model and the parquet projection are **one contract**. A field is consumed in
> both or in neither — the only exception being producer or transport *health*, which never rides on
> the runtime envelope. **Presence is reach**: once a field sits on a runtime snapshot it is within
> reach of decision logic whether anyone intended that or not, so "we only look at it" does not save
> it.

`available_msc_resyncs` / `available_msc_max_correction_ms` stay **Tier 3**: they were settled as
such on 2026-08-20 and are two integers. The consumer has since said they will not consume them —
their own resolution gate already clamps a backwards stamp, and what they lacked was their own
*count*, not our field. So these are ours now, for our own reporting; a settled Tier 3 field is not
downgraded for a change in who reads it.

### What the 38 kB actually is

Measured over the live API, 2026-08-27, so the sizing conversation aims at the right target:

| Share of a production envelope | `crypto_sentiment` | `forex_macro_sentiment` |
|---|---|---|
| `sources[]` (86 / 84 article refs) | **73.4 %** | **74.0 %** |
| `metadata` | 13.8 % | 13.9 % |
| `reasoning` text | 3.2 % | 3.3 % |
| the five fields the consumer discards | **2.0 %** | 2.0 % |

So the envelope **is** `sources[]`, and roughly 87 % of every frame is Tier 4 provenance and
diagnostics rather than anything a decision rests on. That is deliberate — provenance is why an
outcome can be traced back to the articles that produced it (#2) — and it means a future request to
cut transport volume has exactly one lever worth pulling, not five. It also means such a request
cannot be answered by trimming fields: §3.2 forbids a projected frame, because that is where live
and archive drift silently.

One consequence of the tier rule worth stating, because the consumer met it: **Tier 1–3 is
unconditional on the *wire*, not on an archive line.** A live frame cannot predate a field, so an
absent Tier 1–3 field on a frame is unreadable rather than old. An archived row *can* predate one —
which is why a `?since=` replay reaching back before a field existed legitimately carries rows
without it. That is the archive rule, not a contract violation, and the two must not be conflated.

## Not implemented, deliberately

`&project=symbols` and `symbols=` (row projection for a thin client). No consumer asks for it, and a
projection is precisely where live and archive can drift silently — the envelope already carries two
per-symbol maps that would need explicit projection and will gain more. It stays in #9's Definition
of Done rather than being quietly dropped.
