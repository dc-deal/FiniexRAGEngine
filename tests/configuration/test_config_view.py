"""The config views (2026-09-08) — the one place a config object becomes publishable JSON.

`GET /v1/configs/{name}` exists because `user_configs/` is gitignored: which feeds a machine has
switched off and which thresholds it runs were readable only over RDP. Publishing configuration
means publishing the file the bearer tokens live in, so these cases are mostly about what must
*not* come out, and about the answer saying where it was altered.
"""
import json
from pathlib import Path
from typing import Dict, List

import pytest

from finiexragengine.configuration.abstract_config_view import AbstractConfigView
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.configuration.app_config_view import AppConfigView
from finiexragengine.configuration.override_report import OverrideEntry
from finiexragengine.configuration.source_set_config_view import SourceSetConfigView
from finiexragengine.configuration.source_set_registry import SourceSetRegistry
from finiexragengine.types.config_types.source_set_types import SourceSetConfig


def _write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def _manager(tmp_path: Path, base: dict, user: dict) -> AppConfigManager:
    return AppConfigManager(config_path=_write(tmp_path / 'app_config.json', base),
                            user_config_path=_write(tmp_path / 'user' / 'app_config.json', user))


_TOKEN = 'sup3r-secret-bearer-value'
_BOT = '8012345678:AAF-real-looking-token'


# --- the credentials, which is why the view exists at all ---------------------------------------

def test_the_three_credentials_never_reach_the_payload(tmp_path):
    manager = _manager(
        tmp_path,
        {'api': {'tokens': {}}, 'telegram': {}},
        {'api': {'tokens': {'ide': {'token': _TOKEN, 'grants': ['reports:*'], 'note': 'IDE'}}},
         'telegram': {'enabled': True, 'bot_token': _BOT, 'chat_id': '-1001234567890'}})

    document = AppConfigView(manager).render()
    payload = json.dumps(document.documents)

    assert _TOKEN not in payload and _BOT not in payload and '-1001234567890' not in payload
    assert document.documents['app']['api']['tokens']['ide']['token'] == '«redacted»'
    # The structure survives — a diagnosis needs to see THAT a consumer exists and what it may read.
    assert document.documents['app']['api']['tokens']['ide']['grants'] == ['reports:*']
    assert document.documents['app']['api']['tokens']['ide']['note'] == 'IDE'


def test_a_masked_path_is_named_in_the_answer(tmp_path):
    """A withheld value is honest; a silently altered one is not."""
    manager = _manager(tmp_path, {'telegram': {}}, {'telegram': {'bot_token': _BOT}})

    document = AppConfigView(manager).render()

    assert 'telegram.bot_token' in document.redacted
    assert document.unclassified == []          # the policy covers today's models


def test_the_override_half_is_projected_too_and_not_passed_through_raw(tmp_path):
    """The leak that was easy to miss.

    `user_configs/app_config.json` is precisely the file the tokens live in, so an override
    *entry* carries the secret whenever the leaf it names is one. The document being clean while
    the provenance list beside it prints the token would be the worst of both.
    """
    manager = _manager(
        tmp_path, {'api': {'tokens': {}}},
        {'api': {'tokens': {'ide': {'token': _TOKEN, 'grants': ['*'], 'note': 'IDE'}}}})

    document = AppConfigView(manager).render()
    leaves = {leaf.path: leaf.value for leaf in document.overrides['app']}

    assert leaves['api.tokens.ide.token'] == '«redacted»'
    assert _TOKEN not in json.dumps(document.overrides['app'], default=str)
    # ...while the leaves that are not secrets keep their values, or the list says nothing.
    assert leaves['api.tokens.ide.note'] == 'IDE'


# --- the second layer: a credential in a field nobody classified as one --------------------------

def test_a_feed_url_carrying_its_own_key_is_scrubbed_although_the_field_is_public(tmp_path):
    """`sources[].url` must be published — and a key in its query string must not be."""
    _write(tmp_path / 'sets' / 'demo.json',
           {'source_set_id': 'demo',
            'sources': [{'source_id': 'paid_feed',
                         'url': 'https://feeds.example.com/rss?apikey=SEKRET123&format=xml'}]})
    registry = SourceSetRegistry(tmp_path / 'sets', tmp_path / 'user_sets')
    registry.load()

    document = SourceSetConfigView(registry).render()
    url = document.documents['demo']['sources'][0]['url']

    assert 'SEKRET123' not in url
    assert url == 'https://feeds.example.com/rss?apikey=«redacted»&format=xml'
    # Reported apart from `redacted`: this was a pattern catching a value, not a classified field.
    assert 'sources.0.url' in document.scrubbed
    assert 'sources.0.url' not in document.redacted


# --- effective, not tracked ----------------------------------------------------------------------

def test_the_document_is_the_effective_config_and_the_overlay_is_named(tmp_path):
    """The whole reason this exists: the tracked file is not what the machine runs."""
    _write(tmp_path / 'sets' / 'demo.json',
           {'source_set_id': 'demo',
            'detection': {'high_cluster_size': 5, 'cluster_unit': 'articles'},
            'sources': [{'source_id': 'a', 'url': 'https://a.test/feed'},
                        {'source_id': 'b', 'url': 'https://b.test/feed'}]})
    _write(tmp_path / 'user_sets' / 'demo.json',
           {'source_set_id': 'demo',
            'detection': {'cluster_unit': 'feeds'},
            'sources': [{'source_id': 'b', 'enabled': False}]})
    registry = SourceSetRegistry(tmp_path / 'sets', tmp_path / 'user_sets')
    registry.load()

    document = SourceSetConfigView(registry).render()
    detection = document.documents['demo']['detection']

    assert detection['cluster_unit'] == 'feeds'        # the overlay, not the tracked value
    assert detection['high_cluster_size'] == 5         # untouched leaves survive the merge
    assert document.documents['demo']['sources'][1]['enabled'] is False
    paths = {leaf.path for leaf in document.overrides['demo']}
    assert 'detection.cluster_unit' in paths and 'sources[b].enabled' in paths


