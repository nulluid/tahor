from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import data_changes


class PrivateDataTests(unittest.TestCase):
    def test_concurrent_scoped_commits_preserve_unrelated_staged_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'TAHOR_DATA_PUSH': '0'}):
            root = Path(directory)
            def git(*args):
                return subprocess.run(['git', '-C', directory, *args], check=True, capture_output=True, text=True).stdout
            git('init', '-q')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.com')
            (root / 'baseline').write_text('test')
            git('add', 'baseline')
            git('commit', '-qm', 'test baseline')
            (root / 'unrelated').write_text('keep staged')
            git('add', 'unrelated')
            paths = ['prompt.txt', 'sieve.txt']
            for name in paths:
                (root / name).write_text('synthetic configuration')
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(data_changes.commit_data, root, 'update ' + name, [name]) for name in paths]
                self.assertTrue(all(future.result() for future in futures))
            self.assertEqual(git('diff', '--cached', '--name-only').strip(), 'unrelated')
            committed = set(git('diff', '--name-only', 'HEAD~2', 'HEAD').splitlines())
            self.assertEqual(committed, set(paths))

    def test_config_paths_cannot_escape_the_private_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.git').mkdir()
            with self.assertRaises(ValueError):
                data_changes.commit_data(root, 'invalid', ['../config.env'])
