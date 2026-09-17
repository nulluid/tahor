#!/usr/bin/env python3
"""Back up private Tahor preferences and state; credentials are deliberately excluded."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
NAMES = {'settings.json', 'decisions.db', 'prompt.txt', 'vendor_buckets.json', 'sieve.txt', 'ai_routing_state.json', 'free_classifier_guidance.txt', 'coupon_policies.json', 'business_filing.json'}
REQUIRED = {'settings.json', 'decisions.db'}


def safe_path(path):
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('Symlink paths are not supported')
    return path


def private_directory(path, create=False):
    path = safe_path(path)
    if ROOT == path or ROOT in path.parents:
        raise ValueError('Private backups must remain outside the public checkout')
    if any((p / '.git').exists() for p in (path, *path.parents)):
        raise ValueError('Backup storage must remain outside Git repositories')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise ValueError('Backup directory must be owned by this user with mode 0700')
    return path


def read_file(path):
    path = safe_path(path)
    fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Expected a regular file')
        return stream.read()


def write_file(path, data):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def sync_directory(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def database_check(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('SQLite integrity check failed')


def validate_snapshot(folder):
    folder = private_directory(folder)
    if (folder / 'manifest.json').stat().st_mode & 0o077:
        raise ValueError('Backup manifest must have private permissions')
    manifest = json.loads(read_file(folder / 'manifest.json'))
    if set(manifest) != {'version', 'created_at', 'files'} or manifest['version'] != 1:
        raise ValueError('Unsupported backup manifest')
    files = manifest['files']
    if not isinstance(files, dict) or not REQUIRED <= set(files) <= NAMES:
        raise ValueError('Invalid backup file allowlist')
    if {p.name for p in folder.iterdir()} != set(files) | {'manifest.json'}:
        raise ValueError('Unexpected backup contents')
    payloads = {}
    for name, metadata in files.items():
        if not isinstance(metadata, dict) or set(metadata) != {'sha256', 'bytes'}:
            raise ValueError('Invalid backup file metadata')
        path = folder / name
        if path.stat().st_mode & 0o077:
            raise ValueError('Backup files must have private permissions')
        data = read_file(path)
        if len(data) != metadata['bytes'] or hashlib.sha256(data).hexdigest() != metadata['sha256']:
            raise ValueError('Backup checksum mismatch')
        if name in ('settings.json', 'vendor_buckets.json', 'ai_routing_state.json', 'coupon_policies.json', 'business_filing.json') and not isinstance(json.loads(data), dict):
            raise ValueError('Expected a JSON object')
        payloads[name] = data
    database_check(folder / 'decisions.db')
    return payloads


def validate_sources(sources):
    if not (NAMES - {'ai_routing_state.json', 'coupon_policies.json', 'business_filing.json'} <= set(sources) <= NAMES):
        raise ValueError('Invalid source path mapping')
    paths = {name: safe_path(path) for name, path in sources.items()}
    if len(set(paths.values())) != len(paths):
        raise ValueError('Source paths must be distinct')
    for path in paths.values():
        if path == ROOT or ROOT in path.parents:
            raise ValueError('Private state must remain outside the public checkout')
        if path.exists() and not path.is_file():
            raise ValueError('State paths must be regular files')
    return paths


def backup(sources, destination):
    sources = validate_sources(sources)
    destination = private_directory(destination, create=True)
    if not all(sources[name].is_file() for name in REQUIRED):
        raise ValueError('Settings and decisions database are required')
    staging = Path(tempfile.mkdtemp(prefix='.incomplete-', dir=destination))
    try:
        for name, source in sources.items():
            if not source.exists():
                continue
            if name == 'decisions.db':
                write_file(staging / name, b'')
                with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
                    with closing(sqlite3.connect(str(staging / name))) as target:
                        db.backup(target)
                        if target.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                            raise ValueError("Could not create a standalone SQLite snapshot")
                # Older SQLite builds can leave an unused shared-memory sidecar.
                for suffix in ("-wal", "-shm", "-journal"):
                    sidecar = Path(str(staging / name) + suffix)
                    if sidecar.exists():
                        sidecar.unlink()
                with (staging / name).open('rb') as stream:
                    os.fsync(stream.fileno())
            else:
                write_file(staging / name, read_file(source))
        manifest = {'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'files': {}}
        for path in staging.iterdir():
            data = read_file(path)
            manifest['files'][path.name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        write_file(staging / 'manifest.json', json.dumps(manifest, indent=2).encode())
        validate_snapshot(staging)
        sync_directory(staging)
        final = destination / ('backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        staging.rename(final)
        sync_directory(destination)
        return final
    except BaseException:
        shutil.rmtree(staging)
        raise


def restore(snapshot, sources, safety_destination, services_stopped=False):
    if not services_stopped:
        raise ValueError('Stop all Tahor services and acknowledge --services-stopped before restore')
    payloads = validate_snapshot(snapshot)  # Check every file before touching live state.
    sources = validate_sources(sources)
    if not set(payloads).issubset(sources):
        raise ValueError('Destination mapping lacks a backed-up state file')
    for name in payloads:
        if not sources[name].parent.is_dir():
            raise ValueError('Create destination directories with the correct service ownership first')
    for suffix in ('-wal', '-shm', '-journal'):
        safe_path(str(sources['decisions.db']) + suffix)
    safety = backup(sources, safety_destination)
    print('Pre-restore safety snapshot: ' + str(safety), file=sys.stderr, flush=True)
    # Each file replacement is atomic; the whole restore is not a transaction.
    for name, data in payloads.items():
        destination = sources[name]
        fd, temporary = tempfile.mkstemp(prefix='.restore-', dir=destination.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            if name == 'decisions.db':
                for suffix in ('-wal', '-shm', '-journal'):
                    sidecar = safe_path(str(destination) + suffix)
                    if sidecar.exists():
                        sidecar.unlink()
            sync_directory(destination.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return safety


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', type=Path, help='Private instance config; never copied into backups')
    parser.add_argument('--destination', type=Path, help='Private backup directory outside any Git checkout')
    parser.add_argument('--restore', type=Path, help='Restore this validated snapshot after stopping every Tahor service')
    parser.add_argument('--services-stopped', action='store_true', help='Explicitly acknowledge all Tahor services/timers are stopped')
    parser.add_argument('--verify', type=Path, help='Verify an off-host snapshot without loading instance credentials')
    args = parser.parse_args()
    if args.verify:
        if args.restore:
            parser.error('--verify and --restore cannot be combined')
        try:
            validate_snapshot(args.verify)
        except (ValueError, OSError, sqlite3.Error) as exc:
            parser.exit(1, 'Backup verification failed: ' + str(exc) + '\n')
        print('Private backup checksums and database verified.')
        return
    if not args.env or not args.destination:
        parser.error('--env and --destination are required for backup and restore')
    sys.path.insert(0, str(ROOT))
    import run
    run.load_environment(args.env.expanduser())
    data = Path(os.environ.get('DATA_DIR', Path.home() / '.config/tahor/data'))
    sources = {'business_filing.json': Path(os.environ.get('TAHOR_BUSINESS_RULES_PATH') or data / 'business_filing.json'),
               'coupon_policies.json': Path(os.environ.get('TAHOR_COUPON_POLICIES_PATH') or data / 'coupon_policies.json'),
               'settings.json': Path(os.environ.get('TAHOR_SETTINGS_PATH', Path.home() / '.config/tahor/settings.json')),
               'decisions.db': Path(os.environ.get('TAHOR_DB_PATH', ROOT / 'decisions.db')),
               'prompt.txt': Path(os.environ.get('PROMPT_PATH', data / 'prompt.txt')),
               'vendor_buckets.json': Path(os.environ.get('VENDOR_BUCKETS_PATH', data / 'vendor_buckets.json')),
               'sieve.txt': data / 'sieve.txt',
               'free_classifier_guidance.txt': Path(os.environ.get('TAHOR_FREE_CLASSIFIER_GUIDANCE_PATH') or data / 'free_classifier_guidance.txt'),
               'ai_routing_state.json': Path(os.environ.get('TAHOR_DB_PATH', ROOT / 'decisions.db')).parent / 'ai_routing_state.json'}
    try:
        path = restore(args.restore, sources, args.destination, args.services_stopped) if args.restore else backup(sources, args.destination)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, 'Backup operation failed: ' + str(exc) + '\n')
    print(('Pre-restore safety snapshot: ' if args.restore else 'Verified private backup: ') + str(path))


if __name__ == '__main__':
    main()
