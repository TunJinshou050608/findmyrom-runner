"""Guard production workflow topology and trust boundaries."""
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import yaml

from fmr_ingest_stock.pipeline import cli as pipeline_cli

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github/workflows'
ACTIVE_WORKFLOWS = {
    '_collect_vendor.yml',
    'catalogue_schedule.yml',
    'catalogue_publish.yml',
    'deploy_schema.yml',
    'telegram_fetch.yml',
    'telegram_clean.yml',
    'tg_cache_probe.yml',
    'tg_reprocess.yml',
    'tg_dedup.yml',
    'ci.yml',
    'services.yml',
}


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding='utf-8'))


def test_only_supported_entry_points_are_active():
    assert {p.name for p in WORKFLOWS.glob('*.yml')} == ACTIVE_WORKFLOWS
    names = [load(name)['name'] for name in ACTIVE_WORKFLOWS]
    assert len(names) == len(set(names))


def test_production_jobs_require_main_and_never_use_untrusted_pr_target():
    for path in WORKFLOWS.glob('*.yml'):
        workflow = load(path.name)
        assert 'pull_request_target' not in workflow['on']
        for name, job in workflow['jobs'].items():
            if path.name != 'tg_cache_probe.yml':
                for step in job.get('steps', []):
                    if step.get('uses') == 'actions/checkout@v4' and 'repository' not in step.get('with', {}):
                        assert step.get('with', {}).get('persist-credentials') is False, (path.name, name)
            if job.get('environment') != 'production':
                continue
            assert "github.ref == 'refs/heads/main'" in job.get('if', ''), (path.name, name)


def test_tg_has_four_local_collectors_one_cleaner_and_persistent_local_state():
    w = load('telegram_fetch.yml')
    job = w['jobs']['fetch']
    assert job['strategy'] == {'fail-fast': False, 'max-parallel': 4, 'matrix': {'worker': [0, 1, 2, 3]}}
    assert 'schedule' in w['on']
    stages = [job, load('telegram_clean.yml')['jobs']['clean'], load('catalogue_publish.yml')['jobs']['telegram']]
    for stage in stages:
        assert stage['runs-on'] == ['self-hosted', 'findmyrom-metadata']
        assert 'FMR_TG_STATE_DIR' in stage['env']
        assert not any(step.get('uses', '').startswith('actions/setup-python') for step in stage['steps'])
        assert 'findmyrom-tg01-test' not in str(stage['steps'])
        assert 'publish_private' not in str(stage['steps'])
    assert job['concurrency']['group'] == 'tg-local-worker-${{ matrix.worker }}'
    assert load('telegram_clean.yml')['jobs']['clean']['concurrency']['group'] == 'tg-local-cleaner'
    initial = w['jobs']['initialize']['steps']
    checkout = next(step for step in initial if step.get('with', {}).get('repository') == 'TunJinshou050608/findmyrom-tg01-test')
    assert checkout['if'] == "steps.local.outputs.needed == 'true'"
    assert checkout['with']['persist-credentials'] is False


def test_cleaning_cannot_access_d1_and_only_unified_publication_imports_resources():
    for name in ['telegram_fetch.yml', 'telegram_clean.yml']:
        text = (WORKFLOWS / name).read_text(encoding='utf-8')
        assert 'CF_API_TOKEN' not in text
        assert 'local_runtime publish' not in text
    pub = load('catalogue_publish.yml')
    assert 'local_runtime publish' in str(pub['jobs']['telegram']['steps'])
    assert 'scope' in pub['on']['workflow_dispatch']['inputs']
    assert 'FMR_TG_D1_WRITES_PAUSED' in pub['jobs']['telegram']['if']
    assert "inputs.scope != 'stock'" in pub['jobs']['telegram']['if']
    for path in WORKFLOWS.glob('*.yml'):
        assert 'fmr_ingest_stock.catalogue_publish_monitor' not in path.read_text(encoding='utf-8')


def test_ci_has_no_production_repair_or_deployment_side_effects():
    assert set(load('ci.yml')['jobs']) == {'verify'}
    assert 'secrets.' not in (WORKFLOWS / 'ci.yml').read_text(encoding='utf-8')


