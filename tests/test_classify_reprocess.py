from fmr_tg_scanner.classify_reprocess import classify_row


def row(old_name, old_version, old_android, new_name, new_version, new_android, is_release=True):
    return {
        'id': 1,
        'device': 'cactus',
        'old': {
            'project_name': old_name,
            'build_version_tag': old_version,
            'android_version': old_android,
        },
        'cleaned': {
            'is_release': is_release,
            'project_name': {'value': new_name},
            'project_version': {'value': new_version},
            'android_version': {'value': new_android},
        },
    }


def test_split_embedded_project_version():
    record = classify_row(row('LineageOS 18.1', '0.4', '11', 'LineageOS', '18.1', '11'))
    assert record['decision'] == 'apply'
    assert record['proposed'] == {'project_name': 'LineageOS', 'build_version_tag': '18.1'}
    assert 'split_project_version' in record['reasons']


def test_split_android_from_project_name_and_clear_android_build():
    record = classify_row(row('crDroid 6.23 Android 10', 'Android 10', '10', 'crDroid', '6.23', '10'))
    assert record['decision'] == 'apply'
    assert record['proposed'] == {
        'project_name': 'crDroid',
        'build_version_tag': '6.23',
    }


def test_device_name_in_android_is_cleared():
    record = classify_row(row('TeamWin Recovery Project 3.5', '3.5', 'Xiaomi Redmi 6A',
                               'TeamWin Recovery Project', '3.5', None))
    assert record['decision'] == 'apply'
    assert record['proposed'] == {
        'project_name': 'TeamWin Recovery Project',
        'android_version': None,
    }


def test_semantic_rename_goes_to_review():
    record = classify_row(row('Matrixx', 'v11.1.0', '13', 'Project Matrixx', 'v11.1.0', '13'))
    assert record['decision'] == 'review'
    assert record['proposed'] == {}
    assert record['review_fields'] == ['project_name']


def test_android_format_is_normalized():
    record = classify_row(row('Evolution X', 'v6.2.2', 'A15', 'Evolution X', 'v6.2.2', '15'))
    assert record['decision'] == 'apply'
    assert record['proposed'] == {'android_version': '15'}
    assert 'android_format' in record['reasons']


def test_android_conflict_goes_to_review():
    record = classify_row(row('Evolution X', 'v6.2.2', '11 - 12', 'Evolution X', 'v6.2.2', '11'))
    assert record['decision'] == 'review'
    assert record['proposed'] == {}
    assert record['review_fields'] == ['android_version']


def test_labelled_android_build_version_is_cleared():
    record = classify_row(row('DerpFest', 'Android 14', '14', 'DerpFest', None, '14'))
    assert record['decision'] == 'apply'
    assert record['proposed'] == {'build_version_tag': None}
    assert 'version_field_was_android' in record['reasons']


def test_bare_version_equal_to_android_goes_to_review():
    record = classify_row(row('OxygenOS', '11', '11', 'OxygenOS', None, '11'))
    assert record['decision'] == 'review'
    assert record['proposed'] == {}
    assert record['review_fields'] == ['build_version_tag']


def test_equal_values_are_clean():
    record = classify_row(row('Havoc-OS', 'v4.5', '11', 'HavocOS', '4.5', '11'))
    assert record['decision'] == 'clean'
    assert record['proposed'] == {}
    assert record['review_fields'] == []


def test_non_release_is_review_only():
    record = classify_row(row('Stock', '1.0', '13', None, None, None, is_release=False))
    assert record['decision'] == 'review'
    assert record['reasons'] == ['not_release']
    assert record['proposed'] == {}
