"""Check a built image with synthetic data, no network and a disposable volume."""
import argparse
import subprocess
import time
from uuid import uuid4


PROBE = '''
import json
import os
from http.cookiejar import CookieJar
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

assert os.getuid() == 10001
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
    urllib.request.HTTPCookieProcessor(CookieJar()))
base = 'http://127.0.0.1:5500'
status = json.load(opener.open(base + '/api/access/status', timeout=3))
assert status['local'] is True
for path in ('/', '/accounts', '/star-day', '/mobile-rights', '/recharge/', '/static/app.js'):
    with opener.open(base + path, timeout=3) as response:
        assert response.status == 200, path
        assert response.url.startswith(base + '/access?'), path
request = urllib.request.Request(base + '/api/access/status', headers={'Host': 'workspace.example'})
assert json.load(opener.open(request, timeout=3))['local'] is False
request = urllib.request.Request(base + '/api/logins', headers={'Host': 'workspace.example'})
try:
    opener.open(request, timeout=3)
except urllib.error.HTTPError as error:
    assert error.code == (401 if status['configured'] else 428), error.code
else:
    raise AssertionError('Anonymous access must require password login or setup')
password = 'synthetic-docker-smoke-password'
endpoint = '/api/access/login' if status['configured'] else '/api/access/password'
payload = {'password': password, 'confirmation': password}
request = urllib.request.Request(base + endpoint, data=json.dumps(payload).encode(),
    headers={'Content-Type': 'application/json'})
assert opener.open(request, timeout=3).status == 200
for path in ('/', '/accounts', '/star-day', '/mobile-rights', '/recharge/', '/static/app.js',
             '/api/logins', '/recharge/api/accounts'):
    with opener.open(base + path, timeout=3) as response:
        assert response.status == 200 and response.url == base + path, path
for path in ('/app/.local', '/app/analysis', '/app/.git', '/app/.env', '/app/tests',
             '/app/recharge/.local', '/app/recharge/member_benefits_runtime/.cache'):
    assert not Path(path).exists(), path
assert Path('/app/app_login_public_key.json').is_file()
assert Path('/app/mobile_rights_gateway.json').is_file()
subprocess.run(['node', '--input-type=module', '-e',
    'import { createCanvas } from "@napi-rs/canvas"; '
    'if (!createCanvas(2, 2).toBuffer("image/png").length) process.exit(1)'],
    cwd='/app/recharge', check=True)
from runtime_paths import DATA_DIR, RECHARGE_DATA_DIR
assert DATA_DIR == Path('/data')
assert RECHARGE_DATA_DIR == Path('/data/recharge')
'''

SAVE = '''
from hashlib import sha256
from pathlib import Path
from login_storage import DatabaseLoginStorage
storage = DatabaseLoginStorage('/data/accounts.sqlite3')
storage.save_credentials('docker-smoke', 'synthetic-account', {'token': 'synthetic-token'})
storage.save_activity_data('docker-smoke', {'task': 'synthetic-task'})
key = Path('/data/credentials.key')
assert key.stat().st_mode & 0o777 == 0o600
Path('/data/key-digest').write_text(sha256(key.read_bytes()).hexdigest())
Path('/data/recharge').mkdir(mode=0o700, parents=True, exist_ok=True)
Path('/data/recharge/smoke-marker').write_text('synthetic-recharge-task')
'''

RESTORE = '''
from hashlib import sha256
from pathlib import Path
from login_storage import DatabaseLoginStorage
storage = DatabaseLoginStorage('/data/accounts.sqlite3')
assert storage.load_credentials('docker-smoke', 'synthetic-account') == {'token': 'synthetic-token'}
assert storage.load_activity_data('docker-smoke') == {'task': 'synthetic-task'}
assert Path('/data/key-digest').read_text() == sha256(Path('/data/credentials.key').read_bytes()).hexdigest()
assert Path('/data/recharge/smoke-marker').read_text() == 'synthetic-recharge-task'
'''


def docker(*args, check=True):
    return subprocess.run(['docker', *args], check=check, capture_output=True, text=True)


def wait_healthy(container):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = docker('inspect', '--format', '{{.State.Status}} {{.State.Health.Status}}', container)
        if result.stdout.strip() == 'running healthy':
            return
        if not result.stdout.startswith('running '):
            raise RuntimeError('Container exited before becoming healthy')
        time.sleep(1)
    raise RuntimeError('Container did not become healthy within 60 seconds')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    image = parser.parse_args().image
    name = 'lingxi-smoke-' + uuid4().hex[:12]
    volume = name + '-data'
    docker('volume', 'create', volume)
    try:
        # Recreate the container to check the same upgrade path as Compose.
        for cycle in range(2):
            docker('run', '-d', '--init', '--name', name, '--pull', 'never', '--network', 'none',
                   '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                   '--mount', f'type=volume,source={volume},target=/data',
                   '--health-interval', '1s', '--health-start-period', '1s', image)
            wait_healthy(name)
            docker('exec', name, 'python', '-c', PROBE)
            docker('exec', name, 'python', '-c', SAVE if cycle == 0 else RESTORE)
            docker('stop', '--time', '30', name)
            docker('rm', name)
        print('Docker smoke passed: non-root startup, pages, access protection, Node canvas, persistent key, credentials and tasks.')
    except (subprocess.CalledProcessError, RuntimeError) as error:
        logs = docker('logs', '--tail', '80', name, check=False)
        print(logs.stdout + logs.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stdout)
            print(error.stderr)
        raise
    finally:
        docker('rm', '-f', name, check=False)
        docker('volume', 'rm', volume, check=False)


if __name__ == '__main__':
    main()
