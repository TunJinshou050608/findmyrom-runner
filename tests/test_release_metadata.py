from copy import deepcopy
from pathlib import Path
import pytest

from fmr_core.d1_client import D1Client
from fmr_core.firmware_metadata_schema import ensure
from fmr_ingest_stock.device_sync import ensure_base_brands_and_anchor
from fmr_ingest_stock.release_metadata import (
    REVISION,
    google_identity,
    iso_date,
    normalize,
    package_label,
    system_fields,
)


def test_iso_date_and_android_12l_direct_calls():
    assert iso_date('2026-04-01') == '2026-04-01'
    assert iso_date('2026-04-01T14:22:00Z') == '2026-04-01'
    assert iso_date('20260401') == '2026-04-01'
    assert iso_date('2026/04/01') == '2026-04-01'
    assert iso_date('not-a-date') is None
    assert iso_date(None) is None

    assert system_fields('Android 12L', 'google')['android_version'] == '12L'
    assert system_fields('12L (SP2A.220305.013.A3, Mar 2022)', 'google')['android_version'] == '12L'
    device = {'vendor': 'google', 'upstream_id': 'oriole', 'codename': 'oriole'}
    rel = {
        'version': '12L (SP2A.220305.013.A3, Mar 2022)',
        'android_version': '12L (SP2A.220305.013.A3, Mar 2022)',
        'download_url': 'https://dl.google.com/dl/android/aosp/oriole-sp2a.220305.013.a3-factory-12345678.zip',
    }
    norm = normalize(device, rel)
    assert norm['android_version'] == '12L'
    assert norm['display_version'] == 'SP2A.220305.013.A3'


def test_metadata_contract_does_not_run_historical_migrations(tmp_path):
    db = D1Client(local_sqlite_path=str(tmp_path / 'meta.sqlite'))
    try:
        db.execute_script(Path(__file__).parents[1].joinpath('schema.sql').read_text())
        ensure_base_brands_and_anchor(db)
        ensure(db, apply=True)
        assert not db.query(
            "SELECT name FROM sqlite_master WHERE name IN ('stock_firmwares_next','fmr_identity_maintenance')"
        )
    finally:
        db.close()


def test_samsung_metadata_is_scoped_and_complete_identity_stays_intact():
    d = {'vendor': 'samsung', 'upstream_id': 'sm-a716u1', 'codename': 'SM-A716U1'}
    r = {
        'version': 'A716U1UEU9GWF7/A716U1OYM9GWF7/A716U1UEU9GWF7/A716U1UEU9GWF7',
        'region': 'XAA',
        '_official_metadata': {
            'model': 'SM-A716U1',
            'csc': 'XAA',
            'ap': 'A716U1UEU9GWF7',
            'android': '13',
            'date': '2023-07-18',
            'patch': '2023-06-01',
            'source': 'https://doc.samsungmobile.com/SM-A716U1/XAA/doc.html',
        },
    }
    original = deepcopy(r)
    m = normalize(d, r)
    assert m['display_version'] == 'A716U1UEU9GWF7' and m['android_version'] == '13'
    assert m['security_patch'] == '2023-06-01' and m['raw_version'] == r['version']
    assert r == original
    assert normalize(d, {**r, 'region': 'VZW'})['android_version'] is None


@pytest.mark.parametrize(
    'vendor',
    ['xiaomi', 'google', 'samsung', 'oneplus', 'oppo', 'realme', 'vivo', 'asus', 'sony', 'motorola', 'nothing'],
)
def test_shared_metadata_contract_covers_every_active_vendor(vendor):
    device = {'vendor': vendor, 'upstream_id': 'sample', 'codename': 'sample'}
    release = {
        'version': 'BUILD.1',
        'region': 'Global',
        'android_version': 'Android 14',
        'os_raw': 'ColorOS 16',
        'filename': 'firmware.zip',
        'date': '2026-01-01',
    }
    before = deepcopy(release)
    m = normalize(device, release)
    assert m['schema'] == REVISION and m['android_version'] == '14' and m['os_name'] == 'ColorOS' and m['os_version'] == '16'
    assert m['original_filename'] == 'firmware.zip' and release == before


