from fmr_tg_scanner.grounded_cleaner import verify
from fmr_tg_scanner.normalize import normalize_legacy


def node(value, quote=None):
    return {'value': value, 'evidence_quote': quote or value}


def extraction(**overrides):
    out = {
        'is_release': True,
        'resource_kind': 'rom',
        'project_name': node('Project', 'Project'),
        'project_version': node(None),
        'android_version': node(None),
        'system_base': node(None),
        'release_stage': node('unofficial'),
        'target_devices': {'codenames': ['cactus'], 'marketing_names': [], 'evidence_quote': 'cactus'},
        'maintainer': {'handle': None, 'display_name': None, 'evidence_quote': None},
        'build_variants': [],
    }
    out.update(overrides)
    return out


def test_android_prefix_and_a12_are_canonicalized():
    text = 'Arrow OS A12 [UNOFFICIAL]\nBy @nrudesu\nDevice: Redmi 6/6A (cereus/cactus)'
    out = normalize_legacy(extraction(
        project_name=node('Arrow OS', 'Arrow OS'),
        project_version=node('A12', 'A12'),
    ), text, 'cactus', {'cactus': ['Redmi 6A']})
    assert out['android_version']['value'] == '12'
    assert out['project_version']['value'] == 'A12'
    assert out['_normalization']['clear_project_version'] is False


def test_android_version_is_not_a_build_version():
    text = 'PixelExperiencePlus Android 10 [UNOFFICIAL]\nDevice: Redmi 6/6A (cereus/cactus)'
    out = normalize_legacy(extraction(
        project_name=node('PixelExperiencePlus', 'PixelExperiencePlus'),
        project_version=node('Android 10', 'Android 10'),
    ), text, 'cactus', {'cactus': ['Redmi 6A']})
    assert out['project_version']['value'] is None
    assert out['_normalization']['clear_project_version'] is True
    assert out['android_version']['value'] == '10'


def test_embedded_project_version_beats_android_version():
    text = 'crDroid 6.23 Android 10 [UNOFFICIAL]\nDevice: Redmi 6/6A (cereus/cactus)'
    out = normalize_legacy(extraction(
        project_name=node('crDroid 6.23', 'crDroid 6.23'),
        project_version=node('Android 10', 'Android 10'),
    ), text, 'cactus', {'cactus': ['Redmi 6A']})
    assert out['project_name']['value'] == 'crDroid'
    assert out['project_version']['value'] == '6.23'
    assert out['_normalization']['clear_project_version'] is False


def test_vendor_version_in_notes_is_not_project_version():
    text = 'LineageOS 18.1 [UNOFFICIAL]\nDevice: Redmi 6/6A (cereus/cactus)\nNotes: Use the vendor version 0.4'
    out = normalize_legacy(extraction(
        project_name=node('LineageOS 18.1', 'LineageOS 18.1'),
        project_version=node('0.4', '0.4'),
    ), text, 'cactus', {'cactus': ['Redmi 6A']})
    assert out['project_name']['value'] == 'LineageOS'
    assert out['project_version']['value'] == '18.1'


def test_brand_name_never_becomes_android_version():
    text = 'TeamWin Recovery Project 3.5\nDevice: Xiaomi Redmi 6A (cactus)'
    out = normalize_legacy(extraction(
        resource_kind='recovery',
        project_name=node('TeamWin Recovery Project 3.5', 'TeamWin Recovery Project 3.5'),
        android_version=node('Xiaomi Redmi 6A', 'Xiaomi Redmi 6A'),
        target_devices={'codenames': ['cactus'], 'marketing_names': [], 'evidence_quote': 'cactus'},
    ), text, 'cactus', {'cactus': ['Redmi 6A']})
    assert out['android_version']['value'] is None
    assert out['_normalization']['clear_android'] is True


def test_codenames_are_removed_from_marketing_names():
    text = 'HavocOS 3.12 Android 10 [UNOFFICIAL]\nDevice: Xiaomi Redmi 6/6A (Cereus/Cactus)'
    out = normalize_legacy(extraction(
        project_name=node('HavocOS 3.12', 'HavocOS 3.12'),
        android_version=node('10', '10'),
        target_devices={
            'codenames': ['cereus', 'cactus'],
            'marketing_names': ['Xiaomi Redmi 6', 'Cereus', 'Cactus'],
            'evidence_quote': 'Cereus',
        },
    ), text, 'cactus', {'cactus': ['Redmi 6A'], 'cereus': ['Redmi 6']})
    assert out['target_devices']['marketing_names'] == ['Xiaomi Redmi 6']
    verify(out, text)
