import importlib.util
import gzip
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import private_backup
import recovery_bundle as bundle
from restore_provider_ownership import restore_ownership


class RecoveryBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        source = self.root / 'source'
        source.mkdir(mode=0o700)
        self.sources = {name: source / name for name in private_backup.NAMES}
        self.sources['settings.json'].write_text('{"reply_rules":[{"instructions":"Private instructions"}]}')
        with sqlite3.connect(self.sources['decisions.db']) as db:
            db.execute('CREATE TABLE decisions (id INTEGER)')
        self.snapshot = private_backup.backup(self.sources, self.root / 'snapshots')
        self.provider = {'installation': 'a' * 32, 'user_id': 'test-user',
                         'owned': {'offers.example.com': 'rule-one'}, 'pending': None}

    def test_roundtrip_includes_private_rules_and_ownership_but_no_login_secrets(self):
        archive = self.root / 'bundle.tar.gz'
        bundle.pack(self.snapshot, archive, self.provider)
        destination = self.root / 'received'
        bundle.unpack(archive, destination)
        bundle.validate_completed(destination)
        self.assertEqual(json.loads((destination / 'provider-ownership.json').read_text()), self.provider)
        self.assertIn('Private instructions', (destination / 'app/settings.json').read_text())
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        for path in destination.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        (destination / 'app/settings.json').write_text('{}')
        with self.assertRaises(ValueError):
            bundle.validate_completed(destination)

    def test_secret_fields_and_unbound_ownership_rejected(self):
        for bad in (dict(self.provider, password='secret'), dict(self.provider, user_id=None)):
            with self.assertRaises(ValueError):
                bundle.pack(self.snapshot, self.root / 'bad.tar.gz', bad)
        self.assertFalse((self.root / 'bad.tar.gz').exists())

    def test_traversal_links_and_duplicates_rejected_before_writing(self):
        for variant in ('traversal', 'symlink', 'duplicate'):
            archive = self.root / (variant + '.tar.gz')
            with tarfile.open(archive, 'w:gz') as tar:
                info = tarfile.TarInfo('../secret' if variant == 'traversal' else 'bundle.json')
                if variant == 'symlink':
                    info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
                else:
                    info.size = 2
                tar.addfile(info, None if variant == 'symlink' else io.BytesIO(b'{}'))
                if variant == 'duplicate':
                    tar.addfile(info, io.BytesIO(b'{}'))
            with self.assertRaises(ValueError):
                bundle.unpack(archive, self.root / variant)
            self.assertFalse(any((self.root / variant).iterdir()))

    def test_provider_restore_requires_same_enrolled_account_and_preserves_credentials(self):
        archive = self.root / 'bundle.tar.gz'
        bundle.pack(self.snapshot, archive, self.provider)
        destination = self.root / 'received'
        bundle.unpack(archive, destination)
        state = self.root / 'provider'
        state.mkdir(mode=0o700)
        config = state / 'config.json'
        config.write_text(json.dumps({'installation': 'b' * 32, 'unrelated': 'preserve'}))
        session = state / 'session.json'
        session.write_text(json.dumps({'auth_state': {'user_id': 'wrong-user'}, 'session': {'accessToken': 'never-backup'}}))
        before = config.read_bytes()
        with self.assertRaisesRegex(ValueError, 'same Fastmail account'):
            restore_ownership(destination, config, state, os.getuid(), os.getgid())
        self.assertEqual(config.read_bytes(), before)
        session.write_text(json.dumps({'auth_state': {'user_id': 'test-user'}, 'session': {'accessToken': 'never-backup'}}))
        before_session = session.read_bytes()
        restore_ownership(destination, config, state, os.getuid(), os.getgid())
        self.assertEqual(session.read_bytes(), before_session)
        self.assertEqual(json.loads(config.read_text())['installation'], 'a' * 32)
        self.assertEqual(json.loads((state / 'rules.json').read_text())['owned'], self.provider['owned'])
        restore_ownership(destination, config, state, os.getuid(), os.getgid())
        (state / 'rules.json').write_text('{"owned":{"other.example.com":"other"},"pending":null}')
        with self.assertRaisesRegex(ValueError, 'differs'):
            restore_ownership(destination, config, state, os.getuid(), os.getgid())

    def test_extension_headers_rejected_before_metadata_body_processing(self):
        for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME,
                     tarfile.GNUTYPE_LONGLINK, tarfile.GNUTYPE_SPARSE):
            with self.subTest(kind=kind):
                path=self.root/('extension-'+kind.decode()+'.tar.gz')
                header=tarfile.TarInfo('bundle.json')
                header.type=kind
                header.size=128
                # No metadata body is necessary: rejection occurs at the header,
                # before tarfile attempts to read even this small extension.
                with gzip.open(path,'wb') as stream:
                    stream.write(header.tobuf(format=tarfile.USTAR_FORMAT))
                with patch.object(tarfile.TarInfo,'_proc_pax',side_effect=AssertionError('metadata processed')), patch.object(tarfile.TarInfo,'_proc_gnulong',side_effect=AssertionError('metadata processed')):
                    with self.assertRaisesRegex(ValueError,'archive header'):
                        bundle.unpack(path,self.root/('received-'+kind.decode()))

    def test_regular_oversized_header_rejected_before_reading_payload(self):
        path=self.root/'oversized.tar.gz'
        header=tarfile.TarInfo('bundle.json');header.size=129
        with gzip.open(path,'wb') as stream:
            stream.write(header.tobuf(format=tarfile.USTAR_FORMAT))
        with patch.object(bundle,'MAX_BYTES',128):
            with self.assertRaisesRegex(ValueError,'size limit'):
                bundle.unpack(path,self.root/'oversized')
        self.assertFalse(any((self.root/'oversized').iterdir()))

    def test_cumulative_expansion_limit_checked_before_writing(self):
        path=self.root/'combined.tar.gz'
        with tarfile.open(path,'w:gz',format=tarfile.USTAR_FORMAT) as archive:
            for name in ('bundle.json','provider-ownership.json'):
                info=tarfile.TarInfo(name);info.size=80
                archive.addfile(info,io.BytesIO(b' '*80))
        with patch.object(bundle,'MAX_BYTES',128):
            with self.assertRaisesRegex(ValueError,'size limit'):
                bundle.unpack(path,self.root/'combined')
        self.assertFalse(any((self.root/'combined').iterdir()))

    def test_completed_bundle_manifest_must_remain_private(self):
        archive=self.root/'bundle.tar.gz'
        bundle.pack(self.snapshot,archive,self.provider)
        destination=self.root/'received'
        bundle.unpack(archive,destination)
        (destination/'bundle.json').chmod(0o644)
        with self.assertRaisesRegex(ValueError,'manifest must have private'):
            bundle.validate_completed(destination)
