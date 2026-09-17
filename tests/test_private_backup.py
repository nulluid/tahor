import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('private_backup', ROOT / 'scripts/private_backup.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PrivateBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.sources = {name: self.state / name for name in module.NAMES}
        self.sources['settings.json'].write_text(json.dumps({'reply_rules': [{'instructions': 'Private directions', 'signature': 'Private Name', 'excluded_senders': ['skip@example.org']}]}))
        self.sources['vendor_buckets.json'].write_text('{"example.org":"Filed/Example"}')
        self.sources['prompt.txt'].write_text('Private prompt')
        self.sources['sieve.txt'].write_text('# Private rules')
        self.sources['ai_routing_state.json'].write_text(json.dumps({'reply': {'failure_since': 1000, 'tiers': {'paid': {'retry_at': 1300}}}}))
        with closing(sqlite3.connect(str(self.sources['decisions.db']), isolation_level=None)) as db:
            db.execute('CREATE TABLE decisions (value TEXT)')
            db.execute('INSERT INTO decisions VALUES (?)', ('private decision',))
        (self.state / 'config.env').write_text('SECRET=never-copy')
        self.destination = self.root / 'backups'

    def test_roundtrip_private_rules_database_credentials_and_permissions(self):
        snapshot = module.backup(self.sources, self.destination)
        original = self.sources['settings.json'].read_bytes()
        self.sources['settings.json'].write_text('{"changed":true}')
        self.sources['ai_routing_state.json'].write_text('{}')
        with closing(sqlite3.connect(str(self.sources['decisions.db']), isolation_level=None)) as db:
            db.execute('DELETE FROM decisions')
        safety = module.restore(snapshot, self.sources, self.destination, services_stopped=True)
        self.assertEqual(self.sources['settings.json'].read_bytes(), original)
        self.assertEqual(json.loads(self.sources['ai_routing_state.json'].read_text())['reply']['failure_since'], 1000)
        self.assertEqual(json.loads((safety / 'settings.json').read_text()), {'changed': True})
        with closing(sqlite3.connect(str(self.sources['decisions.db']), isolation_level=None)) as db:
            self.assertEqual(db.execute('SELECT value FROM decisions').fetchall(), [('private decision',)])
        self.assertEqual((self.state / 'config.env').read_text(), 'SECRET=never-copy')
        self.assertFalse((snapshot / 'config.env').exists())
        self.assertEqual(json.loads((snapshot / 'ai_routing_state.json').read_text())['reply']['failure_since'], 1000)
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in snapshot.iterdir()))

    def test_corruption_fails_before_touching_live_state(self):
        snapshot = module.backup(self.sources, self.destination)
        (snapshot / 'prompt.txt').write_text('tampered')
        before = {name: p.read_bytes() for name, p in self.sources.items()}
        with self.assertRaisesRegex(ValueError, 'checksum'):
            module.restore(snapshot, self.sources, self.destination, True)
        self.assertEqual(before, {name: p.read_bytes() for name, p in self.sources.items()})
        self.assertEqual(len(list(self.destination.iterdir())), 1)

    def test_path_traversal_symlinks_and_public_storage_rejected(self):
        snapshot = module.backup(self.sources, self.destination)
        manifest = json.loads((snapshot / 'manifest.json').read_text())
        manifest['files']['../config.env'] = {'bytes': 0, 'sha256': '0' * 64}
        (snapshot / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'allowlist'):
            module.validate_snapshot(snapshot)
        with self.assertRaisesRegex(ValueError, 'public checkout'):
            module.backup(self.sources, ROOT / 'private-test-backup')
        link = self.root / 'linked'
        link.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            module.backup(self.sources, link)
        self.sources['prompt.txt'].unlink()
        self.sources['prompt.txt'].symlink_to(self.state / 'config.env')
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            module.backup(self.sources, self.destination)

    def test_insecure_permissions_and_missing_stop_acknowledgement_rejected(self):
        self.destination.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, '0700'):
            module.backup(self.sources, self.destination)
        self.destination.chmod(0o700)
        snapshot = module.backup(self.sources, self.destination)
        with self.assertRaisesRegex(ValueError, 'Stop all Tahor'):
            module.restore(snapshot, self.sources, self.destination)
        (snapshot / 'settings.json').chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private permissions'):
            module.validate_snapshot(snapshot)

    def test_sqlite_backup_includes_committed_wal_data(self):
        db = sqlite3.connect(str(self.sources['decisions.db']))
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('INSERT INTO decisions VALUES (?)', ('committed in WAL',))
        db.commit()
        snapshot = module.backup(self.sources, self.destination)
        module.validate_snapshot(snapshot)
        self.assertEqual({p.name for p in snapshot.iterdir()}, module.NAMES | {'manifest.json'})
        with closing(sqlite3.connect(str(snapshot / 'decisions.db'))) as copied:
            self.assertEqual(copied.execute('SELECT count(*) FROM decisions').fetchone()[0], 2)

    def test_git_directory_and_unexpected_contents_rejected(self):
        repo = self.root / 'private-repo'
        repo.mkdir()
        (repo / '.git').mkdir()
        with self.assertRaisesRegex(ValueError, 'Git repositories'):
            module.backup(self.sources, repo / 'backups')
        snapshot = module.backup(self.sources, self.destination)
        (snapshot / 'credentials.json').write_text('unexpected')
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            module.validate_snapshot(snapshot)

    def test_cli_backup_verify_and_restore(self):
        config = self.state / 'config.env'
        config.write_text('\n'.join(f'{key}="{value}"' for key, value in {
            'DATA_DIR': self.state, 'TAHOR_SETTINGS_PATH': self.sources['settings.json'],
            'TAHOR_DB_PATH': self.sources['decisions.db']}.items()))
        command = [sys.executable, str(ROOT / 'scripts/private_backup.py')]
        arguments = ['--env', str(config), '--destination', str(self.destination)]
        result = subprocess.run(command + arguments, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = next(self.destination.iterdir())
        result = subprocess.run(command + ['--verify', str(snapshot)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        original = self.sources['settings.json'].read_bytes()
        self.sources['settings.json'].write_text('{"changed":true}')
        self.sources['ai_routing_state.json'].write_text('{}')
        result = subprocess.run(command + arguments + ['--restore', str(snapshot), '--services-stopped'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Pre-restore safety snapshot:', result.stderr)
        self.assertEqual(self.sources['settings.json'].read_bytes(), original)
        self.assertEqual(json.loads(self.sources['ai_routing_state.json'].read_text())['reply']['failure_since'], 1000)
