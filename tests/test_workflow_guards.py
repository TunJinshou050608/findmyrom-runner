"""Guard production workflow topology and trust boundaries."""
from pathlib import Path
import yaml

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
    assert {s['cron'] for s in pub['on']['schedule']} == {'7,22,37,52 * * * *'}
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
    assert publish_job['timeout-minutes'] == 15
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
