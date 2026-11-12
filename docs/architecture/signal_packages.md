# Signal packages

Closed UTC days of every pipeline, zipped weekly, fetched by a consumer that confirms each package
with a receipt. The consumer is the Testing IDE's import. It fetches the collector's tick packages
the same way, so the **contract is the collector's** (FiniexDataCollector, `docs/operations/tick_packages.md`):
the same routes, fields and answers, and one client reads both services. Where this service differs,
this document says so.

| | tick packages (collector) | signal packages (this engine) |
|---|---|---|
| source | one per collector (`kraken`, `mt5`) | one per pipeline (`crypto_sentiment`, …) |
| member | one tick file, as the collector wrote it | one pipeline's one UTC day, `<pipeline_id>_<YYYY-MM-DD>.jsonl` |
| what a package is built from | finished files, the only copy | the outcome journal; files can be rebuilt at any time |
| member count field | `tick_count` | `record_count` |
| source key | `collector` (and `source`) | `source` |
| starts in the span | `collector_starts` (and `process_starts`) | `process_starts` |

## How a package is made

A run packs one source to a **cutoff**, a UTC midnight. It takes every closed UTC day before the
cutoff that holds envelopes and that no earlier package of this source names, and writes each as one
member. The **first package of a pipeline is its whole history**. Every later one takes what closed
since.

- **The member's bytes are the export file of that day**, byte for byte: the same bucket read
  (`OutcomeArchiveExporter.read_window`) and the same rendering (`archive_text`) as
  `export_cli` (see `output_archive_layout.md`). Each line is `{collected_msc, collected_msc_timebase,
  …envelope}`, and every line carries the pipeline as `pipeline_id`.
- **The name carries the pipeline**: `crypto_sentiment_2026-11-10.jsonl`, read from the right (the last
  ten characters before `.jsonl` are the day). A file taken out of its zip stays unique next to another
  pipeline's day of the same date.
- **One day in memory at a time.** The journal is read day by day, never whole, so the history
  package costs no more memory than a weekly one.
- **The steps**, each failing loudly:
  1. Refuse a cutoff that is not a passed UTC midnight, and a start inside the quiet minutes.
  2. Take the source's pack lock. Clean what a dead run left behind (a `.part`, or a zip without a
     manifest). Book any committed package the ledger lacks.
  3. Refuse if there is no mint, if the folder cannot be written, or if the disk is below
     `min_free_percent`. The mint is the journal's instance identity, `journal_identity`.
  4. Write `<id>.zip.part`. A day whose lines name another producer than the mint is listed in
     `excluded` with both ids, and is never packed.
  5. Read every member back out of the written zip and compare its name and SHA-256, not only the CRC.
  6. fsync the zip, rename it to `<id>.zip`, then write the manifest atomically. **The manifest is the
     commit**: a package exists from the moment its manifest exists.
  7. Book `packed` in the ledger, and write the source's `last_run.json`.

**Why the journal and not the export folder.** The collector packs files that are its only copy. It
therefore stages its originals, proves them again before deleting, and guards an open-file
pattern. Our files are a rendering of the journal, so none of that is needed. A peek export of a
still-open day (`export_cli --include-open`) can never reach a package, and `held_for_next` and
`open_files` are always empty. They stay in the manifest so one client reads both services.

## Where it runs

The engine keeps the clock and serves the files. **Every run is a child process**, started by
the engine as `python -m finiexragengine.cli.package_cli pack --source … --cutoff … --json`:

- its memory goes back to the operating system when it exits;
- it sets its own priority to below normal, so a pass of the signal pipelines never waits behind it;
- a run that outlives `pack_timeout_seconds` is stopped and recorded as `failed`;
- a run still going when the engine shuts down is stopped too. It leaves a `.part` and a lock behind,
  which the next run cleans up and takes over.

This is the usual split between a web process and a job. A separate service, as the collector runs,
pays off when the work is heavy or the producer must never restart, and neither holds here: a week of
every pipeline is about 11 MB zipped, and the zip of one day takes well under a second.