def test_operational_service_cadences_and_locks_are_preserved():
    w = load('services.yml')
    assert {s['cron'] for s in w['on']['schedule']} == {'*/15 * * * *', '43 */6 * * *'}
    assert set(w['jobs']) == {'sony_bundle', 'public_index', 'check'}
    assert w['jobs']['sony_bundle']['runs-on'] == 'ubuntu-latest'
    assert w['jobs']['sony_bundle']['concurrency']['group'] == 'sony-complete-bundle-cache'


def test_stock_collector_workflows_have_zero_d1_secrets_and_zero_self_handoffs():
    for name in ('_collect_vendor.yml', 'catalogue_schedule.yml'):
        text = (WORKFLOWS / name).read_text(encoding='utf-8')
        workflow = load(name)
        assert workflow['permissions'] == {'contents': 'read'}
        for secret_name in ('CF_API_TOKEN', 'CF_ACCOUNT_ID', 'CF_D1_DATABASE_ID', 'SIGN_PRIVATE_KEY_HEX'):
            assert secret_name not in text, (name, secret_name)
        assert 'gh workflow run' not in text
        assert 'staged_catalogue' not in text
        assert 'catalogue-code' not in text

    collector = load('_collect_vendor.yml')
    assert set(collector['on']) == {'workflow_call'}
    collect_job = collector['jobs']['collect']
    assert collect_job['if'] == "github.ref == 'refs/heads/main'"
    assert collect_job['timeout-minutes'] == 20
    assert collect_job['concurrency'] == {
        'group': 'stock-collect-${{ inputs.vendor }}',
        'cancel-in-progress': False,
    }
    assert 'fmr_ingest_stock.pipeline.cli collect' in str(collect_job['steps'])

    schedule = load('catalogue_schedule.yml')
    assert {s['cron'] for s in schedule['on']['schedule']} == {'12,42 * * * *'}
    assert set(schedule['jobs']) == {'plan', 'collect'}
    assert schedule['jobs']['plan']['if'] == "github.ref == 'refs/heads/main'"
    assert 'config/vendors.yaml' in str(schedule['jobs']['plan']['steps'])
    matrix_job = schedule['jobs']['collect']
    assert matrix_job['uses'] == './.github/workflows/_collect_vendor.yml'
    assert matrix_job['if'] == "needs.plan.outputs.has_vendors == 'true'"
    assert matrix_job['strategy']['fail-fast'] is False
    assert matrix_job['strategy']['max-parallel'] == 8


def test_publisher_and_schema_workflows_are_decoupled_and_serialized():
    pub_text = (WORKFLOWS / 'catalogue_publish.yml').read_text(encoding='utf-8')
    pub = load('catalogue_publish.yml')
    assert pub['permissions'] == {'contents': 'read'}
    assert {s['cron'] for s in pub['on']['schedule']} == {'0 22 * * *'}
    assert set(pub['jobs']) == {'publish', 'telegram'}
    for forbidden in (
        'catalogue_publish_monitor',
        'schema_migrations',
        'staged_catalogue',
        'gh workflow run',
        'publisher-code',
        'samsung-publisher-code',
    ):
        assert forbidden not in pub_text

    publish_job = pub['jobs']['publish']
    assert publish_job['environment'] == 'production'
    assert publish_job['timeout-minutes'] == 360
    assert publish_job['concurrency'] == {
        'group': 'catalogue-d1-publisher',
        'cancel-in-progress': False,
    }
    assert 'fmr_ingest_stock.pipeline.cli publish' in str(publish_job['steps'])

    schema_text = (WORKFLOWS / 'deploy_schema.yml').read_text(encoding='utf-8')
    schema = load('deploy_schema.yml')
    assert schema['permissions'] == {'contents': 'read'}
    assert set(schema['jobs']) == {'migrate'}
    assert 'gh workflow run' not in schema_text
    migrate_job = schema['jobs']['migrate']
    assert migrate_job['environment'] == 'production'
    assert migrate_job['if'] == "github.ref == 'refs/heads/main'"
    assert migrate_job['concurrency'] == publish_job['concurrency']
    assert 'fmr_core.schema_migrations --apply' in str(migrate_job['steps'])
    assert 'fmr_core.firmware_metadata_schema --apply' in str(migrate_job['steps'])

    # All v4 stock/schema jobs check out directly into isolated $GITHUB_WORKSPACE
    # and reuse the immutable /opt/fmr-venvs cache with flock + temp fallback.
    v4_jobs = [
        load('_collect_vendor.yml')['jobs']['collect'],
        load('catalogue_schedule.yml')['jobs']['plan'],
        publish_job,
        migrate_job,
    ]
    for job in v4_jobs:
        assert 'defaults' not in job
        checkout = next(s for s in job['steps'] if s.get('uses') == 'actions/checkout@v4')
        assert checkout['with'] == {'persist-credentials': False}
        steps_blob = str(job['steps'])
        assert '/opt/fmr-venvs' in steps_blob
        assert 'flock' in steps_blob


