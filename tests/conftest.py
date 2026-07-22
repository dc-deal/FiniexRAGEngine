"""Shared pytest fixtures."""
import os

# Console-only logging for the suite: booting the app in a test (the client fixture) must not
# append test output — including deliberately-raised errors — to the real logs/finiex.log.
os.environ['FINIEX_LOG_FILE'] = ''

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
    return TestClient(create_app(attach_runners=False))


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
    unstamped, as it would on a fresh corpus.
    """
    import psycopg

    with psycopg.connect(db_dsn) as conn:
        conn.execute('TRUNCATE articles, corpus_meta, outcomes, cost_log, query_vectors, '
                     'source_health, archive_export_log')
        conn.commit()
    yield db_dsn