No route packs. The routes read small JSON files and stream a zip, and the route census in
`tests/packages/test_package_router.py` keeps it that way.

## Folders

```
<output_dir>/<source>/ready/<id>.zip               not yet confirmed
<output_dir>/<source>/ready/<id>.manifest.json     the commit point
<output_dir>/<source>/delivered/…                  the same, once a verified receipt arrived
<output_dir>/<source>/last_run.json                the last run's outcome for this source
<output_dir>/ledger.jsonl                          every step of every package, appended
<output_dir>/schedule.json                         the weekly rule's set-asides and the special runs
```

A delivered package stays listed, and its bytes never change. Nothing is purged: a year of weekly
packages is a few hundred megabytes, and the files can be rebuilt from the journal anyway.

## The manifest

```json
{"schema": 1, "package_id": "crypto_sentiment-0002-20261122", "sequence": 2, "source": "crypto_sentiment",
 "instance_id": "<the journal's mint>", "producer": "finiex-rag-engine",
 "cutoff": "2026-11-22T00:00:00+00:00", "previous_package_id": "crypto_sentiment-0001-20261117",
 "created_at": "2026-11-22T01:30:41+00:00",
 "zip": {"name": "crypto_sentiment-0002-20261122.zip", "size_bytes": 7012345, "sha256": "…", "compression": "deflate"},
 "member_count": 5, "record_count": 761,
 "span": {"from": "2026-11-17T00:00:00+00:00", "until": "2026-11-22T00:00:00+00:00"},
 "members": [{"name": "crypto_sentiment_2026-11-17.jsonl", "size_bytes": 7332110, "sha256": "…",
              "record_count": 153, "first_collected_msc": 1794873600123,
              "last_collected_msc": 1794959400456, "config_fingerprints": ["…"]}],
 "excluded": [], "held_for_next": [], "open_files": [],
 "outages": {"count": 0, "days_missing": [], "records": []},
 "process_starts": {"count": 1, "days_missing": [], "starts": ["2026-11-18T10:02:11+00:00"]},
 "packer": {"program": "finiex-rag-packages", "version": "…", "commit": "…", "dirty": false}}
```

- **The contract.** The top-level keys, and a member's `name`, `size_bytes` and `sha256`, are shared with the collector. `record_count`, the collected-time bounds and `config_fingerprints` are this producer's own.
- **Package ids** have the form `<source>-<sequence>-<cutoff YYYYMMDD>`, and are read from the right because pipeline ids contain underscores. `sequence` and `previous_package_id` chain one source's packages, so a gap shows.
- **The span** runs from the previous package's cutoff, or for a first package from its first day, to this cutoff.
- **`process_starts`** come from `config_generations.process_started_at`. That table is kept forever.
- **`outages`** are gaps of more than 180 s in `resource_samples`, the watchdog's 60 s sample, with a span that begins or ends inside one cut at that edge. That table is kept 14 days, so an older span's days are listed in `days_missing` rather than reported as clean.
- **A value of `null`** means the record is not known at all, and it is never written as an empty list.

## Routes and grants

| route | gate | what it does |
|---|---|---|
| `GET /v1/packages/health` | open | verdict, reason codes, the next runs, the last run per source |
| `GET /v1/packages/schedule` | open | the weekly runs ahead, the special runs, the limits |
| `GET /v1/packages` (`?source=`, `?unacknowledged=true`) | any `packages` grant, filtered per token | the list, oldest first |
| `GET /v1/packages/ledger` (`?package_id=`, `?event=`) | any `packages` grant, filtered per token | every step; schedule events need `packages:*` |
| `GET /v1/packages/{package_id}` | `packages:<id>` | the manifest, plus `receipt` |
| `GET /v1/packages/{package_id}/zip` | `packages:<id>` | the zip, `application/zip`; `Range` answers 206 |
| `POST /v1/packages/{package_id}/receipt` | `receipts:<id>` | the acknowledgement |
| `POST /v1/packages/schedule` · `POST …/schedule/weekly` · `DELETE …/schedule/{special_id}` | `package_schedule` | the operator's schedule changes |

