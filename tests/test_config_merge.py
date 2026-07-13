"""Override deep-merge semantics — dicts merge, plain lists replace, keyed lists patch-by-id."""
from finiexragengine.configuration.config_merge import deep_merge


def test_nested_dicts_merge_scalars_replace():
    out = deep_merge({'a': 1, 'nested': {'x': 1, 'y': 2}},
                     {'a': 9, 'nested': {'y': 20, 'z': 3}})
    assert out == {'a': 9, 'nested': {'x': 1, 'y': 20, 'z': 3}}


def test_plain_lists_replace_wholesale():
    assert deep_merge({'symbols': ['A', 'B', 'C']}, {'symbols': ['A']}) == {'symbols': ['A']}


def test_keyed_list_patches_one_item_and_keeps_the_rest():
    base = {'models': [{'sub_pipeline_id': 'mini', 'name': 'm', 'default': True},
                       {'sub_pipeline_id': '4o', 'name': 'g', 'enabled': True}]}
    out = deep_merge(base, {'models': [{'sub_pipeline_id': '4o', 'enabled': False}]},
                     list_keys={'models': 'sub_pipeline_id'})
    assert out['models'][0] == {'sub_pipeline_id': 'mini', 'name': 'm', 'default': True}  # untouched
    assert out['models'][1] == {'sub_pipeline_id': '4o', 'name': 'g', 'enabled': False}   # patched


def test_keyed_list_appends_an_unknown_id():
    out = deep_merge({'models': [{'sub_pipeline_id': 'mini', 'name': 'm'}]},
                     {'models': [{'sub_pipeline_id': 'new', 'name': 'n'}]},
                     list_keys={'models': 'sub_pipeline_id'})
    assert [m['sub_pipeline_id'] for m in out['models']] == ['mini', 'new']


def test_without_list_keys_a_list_still_replaces():
    out = deep_merge({'models': [{'sub_pipeline_id': 'mini', 'name': 'm'}]},
                     {'models': [{'sub_pipeline_id': '4o'}]})
    assert out['models'] == [{'sub_pipeline_id': '4o'}]        # replaced (no list_keys)


def test_base_is_not_mutated():
    base = {'models': [{'sub_pipeline_id': 'mini', 'enabled': True}]}
    deep_merge(base, {'models': [{'sub_pipeline_id': 'mini', 'enabled': False}]},
               list_keys={'models': 'sub_pipeline_id'})
    assert base['models'][0]['enabled'] is True               # original untouched
