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
| `AppStopMethodConsole` | `20000` | see the stop section |
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

## Stopping, and why the timeout is generous

NSSM stops a service with `AppStopMethodConsole`: it attaches to the process's console and raises a
Ctrl+C there. Two things had to be true for that to work:

1. **The engine removes an inherited "ignore Ctrl+C" state** at startup
   (`utils/console_ctrl.py`). That setting is inherited from whatever launched the process; with it
   in place the console *accepts* the event, no handler runs, and nothing is logged anywhere. The
   collector reproduced exactly this against their real process: it ran on for a full minute.
2. **The timeout is 20 s, not NSSM's default 1500 ms.** The collector measured graceful stops of
   **0.15 s and 5.1 s** on this box depending on what was in flight. At 1500 ms the second case
   escalates to `TerminateProcess`.

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
| `1` | crashed | restart |
| `2` | **this must not run** — a configuration refusal: schema behind, instance identity missing or malformed, unnamed journal, token problem | **do not restart** (`AppExit 2 Exit`) |

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