- **Open routes.** The two open routes describe the service, so a consumer's park check can read them without a token. A private pipeline appears there only under its public alias (`private-1`, …), as on `/v1/health`.
- **Downloads.** Every download is booked in the ledger when it ends. The entry records `status`, `content_length`, `bytes_sent`, `complete` and `disconnected`, for a finished download and for an aborted one alike.

**The receipt** (`{"zip_sha256": "<hex>", "note": "…"}`, sent once the zip is verified, imported and
filed) is checked in this order:
1. 400 for a note longer than 500 characters or a hash that is not 64 hex digits.
2. 404 for no such package.
3. 409 when the hash is not this package's.
4. 409 when the same token has not downloaded the whole zip. The list publishes the hash, so the hash
   alone proves nothing. A resumed download adds up across its attempts, abandoned ones included.
5. A token listed in `packages.dev_consumers` **rehearses**: every check runs, `dev_received` is booked,
   and nothing moves.
6. Otherwise 201 for the first verified receipt, and the package moves to `delivered`. A repeat answers
   200 with the original.

A receipt **without** `zip_sha256` is booked and changes nothing. It never takes the receipt slot, so it
cannot block a verified receipt later. The collector's store let a hashless first receipt take that
slot.

## The schedule

- **The weekly run** is `packages.weekday/hour/minute` in UTC, by default Sunday 01:30. It packs
  every pipeline up to 00:00 of its own day, the same cutoff as the tick packages, so a week's ticks
  and signals cover one span. It can be set aside, either for the next run only or until a date. A
  weekly run missed while the engine was down is not made up; the next one takes everything since.
- **Special runs** pack one source, or `all`, to a cutoff the operator names. By default they run at
  01:00 on the cutoff day. A special run that was due while the engine was down runs at the next
  start.
- **The limits** each answer 409 with the rule that refused:
  - no start inside the quiet minutes;
  - none in the 24 h before a weekly run;
  - none to a weekly run's own cutoff;
  - one per source per cutoff;
  - two per source per ISO week.
- **The quiet minutes** are 23:55–00:15 UTC, defined once (`utils/quiet_minutes.py`). They cover the
  day cut, the last pass of a day still committing, and the consumers' own daily order. The
  configuration refuses a weekly or special time inside them.

## Health

`ok`, or `degraded` with reason codes:
- `disk_low`: less free disk than `min_free_percent`.
- `receipt_overdue`: a package has been `ready` for more than `receipt_overdue_days`; the ids are listed.
- `last_run_failed`, `last_run_refused`: the last run of some source ended that way.

None of these touches the engine's own health or its outside heartbeat. A package waiting for its
consumer does not mean the engine is down.

## Configuration

`packages` in the app configuration. The defaults are in `configs/app_config.json`, and a machine
switches the service on in its `user_configs`:

```json
"packages": { "enabled": true, "dev_consumers": ["<operator token name>"] }
```

The other fields:
- `output_dir`, which defaults to `data/packages`;
- the weekly time and the special-run limits;
- `min_free_percent`;
- `receipt_overdue_days`;
- `pack_timeout_seconds`.

The consumer's token needs `packages:*` and `receipts:*` beside whatever it already holds.

**Order on a machine: deploy the code first, then write the grants.** A grant naming a surface the
running build does not know fails the configuration at parse time. Since 2026-11 that is an exit 2,
a refusal that the service manager does not restart, but it is still a refusal.

## By hand

```
python -m finiexragengine.cli.package_cli check                        # mint, folder, disk, sources, last runs
python -m finiexragengine.cli.package_cli pack --source all            # to today 00:00 UTC
python -m finiexragengine.cli.package_cli pack --source crypto_sentiment --cutoff 2026-11-17
```

A hand pack is how a new installation makes its first packages. It refuses while
`packages.enabled` is false. The collector's reference client (`python -m python.packages pickup`)
speaks this contract, so it can be pointed at this service with a rehearsal token as a contract test
before the consumer's own client touches it.
