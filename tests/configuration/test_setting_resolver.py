"""`SettingResolver` (ISSUE_98) — one precedence rule, applied in one place.

The unit exists because three secrets are about to share it (`OPENAI_API_KEY`, `DATABASE_URL`,
`SSL_CERT_FILE`), and because a precedence rule invented separately at each call site is a rule
that disagrees with itself. What is actually tested here is not the lookup — it is that the
*source* comes back with the value, since a config entry shadowed by a forgotten environment
variable is the silent no-op this project keeps paying for.
"""
import logging

import pytest

from finiexragengine.configuration.setting_resolver import SettingResolver
from finiexragengine.types.setting_types import SETTING_SOURCES


def test_the_environment_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('FINIEX_TEST_SETTING', 'from-env')
    setting = SettingResolver(report=False).resolve('FINIEX_TEST_SETTING', 'from-config')
    assert setting.value == 'from-env'
    assert setting.source == 'environment'
    assert setting.is_set()


def test_the_config_fills_in_when_the_environment_is_silent(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('FINIEX_TEST_SETTING', raising=False)
    setting = SettingResolver(report=False).resolve('FINIEX_TEST_SETTING', 'from-config')
    assert setting.value == 'from-config'
    assert setting.source == 'user_configs'


def test_neither_source_is_reported_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('FINIEX_TEST_SETTING', raising=False)
    setting = SettingResolver(report=False).resolve('FINIEX_TEST_SETTING')
    assert setting.source == 'none'
    assert not setting.is_set()
    assert 'unset' in setting.describe()


@pytest.mark.parametrize('blank', ['', '   ', '\t'])
def test_a_blank_environment_variable_counts_as_absent(
        blank: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`FINIEX_X=""` is how a variable gets "unset" in a shell — it must not win over the config."""
    monkeypatch.setenv('FINIEX_TEST_SETTING', blank)
    setting = SettingResolver(report=False).resolve('FINIEX_TEST_SETTING', 'from-config')
    assert setting.value == 'from-config'
    assert setting.source == 'user_configs'


def test_the_parser_runs_only_on_the_environment_branch(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A config value arrives already typed by Pydantic; parsing it again would be wrong."""
    calls = []

    def parse(raw: str) -> dict:
        calls.append(raw)
        return {'parsed': raw}

    monkeypatch.setenv('FINIEX_TEST_SETTING', 'raw-value')
    env = SettingResolver(report=False).resolve('FINIEX_TEST_SETTING', parse=parse)
    assert env.value == {'parsed': 'raw-value'} and calls == ['raw-value']

    monkeypatch.delenv('FINIEX_TEST_SETTING')
    config = SettingResolver(report=False).resolve(
        'FINIEX_TEST_SETTING', {'already': 'typed'}, parse=parse)
    assert config.value == {'already': 'typed'}
    assert calls == ['raw-value']                       # untouched


def test_a_credential_is_never_echoed_but_a_path_may_be(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """`printable` is the whole difference between a certificate path and a token."""
    monkeypatch.setenv('FINIEX_TEST_SETTING', 'super-secret-value')
    with caplog.at_level(logging.INFO):
        secret = SettingResolver().resolve('FINIEX_TEST_SETTING')
        shown = SettingResolver().resolve('FINIEX_TEST_SETTING', printable=True)

    assert 'super-secret-value' not in secret.describe()
    assert 'environment' in secret.describe()
    assert 'super-secret-value' in shown.describe()
    # The report carries the provenance of both, and the value of only one.
    assert caplog.text.count('FINIEX_TEST_SETTING') == 2
    assert caplog.text.count('super-secret-value') == 1


def test_the_resolver_remembers_what_it_decided(monkeypatch: pytest.MonkeyPatch) -> None:
    """A boot summary reads this back — provenance for every secret the process runs on."""
    monkeypatch.setenv('FINIEX_TEST_A', 'a')
    monkeypatch.delenv('FINIEX_TEST_B', raising=False)
    resolver = SettingResolver(report=False)
    resolver.resolve('FINIEX_TEST_A', 'ignored')
    resolver.resolve('FINIEX_TEST_B', 'from-config')
    resolver.resolve('FINIEX_TEST_C')

    assert [s.name for s in resolver.resolved()] == ['FINIEX_TEST_A', 'FINIEX_TEST_B',
                                                     'FINIEX_TEST_C']
    assert [s.source for s in resolver.resolved()] == ['environment', 'user_configs', 'none']
    assert all(s.source in SETTING_SOURCES for s in resolver.resolved())
