"""Write mailbox configuration atomically and optionally version it privately."""
import os
import fcntl
from pathlib import Path
import subprocess
import tempfile


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.' + path.name, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def commit_data(data_dir, message, paths):
    data_dir = Path(data_dir).resolve()
    if data_dir == Path(__file__).resolve().parent or not (data_dir / '.git').exists():
        return False
    for path in paths:
        if Path(path).is_absolute() or '..' in Path(path).parts:
            raise ValueError('Data paths must stay inside DATA_DIR')
    existing = [path for path in paths if (data_dir / path).is_file()]
    if not existing:
        return False
    def git(*args):
        return subprocess.run(['git', '-C', str(data_dir), *args], check=True, capture_output=True, text=True, timeout=30)
    git_dir = Path(git('rev-parse', '--absolute-git-dir').stdout.strip())
    with (git_dir / 'tahor-data.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        git('add', '--', *existing)
        changed = subprocess.run(['git', '-C', str(data_dir), 'diff', '--cached', '--quiet', '--', *existing], timeout=30)
        if changed.returncode not in (0, 1):
            raise RuntimeError('Could not inspect data changes')
        if changed.returncode == 1:
            git('commit', '-m', message, '--', *existing)
        if os.environ.get('TAHOR_DATA_PUSH') == '1':
            git('push', 'origin', 'HEAD')
        return changed.returncode == 1
