"""Keep production credentials off non-main workflow jobs."""
from pathlib import Path
import yaml


def test_production_jobs_require_main_and_never_use_untrusted_pr_target():
    root = Path(__file__).resolve().parents[1] / '.github/workflows'
    production = 0
    for path in root.glob('*.yml'):
        workflow = yaml.safe_load(path.read_text())
        triggers = workflow.get('on', workflow.get(True, {})) or {}
        for name, job in workflow.get('jobs', {}).items():
            if job.get('environment') != 'production':
                continue
            production += 1
            assert "github.ref == 'refs/heads/main'" in str(job.get('if', '')), (path.name, name)
            assert 'pull_request_target' not in triggers, path.name
    assert production >= 10