def test_an_absent_overlay_leaves_the_overrides_empty_rather_than_guessing(tmp_path):
    _write(tmp_path / 'sets' / 'demo.json',
           {'source_set_id': 'demo', 'sources': [{'source_id': 'a', 'url': 'https://a.test/f'}]})
    registry = SourceSetRegistry(tmp_path / 'sets', tmp_path / 'user_sets')
    registry.load()

    assert SourceSetConfigView(registry).render().overrides == {}


def test_the_layers_list_only_names_files_that_exist(tmp_path):
    """An absent overlay and an inert one are different states, and only one is worth chasing."""
    base = _write(tmp_path / 'app_config.json', {'log_level': 'INFO'})
    manager = AppConfigManager(config_path=base, user_config_path=tmp_path / 'nope.json')

    assert AppConfigView(manager).layers() == ['app_config.json']


# --- narrowing ------------------------------------------------------------------------------------

def test_narrowing_returns_one_document_and_an_unknown_id_is_an_absence(tmp_path):
    for name in ('one', 'two'):
        _write(tmp_path / 'sets' / f'{name}.json',
               {'source_set_id': name,
                'sources': [{'source_id': 'a', 'url': f'https://{name}.test/feed'}]})
    registry = SourceSetRegistry(tmp_path / 'sets', tmp_path / 'user_sets')
    registry.load()
    view = SourceSetConfigView(registry)

    assert list(view.render().documents) == ['one', 'two']
    assert list(view.render('two').documents) == ['two']
    # None, not an empty document: "no such set" and "a set with nothing in it" are different
    # answers, and only the first one is a 404.
    assert view.render('three') is None


# --- the serializer refuses what nobody decided to publish -----------------------------------------

def test_an_unpublishable_value_raises_instead_of_being_stringified(tmp_path):
    """FastAPI's encoder would happily `vars()` an engine object onto the wire.

    The same discipline `utils/dataclass_json` applies to reports: a shape that belongs in a
    payload says so, and everything else is a defect the suite sees rather than a surprise in
    production.
    """
    class _Rogue(AbstractConfigView):
        NAME = 'rogue'
        SUMMARY = 'holds something nobody decided how to publish'

        def documents(self) -> Dict[str, SourceSetConfig]:
            return {}

        def override_entries(self) -> Dict[str, List[OverrideEntry]]:
            return {}

    with pytest.raises(TypeError, match='cannot publish'):
        _Rogue()._project({'thing': Path('/etc/passwd')}, '', None)


# --- what the live surface taught this file, 2026-09-08 -------------------------------------------

def test_an_unrecognised_override_key_is_masked_without_reading_as_a_policy_gap(tmp_path):
    """Found on production within an hour of shipping.

    `user_configs/app_config.json` there sets `weekly_report.report_command` — a key that lives on
    `telegram`, so Pydantic drops it and the override does nothing. It names no field in any model,
    so it can never be classified and no contract test can ever cover it. Masking it is right (a
    key misfiled by hand is exactly where a secret ends up by accident); putting it in
    `unclassified` was not, because that census has to keep meaning "a model grew a string the
    policy does not name, and the contract test is already red".
    """
    manager = _manager(tmp_path, {'weekly_report': {'hour': 18}},
                       {'weekly_report': {'report_command': '/report', 'hour': 9}})

    document = AppConfigView(manager).render()
    leaves = {leaf.path: leaf for leaf in document.overrides['app']}

    assert leaves['weekly_report.report_command'].unknown is True
    assert leaves['weekly_report.report_command'].value == '«redacted»'
    assert document.unclassified == [], 'an unknown key is not a gap in the policy'
    # The override that DID apply is unaffected — a number, and it keeps its value.
    assert leaves['weekly_report.hour'].value == 9 and leaves['weekly_report.hour'].unknown is False


def test_the_layers_are_posix_paths_whatever_os_answered(tmp_path):
    """The engine runs on Windows; `str(Path)` there yields `configs\\app_config.json`.

    The other two views declare their layers as forward-slash constants, so one document would
    have carried two separators depending on which machine served it.
    """
    manager = _manager(tmp_path, {'log_level': 'INFO'}, {'log_level': 'DEBUG'})

    assert all('\\' not in layer for layer in AppConfigView(manager).layers())


def test_an_unset_credential_is_published_empty_rather_than_claimed_to_exist(tmp_path):
    """"Is Telegram configured on this machine" is a question this surface exists to answer.

    Masking an empty field turns "no bot token here" into "a bot token you may not see" — the same
    payload for two states an operator needs to tell apart. Nothing is protected by hiding an empty
    string.
    """
    manager = _manager(tmp_path, {'telegram': {}}, {'telegram': {'enabled': False}})

    document = AppConfigView(manager).render()
    telegram = document.documents['app']['telegram']

    assert telegram['bot_token'] == '' and telegram['chat_id'] == ''
    assert document.redacted == []          # nothing was masked, so nothing is claimed
    # ...while a real one is still masked and still named.
    manager = _manager(tmp_path, {'telegram': {}}, {'telegram': {'bot_token': _BOT}})
    document = AppConfigView(manager).render()
    assert document.documents['app']['telegram']['bot_token'] == '«redacted»'
    assert document.redacted == ['telegram.bot_token']
