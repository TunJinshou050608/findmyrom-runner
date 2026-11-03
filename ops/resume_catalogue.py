"""Resume the single local D1 publisher after an explicit start.

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
        ['gh', 'workflow', 'enable', 'catalogue_publish.yml', '--repo', REPO],
        ['gh', 'variable', 'set', 'FMR_D1_WRITES_PAUSED', '--repo', REPO, '--body', 'false'],
        ['gh', 'workflow', 'run', 'catalogue_publish.yml', '--repo', REPO, '--ref', 'main'],
    ]
    if not args.apply:
        for command in commands:
            print(' '.join(command))
        print('Plan only. No network or database requests. Add --apply only when D1 may resume.')
        return 0
    for command in commands:
        subprocess.run(command, check=True, timeout=90)
    print('D1 publisher dispatched. TG retrieval and VPS deployment remain separately controlled.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
