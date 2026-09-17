from contextlib import closing, redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import offhost_backup as client
import private_backup
import recovery_bundle


class OffhostBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.key = self.root / 'identity'
        self.key.write_text('synthetic private key')
        self.key.chmod(0o600)
        self.destination = self.root / 'received'
        self.source = self.root / 'sources'
        self.source.mkdir(mode=0o700)
        (self.source / 'settings.json').write_text('{"reply_rules":[]}')
        with closing(sqlite3.connect(str(self.source / 'decisions.db'))) as database:
            database.execute('CREATE TABLE decisions (id INTEGER)')
        self.snapshot = private_backup.backup({name: self.source / name for name in private_backup.NAMES}, self.root / 'snapshots')
        self.archive = self.root / 'bundle.tar.gz'
        recovery_bundle.pack(self.snapshot, self.archive, {'installation': 'a'*32, 'user_id': 'synthetic-user', 'owned': {'updates.example': 'synthetic-rule'}, 'pending': None})
        self.remote_name = 'recovery-' + self.snapshot.name[len('backup-'):] + '.tar.gz'
        self.commands = []

    def transport(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == 'scp':
            shutil.copyfile(self.archive, command[-1])
            return b''
        if '--ack' in command:
            self.assertTrue((self.destination / self.remote_name[:-7] / 'app/decisions.db').exists())
            return b'{"acknowledged":true}'
        return json.dumps({'archive': client.REMOTE + self.remote_name}).encode()

    def test_real_bundle_verified_atomically_and_acknowledged_with_private_files(self):
        with patch.object(client, 'run_command', side_effect=self.transport):
            result = client.pull('backup@example.org', self.key, self.destination)
        recovery_bundle.validate_completed(result)
        self.assertEqual(result.stat().st_mode & 0o777, 0o700)
        for path in result.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertFalse(list(self.destination.glob('.incomplete-*')))
        self.assertEqual(json.loads((self.destination / 'status.json').read_text())['status'], 'verified')
        self.assertEqual(len(self.commands), 3)
        for command in self.commands:
            self.assertIn('BatchMode=yes', command)
            self.assertIn('StrictHostKeyChecking=yes', command)
            self.assertIn('ClearAllForwardings=yes', command)
            self.assertIn('ForwardAgent=no', command)
        self.assertEqual(self.commands[-1][-2:], ['--ack', self.remote_name])
        with patch.object(client, 'run_command', side_effect=self.transport):
            self.assertEqual(client.pull('backup@example.org', self.key, self.destination), result)

    def test_invalid_remote_response_never_copies_or_acknowledges(self):
        invalid = [{'archive': '/tmp/secrets'}, {'archive': client.REMOTE + '../other'}, {'archive': client.REMOTE + 'recovery-123.tar.gz;bad'}, {'archive': client.REMOTE + self.remote_name, 'extra': True}, ['unexpected']]
        for response in invalid:
            with self.subTest(response=response), patch.object(client, 'run_command', return_value=json.dumps(response).encode()) as run:
                with self.assertRaises(ValueError):
                    client.pull('backup@example.org', self.key, self.destination)
                self.assertEqual(run.call_count, 1)
        self.assertFalse(list(self.destination.glob('recovery-*')))

    def test_corrupt_download_is_not_acknowledged_and_does_not_prune(self):
        def corrupted(command, **kwargs):
            result = self.transport(command, **kwargs)
            if command[0] == 'scp':
                Path(command[-1]).write_bytes(b'not an archive')
            return result
        with patch.object(client, 'run_command', side_effect=corrupted), patch.object(client, 'prune') as prune:
            with self.assertRaises(Exception):
                client.pull('backup@example.org', self.key, self.destination)
            prune.assert_not_called()
        self.assertFalse(any('--ack' in command for command in self.commands))
        self.assertFalse(list(self.destination.glob('recovery-*')))
        self.assertFalse(list(self.destination.glob('.incomplete-*')))
        self.assertEqual(json.loads((self.destination / 'status.json').read_text()), {'status': 'failed', 'checked_at': unittest.mock.ANY})

    def test_failed_ack_keeps_verified_copy_and_never_prunes(self):
        def failure(command, **kwargs):
            if '--ack' in command:
                raise RuntimeError('SENSITIVE REMOTE RESPONSE')
            return self.transport(command, **kwargs)
        with patch.object(client, 'run_command', side_effect=failure), patch.object(client, 'prune') as prune:
            with self.assertRaises(RuntimeError):
                client.pull('backup@example.org', self.key, self.destination)
            prune.assert_not_called()
        completed = self.destination / self.remote_name[:-7]
        recovery_bundle.validate_completed(completed)
        self.assertNotIn('SENSITIVE', (self.destination / 'status.json').read_text())

    def test_retention_only_removes_valid_completed_copies_after_success(self):
        self.destination.mkdir(mode=0o700)
        for name in ('recovery-20000101T000000Z', 'recovery-20010101T000000Z'):
            recovery_bundle.unpack(self.archive, self.destination / name)
        invalid = self.destination / 'recovery-19990101T000000Z'
        invalid.mkdir(mode=0o700)
        (invalid / 'unrecognized').write_text('keep')
        with patch.object(client, 'run_command', side_effect=self.transport):
            client.pull('backup@example.org', self.key, self.destination, keep=1)
        self.assertTrue(invalid.exists())
        self.assertFalse((self.destination / 'recovery-20000101T000000Z').exists())
        self.assertFalse((self.destination / 'recovery-20010101T000000Z').exists())
        self.assertTrue((self.destination / self.remote_name[:-7]).exists())

    def test_bad_hosts_keys_symlinks_and_git_destinations_rejected_before_network(self):
        with patch.object(client, 'run_command') as run:
            for host in ('-oProxyCommand=bad', 'owner@host;bad', 'owner@host/path', 'owner@host\nother'):
                with self.assertRaises(ValueError):
                    client.pull(host, self.key, self.destination)
            self.key.chmod(0o644)
            with self.assertRaises(ValueError):
                client.pull('backup@example.org', self.key, self.destination)
            self.key.chmod(0o600)
            git = self.root / 'checkout'
            git.mkdir(mode=0o700)
            (git / '.git').mkdir()
            with self.assertRaises(ValueError):
                client.pull('backup@example.org', self.key, git / 'copies')
            linked = self.root / 'linked'
            linked.symlink_to(self.root, target_is_directory=True)
            with self.assertRaises(ValueError):
                client.pull('backup@example.org', self.key, linked / 'copies')
            run.assert_not_called()

    def test_overlapping_pull_does_not_contact_remote_or_overwrite_status(self):
        import fcntl
        self.destination.mkdir(mode=0o700)
        with (self.destination / '.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(client, 'run_command') as run, self.assertRaises(BlockingIOError):
                client.pull('backup@example.org', self.key, self.destination)
            run.assert_not_called()

    def test_subprocess_output_runtime_and_error_reporting_are_bounded(self):
        self.assertEqual(client.run_command([sys.executable, '-c', 'print("ok")']), b'ok\n')
        with self.assertRaises(ValueError):
            client.run_command([sys.executable, '-c', 'print("x"*20000)'], maximum=100)
        with self.assertRaises(TimeoutError):
            client.run_command([sys.executable, '-c', 'import time;time.sleep(10)'], timeout=0.05)
        monitored = self.root / 'growing-transfer'
        monitored.write_bytes(b'')
        with patch.object(recovery_bundle, 'MAX_BYTES', 100), self.assertRaises(ValueError):
            client.run_command([sys.executable, '-c', 'import pathlib,time;pathlib.Path(__import__("sys").argv[1]).write_bytes(b"x"*200);time.sleep(10)', str(monitored)], watched_path=monitored)
        error = io.StringIO()
        with patch.object(sys, 'argv', ['offhost_backup', '--host', 'backup@example.org', '--identity', str(self.key), '--destination', str(self.destination)]), patch.object(client, 'pull', side_effect=RuntimeError('SENSITIVE REMOTE RESPONSE')), redirect_stderr(error):
            self.assertEqual(client.main(), 1)
        self.assertNotIn('SENSITIVE', error.getvalue())
