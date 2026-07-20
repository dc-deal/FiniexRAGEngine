"""Override report (startup config hygiene) — collect/format/emit, no DB/API.

`collect_overrides` walks a user override against the tracked base AND the validated
merged config; `format_override_report` renders ONE line per file (`leaf old→new`,
shortened paths, `~changed` for prose, `⚠ key?` for typo'd keys, `+N more` cap).
`emit_override_report` logs a file's line once per process.
"""
import logging

from finiexragengine.configuration import override_report
from finiexragengine.configuration.override_report import (
    OverrideEntry,
    collect_overrides,
    emit_override_report,
    format_override_report,
)

_LIST_KEYS = {'models': 'sub_pipeline_id', 'sources': 'source_id'}


def _line(entries) -> str:
    return format_override_report('user_configs/pipelines/p.json', entries)


def test_scalar_change_renders_old_to_new():
    base = {'retrieval': {'floor_distance': 0.7, 'top_k': 12}}
    override = {'retrieval': {'floor_distance': 0.65}}
    validated = {'retrieval': {'floor_distance': 0.65, 'top_k': 12}}
    entries = collect_overrides(base, override, validated)
    assert [(e.path, e.base_value, e.override_value) for e in entries] == [
        ('retrieval.floor_distance', 0.7, 0.65)]
    # Path shortened to the leaf (the file names the context), user_configs/ stripped.
    assert _line(entries) == '[OVERRIDE] pipelines/p.json · floor_distance 0.7→0.65'


def test_ambiguous_leaves_keep_their_full_path():
    # `telegram.enabled` and `weekly_report.enabled` both compress to `enabled` —
    # colliding labels fall back to the full path; unique leaves stay compressed.
    base = {'telegram': {'enabled': False, 'chat_id': ''},
            'weekly_report': {'enabled': False}}
    override = {'telegram': {'enabled': True, 'chat_id': '42'},
                'weekly_report': {'enabled': True}}
    validated = {'telegram': {'enabled': True, 'chat_id': '42'},
                 'weekly_report': {'enabled': True}}
    line = _line(collect_overrides(base, override, validated))
    assert 'telegram.enabled false→true' in line
    assert 'weekly_report.enabled false→true' in line
    assert 'chat_id ~changed' in line                    # unique leaf: still compressed


def test_wholesale_list_renders_lengths():
    base = {'symbols': ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']}
    override = {'symbols': ['A', 'B']}
    entries = collect_overrides(base, override, {'symbols': ['A', 'B']})
    assert _line(entries).endswith('· symbols 8→2')


def test_id_list_patch_keeps_id_segment():
    base = {'llm': {'models': [
        {'sub_pipeline_id': 'mini', 'enabled': True},
        {'sub_pipeline_id': '4o_enhanced', 'enabled': True}]}}
    override = {'llm': {'models': [{'sub_pipeline_id': '4o_enhanced', 'enabled': False}]}}
    validated = {'llm': {'models': [
        {'sub_pipeline_id': 'mini', 'enabled': True},
        {'sub_pipeline_id': '4o_enhanced', 'enabled': False}]}}
    entries = collect_overrides(base, override, validated, _LIST_KEYS)
    assert [e.path for e in entries] == ['llm.models[4o_enhanced].enabled']
    assert '· models[4o_enhanced].enabled true→false' in _line(entries)


def test_sources_segment_collapses_to_the_id():
    # A source-set file is nothing but sources — `sources[fxstreet]` reads as `fxstreet`.
    base = {'sources': [{'source_id': 'fxstreet', 'url': 'https://a.test/rss'}]}
    override = {'sources': [{'source_id': 'fxstreet', 'enabled': False}]}
    validated = {'sources': [{'source_id': 'fxstreet', 'url': 'https://a.test/rss',
                              'enabled': False}]}
    entries = collect_overrides(base, override, validated, _LIST_KEYS)
    # `enabled` is absent in the tracked file but known to the schema: an addition.
    assert entries[0].base_value is override_report._ABSENT and not entries[0].unknown
    assert '· fxstreet.enabled →false' in _line(entries)


def test_string_values_render_as_changed_never_quoted():
    base = {'sources': [{'source_id': 'f1', 'comment': 'short'}]}
    override = {'sources': [{'source_id': 'f1', 'comment': 'x' * 300}]}
    validated = {'sources': [{'source_id': 'f1', 'comment': 'x' * 300}]}
    entries = collect_overrides(base, override, validated, _LIST_KEYS)
    assert '· f1.comment ~changed' in _line(entries)
    assert 'xxx' not in _line(entries)                    # prose never leaks into the line


def test_typo_key_is_flagged_as_unknown():
    # Pydantic ignores unknown keys, so a typo'd override silently does nothing —
    # the validated dump misses the key, and that is exactly the detection signal.
    base = {'retrieval': {'floor_distance': 0.7}}
    override = {'retrieval': {'floor_distanze': 0.65}}
    validated = {'retrieval': {'floor_distance': 0.7}}
    (entry,) = collect_overrides(base, override, validated)
    assert entry.unknown and entry.path == 'retrieval.floor_distanze'
    assert '⚠ floor_distanze?' in _line([entry])


def test_same_value_is_not_a_divergence():
    base = {'retrieval': {'floor_distance': 0.7}}
    override = {'retrieval': {'floor_distance': 0.7}}
    validated = {'retrieval': {'floor_distance': 0.7}}
    assert collect_overrides(base, override, validated) == []


def test_appended_list_item_reads_as_new():
    base = {'llm': {'models': [{'sub_pipeline_id': 'mini'}]}}
    override = {'llm': {'models': [{'sub_pipeline_id': 'extra', 'name': 'gpt-4o'}]}}
    validated = {'llm': {'models': [{'sub_pipeline_id': 'mini'},
                                    {'sub_pipeline_id': 'extra', 'name': 'gpt-4o'}]}}
    (entry,) = collect_overrides(base, override, validated, _LIST_KEYS)
    assert entry.path == 'llm.models[extra]'
    assert entry.base_value is override_report._ABSENT
    assert '· models[extra] →{…}' in _line([entry])


def test_many_leaves_collapse_to_more():
    entries = [OverrideEntry(f'k{i}', i) for i in range(9)]
    line = _line(entries)
    assert '+4 more' in line                              # 5 shown + the rest counted
    assert 'k5' not in line


def test_emit_logs_once_per_process(caplog):
    override_report._REPORTED.clear()
    entries = [OverrideEntry('a.b', 2, 1)]
    with caplog.at_level(logging.WARNING,
                         logger='finiexragengine.configuration.override_report'):
        emit_override_report('user_configs/pipelines/p.json', entries)
        emit_override_report('user_configs/pipelines/p.json', entries)   # spam guard
        emit_override_report('user_configs/pipelines/empty.json', [])    # nothing to say
    blocks = [r for r in caplog.records if '[OVERRIDE]' in r.getMessage()]
    assert len(blocks) == 1
    assert 'b 1→2' in blocks[0].getMessage()