def test_google_real_caption_carrier_first_identity_is_safe():
    device = {'vendor': 'google', 'upstream_id': 'marlin', 'codename': 'marlin', 'name': 'Google Pixel XL'}
    release = {
        'version': '7.1.0 (Verizon, NDE63X, Nov 2016)',
        'region': 'Global',
        'kind': 'factory',
        'download_url': 'https://dl.google.com/dl/android/aosp/marlin-nde63x-factory-8b520419.zip',
        'android_version': '7.1.0',
    }
    assert google_identity(device, release) == ('NDE63X', 'Verizon', ['Verizon'])
    m = normalize(device, release)
    assert m['display_version'] == 'NDE63X' and m['region'] == 'Verizon'

    ota = {
        **release,
        'download_url': 'https://dl.google.com/dl/android/aosp/marlin-ota-qp1a.191005.007.a3-12345678.zip',
        'version': '10.0.0 (QP1A.191005.007.A3, Oct 2019, Verizon)',
    }
    assert google_identity(device, ota) == ('QP1A.191005.007.A3', 'Verizon', ['Verizon'])


def test_nothing_os_numeric_source_is_not_android():
    device = {'vendor': 'nothing', 'upstream_id': 'spacewar', 'codename': 'spacewar'}
    release = {
        'version': 'Spacewar-V3.2-260618-1045',
        'android_version': '3.2',
        'os_raw': '3.2',
        'os_name': 'Nothing OS',
        'os_version': '3.2',
    }
    fields = system_fields('3.2', 'nothing')
    assert fields['android_version'] == 'unknown' and fields['os_name'] == 'Nothing OS'
    assert system_fields('Android 15', 'nothing')['android_version'] == '15'
    assert normalize(device, release)['android_version'] is None


def test_realme_ui_and_sony_source_projection():
    d = {'vendor': 'realme', 'codename': 'sample', 'upstream_id': 'sample'}
    m = normalize(d, {'version': 'build', 'android_version': 'realme UI 16'})
    assert m['os_name'] == 'realme UI' and m['os_version'] == '16' and m['android_version'] is None
    assert system_fields('realme UI 16', 'realme')['os_name'] == 'realme UI'
    d['vendor'] = 'sony'
    m = normalize(
        d,
        {
            'version': '32.1.C.0.401-R19C',
            'bundle': {'service': 'https://app.swup.update.sony.net/service?password=private', 'cdf': '1295-0427', 'cust': 'KDDI JP'},
        },
    )
    assert m['sources']['catalogue'] == 'https://app.swup.update.sony.net/service'
    assert package_label('xiaomi', 'recovery_zip') == 'Recovery / OTA ZIP'
    assert package_label('sony', 'sony_bundle') == 'Sony complete firmware ZIP'


def test_samsung_repeated_ap_dates_do_not_discard_other_official_builds():
    from fmr_ingest_stock.samsung_metadata import parse

    def block(ap, android, dt, patch):
        return (
            f'<div>Build Number : {ap}</div>'
            f'<div>Android version : T(Android {android})</div>'
            f'<div>Release Date : {dt}</div>'
            f'<div>Security patch level : {patch}</div>'
        )

    page = (
        block('A716U1UEU9GWF7', '13', '2023-07-18', '2023-06-01')
        + block('A716U1UEU6DVA1', '11', '2022-04-18', '2022-01-01')
        + block('A716U1UEU6DVA1', '11', '2022-03-04', '2022-01-01')
    )
    result = parse(page, 'SM-A716U1', 'XAA', 'source')
    assert result['A716U1UEU9GWF7']['android'] == '13'
    assert result['A716U1UEU6DVA1']['android'] == '11'
    assert result['A716U1UEU6DVA1']['date'] is None
    assert result['A716U1UEU6DVA1']['patch'] == '2022-01-01'
