"""Build identity sampling (deploy verification) — it must never be able to break a boot.

The unit answers one question: which code did THIS process import. Everything here is about the two
ways that answer can go wrong — a deployment with no git at all, and a value that describes the
working tree instead of the running process.
"""
from datetime import datetime, timezone
from typing import Optional

import pytest

from finiexragengine.core.observability import build_info
from finiexragengine.types.api_types import BuildInfo


def test_a_repository_yields_a_commit_and_a_clean_or_dirty_verdict() -> None:
    info = build_info.sample_build_info('9.9.9')

    assert info.version == '9.9.9'
    assert info.commit is not None and len(info.commit) >= 7
    assert isinstance(info.dirty, bool)          # a verdict, not "unknown"
    assert info.started_at.tzinfo is not None    # timezone-aware UTC, like every stamp here


def test_no_git_available_yields_nulls_rather_than_an_exception(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A container image or an unpacked archive has no repository. That is not a boot failure."""
    def _no_git(*args: str, **kwargs: object) -> None:
        raise FileNotFoundError('git')
    monkeypatch.setattr(build_info.subprocess, 'run', _no_git)

    info = build_info.sample_build_info('9.9.9')

    assert isinstance(info, BuildInfo)
    assert info.commit is None
    assert info.committed_at is None
    # None, not False: "clean" and "there is no repository here" are different answers, and only
    # one of them means the running code can be matched to a commit.
    assert info.dirty is None
    assert info.version == '9.9.9'


def test_a_failing_git_call_is_not_fatal_either(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a repository, a broken index, a permission problem — all the same: report nothing."""
    class _Failed:
        returncode = 128
        stdout = ''
        stderr = 'fatal: not a git repository'

    monkeypatch.setattr(build_info.subprocess, 'run', lambda *a, **k: _Failed())

    info = build_info.sample_build_info('9.9.9')

    assert (info.commit, info.committed_at, info.dirty) == (None, None, None)


def test_the_start_time_is_the_sample_time() -> None:
    before = datetime.now(timezone.utc)
    info = build_info.sample_build_info('9.9.9')
    after = datetime.now(timezone.utc)

    assert before <= info.started_at <= after


# --- the shared auth package (finiex_auth) -------------------------------------------------------

class _Distribution:
    """Stands in for an installed distribution: a version and an optional `direct_url.json`."""

    def __init__(self, version: str, direct_url: Optional[str]) -> None:
        self.version = version
        self._direct_url = direct_url

    def read_text(self, name: str) -> Optional[str]:
        return self._direct_url if name == 'direct_url.json' else None


def _installed(monkeypatch: pytest.MonkeyPatch, distribution: Optional[_Distribution]) -> None:
    def _lookup(name: str) -> _Distribution:
        if distribution is None:
            raise build_info.importlib.metadata.PackageNotFoundError(name)
        return distribution
    monkeypatch.setattr(build_info.importlib.metadata, 'distribution', _lookup)


def test_an_editable_auth_package_is_reported_as_editable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The development state no pin watches: a change is live the moment it is saved."""
    _installed(monkeypatch, _Distribution(
        '0.1.0', '{"dir_info": {"editable": true}, "url": "file:///finiex-auth"}'))

    info = build_info.sample_build_info('9.9.9')

    assert (info.auth_package_version, info.auth_package_editable) == ('0.1.0', True)


def test_a_pinned_git_install_is_reported_as_not_editable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production state: the tag in requirements.txt, installed from the repository."""
    _installed(monkeypatch, _Distribution(
        '0.1.0', '{"url": "https://github.com/dc-deal/finiex-modules-auth.git", '
                 '"vcs_info": {"vcs": "git", "requested_revision": "v0.1.0", "commit_id": "abc"}}'))

    info = build_info.sample_build_info('9.9.9')

    assert (info.auth_package_version, info.auth_package_editable) == ('0.1.0', False)


def test_a_missing_or_unreadable_auth_package_never_breaks_the_sample(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Like every field here: `None` says "not determinable", and nothing raises."""
    _installed(monkeypatch, None)
    info = build_info.sample_build_info('9.9.9')
    assert (info.auth_package_version, info.auth_package_editable) == (None, None)

    _installed(monkeypatch, _Distribution('0.1.0', 'not json'))
    info = build_info.sample_build_info('9.9.9')
    assert (info.auth_package_version, info.auth_package_editable) == ('0.1.0', None)

    _installed(monkeypatch, _Distribution('0.1.0', None))      # from an index: nothing editable
    info = build_info.sample_build_info('9.9.9')
    assert (info.auth_package_version, info.auth_package_editable) == ('0.1.0', False)
