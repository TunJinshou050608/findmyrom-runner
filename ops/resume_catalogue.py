"""Resume the local link publisher and cloud extractor after an explicit start.

Default prints the plan without network access. --apply uses an authenticated
gh CLI; no production secrets, D1 access or firmware downloads on this machine.
"""
import argparse
import subprocess

REPO = 'TunJinshou050608/findmyrom-runner'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    commands = [
        ['gh', 'variable', 'set', 'FMR_D1_WRITES_PAUSED', '--repo', REPO, '--body', 'true'],
        ['gh', 'variable', 'set', 'FMR_EXTRACTION_ENABLED', '--repo', REPO, '--body', 'true'],
        ['gh', 'workflow', 'enable', 'catalogue_publish.yml', '--repo', REPO],
        ['gh', 'workflow', 'enable', 'batch_extract.yml', '--repo', REPO],
        ['gh', 'variable', 'set', 'FMR_D1_WRITES_PAUSED', '--repo', REPO, '--body', 'false'],
        ['gh', 'workflow', 'run', 'catalogue_publish.yml', '--repo', REPO, '--ref', 'main'],
        ['gh', 'workflow', 'run', 'batch_extract.yml', '--repo', REPO, '--ref', 'main',
         '-f', 'dry_run=false', '-f', 'repair_skipped=true'],
    ]
    if not args.apply:
        for command in commands:
            print(' '.join(command))
        print('Plan only. No network or database requests. Add --apply only when D1 may resume.')
        return 0
    for command in commands:
        subprocess.run(command, check=True, timeout=90)
    print('Publisher and cloud extractor dispatched; extraction includes the protected backlog repair.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
