"""Pure official OEM host validation, refresh descriptors, and codebase domain-purity guards."""
from pathlib import Path
import pytest

from fmr_ingest_stock.download_refresh import (
    PREFIX,
    expiring,
    fingerprint,
    renewable,
    valid_release,
    validate,
)
from fmr_ingest_stock.oem_sources import canonical_code, download_origin, safe_url

RUNNER_ROOT = Path(__file__).parents[1]


def test_safe_url_rejects_non_https_ports_credentials_and_whitespace():
    parsed = safe_url('https://dl.google.com/dl/android/aosp/husky.zip')
    assert parsed.hostname == 'dl.google.com'

    for bad, issue in [
        ('http://dl.google.com/husky.zip', 'scheme'),
        ('https:///husky.zip', 'host'),
        ('https://user:pass@dl.google.com/husky.zip', 'credentials'),
        ('https://dl.google.com:8443/husky.zip', 'port'),
        ('https://dl.google.com/husky file.zip', 'whitespace'),
    ]:
        with pytest.raises(ValueError) as exc_info:
            safe_url(bad)
        assert issue in exc_info.value.catalogue_url_issues


def test_download_origin_strictly_matches_official_cdn_hosts_only():
    assert download_origin('google', 'https://dl.google.com/dl/android/aosp/husky.zip') == 'oem'
    assert download_origin('xiaomi', 'https://bigota.d.miui.com/V14.0/alioth.zip') == 'oem'
    assert download_origin('xiaomi', 'https://bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com/V14.0/alioth.zip') == 'oem'
    assert download_origin('sony', 'https://software.sonymobile.com/content') == 'oem'
    assert download_origin('sony', 'https://edge.swup.sony.net/content') == 'oem'
    assert download_origin('oneplus', 'https://gauss-compota-c-cn.allawnfs.com/pkg.zip') == 'oem'
    assert download_origin('oneplus', 'https://android.googleapis.com/packages/ota-api/package/aabb1234.zip') == 'oem'
    assert download_origin('tecno', 'https://android.googleapis.com/packages/ota-api/package/aabb1234.zip') == 'oem'

    # Rejects arbitrary android.googleapis.com paths and foreign/mirror hosts
    assert download_origin('oneplus', 'https://android.googleapis.com/unrelated/file.zip') is None
    assert download_origin('google', 'https://android.googleapis.com/packages/ota-api/package/aabb1234.zip') is None
    assert download_origin('google', 'https://dl.google.com.evil.example/sample.zip') is None
    assert download_origin('motorola', 'https://mirrors.loli' + 'net.com/sample.zip') is None


def test_canonical_code_preserves_tg_scanner_contract():
    assert canonical_code('google', 'sample') == 'google-sample'
    assert canonical_code('motorola', 'sample') == 'motorola-sample'
    assert canonical_code('xiaomi', 'ishtar') == 'ishtar'
    long_code = canonical_code('motorola', 'very-long-model-identity-' * 5)
    assert len(long_code) <= 32
    assert long_code == canonical_code('motorola', 'very-long-model-identity-' * 5)
    with pytest.raises(ValueError):
        canonical_code('xiaomi', '---')


def test_oplus_refresh_descriptor_requires_official_download_check_gate():
    gate = 'https://component-ota-sg.allawnos.com/downloadCheck?token=abc_123&region=sg'
    rel = {
        'vendor': 'oneplus',
        'version': 'CPH2653_15.0.0.401(EX01)',
        'download_url': 'https://gauss.allawnofs.com/CPH2653_15.0.0.401.zip?x-oss-expires=60&x-oss-signature=sig',
    }
    assert expiring(rel['download_url']) is True
    wrapped = renewable(rel, gate)
    assert valid_release('oneplus', wrapped)
    assert wrapped['download_url'] == PREFIX + fingerprint(wrapped['refresh_source'])
    assert wrapped['refresh_source'] == {
        'schema': 1,
        'vendor': 'oneplus',
        'version': 'CPH2653_15.0.0.401(EX01)',
        'gate_url': gate,
        'filename': 'CPH2653_15.0.0.401.zip',
    }

    for invalid_gate in [
        'https://evil.example/downloadCheck?token=1',
        'https://component-ota-sg.allawnos.com/download?token=1',
        'https://component-ota-sg.allawnos.com/downloadCheck',
        'http://component-ota-sg.allawnos.com/downloadCheck?token=1',
    ]:
        with pytest.raises(ValueError):
            validate({**wrapped['refresh_source'], 'gate_url': invalid_gate})


def test_codebase_strict_third_party_domain_purity_guard():
    banned = (
        'fwatlas' + '.com',
        'xmfirmwareupdater' + '.com',
        'XiaomiFirmware' + 'Updater',
        'igoreisberg' + '.com',
        'otachecker' + '.com',
        'danielspringer' + '.at',
        'lolinet' + '.com',
    )
    scan_dirs = ('fmr_core', 'fmr_ingest_stock', 'fmr_extractor', 'tests')
    violations = []
    for folder in scan_dirs:
        base = RUNNER_ROOT / folder
        for py_file in sorted(base.rglob('*.py')):
            if py_file.name == 'test_workflow_guards.py':
                continue
            text = py_file.read_text(encoding='utf-8')
            for marker in banned:
                if marker in text:
                    violations.append(f'{py_file.relative_to(RUNNER_ROOT)}: {marker}')
    for root_py in sorted(RUNNER_ROOT.glob('*.py')):
        text = root_py.read_text(encoding='utf-8')
        for marker in banned:
            if marker in text:
                violations.append(f'{root_py.relative_to(RUNNER_ROOT)}: {marker}')
    assert violations == [], f'Banned third-party markers found: {violations}'
