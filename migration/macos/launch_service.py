"""Installed as <deployment root>/runtime/launch_service.py; contains no secrets."""
from pathlib import Path
import os
import shlex
import sys

BASE = Path(os.environ.get('MITAO_ROOT', Path(__file__).resolve().parent.parent))
RUNTIME = BASE / 'runtime'
APP = BASE / 'app'
os.umask(0o077)
os.environ.update(
    PATH=f'{RUNTIME}/bin:{APP}/.venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
    LANG='en_US.UTF-8', LC_ALL='en_US.UTF-8', PYTHONDONTWRITEBYTECODE='1',
)
for line in (RUNTIME / 'private/lavalink.env').read_text().splitlines():
    if line and not line.lstrip().startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        if key.startswith('LAVALINK_'):
            os.environ[key] = shlex.split(value)[0] if value else ''

mode = sys.argv[1]
if mode == 'bot':
    if not (RUNTIME / 'ALLOW_BOT_START').exists():
        raise SystemExit(0)
    os.chdir(APP)
    args = [str(APP / '.venv/bin/python'), str(APP / 'main.py')]
elif mode == 'lavalink':
    os.chdir(BASE / 'lavalink')
    java = next((RUNTIME / 'java').glob('*/Contents/Home/bin/java'))
    args = [str(java), '-Xms64m', '-Xmx512m',
            '-Djava.io.tmpdir=' + str(RUNTIME / 'tmp'), '-jar', 'Lavalink.jar']
elif mode == 'bgutil':
    os.chdir(RUNTIME / 'bgutil-ytdlp-pot-provider-1.3.2/server')
    args = [str(RUNTIME / 'bin/node'), 'build/main.js']
else:
    raise SystemExit('Expected bot, lavalink or bgutil')
os.execvpe(args[0], args, os.environ)