def _extract_cli_tokens_from_workflow_run(run_script: str, replacements: dict[str, str]) -> list[str]:
    joined = run_script.replace('\\\n', ' ')
    for key, val in replacements.items():
        joined = joined.replace(key, val)
    match = re.search(r'python\s+-m\s+fmr_ingest_stock\.pipeline\.cli\s+(.+)', joined)
    assert match is not None, f'Could not locate pipeline CLI invocation in:\n{run_script}'
    return shlex.split(match.group(1).splitlines()[0])


def test_workflow_pipeline_cli_args_never_drift(monkeypatch, tmp_path: Path):
    parser = pipeline_cli.build_parser()

    # Exact flag orders used in _collect_vendor.yml, catalogue_publish.yml, and top-level order
    args_collect_sub = parser.parse_args(
        ['collect', '--vendor', 'google', '--state-dir', '/tmp/s', '--max-seconds', '600']
    )
    assert args_collect_sub.command == 'collect'
    assert args_collect_sub.vendor == 'google'
    assert args_collect_sub.state_dir == '/tmp/s'
    assert args_collect_sub.max_seconds == 600

    args_publish_sub = parser.parse_args(
        ['publish', '--state-dir', '/tmp/s', '--batch-size', '40']
    )
    assert args_publish_sub.command == 'publish'
    assert args_publish_sub.state_dir == '/tmp/s'
    assert args_publish_sub.batch_size == 40

    args_collect_top = parser.parse_args(
        ['--state-dir', '/tmp/s', '--vendors-yaml', '/tmp/v.yaml', 'collect', '--vendor', 'google']
    )
    assert args_collect_top.command == 'collect'
    assert args_collect_top.vendor == 'google'
    assert args_collect_top.state_dir == '/tmp/s'
    assert args_collect_top.vendors_yaml == '/tmp/v.yaml'
    assert args_collect_top.max_seconds is None

    # Subcommand flags may override top-level flags, while omitted subcommand flags must not clobber them
    args_override = parser.parse_args(
        ['--state-dir', '/tmp/top', '--vendors-yaml', '/tmp/v.yaml', 'collect', '--vendor', 'google', '--state-dir', '/tmp/sub']
    )
    assert args_override.state_dir == '/tmp/sub'
    assert args_override.vendors_yaml == '/tmp/v.yaml'

    # Parse actual commands embedded in the workflow YAML files so future workflow edits cannot drift
    collect_wf = load('_collect_vendor.yml')
    collect_step = next(
        s for s in collect_wf['jobs']['collect']['steps']
        if 'fmr_ingest_stock.pipeline.cli collect' in s.get('run', '')
    )
    collect_tokens = _extract_cli_tokens_from_workflow_run(
        collect_step['run'],
        {
            '${{ inputs.vendor }}': 'google',
            '${FMR_STAGING_V4_DIR:-/var/lib/findmyrom/staging-v4}': '/tmp/s',
            '${{ inputs.max_seconds }}': '600',
        },
    )
    parsed_wf_collect = parser.parse_args(collect_tokens)
    assert parsed_wf_collect.command == 'collect'
    assert parsed_wf_collect.vendor == 'google'
    assert parsed_wf_collect.state_dir == '/tmp/s'
    assert parsed_wf_collect.max_seconds == 600

    publish_wf = load('catalogue_publish.yml')
    publish_step = next(
        s for s in publish_wf['jobs']['publish']['steps']
        if 'fmr_ingest_stock.pipeline.cli publish' in s.get('run', '')
    )
    publish_tokens = _extract_cli_tokens_from_workflow_run(
        publish_step['run'],
        {
            '$FMR_STAGING_V4_DIR': '/tmp/s',
            "${{ inputs.batch_size || '40' }}": '40',
        },
    )
    parsed_wf_publish = parser.parse_args(publish_tokens)
    assert parsed_wf_publish.command == 'publish'
    assert parsed_wf_publish.state_dir == '/tmp/s'
    assert parsed_wf_publish.batch_size == 40
    assert parsed_wf_publish.stop_before_utc_midnight == 60

    # End-to-end main() dispatch passes max_collect_seconds=600 and state_dir into run_collector
    captured_collect: list[dict] = []

    def fake_run_collector(vendor_id: str, **kwargs):
        captured_collect.append({'vendor_id': vendor_id, **kwargs})
        return {'vendor': vendor_id, 'status': 'ok'}

    monkeypatch.setattr(pipeline_cli, 'run_collector', fake_run_collector)
    rc = pipeline_cli.main(
        ['collect', '--vendor', 'google', '--state-dir', str(tmp_path), '--max-seconds', '600']
    )
    assert rc == 0
    assert len(captured_collect) == 1
    assert captured_collect[0]['vendor_id'] == 'google'
    assert captured_collect[0]['state_dir'] == tmp_path
    assert captured_collect[0]['max_collect_seconds'] == 600

    # End-to-end main() dispatch for publish accepts SIGN_PRIVATE_KEY_HEX from catalogue_publish.yml
    assert 'SIGN_PRIVATE_KEY_HEX' in publish_wf['jobs']['publish']['env']
    monkeypatch.delenv('ED25519_PRIVATE_KEY_HEX', raising=False)
    monkeypatch.delenv('FMR_ED25519_PRIVATE_KEY', raising=False)
    monkeypatch.setenv('SIGN_PRIVATE_KEY_HEX', '11' * 32)
    captured_publish: list[dict] = []

    class DummyD1:
        def __init__(self, local_sqlite_path=None):
            self.local_sqlite_path = local_sqlite_path
            self.closed = False

        def close(self):
            self.closed = True

    def fake_run_publisher(**kwargs):
        captured_publish.append(kwargs)
        return {'status': 'ok', 'published': 0}

    monkeypatch.setattr(pipeline_cli, 'D1Client', DummyD1)
    monkeypatch.setattr(pipeline_cli, 'run_publisher', fake_run_publisher)
    rc_pub = pipeline_cli.main(
        [
            'publish',
            '--state-dir',
            str(tmp_path),
            '--batch-size',
            '40',
            '--stop-before-utc-midnight',
            '60',
        ]
    )
    assert rc_pub == 0
    assert len(captured_publish) == 1
    assert captured_publish[0]['state_dir'] == tmp_path
    assert captured_publish[0]['batch_size'] == 40
    assert captured_publish[0]['stop_before_utc_midnight_seconds'] == 60
    assert captured_publish[0]['private_key_hex'] == '11' * 32
    assert captured_publish[0]['db'].closed is True


