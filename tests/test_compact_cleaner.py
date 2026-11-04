import pytest

from fmr_tg_scanner.cache_probe import (
    NAMES, TEXT, TARGET, from_compact, from_named, from_payload,
)
from fmr_tg_scanner.grounded_cleaner import compact_to_nodes, verify

ROWS = [True, 'rom', ['Project Blaze', 'Pro'], ['v1.4', 'v1.'], ['Android 12L', 'And'],
        None, 'official', [['alioth'], 'Poc'], [['Poco F3', 'Mi 11x', 'Redmi K40'], 'Poc'],
        ['@AbrarNoob', '@Ab'], None, None]


def test_compact_accepts_wrapped_array_and_string_boolean():
    out = from_compact({'v': [['true', *ROWS[1:]]]})
    assert out['is_release'] is True
    assert out['resource_kind'] == 'rom'
    assert out['codenames'] == ['alioth']
    assert out['_anchors']['maintainer'] == '@Ab'


def test_compact_rejects_wrong_row_count():
    with pytest.raises(ValueError, match='invalid_row_count'):
        from_compact({'v': ROWS[:-1]})


def test_named_repair_defaults_missing_keys_to_null():
    out = from_named({
        'is_release': True,
        'resource_kind': 'rom',
        'project_name': ['Project Blaze', 'Pro'],
        'project_version': ['v1.4', 'v1.'],
        'android_version': ['Android 12L', 'And'],
        'release_stage': 'official',
        'target_codenames': [['alioth'], 'Poc'],
        'target_marketing_names': [['Poco F3', 'Mi 11x', 'Redmi K40'], 'Poc'],
        'maintainer_handle': ['@AbrarNoob', '@Ab'],
    })
    assert out['build_variants'] == []
    assert out['display_name'] is None
    assert from_payload({'is_release': True})['is_release'] is True


def test_compact_nodes_pass_strict_verification():
    nodes = compact_to_nodes({'v': ROWS}, TEXT, TARGET, NAMES)
    verify(nodes, TEXT)
    assert nodes['project_name'] == {'value': 'Project Blaze', 'evidence_quote': 'Pro'}
    assert nodes['maintainer']['handle'] == '@AbrarNoob'
    assert nodes['target_devices']['codenames'] == ['alioth']
