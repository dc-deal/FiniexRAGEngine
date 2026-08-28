"""No async psycopg anywhere in the package — a platform invariant no runtime test can reach.

**Why this is a contract test and not a unit test.** `psycopg`'s async mode requires a
selector-based event loop. Windows defaults to `ProactorEventLoop`, so on the deployed host every
`AsyncConnection.connect()` raises `InterfaceError`. The dev container is Linux and therefore cannot
fail: on 2026-08-28 the stream's journal tailer used an async connection, every test was green here,
and in production it reconnected every two seconds for 22 hours — serving connect and replay
correctly while pushing **nothing**. The consumer found it by running a real session.

So the invariant cannot be asserted by *behaviour* on the machine the suite runs on. It is asserted
structurally instead: no module in `finiexragengine/` may reach for psycopg's async API. The
replacement is a sync connection waited on inside `asyncio.to_thread`, which has no such dependency
(`core/outcome/stream_dispatcher.py`).

Parsed as an **AST**, not grepped. A textual search would trip over this file's own explanation and
over the dispatcher's docstring, which names `AsyncConnection` precisely in order to say why it is
absent — a guard that fails on its own reasoning teaches the next person to delete it.
"""
import ast
from pathlib import Path
from typing import List, Tuple
import pytest



# Platform-sensitive: the event loop / async-driver class itself. A Linux runner cannot exercise it, so this file is part
# of the version-bump run on the production machine (`pytest -m deploy`).
pytestmark = pytest.mark.deploy

_PACKAGE = Path(__file__).resolve().parents[2] / 'finiexragengine'
# psycopg's async surface, plus the pool package's. Names rather than paths: an import can be
# spelled several ways and they all end in one of these being used.
_FORBIDDEN = {'AsyncConnection', 'AsyncConnectionPool', 'AsyncCursor', 'AsyncClientCursor',
              'AsyncServerCursor'}


def _offences(path: Path) -> List[Tuple[int, str]]:
    """Every place this module names psycopg's async API in CODE — imports and attribute access."""
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        # `from psycopg import AsyncConnection` / `from psycopg_pool import AsyncConnectionPool`
        if isinstance(node, ast.ImportFrom) and (node.module or '').startswith('psycopg'):
            for alias in node.names:
                if alias.name in _FORBIDDEN:
                    found.append((node.lineno, f'from {node.module} import {alias.name}'))
        # `psycopg.AsyncConnection.connect(...)`
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN:
            found.append((node.lineno, f'.{node.attr}'))
    return found


def test_the_package_uses_no_async_psycopg() -> None:
    """A Linux runner cannot see this failure any other way, so it is checked rather than remembered.

    If a future change genuinely needs async psycopg, the decision is not to delete this test — it is
    to decide what happens on Windows first, and to record that decision here.
    """
    offences = {
        str(path.relative_to(_PACKAGE.parent)): hits
        for path in sorted(_PACKAGE.rglob('*.py'))
        if (hits := _offences(path))
    }

    assert not offences, (
        'psycopg async API used in the package — it cannot work on the deployed host '
        f'(Windows / ProactorEventLoop): {offences}. Wait on a sync connection inside '
        'asyncio.to_thread instead; see core/outcome/stream_dispatcher.py.')


def test_the_dispatcher_still_listens_the_way_the_fix_requires() -> None:
    """The paired positive: absence alone would also be satisfied by deleting the tail entirely.

    So this asserts the *replacement* is present — a sync connect and a threaded wait — which is what
    makes the negative above a fix rather than a removal.
    """
    source = (_PACKAGE / 'core' / 'outcome' / 'stream_dispatcher.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    functions = {node.name for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assert '_open_listener' in functions, 'the sync listener factory is gone'
    assert '_wait_for_payloads' in functions, 'the threaded wait is gone'
    assert 'to_thread' in source, 'the listener is no longer waited on in a thread'
    assert 'connect_timeout' in source, 'the listener connect is unbounded again (ISSUE_73 shape)'
