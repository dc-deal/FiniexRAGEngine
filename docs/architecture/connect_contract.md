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
{ "api": { "tokens": { "ide": "<token>" } } }
```

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

## Diagnostics: `GET /v1/reports`

Token-gated like everything else. It serves the engine's own metrics surfaces — source health and
quarantine history, fetch latency, the breaking funnel — as JSON, so a question about the live
engine's behaviour no longer needs a session on the host. Deliberately **not** part of the frame
contract a collector builds against: the shapes are diagnostic and stay free to change. Details in
`report_api.md`.

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
