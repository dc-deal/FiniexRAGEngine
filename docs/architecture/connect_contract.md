# How to connect — authentication, tokens and rotation

The other half of the output contract. `output_archive_layout.md` and #9's frame contract say *what
arrives*; this says *how to reach it*. A contract that describes the frames but not the connection
is half a contract, and the half that is missing is the one that fails at 3 a.m.

It ships with **#98** rather than with #9's handshake document, deliberately. The consumer's own
sequencing hands them a token three phases before that document would otherwise exist — so for
three phases they would hold a credential whose lifetime and rotation procedure were written down
nowhere. The rule that follows from it is general: **a token is never issued ahead of the procedure
for rotating it.**

---

## The address

```
https://finiex-rag.duckdns.org
```

**Configure the hostname, never the address behind it.** It is a free DuckDNS record today and may
become a purchased domain later; with a hostname configured, that migration is a DNS change on the
producer's side and nothing on the consumer's. Configuring the IP makes the same move a coordinated
break across two projects.

TLS terminates in a reverse proxy (Caddy, Let's Encrypt certificate, renewed automatically) which
forwards to `127.0.0.1:8100`. The engine binds loopback and never speaks to the internet directly;
port 8100 has no firewall rule and does not get one.

## The development endpoint

A second instance runs in the dev container on the producer's machine, so consumer work can be
pointed at a rebuildable engine instead of the live series.

```
http://host.docker.internal:8100    from a container on the same machine
http://127.0.0.1:8100               from that machine directly
```

**It is not public, and it is not the same engine behind a different name.** The container
publishes the port on the host's *loopback* (`127.0.0.1:8100:8100`), so nothing outside that machine
can reach it — which is also why it speaks plain HTTP: no traffic leaves the host, so there is no
transport to terminate. There is no DNS name and no proxy in front of it, deliberately: giving dev a
public address would mean giving it a certificate, a rate limit and a second thing to keep patched.

**It carries its own token.** A dev instance is restarted, rebuilt and pointed at test data; a
credential shared with production would make revoking either one revoke both. Everything else is
identical by design — bearer on every route except `/v1/health`, `/run` not registered, `/docs` off
— so switching endpoints changes the address and the credential, never the shape of the contract.

**One failure mode is worth recognising, because it does not look like one.** The engine binds
loopback by default, which is correct on the deployed host and wrong inside a container: there it
binds the *container's* loopback and leaves the publish above without an upstream. Docker's port
forwarder still accepts the TCP connection and then closes it immediately, so the port appears open
and answers nothing — no HTTP, no TLS, an EOF before either can begin. The dev launch entries pass
`--host 0.0.0.0` for exactly that reason; exposure stays bounded by the loopback publish, not by the
bind. A consumer seeing an immediate EOF on the dev port is looking at this, not at a network fault.

## The scheme

Every route except `/v1/health` and `/v1/build` requires a bearer token:

```http
GET /v1/pipelines HTTP/1.1
Host: finiex-rag.duckdns.org
Authorization: Bearer <token>
```

| Response | Meaning |
|---|---|
| `200` | authenticated |
| `401` + `WWW-Authenticate: Bearer` | absent, malformed or unknown credential |
| `429` + `Retry-After` | rate limited — see below |
| `403` | authenticated, but this token's `reports` scope does not include that report |
| `404` | the route does not exist (see `POST /run`) |

The `401` body is the same for every cause. Distinguishing "no header" from "unknown token" would
answer a question the caller has no right to ask, and it is exactly what a guesser probes for.

**`401` is not a transport failure.** A consumer that receives it should stop retrying and report a
credential problem — retrying forever against a dead token and calling it an outage is the failure
this distinction prevents (#9 §3.6).

## One token per consumer

Not one shared token. Only a per-consumer token can be revoked without disrupting everyone, and the
Testing IDE is not the only future reader (#42 fan-out, a second collector).

The engine stores a **SHA-256 digest**, never the token. A leaked configuration file, a memory dump
or a support ticket carrying that state is therefore not a leaked credential — the same reason a
provider shows an API key once and never again.

Verification is `hmac.compare_digest` over digests, and the lookup does **not** break on a match:
neither the comparison nor the loop reports anything through response time.

### On the engine side

Two sources, one rule: **the environment wins, the config fills in** (`SettingResolver`). Whichever
answered is announced at boot — `[SETTING] FINIEX_API_TOKENS <- user_configs` — so a value placed in
the overlay and shadowed by a forgotten variable can never be a silent no-op.

**Preferred: the gitignored overlay**, `user_configs/app_config.json`:

```json
{ "api": { "tokens": {
    "ide": { "token": "<token>",
             "grants": ["pipelines:crypto_sentiment", "reports:source_health"],
             "active": true,
             "note": "Testing IDE, issued 2026-08-23" } } } }
```

`grants` is **mandatory** and lists what this consumer may reach, as `<surface>:<name>` —
`pipelines:crypto_sentiment`, `reports:source_health` — with `<surface>:*` for a whole surface and
a bare `*` for everything. A token without it fails at boot rather than defaulting, so access is
granted by writing a name down and never by omission: a surface added later stays out of reach
until someone puts it in a token.

A grant names a **thing**, not a route. `reports:source_health` keeps meaning what it means if the
route is renamed or a `/v2` appears; the alternative — a list of paths — would silently stop
matching and answer you with a `403` for something you were entitled to.

Reaching something outside the grants answers `403`, naming what the token *does* hold. Listing
endpoints (`GET /v1/pipelines`, `GET /v1/reports`) are **filtered** rather than refused, so they
always show exactly what that token can fetch.

`active: false` switches a consumer off without deleting the token — for an incident, or to keep a
superseded token in place through a rotation. `note` records who holds it: the question that
otherwise arrives during a rotation months later.

**Or the environment**, for a container or CI, which have no overlay (`user_configs/` is gitignored,
so a fresh clone has none):

```powershell
$token = python -c "import secrets; print(secrets.token_urlsafe(32))"
[Environment]::SetEnvironmentVariable("FINIEX_API_TOKENS", "ide:$token", "Machine")
```

Generating straight into the variable keeps the value off the screen. `token_urlsafe(32)` is 256
bits from the OS CSPRNG; never `random`, whose Mersenne Twister is reconstructable from a handful
of outputs. Note that `Machine` scope reaches only **new** shells.

**Never in a pasteable startup script.** That is the pattern both of the above replace: a script
carrying credentials in plaintext is a file that cannot be shared — and because it doubles as the
operational cheat-sheet, it eventually is.

The tracked `configs/app_config.json` carries `api.tokens: {}` and keeps carrying an empty one. A
credential in a committed file is a credential in everyone's clone; a test asserts it stays empty.

**The engine refuses to boot** with authentication enabled and no tokens configured. Starting
unprotected because a variable was missing is the accident this whole issue was written about, and
a warning in a log nobody reads is not a control.

## Lifetime

**A token does not expire on its own.** There is no issue date, no TTL and no renewal handshake —
deliberately: an expiry that nobody is watching turns into an outage at the moment it lapses, and
the engine has no channel to warn a consumer in advance.

A token is valid until it is removed from whichever source supplied it. That makes **revocation**, not
expiry, the control — and revocation is immediate and deliberate rather than scheduled and
forgotten.

## Rotation

Rotation is additive, never a swap. The point is that **no window exists in which the consumer has
no working credential**:

1. Generate the new token and add it *beside* the existing one — two entries, two names, both
   valid: `{"ide": "<old>", "ide-next": "<new>"}`, or
   `FINIEX_API_TOKENS="ide:<old>,ide-next:<new>"`.
2. Restart the engine (the registry is read at boot).
3. Hand the new token to the consumer out of band; they switch at their convenience.
4. Confirm the switch — the engine's rejection log names the path, and a consumer still on the old
   token simply keeps working, so there is no deadline to miss.
5. Remove the old entry and restart. The old token is dead from that moment.

**Rotate when a token has actually been exposed** — published, sent through a channel you do not
control, or written into a log or artifact that leaves this machine. Because rotation is additive
and cheap (step 1 plus a restart), there is little reason to deliberate when in doubt.

What it does not call for is treating every sight of the value as a breach. Reading it back on the
machine that holds it, to hand it to the consumer, is ordinary handling — the point is mindful
custody, not ceremony.

## Rate limits

| Scope | Limit |
|---|---|
| `/v1/health` (the only route without a token) | 60 requests/minute |
| failed authentication attempts | 10/minute |

Both are **per originating client**, keyed on the first entry of `X-Forwarded-For` — which the proxy
sets, and which is trustworthy here specifically because the engine binds loopback: the only route
in is through the proxy.

A successful call is never throttled by the failure limit, so a busy consumer cannot rate-limit
itself by working. Exceeding a limit answers `429` with `Retry-After: 60`.

For scale: the consumer's live session probes `/v1/health` once at start and then once per interval
(300 s today), with no burst path and no transport-triggered probes. A 30-minute session spends
seven requests. The limit has roughly two orders of magnitude of headroom.

## `POST /v1/pipelines/{id}/run` does not exist in production

It is the one route that converts an HTTP request directly into OpenAI spend, and it is **not
registered** when disabled — not registered and refusing. A route that answers `403` is still in the
schema, still discoverable, and one config edit from live.

The principle behind it: **an external consumer must not be able to cause spend at all.** The
engine's own workers produce the series, so every paid call originates inside the engine where the
cost log accounts for it. This route was the one hole in that property.

A caller who wants the latest signal uses `GET /v1/pipelines/{id}/latest`, which never spends.

## The live stream and the range endpoint

```
GET /v1/stream/{pipeline_id}                              text/event-stream
GET /v1/pipelines/{pipeline_id}/envelopes?since=&epoch=   application/json
```

Token-gated like everything else, and gated **by name**: both carry `{pipeline_id}` as a path
segment, so the grant checked is `pipelines:<pipeline_id>` — the same one that governs `/latest`. A
stream is the pipeline's series through another transport, not a separate surface.

The pipeline is a path segment rather than `?pipeline=` for that reason. Authorization derives the
grant from the matched route's first path parameter, so a query-parameter form would be
*authenticated but ungated*: reachable by any valid token, including one holding nothing.

Neither route can spend. The stream reads the journal forward; the range endpoint is one bounded
`SELECT`. The only route in the engine that converts a request into provider spend is
`POST /v1/pipelines/{id}/run`, and it is not registered in production.

Field-by-field contract: [`signal_stream_contract.md`](signal_stream_contract.md).

## Diagnostics: `GET /v1/reports`

Token-gated like everything else. It serves the engine's own metrics surfaces — source health and
quarantine history, fetch latency, the breaking funnel — as JSON, so a question about the live
engine's behaviour no longer needs a session on the host. Deliberately **not** part of the frame
contract a collector builds against: the shapes are diagnostic and stay free to change. Details in
`report_api.md`.

## Diagnostics: `GET /v1/logs/{name}`

```
GET /v1/logs/engine?since=&until=&min_level=&limit=      application/json
```

The engine's own log file, over a UTC time range. ISSUE_104 made every *report* answerable over
HTTP; the log was the one diagnostic still behind RDP, and on 2026-09-08 that was the whole gap —
four host-connectivity outages in nine hours whose cause was one word inside a traceback
(`getaddrinfo failed`), while the reports could only say that every feed had failed at once.

**A new grant surface, `logs`,** and no existing token holds it: the surface is declared once on the
router (`Security(build_grant_dependency(tokens), scopes=['logs'])`) and the *name* is the path
parameter, exactly as `reports:<name>` works. `{name}` is checked against a closed set (`engine`
today) rather than being a path — a caller must never be able to name a file, which is the
difference between a log route and an arbitrary read primitive.

| parameter | |
|---|---|
| `since` / `until` | **UTC** bounds; both optional |
| `min_level` | `DEBUG`…`CRITICAL`, default `WARNING` — the file carries thousands of INFO lines a night, and the question this route answers is "what went wrong" |
| `limit` | entries returned, default 200, capped by `max_lines` (2000); the **newest** end is what a limit keeps |

**The clocks differ, and the route is where that is resolved.** The engine is UTC throughout, as
CLAUDE.md requires — but the logging formatter stamps the OS clock, and the server runs GMT+2. One
production line carries both at once:

```
2026-09-08T04:40:43.978+02:00  …  [HOST] host connectivity — … retry 02:45:43 UTC
└─ the formatter: local time                                   └─ the app: UTC
```

Same instant, two clocks. Because the offset is written out the conversion is lossless, so every
line is parsed offset-aware, compared in UTC and **returned in UTC** — a `since` you took from an
envelope, a report or `/v1/health` means what it says. A naive string comparison would be two hours
wrong, silently, which is the exact class of error this route exists to help find.

Three more properties, each because the obvious version is wrong:

- **A rotated file is part of a range.** Rotation is daily at UTC midnight with 14 kept, so a window
  reaching past midnight reads the siblings too — otherwise "query a time range" quietly means
  "today". `files_read` names what was opened.
- **A traceback belongs to its entry.** Continuation lines carry no timestamp, so they travel with
  the entry above them (`continuation[]`) and a filtered window never returns a stack fragment with
  no head — which is what would have made a filtered read useless on 2026-09-08.
- **Redaction is counted, not silent.** DSN passwords, `Bearer …`, `sk-…` and Telegram bot tokens are
  masked with `«redacted»`, and the answer carries `redacted_lines: N`. A reader trusts a log line,
  so an altered one that does not say so is worse than a withheld one.

It cannot spend and it has no write. `matched` (before the limit) and `truncated` say what was left
out, so a bounded answer never reads as a complete one. No CLI: on the box `Get-Content` is already
the better tool — *remote* is the case that was missing.

## Diagnostics: `GET /v1/configs/{name}`

```
GET /v1/configs                 → the documents this caller may read
GET /v1/configs/{name}?id=      → one document, effective and redacted
```

The configuration **this process is running**, for the three domains that have one: `app`,
`pipelines`, `source_sets`. It exists because the layer that differs between two machines is the one
nothing exposed — `user_configs/` is gitignored, so which feeds a machine has switched off, which
model variant is disabled and which detection thresholds it actually uses were readable only on the
host. The `[OVERRIDE]` boot line is a notice rather than an answer: capped at six leaves, and it
never prints a string value.

A new grant surface `configs`, held by nobody until it is written into a token. Three names rather
than one per pipeline, so pipeline ids and source-set ids never share a namespace; `?id=` narrows
within a document and never selects a different one.

**Effective means this process, not this disk.** The views are built at boot over the objects the
engine loaded — the app config manager, the pipeline registry, and the source-set registry the
ingest workers themselves poll from. Nothing is re-read per request, for the same reason `/v1/build`
samples its commit once: a file edited after startup must not make this surface disagree with the
engine that is running.

**Every string is classified before it can be served.** Two layers, and the second is the guard:

- **by path** — `configuration/config_redaction.py` names each string leaf as public or secret. The
  secret list is three entries (`api.tokens.*.token`, `telegram.bot_token`, `telegram.chat_id`),
  because the credentials that matter are not in the config models at all: `DATABASE_URL` and
  `OPENAI_API_KEY` are environment variables. A string the policy does not name is **masked** and
  reported as `unclassified`, and `tests/contracts/test_config_exposure.py` fails the build until
  someone classifies it — so an unclassified field is a short-lived state, not a leak.
- **by pattern** — the same scrubber the log route uses (`finiex_auth.redaction`), for the credential
  that reaches a field nobody expected to hold one: a feed URL carrying its own key in the query
  string is masked although `sources[].url` is legitimately public.

Both halves report what they touched (`redacted`, `unclassified`, `scrubbed`), and the projection
lives in `configuration/abstract_config_view.py` rather than the router — a config document is
exactly the payload where "the route remembered to sanitize" is not a property worth resting on.

The answer also carries `overrides`: which leaves the gitignored overlay moved, with their previous
values, `added` for a key the tracked file never had, and `unknown` for one the schema does not know
(Pydantic ignores unknown keys, so a typo'd override silently does nothing — and the payload says
so). Those values pass the same projection: `user_configs/app_config.json` is precisely the file the
bearer tokens live in.

Two details that only became visible once this ran against production:

- **An `unknown` key's strings are masked but never counted as `unclassified`.** It names no field
  in any model, so it cannot be classified and no contract test can cover it — while a key misfiled
  by hand is exactly where a secret ends up by accident. The `unknown: true` flag is the signal;
  keeping it out of the census leaves `unclassified` meaning one thing only.
- **An unset credential is published as `""`, not as a mask.** Masking an empty field turns "no bot
  token on this machine" into "a bot token you may not see" — one payload for two states an
  operator needs to tell apart, and nothing is protected by hiding an empty string.

It cannot spend and has no write. An unknown `{name}` is a 403 for a scoped caller — authorisation
before resolution, so the endpoint is not an existence oracle — while an unknown `?id=` is a 404,
because absence is only informative to someone entitled to the thing that is absent.

## Diagnostics: `GET /v1/diagnose/{name}`

```
GET /v1/diagnose/feed?source_id=…      → one configured feed, fetched and diagnosed live
```

The feed doctor (ISSUE_11) — a raw GET plus the same feedparser path the ingest worker takes,
classified with the taxonomy source-health records. It exists on this surface because a parse error
names a line and a column **in the bytes that machine received**, and those are not the bytes this
container fetches: on 2026-09-09 `boj_press` was well-formed here (318 lines, 14,722 bytes, line 11
just 51 characters — there is no column 69) and unparseable there. The difference was a response
that never arrived intact, and nothing reachable remotely could say so.

New grant surface `diagnose`; `diagnose:feed` reaches nobody by default.

**This is the first route that reaches *outward* on request.** Every other one reads the journal,
the health tables or a local file. That is a genuine change of kind, so what bounds it is written
down rather than assumed:

- **`source_id` is required and has no default.** The CLI's default is *all* feeds — 39 of them at
  two requests each — and as a GET that shape is a 78-request amplifier that also perturbs the very
  feeds whose health it reports on. One call is one feed is **two outbound requests**, less than the
  engine's own 15-second poll already costs.
- **It is resolved against the configured catalogue** — the source-set registry this process
  loaded. A caller names a feed the engine already polls and can never name a URL, which is what
  separates a diagnostic from an open proxy. An unknown id is a `404` and nothing leaves the
  process; scaffold-mock mode (no catalogue) is a `503` that says so.
- **A 10-second deadline**, half the unit's own default, because a sync endpoint runs in the pool
  that serves every other `def` route.
- **A disabled feed stays probeable**, deliberately: asking whether a switched-off feed has become
  reachable again is precisely a question about a feed nobody is polling.

**Redaction, and it names the field.** `head` carries the first bytes of the remote body — the
answer to "what did that machine actually receive", and therefore arbitrary content this engine did
not write. It, the URL and the parser/transport messages pass the shared scrubber
(`finiex_auth.redaction`), and the response lists `redacted: ["head", "url"]` rather than a count: with
four candidate fields, *which* was altered is the useful half.

It cannot spend and it has no write.

## `GET /v1/build` is the second open route

It reports what code the process is running: `version`, the short `commit`, whether the working tree
was `dirty` at startup, and when the process started. It exists because `version` moves only when a
roadmap batch ships — between two tags every deploy looks identical from outside, so "is the fix I
deployed the one that is running?" was previously answered by inference.

Two properties are deliberate. The value is **sampled once, at startup**: a hash read per request
would describe the working tree at that moment, so after a pull without a restart it would report
the new commit while the old code serves — the field would be wrong in exactly its one real case.
And it is **its own route rather than a field on `/health`**: health describes state and is polled
on an interval, build identity is constant for the process's lifetime, and keeping them apart leaves
the health payload — which a consumer reads — unchanged.

Public here for a specific reason, not a general one: this repository is public, so a commit hash
discloses nothing that is not already readable on GitHub. Behind a private repository the same field
would fingerprint the exact version and therefore its known defects, which is why it is a switch
(`api.build_info_public`) rather than a fixture of the code.

## `GET /v1/health` is deliberately open

An uptime probe needs it without a credential, and that exemption is written down rather than
implied. Note what it publishes: journal identity, worker cadences, budget and stall state. That is
operational information, not a bare `ok`, and the exemption is accepted with that understood.

`docs/architecture/health_contract.md` records which of its fields a consumer depends on, and why
changing them is a coordinated break.