def test_local_collect_script_and_vendors_yaml_contract():
    script = ROOT / 'ops' / 'run_local_collect.sh'
    assert script.is_file(), 'ops/run_local_collect.sh must exist'
    mode = stat.S_IMODE(script.stat().st_mode)
    assert mode == 0o755, f'Expected mode 0o755, got {oct(mode)}'

    syntax = subprocess.run(['bash', '-n', str(script)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    text = script.read_text(encoding='utf-8')
    assert 'flock' in text
    assert 'local-collect-daemon.lock' in text
    assert 'local_collect_daemon.py' in text
    assert 'FMR_STAGING_V4_DIR' in text

    daemon_script = ROOT / 'ops' / 'local_collect_daemon.py'
    assert daemon_script.is_file(), 'ops/local_collect_daemon.py must exist'
    from ops import local_collect_daemon

    daemon_parser = local_collect_daemon.build_parser()
    once_args = daemon_parser.parse_args(['--once', '--state-dir', '/tmp/s'])
    assert once_args.once is True
    assert once_args.daemon is False
    default_args = daemon_parser.parse_args(['--state-dir', '/tmp/s'])
    assert default_args.once is False

    vendors_data = yaml.safe_load((ROOT / 'config' / 'vendors.yaml').read_text(encoding='utf-8'))
    vendors = vendors_data['vendors']
    assert vendors['google']['verify_workers'] == 1
    assert vendors['google']['max_devices'] == 25
    enabled = {vid for vid, cfg in vendors.items() if cfg.get('enabled') is True}
    assert enabled == {'google', 'samsung', 'xiaomi', 'oneplus', 'oppo', 'realme', 'asus', 'vivo'}


def test_local_collect_daemon_adaptive_sleep_and_next_due(tmp_path: Path):
    import sqlite3
    from fmr_ingest_stock.pipeline.collector import _open_collector_db
    from ops import local_collect_daemon

    db_path = tmp_path / 'collectors' / 'google.sqlite'
    conn = _open_collector_db(db_path)
    now_ts = 10_000

    try:
        # 1. Any device with last_checked_at == 0 -> 0s sleep (immediate next pass)
        conn.execute(
            """
            INSERT INTO collector_devices (
                codename, brand_id, marketing_name, model_numbers_json,
                boot_partition_type, device_sha256, updated_at, last_checked_at
            ) VALUES
                ('dev_fresh', 'google', 'Fresh', '[]', 'init_boot', 'h1', ?, ?),
                ('dev_unseen', 'google', 'Unseen', '[]', 'init_boot', 'h2', ?, 0)
            """,
            (now_ts, now_ts - 100, now_ts),
        )
        conn.commit()
        assert local_collect_daemon.compute_vendor_sleep_seconds(db_path, now_ts=now_ts) == 0

        # 2. Any device older than freshness window (now - 1800) -> 0s sleep
        conn.execute(
            "UPDATE collector_devices SET last_checked_at = ? WHERE codename = 'dev_unseen'",
            (now_ts - 1801,),
        )
        conn.commit()
        assert local_collect_daemon.compute_vendor_sleep_seconds(db_path, now_ts=now_ts) == 0

        # 3. 100% of devices fresh: oldest checked 1680s ago -> next due in 120s -> sleep 120s
        conn.execute(
            "UPDATE collector_devices SET last_checked_at = ? WHERE codename = 'dev_unseen'",
            (now_ts - 1680,),
        )
        conn.commit()
        assert local_collect_daemon.compute_vendor_sleep_seconds(db_path, now_ts=now_ts) == 120

        # 4. 100% of devices checked just now (10s ago -> 1790s remaining) -> clamped to max 300s
        conn.execute(
            "UPDATE collector_devices SET last_checked_at = ?",
            (now_ts - 10,),
        )
        conn.commit()
        assert local_collect_daemon.compute_vendor_sleep_seconds(db_path, now_ts=now_ts) == 300

        # 5. Oldest device ages in 5s -> clamped to min 30s
        conn.execute(
            "UPDATE collector_devices SET last_checked_at = ? WHERE codename = 'dev_unseen'",
            (now_ts - 1795,),
        )
        conn.commit()
        assert local_collect_daemon.compute_vendor_sleep_seconds(db_path, now_ts=now_ts) == 30

        # 6. DEFERRED_VERIFY next_retry_at due in 90s wakes sooner than device freshness (1790s)
        conn.execute(
            "UPDATE collector_devices SET last_checked_at = ?",
            (now_ts - 10,),
        )
        conn.execute(
            """
            INSERT INTO collector_releases (
                identity_key, device_codename, model_code, region_code, version_str,
                package_type, conflict_sig, input_sha256, verify_state,
                failure_count, next_retry_at, updated_at
            ) VALUES ('k1', 'dev_fresh', 'dev_fresh', 'GLOBAL', '1.0', 'factory_zip', 'c', 'i', 'DEFERRED_VERIFY', 1, ?, ?)
            """,
            (now_ts + 90, now_ts),
        )
        conn.commit()
        assert local_collect_daemon.compute_vendor_sleep_seconds(db_path, now_ts=now_ts) == 90

        # 7. Batch hit max_devices / max_collect_seconds -> 0s sleep even if SQLite shows fresh
        assert (
            local_collect_daemon.compute_vendor_sleep_seconds(
                db_path,
                now_ts=now_ts,
                last_result={'devices_discovered': 100, 'devices_checked': 25},
                vendor_cfg={'max_devices': 25, 'max_collect_seconds': 600},
                elapsed_seconds=12.0,
            )
            == 0
        )
        assert (
            local_collect_daemon.compute_vendor_sleep_seconds(
                db_path,
                now_ts=now_ts,
                last_result={'devices_discovered': 25, 'devices_checked': 25},
                vendor_cfg={'max_devices': 25, 'max_collect_seconds': 600},
                elapsed_seconds=601.0,
            )
            == 0
        )

        # 8. Non-zero exit code -> min_sleep_seconds backoff
        assert local_collect_daemon.compute_vendor_sleep_seconds(db_path, now_ts=now_ts, last_rc=1) == 30
    finally:
        conn.close()
