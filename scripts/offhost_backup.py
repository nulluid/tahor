#!/usr/bin/env python3
"""Pull and verify a private recovery copy from an explicitly trusted SSH host."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

import recovery_bundle
from private_backup import private_directory, safe_path, sync_directory

EXPORT = '/opt/tahor/scripts/recovery_export.py'
REMOTE = '/var/lib/tahor-recovery-export/'
NAME = r'recovery-[0-9TZ]+'


def run_command(command, timeout=300, maximum=16384, watched_path=None):
    """Bound runtime and output without logging SSH/provider responses."""
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + timeout
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                if watched_path is not None:
                    info = Path(watched_path).lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_size > recovery_bundle.MAX_BYTES:
                        raise ValueError('Backup transfer exceeded limit')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Backup command timed out')
                for key, _ in selector.select(min(remaining, 0.25)):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > maximum:
                            raise ValueError('Backup command output exceeded limit')
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if code:
                raise RuntimeError('Backup command failed')
        return bytes(output)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        process.stdout.close()


def ssh_options(host, identity):
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]*@[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?', host):
        raise ValueError('Use an explicit user@host without shell options')
    identity = safe_path(identity)
    info = identity.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('SSH identity must be an owned private regular file')
    return ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes', '-o', 'RequestTTY=no',
            '-o', 'IdentitiesOnly=yes', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
            '-i', str(identity)]


def status_write(destination, value):
    fd, name = tempfile.mkstemp(prefix='.status-', dir=str(destination))
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, destination / 'status.json')
        sync_directory(destination)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def prune(destination, keep):
    valid = []
    for path in destination.iterdir():
        if not re.fullmatch(NAME, path.name) or path.is_symlink() or not path.is_dir():
            continue
        try:
            recovery_bundle.validate_completed(path)
        except Exception:
            continue  # Never prune an unrecognized or damaged directory.
        valid.append(path)
    for path in sorted(valid)[:-keep]:
        shutil.rmtree(path)
    sync_directory(destination)


def pull(host, identity, destination, keep=28):
    if type(keep) is not int or not 1 <= keep <= 3650:
        raise ValueError('Retention must be between 1 and 3650 copies')
    options = ssh_options(host, identity)
    destination = private_directory(destination, create=True)
    lock_fd = os.open(str(destination / '.lock'), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(lock_fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        staging = None
        try:
            prefix = ['ssh'] + options + [host, 'sudo', '-n', '/opt/tahor/venv/bin/python', EXPORT]
            response = json.loads(run_command(prefix))
            archive = response.get('archive') if isinstance(response, dict) and set(response) == {'archive'} else None
            if not isinstance(archive, str) or not re.fullmatch(re.escape(REMOTE) + NAME + r'\.tar\.gz', archive):
                raise ValueError('Invalid exported recovery path')
            basename = Path(archive).name
            completed = destination / basename[:-7]
            staging = Path(tempfile.mkdtemp(prefix='.incomplete-', dir=str(destination)))
            transfer = staging / 'archive.tar.gz'
            # Pre-create with private permissions; the containing directory is 0700.
            fd = os.open(str(transfer), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            run_command(['scp'] + options + ['--', host + ':' + archive, str(transfer)], timeout=900, watched_path=transfer)
            info = transfer.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > recovery_bundle.MAX_BYTES or info.st_uid != os.geteuid():
                raise ValueError('Invalid downloaded recovery file')
            transfer.chmod(0o600)
            unpacked = staging / 'verified'
            recovery_bundle.unpack(transfer, unpacked)
            recovery_bundle.validate_completed(unpacked)
            if completed.exists() or completed.is_symlink():
                recovery_bundle.validate_completed(completed)
                if (completed / 'bundle.json').read_bytes() != (unpacked / 'bundle.json').read_bytes():
                    raise ValueError('Conflicting completed recovery copy')
            else:
                os.rename(unpacked, completed)
                sync_directory(destination)
            acknowledgement = json.loads(run_command(prefix + ['--ack', basename]))
            if acknowledgement != {'acknowledged': True}:
                raise ValueError('Recovery acknowledgement failed')
            prune(destination, keep)
            status_write(destination, {'status': 'verified', 'verified_at': time.time(), 'snapshot': completed.name})
            return completed
        except Exception:
            status_write(destination, {'status': 'failed', 'checked_at': time.time()})
            raise
        finally:
            if staging is not None:
                shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--identity', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--keep', type=int, default=28)
    args = parser.parse_args()
    try:
        pull(args.host, args.identity, args.destination, args.keep)
    except Exception:
        print('Off-host recovery copy failed; check connectivity, host trust, permissions and export availability.', file=sys.stderr)
        return 1
    print('Off-host recovery copy verified.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
