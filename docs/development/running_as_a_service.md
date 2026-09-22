# Running the engine as a service

**Why this page exists, in one measurement.** On 2026-09-20 the host reset the machine. The reset
cost fifteen minutes; the engine was down **12 h 50 m 46 s** (19:40:14Z → 08:39:51Z, ~77 envelopes
missing per stream), because it ran on a console window somebody had started by hand while Caddy — a
*service* — came back on its own. **The host event was 2 % of the outage.** Nobody noticed for twelve
hours: every guard the engine owns (`/v1/health`, the stall watchdog #75, the dead-worker check #97)
runs inside the process and goes silent with it.

A service manager removes the cause. Everything below is the parameters, the traps, and the one test
that actually proves it.

## The display does not come along, and that is deliberate

The service runs **without `--live`**. Two reasons, one measured on this same box by the
FiniexDataCollector on 2026-09-21: a Rich frame cost up to **3.9 s**, and with the display on there
were **21 stalls over 500 ms in eight minutes** against **0** with it off. Anything timestamped on the
loop that also draws carries those milliseconds. A console in QuickEdit mode adds a second coupling —
it suspends the next write while text is selected, which cost them a 24-second stall on 2026-09-18.

What replaces the dashboard until the viewer ships (#126 Phase 2):

| Question | Where |
|---|---|
| is it alive, what are the workers doing | `GET /v1/health` |
| which code is running | `GET /v1/build` |
| what happened in the last hour | `logs/finiex.log` (rotating, unchanged) or `GET /v1/logs/engine` |
| what did the last pass produce | `GET /v1/pipelines/{id}/latest` |
| everything else | the report routes — `docs/architecture/report_api.md` |
| what the panel *would* be showing | `GET /v1/dashboard/engine` — one reading of the live state, stamped with the engine's clock. This is the **engine half** of the viewer (ISSUE_126 Phase 2); the process that draws it is not built yet |

Starting it by hand with `--live` stays available for debugging. It is simply not what the service
runs.

## NSSM, mirroring Caddy

Caddy already runs under NSSM from `C:\nssm\nssm.exe` on this machine, startup type **Automatic
(Delayed Start)**. Mirror it rather than invent a second pattern.

| Parameter | Value | Why |
|---|---|---|
| `Application` | `<checkout>\.venv\Scripts\python.exe` | the venv's interpreter directly — no activation, no shell |
| `AppParameters` | `-m finiexragengine.cli.server_cli --workers` | **module form**, never a file path: running a file puts that file's directory on `sys.path` instead of the project root. **No `--live`.** |
| `AppDirectory` | `<checkout>` | the project root, so relative paths (`logs/`, `configs/`, `migrations/`) resolve |
| `AppEnvironmentExtra` | `PYTHONUTF8=1`, `DATABASE_URL=…`, `OPENAI_API_KEY=…` | **see the trap below** |
| Startup type | Automatic (Delayed Start) | same as Caddy; the database and the network are up first |
| `AppStopMethodConsole` | `330000` | see the stop section — **ours, not the collector's number** |
| `AppExit 2 Exit` | — | exit 2 is a refusal, not a crash; do not restart it |
| `AppStdout` / `AppStderr` | a file under `logs/` | catches whatever is printed *before* logging is configured — the rotating log cannot |

### The trap: a service has no shell

Every entry point reads `DATABASE_URL` and `OPENAI_API_KEY` from the **process environment**, and a
native venv run does **not** read `.env` — that file is only injected by `docker compose`. On the
console those variables come from the shell that started the process. **A service has no shell**, so
without `AppEnvironmentExtra` the service starts, fails with `DATABASE_URL is not set`, exits 2 and
stays down. Correct behaviour, confusing symptom.

Note what this means for #102 (secrets out of the startup script): the service definition becomes a
place a credential lives. That is the registry rather than a pasteable command line, which is better,
and it is still a copy — #102's `SettingResolver` is where it ends.

## As installed on the production box — 2026-09-22

The record of what was actually run, so a rebuild reproduces it instead of re-deriving it. Secrets
appear as placeholders; the real values live in the service's own environment and in the gitignored
server notes.

### First, what the environment actually held

```powershell
"Machine","User" | % { "$_ : DB=" + [bool][Environment]::GetEnvironmentVariable("DATABASE_URL",$_) +
                       " KEY=" + [bool][Environment]::GetEnvironmentVariable("OPENAI_API_KEY",$_) }

Machine : DB=False KEY=False
User    : DB=False KEY=True
```

**`DATABASE_URL` was persisted nowhere at all.** It had been typed into whichever shell started the
engine, which is exactly why the console worked for months and a service could not have. That one
line is the whole argument for `AppEnvironmentExtra`, measured rather than assumed — re-run it before
any future rebuild instead of trusting this paragraph.

### The install, as executed

```powershell
C:\nssm\nssm.exe install FiniexRAGEngine `
  "C:\Users\Administrator\Documents\code\FiniexRAGEngine\.venv\Scripts\python.exe" `
  "-m finiexragengine.cli.server_cli --workers"

C:\nssm\nssm.exe set FiniexRAGEngine AppDirectory         "C:\Users\Administrator\Documents\code\FiniexRAGEngine"
C:\nssm\nssm.exe set FiniexRAGEngine AppEnvironmentExtra  "OPENAI_API_KEY=sk-…" "DATABASE_URL=postgresql://…@localhost:5432/…" "SSL_CERT_FILE=C:\Users\…\cacert.pem"
C:\nssm\nssm.exe set FiniexRAGEngine AppStopMethodConsole 20000
C:\nssm\nssm.exe set FiniexRAGEngine AppExit 2 Exit
C:\nssm\nssm.exe set FiniexRAGEngine Start SERVICE_DELAYED_AUTO_START

# added right after the first start — see "what a refusal would have been invisible in", below
C:\nssm\nssm.exe set FiniexRAGEngine AppStdout "…\FiniexRAGEngine\logs\service.out.log"
C:\nssm\nssm.exe set FiniexRAGEngine AppStderr "…\FiniexRAGEngine\logs\service.err.log"
```

**Without `AppStdout`/`AppStderr`, an exit-2 refusal is invisible.** Everything printed before logging
is configured — including the configuration-error message this page's exit codes exist for — goes to
stderr and nowhere else. A service has no console to catch it, so the one text an operator needs
would be replaced by an event-log entry saying the process exited.

Log on as **LocalSystem**, startup **Automatic (Delayed Start)** — the same shape as Caddy beside it.

Two entries in that environment list deserve a word:

- **`SSL_CERT_FILE`** is carried over from the console environment that worked. A service whose CA
  bundle differs from the shell's fails nowhere at boot and everywhere on outbound HTTPS — the feeds
  and the OpenAI call — which is the worst place to discover a difference. Here it points at the
  venv's own bundle (`…\.venv\Lib\site-packages\certifi\cacert.pem`, confirmed present), so it
  survives a `pip install` and dies with a venv rebuilt at a different path.
- **`PYTHONUTF8=1`** is belt-and-braces here and not load-bearing: `use_utf8_output()` reconfigures
  stdout/stderr at the top of every CLI, and the rotating file handler pins `encoding='utf-8'`
  explicitly. Set it anyway; it costs nothing and removes a class of question.

Before the switch, stop the running console and let it drain. Nothing yet prevents both from running
at once — that is the run lock, which ships after this page's reboot test.

### Verified from outside, immediately after the start

```
/v1/build   started_at 2026-09-22T11:30:41Z      auth_package 0.3.0, not editable
/v1/health  ok · instance_id 1dcb470e3d17 · journal 138c68e48b15 · production
            ingest:crypto_news  ok · ingest:forex_news  ok
            eval:crypto_sentiment ok · eval:forex_macro_sentiment ok
            eval:crypto_sentiment_nano pending   (its pass takes ~100 s)
            budget not suspended · no stalls · stream listening · RSS 198 MB
```

And the boot lines, which are the half that says the *switch* changed nothing it should not have
(`min_level=INFO` — the route defaults to `WARNING` and would show none of these):

```
[SETTING] FINIEX_API_TOKENS <- user_configs
[AUTH] 2 consumer token(s) · /health public · POST /run DISABLED
[AUTH] token ide        · grants: pipelines:*
[AUTH] token claude-dev · grants: *
[JOURNAL] 138c68e48b15 · production
```

**That `[SETTING]` line is the one to read after any move to a service.** The environment form of
`FINIEX_API_TOKENS` means `*` and *wins* over the overlay, so a machine-scoped leftover would have
silently replaced the whole grant model — in either direction, since a user-scoped one is visible to
a console and invisible to LocalSystem. Here the overlay is in force and the consumer list is
unchanged.

### Two findings from the switch

**`/v1/build` lost the commit — solved, and the cause is worth keeping.** Under the service it
reported `commit: null` where the console had reported a hash, with the boot log saying
`[BUILD] … commit not determinable (no git repository here)`. `build_info` shells out to
`git -C <root> rev-parse`, and as LocalSystem that can fail two ways: git missing from that account's
PATH, or git refusing a repository owned by another user. The reason is logged at `debug` only, so it
was settled by elimination — `[Environment]::GetEnvironmentVariable("PATH","Machine")` contains
`C:\Program Files\Git\cmd`, so git was findable and **ownership was the cause**:

```powershell
git config --system --add safe.directory "C:/Users/Administrator/Documents/code/FiniexRAGEngine"
Restart-Service FiniexRAGEngine
```

After that, `/v1/build` reports the commit again, `dirty: false`, and the boot log reads
`[BUILD] version 0.3.3 · commit b52e5ac · finiex_auth 0.3.0` — no "WORKING TREE DIRTY", no
"(EDITABLE)", which is what a production install should look like.

This matters more than it looks: *"is the process the commit I just pushed?"* is the first step of
the deploy check, and `started_at` without a commit is the single most common false "it's live".
**Re-run it after any change of service account or checkout location** — it is a per-user refusal,
so it comes back the moment either moves.

**Stopping the old console left a traceback.** `KeyboardInterrupt` inside `threading._shutdown`, at
interpreter exit after the lifespan had already drained — a non-daemon worker thread being joined
when the second interrupt arrived. Cosmetic today, and worth a look if it ever appears under
`nssm stop`, because there it would mean the drain did not finish before the console event landed.

### The stop, measured on the first restart

`Restart-Service` printed "Waiting for service to stop..." three times, which is the drain rather
than a hang. The log shows it completing in order:

```
11:38:22  Scheduler has been shut down
11:38:25  workers stopped (5)
11:38:36  [IDENTITY] instance 1dcb470e3d17          ← the new process
11:38:44  [JOURNAL] 138c68e48b15 · production
11:38:49  [BUILD] version 0.3.3 · commit b52e5ac · finiex_auth 0.3.0
11:38:49  five workers started
```

**Total gap: 24 s**, of which roughly 3–5 s was the drain. That is the case NSSM's default 1500 ms
stop timeout would have cut short — so `AppStopMethodConsole 20000` earned itself on the first
restart rather than in theory. It also means the Ctrl+C path works end to end under the service: the
event arrived, the handler ran, the lifespan drained in its declared order.

### What can be checked without a reboot

A reboot is not free on this box — it also takes down whatever else runs there by hand — so most of
the confidence is available without one:

```powershell
sc.exe qc FiniexRAGEngine            # START_TYPE must read AUTO_START (DELAYED)
C:\nssm\nssm.exe dump FiniexRAGEngine   # the full configuration, as the commands that recreate it
```

`nssm dump` is the authoritative as-built record — **and it prints the API key**, so redact before
pasting it anywhere. Everything it shows lives in the registry under
`HKLM\SYSTEM\CurrentControlSet\Services\FiniexRAGEngine\Parameters` and survives a reboot; the
`safe.directory` entry is not NSSM at all but `C:\ProgramData\Git\config`, and survives for the same
reason.

**Logging off is the cheap two-thirds of the test.** A service survives an ended session; a console
process does not. Log off — not disconnect — wait, and query `/v1/health` from outside. It proves the
engine is no longer tied to an interactive session, which is most of what a reboot proves. The catch
is that it kills anything else on the box that still runs on a console, so it waits until those are
services too.

**A database that is not up yet is not a problem to solve here.** If PostgreSQL lags the engine at
boot, the schema guard raises, the process exits **1** — retryable, deliberately not 2 — and NSSM
restarts it with a back-off that reaches about 256 s. A Windows service dependency would remove the
race outright; the retry survives it, which was judged enough.

### The reboot checklist

Written in advance so the reboot is a test rather than a restart. **Run it before 09:00 UTC**: the
weekly report is scheduled for Saturdays at 09:00, so an engine that booted earlier reschedules it
for the same morning — and a report that then fires proves the scheduler came back too. Reboot after
09:00 and that check slides a week.

| When | Check | Expected |
|---|---|---|
| T+0 | `Restart-Computer` | — |
| T+3 min | `GET /v1/build` | `started_at` after the boot · a commit, **not `null`** · `dirty: false` |
| T+3 min | `GET /v1/health` | five workers, the known `instance_id`, `journal_id`, `environment: production` |
| T+5 min | `GET /v1/logs/engine?min_level=INFO` | `[IDENTITY]` · `[JOURNAL]` · `[BUILD]` · three `[AUTH]` lines |
| T+5 min | `logs\service.err.log` | empty, or exactly the retryable database line |
| 09:00 | the weekly report fires | the scheduler survived the boot |

**Check from outside first, then log in and watch.** An earlier version of this page said nobody may
log in until the checks pass; that is too strict and it would rule out attending the reboot at all. A
logon cannot start a service retroactively, so the service either came back on its own or it did not.
What matters is that the verdict comes from `/v1/build` **off the machine**, where a session cannot
have influenced it.

Two negatives worth naming, because each has a known cause: a `commit: null` means the
`safe.directory` entry did not survive (it should — it is system-wide), and a second set of workers
means the run lock did not hold.

**This box runs things that are not services**, and they do not come back without an interactive
logon — the MT5 terminal starts from the user's Startup folder, which runs at logon and not at boot.
Whether that matters depends on `AutoAdminLogon`:

```powershell
Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" |
  Select-Object AutoAdminLogon, DefaultUserName
```

Not this engine's dependency — it holds no price and reads no terminal — but it is on the same
machine, so a reboot planned from here should know what else it takes down.

### Still unproven

**The reboot.** Everything above shows the service starts, serves and stops cleanly; none of it
shows the machine brings it back on its own. Planned as a controlled test on **Saturday 2026-09-26,
before 09:00 UTC**. Until it has run, this page documents a service, not a solved outage.

**`PYTHONUTF8=1`** is not confirmed to be in the installed environment list. Harmless either way for
the reasons above, and worth setting when the list is next edited.

## Stopping, and why the timeout is generous

NSSM stops a service with `AppStopMethodConsole`: it attaches to the process's console and raises a
Ctrl+C there. Two things had to be true for that to work:

1. **The engine removes an inherited "ignore Ctrl+C" state** at startup
   (`utils/console_ctrl.py`). That setting is inherited from whatever launched the process; with it
   in place the console *accepts* the event, no handler runs, and nothing is logged anywhere. The
   collector reproduced exactly this against their real process: it ran on for a full minute.
2. **The timeout is 330 s, and the number is ours rather than borrowed.** It started at 20 s, from
   the collector's measured stops of 0.15 s and 5.1 s — their workload, not ours. Two of our own
   restarts then measured **3–5 s** and **8 s**, and the second one showed why the ceiling matters:
   the drain ended in the same second the `crypto_sentiment_nano` pass completed. It had been
   waiting for it, correctly.

   That pass takes **~100 s**, and `pass_timeout_seconds` bounds any pass at **300 s**. So a stop
   landing early in a nano pass needs far more than 20 s of drain, and NSSM would have terminated a
   pass the engine was still legitimately finishing — survivable, because every envelope commits in
   its own transaction, but it costs that pass's envelope and skips the ordered shutdown. 330 s sits
   just above the engine's own bound.

   **A ceiling is not a wait.** Raising it does not make a restart slower: a normal stop still takes
   seconds. It only means a genuinely hung process takes five and a half minutes to be killed, and
   the stall watchdog is what notices that case.

A clean stop drains in the order `api_app.lifespan` declares: weekly scheduler → command poller →
stall watchdog → workers → stream dispatcher → live display. Read the tail of the log after a stop and
confirm those lines; their absence means the process was terminated rather than stopped.

**A hard kill is survivable and always was.** Every envelope commits in its own transaction together
with its `seq` mint and its episode rows, so the cost is the pass in flight — never a buffer. That is
why no write-ahead log is needed here.

## Exit codes

| Code | Meaning | What the manager should do |
|---|---|---|
| `0` | stopped on request | nothing |
| `1` | crashed — **including a database that is not up yet**, which is the one failure here that a restart actually fixes | restart |
| `2` | **this must not run** — a configuration refusal (schema behind, instance identity missing or malformed, token problem), or **another live process already owns this journal's worker role** | **do not restart** (`AppExit 2 Exit`) |

None of the exit-2 causes improves by being retried, and a restart loop over one of them buries the
actual message under identical log entries.

## The acceptance test is a reboot

Not a start, not a stop. **Restart the machine and confirm `/v1/health` answers without anybody
logging in.** That is the only test that measures the thing this page is about — the collector's own
service step is explicitly unproven for exactly this reason.

```powershell
sc.exe qc FiniexRAGEngine          # START_TYPE must read AUTO_START (DELAYED)
Restart-Computer
# from anywhere, once the box is back:
curl https://finiex-rag.duckdns.org/v1/build      # started_at must be after the reboot
```

**When `Start-Service` fails without a reason**, read the event log rather than retrying — NSSM
reports the underlying cause there and not on the console:

```powershell
Get-WinEvent -LogName Application -MaxEvents 50 |
  Where-Object { $_.Message -match 'nssm|FiniexRAGEngine' } |
  Format-List TimeCreated, Message
```

## Linux

Written beside the Windows parameters rather than retrofitted: nothing in this tree is Windows-bound
except the console handling above, which is a no-op elsewhere.

```ini
[Unit]
Description=FiniexRAGEngine
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/finiexragengine
Environment=PYTHONUTF8=1
EnvironmentFile=/etc/finiexragengine.env
ExecStart=/opt/finiexragengine/.venv/bin/python -m finiexragengine.cli.server_cli --workers
Restart=on-failure
RestartPreventExitStatus=2
TimeoutStopSec=20
KillSignal=SIGINT

[Install]
WantedBy=multi-user.target
```

`RestartPreventExitStatus=2` is the systemd spelling of `AppExit 2 Exit`; `KillSignal=SIGINT` gives
uvicorn the same orderly shutdown the console Ctrl+C triggers on Windows.

## What this page does not cover

**Liveness from outside.** A service that restarts still needs somebody outside the process to notice
when it does not — `/v1/health` cannot report that it is unreachable. That is its own small piece of
work, not part of the service.
