from concurrent.futures import ThreadPoolExecutor
import errno
from ipaddress import ip_address
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from credential_protection import FileCredentialCipher
from login_storage import DatabaseLoginStorage, LoginStorageError
from network_interfaces import _linux_addresses, interface_addresses


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux runtime integration')
class LinuxStorageTests(unittest.TestCase):
    def test_accounts_sessions_and_tasks_survive_another_process_without_plaintext(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/'accounts.sqlite3'
            storage = DatabaseLoginStorage(path)
            account = {'id': 'login-a', 'phone': '19900000001', 'cookie': 'private-cookie-marker'}
            storage.save([account])
            storage.save_credentials('test-domain', 'login-a', {'token': 'private-token-marker'})
            storage.save_lingxi_tasks([{'id': 'task-a', 'content': 'private-task-marker'}])
            result = subprocess.run([sys.executable, '-c', '''
import json, sys
from login_storage import DatabaseLoginStorage
storage = DatabaseLoginStorage(sys.argv[1])
assert storage.load()[0]['cookie'] == 'private-cookie-marker'
assert storage.load_credentials('test-domain', 'login-a')['token'] == 'private-token-marker'
assert storage.load_lingxi_tasks()[0]['content'] == 'private-task-marker'
print(json.dumps({'id': storage.load()[0]['id']}))
''', str(path)], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), {'id': 'login-a'})
            self.assertEqual((Path(folder)/'credentials.key').stat().st_mode & 0o777, 0o600)
            for file in Path(folder).glob('accounts.sqlite3*'):
                for marker in (b'private-cookie-marker', b'private-token-marker', b'private-task-marker', b'19900000001'):
                    self.assertNotIn(marker, file.read_bytes())

    def test_missing_or_corrupt_key_preserves_existing_database(self):
        for key in (None, b'invalid-key'):
            with self.subTest(key_missing=key is None), TemporaryDirectory() as folder:
                path = Path(folder)/'accounts.sqlite3'
                storage = DatabaseLoginStorage(path)
                storage.save([{'id': 'a', 'phone': '19900000001', 'cookie': 'cookie'}])
                before = path.read_bytes()
                key_path = Path(folder)/'credentials.key'
                original = key_path.read_bytes()
                if key is None:
                    key_path.unlink()
                else:
                    key_path.write_bytes(key)
                with self.assertRaises(LoginStorageError):
                    DatabaseLoginStorage(path)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(key_path.exists(), key is not None)
                key_path.write_bytes(original)
                self.assertEqual(DatabaseLoginStorage(path).load()[0]['id'], 'a')

    def test_wrong_key_and_tampered_payload_cannot_be_decrypted(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/'accounts.sqlite3'
            storage = DatabaseLoginStorage(path)
            storage.save([{'id': 'a', 'phone': '19900000001', 'cookie': 'cookie'}])
            with self.assertRaises(LoginStorageError):
                storage.unprotect(storage.protect(b'payload')[:-8] + b'tampered')
            key_path = Path(folder)/'credentials.key'
            original = key_path.read_bytes()
            key_path.write_bytes(Fernet.generate_key())
            with self.assertRaises(LoginStorageError):
                DatabaseLoginStorage(path).load()
            key_path.write_bytes(original)
            self.assertEqual(DatabaseLoginStorage(path).load()[0]['id'], 'a')

    def test_independent_data_directories_use_different_keys(self):
        with TemporaryDirectory() as folder:
            one = DatabaseLoginStorage(Path(folder)/'one/accounts.sqlite3')
            two = DatabaseLoginStorage(Path(folder)/'two/accounts.sqlite3')
            self.assertEqual(one.path.parent.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(LoginStorageError):
                two.unprotect(one.protect(b'private-session'))

    def test_concurrent_initialization_reuses_one_key(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/'credentials.key'
            def initialize(_):
                return FileCredentialCipher(path, create=True).encrypt(b'private-session')
            with ThreadPoolExecutor(max_workers=8) as workers:
                payloads = list(workers.map(initialize, range(16)))
            cipher = FileCredentialCipher(path, create=False)
            self.assertEqual([cipher.decrypt(value) for value in payloads], [b'private-session'] * 16)
            self.assertEqual([file.name for file in Path(folder).iterdir()], ['credentials.key'])


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux network integration')
class LinuxNetworkTests(unittest.TestCase):
    def test_all_interfaces_have_identity_without_hostname_resolution(self):
        with patch('network_interfaces.socket.getaddrinfo', side_effect=AssertionError('DNS should not be used')):
            raw = _linux_addresses()
            filtered = interface_addresses()
        self.assertTrue(any(ip_address(row['address']).is_loopback for row in raw))
        names = {name for _, name in socket.if_nameindex()}
        for row in raw:
            self.assertIn(row['interface_name'], names)
            self.assertEqual(row['interface_id'], 'linux:' + row['interface_name'])
        for row in filtered:
            self.assertFalse(ip_address(row['address']).is_loopback)

    def test_ipv4_ipv6_filtering_keeps_interface_identity(self):
        rows = [{'address': address, 'interface_id': 'linux:eth0', 'interface_name': 'eth0'}
                for address in ('192.0.2.4', '2001:db8::4', 'fe80::1', '127.0.0.1', '::1', '0.0.0.0', 'ff02::1')]
        with patch('network_interfaces._linux_addresses', return_value=rows):
            found = interface_addresses()
        self.assertEqual([(row['address'], row['family']) for row in found],
                         [('192.0.2.4', 'ipv4'), ('2001:db8::4', 'ipv6')])
        self.assertTrue(all(row['interface_id'] == 'linux:eth0' for row in found))

    def test_ipv6_disabled_still_starts_an_ipv4_listener(self):
        from flask import Flask
        from network_access import create_http_server, register_network
        app = Flask('ipv4-only')
        manager = register_network(app, None)
        original = socket.create_server
        def bind(address, **kwargs):
            if kwargs['family'] == socket.AF_INET6:
                raise OSError(errno.EAFNOSUPPORT, 'IPv6 is disabled')
            return original(address, **kwargs)
        with patch.dict(os.environ), patch('network_access.socket.create_server', side_effect=bind):
            os.environ.pop('LINGXI_HOST', None)
            server = create_http_server(app, 0)
        try:
            self.assertEqual(manager.bindings, [('0.0.0.0', server.effective_port)])
            self.assertEqual(manager.active, {'ipv4': True, 'ipv6': False})
        finally:
            server.close()


class LinuxPathTests(unittest.TestCase):
    def test_frozen_linux_uses_xdg_data_without_localappdata(self):
        with TemporaryDirectory() as folder:
            env = {key: value for key, value in os.environ.items() if key not in ('LINGXI_DATA_DIR', 'LOCALAPPDATA')}
            env['XDG_DATA_HOME'] = folder
            result = subprocess.run([sys.executable, '-c',
                'import sys;sys.frozen=True;sys.platform="linux";from runtime_paths import DATA_DIR;print(DATA_DIR)'],
                capture_output=True, text=True, env=env, check=True)
            self.assertEqual(Path(result.stdout.strip()), Path(folder)/'LingxiWorkspace')


if __name__ == '__main__':
    unittest.main()
