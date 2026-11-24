"""Generate launch agents after runtimes/config are provisioned; never starts a bot."""
import argparse
from pathlib import Path
import plistlib

parser = argparse.ArgumentParser()
parser.add_argument('--root', type=Path, required=True)
parser.add_argument('--output-dir', type=Path, default=Path.home() / 'Library/LaunchAgents')
args = parser.parse_args()
root = args.root.expanduser().resolve()
assert (root / 'app/.venv/bin/python').exists(), 'Create the Python environment first'
assert (root / 'runtime/launch_service.py').is_file(), 'Install launch_service.py first'
args.output_dir.mkdir(parents=True, exist_ok=True)
for service in ('lavalink', 'bgutil', 'bot'):
    label = 'com.mitao.' + service
    dest = args.output_dir / (label + '.plist')
    data = {
        'Label': label,
        'ProgramArguments': [str(root / 'app/.venv/bin/python'), str(root / 'runtime/launch_service.py'), service],
        'WorkingDirectory': str(root), 'RunAtLoad': True,
        'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 10,
        'ProcessType': 'Background' if service == 'bgutil' else 'Interactive',
        'Umask': 0o077,
        'StandardOutPath': str(root / 'logs' / ('launchd-' + service + '.out.log')),
        'StandardErrorPath': str(root / 'logs' / ('launchd-' + service + '.err.log')),
    }
    if service != 'bgutil':
        data['LowPriorityIO'] = False
    if dest.exists():
        if plistlib.loads(dest.read_bytes()) != data:
            raise SystemExit(f'Existing configuration differs; back it up and review before replacing: {dest}')
    else:
        dest.write_bytes(plistlib.dumps(data))
        dest.chmod(0o600)
    print(dest)
