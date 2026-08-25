"""Shared pytest fixtures."""
import os

# Console-only logging for the suite: booting the app in a test (the client fixture) must not
# append test output — including deliberately-raised errors — to the real logs/finiex.log.
os.environ['FINIEX_LOG_FILE'] = ''

# ISSUE_98: the app now refuses to boot with `require_auth` on and no tokens configured — which is
# the point. The suite therefore configures one and calls with it, so the *authenticated* path is
# what every existing API test exercises. `tests/test_api_auth.py` builds its own unauthenticated
# clients for the rejection cases; nothing here weakens the default.
_SUITE_TOKEN = 'suite-token-not-a-real-credential'
os.environ.setdefault('FINIEX_API_TOKENS', f'suite:{_SUITE_TOKEN}')

from typing import Iterator  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from finiexragengine.api.api_app import create_app  # noqa: E402
from finiexragengine.configuration.app_config_manager import AppConfigManager  # noqa: E402
from finiexragengine.core.schema.migration_runner import MigrationRunner  # noqa: E402

# Every DB-touching test runs against this schema, never against the operator's real corpus.
_TEST_SCHEMA = 'finiex_test'
_DEFAULT_DSN = 'postgresql://ragengine:ragengine@127.0.0.1:5433/ragengine'


@pytest.fixture
def client() -> TestClient:
    # attach_runners=False pins the app to scaffold-mock mode: the free suite must
    # never make paid API calls just because DATABASE_URL/OPENAI_API_KEY are set in
    # the developer's (or CI's) environment. Real runs are the fenced `paid` tests
    # and the 💸 CLIs — exercised deliberately, never as a suite side effect.
    return TestClient(create_app(attach_runners=False),
                      headers={'Authorization': f'Bearer {_SUITE_TOKEN}'})


@pytest.fixture(scope='session')
def db_dsn() -> Iterator[str]:
    """A DSN pointing at a throwaway schema built by the **real** migrations (ISSUE_14).

    Isolation is the DSN's job, not the code's: `search_path=finiex_test,public` puts every
    table this session creates into a private schema while `public` still resolves the `vector`
    type from the extension. So the tests exercise production classes with their canonical table
    names, against the exact schema `migrations/` defines — a migration that is broken or drifts
    from the code fails the suite instead of hiding behind hand-written test DDL.

    Skips (never fails) when no Postgres is reachable: the free suite must stay runnable without
    a database.
    """
    pytest.importorskip('psycopg')
    import psycopg

    base = os.environ.get('DATABASE_URL', _DEFAULT_DSN)
    try:
        with psycopg.connect(base) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE')
            conn.execute(f'CREATE SCHEMA {_TEST_SCHEMA}')
            conn.commit()
    except psycopg.Error as exc:
        pytest.skip(f'PostgreSQL not available: {exc}')

    dsn = f'{base}?options=-csearch_path%3D{_TEST_SCHEMA},public'
    MigrationRunner(dsn, AppConfigManager().get_migrations_dir()).apply()
    yield dsn

    with psycopg.connect(base) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE')
        conn.commit()


@pytest.fixture
def clean_db(db_dsn: str) -> Iterator[str]:
    """`db_dsn`, with every data table emptied first — tests share one migrated schema.

    Truncate rather than re-migrate: the schema is the expensive part and it does not change
    between tests; the rows do. `corpus_meta` is included so the corpus guard (ISSUE_16) starts
    unstamped, as it would on a fresh corpus, and `stream_seq` so each test mints from 1 —
    a leaked counter would make sequence assertions depend on test order. `breaking_episodes`
    likewise (ISSUE_65): the registry upserts by episode id, so a row surviving from an earlier
    test would turn an insert into a continuation and quietly change what `n_passes` proves.
    """
    import psycopg

    with psycopg.connect(db_dsn) as conn:
        conn.execute('TRUNCATE articles, corpus_meta, outcomes, cost_log, query_vectors, '
                     'source_health, source_poll_log, source_quarantine_log, '
                     'resource_samples, archive_export_log, config_fingerprints, '
                     'stream_seq, breaking_episodes')
        conn.commit()
    yield db_dsn
