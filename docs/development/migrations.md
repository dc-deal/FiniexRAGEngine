# Schema migrations

The database schema is owned by the numbered SQL files in `migrations/` — not by the code, and
not by config. This exists so the schema can evolve on a **populated** database: once real
history is in the corpus, drop-and-recreate is no longer available.

## Daily use

```bash
python -m finiexragengine.cli.migrate_cli --status   # read-only: what ran, what is waiting
python -m finiexragengine.cli.migrate_cli            # apply the pending ones, in order
```

Re-running is a no-op, so it is safe before every start and every deploy.

The engine **refuses to boot** against a schema that is behind the repo:

```
ConfigurationError: schema is 1 migration behind (pending: 002_articles_add_sentiment_hint) —
run `python -m finiexragengine.cli.migrate_cli` before starting the engine
```

That check (`core/schema/schema_guard.py`) runs in `PipelineAssembler` — the one place every
DB-touching path builds its object graph — so a stale schema fails immediately and clearly,
instead of surfacing as a confusing `column does not exist` in the middle of a paid pass.

## Adding a migration

1. Create `migrations/NNN_short_name.sql` — next free number, `snake_case` name.
2. Write plain forward DDL. **No `IF NOT EXISTS`**: the runner guarantees each file runs exactly
   once, so defensive DDL only hides mistakes.
3. Run `migrate_cli --status`, then apply it.

```sql
-- migrations/002_articles_add_sentiment_hint.sql
ALTER TABLE articles ADD COLUMN sentiment_hint REAL;
```

Existing rows get `NULL` — plan for that in the reading code, or backfill in the same file.

## Rules

- **Applied migrations are immutable.** Each file's checksum is recorded; editing one after it
  ran is *drift*, and the runner refuses (re-running cannot fix it — the version is already
  recorded). Express the change as a new migration instead.
- **Forward-only.** There are no down-migrations: a "down" that drops a column does not restore
  data, it destroys it — that is a new migration wearing a rollback costume. Back up before a
  destructive change.
- **One file, one transaction.** PostgreSQL has transactional DDL, so a failing migration leaves
  nothing half-applied — the DDL and its `schema_migrations` row commit together or not at all.
- **Concurrency is handled.** The runner takes a `pg_advisory_lock` for the whole pass, so
  several processes starting at once (separate ingest/eval/API containers) cannot double-apply.

## The one exception: `-- finiex:no-transaction`

A few statements are precisely the ones PostgreSQL refuses *inside* a transaction block — most
importantly `CREATE INDEX CONCURRENTLY`, which is how an index is built on a live table **without
locking out writes**. Since every ordinary migration runs in a transaction, such a file needs an
opt-out:

```sql
-- migrations/003_articles_embedding_hnsw.sql
-- finiex:no-transaction
CREATE INDEX CONCURRENTLY idx_articles_embedding ON articles USING hnsw (embedding vector_cosine_ops);
```

The marker must be **its own comment line**; prose mentioning it does not count. The file then
runs on an autocommit connection.

**One statement per no-transaction file — the runner enforces it.** `autocommit` alone is not
enough: PostgreSQL wraps several statements of one query in an *implicit* transaction, which
fails with the very error the marker exists to avoid. One statement per file is also the right
unit: with no transaction, the `schema_migrations` ledger is the only atomicity left, and it
counts in whole files. Two indexes = two files, tracked and retried separately.

**The honest price.** Such a migration is **not** all-or-nothing. If it fails, PostgreSQL has
nothing to roll back and a partial effect survives — a failed `CREATE INDEX CONCURRENTLY` leaves
an **invalid** index behind, which you must drop before re-running:

```sql
SELECT indexrelid::regclass, indisvalid FROM pg_index WHERE NOT indisvalid;
DROP INDEX idx_articles_embedding;
```

The error message says so rather than claiming a rollback that did not happen. This is not a
weakness of this runner — Flyway and Alembic carry the same warning, because it is PostgreSQL's
rule, not theirs. **Use the marker only when a statement genuinely requires it.**

## `001_init.sql` is special

It is the **baseline**: the schema exactly as it stood when migrations were introduced, and the
only file written idempotently (`CREATE TABLE IF NOT EXISTS`, …). That property is what let it
be adopted by the already-running database — it applied there as a complete no-op and simply
recorded itself, with no separate "stamp this DB as migrated" mechanism. On a fresh database it
builds everything. **Do not copy its style** for new migrations.

It also replaced five `_ensure_schema()` methods and four inline
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements that used to re-run on every boot.

## What is *not* a migration

- **The corpus guard** (`PgVectorStore._verify_corpus_stamp`, ISSUE_16) stamps `corpus_meta`
  with the embedding model and refuses to boot on a mismatch. It is a guard, not schema work.
  Changing the embedding model is a deliberate **re-embed migration**, never a config flip —
  which is why `embedding vector(1536)` is stated literally in `001` rather than templated.
- **The corpus table name.** It lives in the migration, not in `app_config.json`: a config key
  there could only ever disagree with the schema that actually exists.

## Tests

`tests/test_migration_runner.py` covers the runner against throwaway schemas of its own.

More importantly, **every DB test runs against the real migrations**: the `db_dsn` fixture
(`tests/conftest.py`) creates a `finiex_test` schema, applies `migrations/` into it, and hands
back a DSN carrying `search_path=finiex_test,public`. Isolation is the DSN's job, so the tests
exercise production classes with their canonical table names against the exact schema this
directory defines — a broken migration fails the suite instead of hiding behind hand-written
test DDL. `clean_db` adds a truncate for a per-test blank slate.
