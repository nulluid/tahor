"""Credential-free recovery bundles shared by the exporter and off-host receiver."""
import hashlib
import json
import os
from pathlib import Path
import re
import tarfile
import io

from private_backup import NAMES, validate_snapshot, write_file, private_directory

MAX_BYTES = 1024 * 1024 * 1024
ALLOWED = {'app/' + name for name in NAMES | {'manifest.json'}} | {'bundle.json', 'provider-ownership.json'}


class RecoveryTarInfo(tarfile.TarInfo):
    @staticmethod
    def validate_header(member):
        if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or member.name not in ALLOWED:
            raise ValueError('Unexpected recovery archive header')
        if not 0 <= member.size <= MAX_BYTES:
            raise ValueError('Recovery archive exceeds size limit')
        return member

    @classmethod
    def frombuf(cls, buf, encoding, errors):
        return cls.validate_header(super().frombuf(buf, encoding, errors))

    def _proc_member(self, archive):
        # Python 3.14 uses _frombuf directly, bypassing the public frombuf hook.
        # All supported versions dispatch here before any PAX/GNU extension
        # body is read; reject extensions before decompression/allocation.
        self.validate_header(self)
        return super()._proc_member(archive)


def validate_completed(folder):
    folder = private_directory(folder)
    from private_backup import read_file
    if (folder / 'bundle.json').stat().st_mode & 0o077:
        raise ValueError('Recovery manifest must have private permissions')
    manifest = json.loads(read_file(folder / 'bundle.json'))
    if (set(manifest) != {'version', 'snapshot', 'files'} or manifest['version'] != 1
            or not re.fullmatch(r'backup-[0-9TZ]+', manifest['snapshot'])):
        raise ValueError('Invalid recovery manifest')
    expected = {'app/' + name for name in validate_snapshot(folder / 'app')} | {'app/manifest.json'}
    if (folder / 'provider-ownership.json').exists():
        expected.add('provider-ownership.json')
        provider_state(json.loads(read_file(folder / 'provider-ownership.json')))
    if set(manifest['files']) != expected or {p.name for p in folder.iterdir()} != ({'app', 'bundle.json'} | ({'provider-ownership.json'} if 'provider-ownership.json' in expected else set())):
        raise ValueError('Unexpected recovery contents')
    for name in expected:
        path = folder / name
        if path.stat().st_mode & 0o077:
            raise ValueError('Recovery files must have private permissions')
        data = read_file(path)
        if manifest['files'][name] != {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}:
            raise ValueError('Recovery checksum mismatch')
    return manifest


def provider_state(value):
    if not isinstance(value, dict) or set(value) != {'installation', 'user_id', 'owned', 'pending'}:
        raise ValueError('Invalid provider recovery fields')
    if not re.fullmatch('[a-f0-9]{32}', value['installation']):
        raise ValueError('Invalid provider installation identity')
    if value['user_id'] is not None and (not isinstance(value['user_id'], str) or not 0 < len(value['user_id']) <= 256):
        raise ValueError('Invalid provider account identity')
    if not isinstance(value['owned'], dict) or len(value['owned']) > 250:
        raise ValueError('Invalid provider ownership journal')
    for domain, rule_id in value['owned'].items():
        if (not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', domain)
                or '.' not in domain or '..' in domain
                or not isinstance(rule_id, str) or not 0 < len(rule_id) <= 256):
            raise ValueError('Invalid owned provider rule')
    pending = value['pending']
    if pending is not None and (not isinstance(pending, str) or not re.fullmatch(r'[a-z0-9.-]{1,253}', pending)):
        raise ValueError('Invalid pending provider intent')
    if (value['owned'] or pending) and not value['user_id']:
        raise ValueError('Provider recovery requires account binding')
    return value


def pack(snapshot, output, provider=None):
    snapshot = Path(snapshot)
    payloads = {'app/' + name: data for name, data in validate_snapshot(snapshot).items()}
    payloads['app/manifest.json'] = (snapshot / 'manifest.json').read_bytes()
    if provider is not None:
        payloads['provider-ownership.json'] = json.dumps(provider_state(provider), sort_keys=True).encode()
    manifest = {'version': 1, 'snapshot': snapshot.name, 'files': {
        name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in payloads.items()}}
    payloads['bundle.json'] = json.dumps(manifest, sort_keys=True).encode()
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as stream, tarfile.open(fileobj=stream, mode='w:gz', format=tarfile.USTAR_FORMAT) as archive:
        for name, data in payloads.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))


def unpack(archive_path, destination):
    destination = private_directory(destination, create=True)
    if any(destination.iterdir()):
        raise ValueError('Recovery destination must be empty')
    payloads, total = {}, 0
    with tarfile.open(archive_path, 'r:gz', tarinfo=RecoveryTarInfo) as archive:
        for member in archive:
            if member.name not in ALLOWED or member.name in payloads or not member.isfile():
                raise ValueError('Unexpected recovery archive member')
            total += member.size
            if member.size < 0 or total > MAX_BYTES:
                raise ValueError('Recovery archive exceeds size limit')
            payloads[member.name] = archive.extractfile(member).read(member.size + 1)
            if len(payloads[member.name]) != member.size:
                raise ValueError('Truncated recovery archive')
    manifest = json.loads(payloads.pop('bundle.json'))
    if (set(manifest) != {'version', 'snapshot', 'files'} or manifest['version'] != 1
            or not re.fullmatch(r'backup-[0-9TZ]+', manifest['snapshot'])
            or set(manifest['files']) != set(payloads)):
        raise ValueError('Invalid recovery manifest')
    for name, data in payloads.items():
        if manifest['files'][name] != {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}:
            raise ValueError('Recovery checksum mismatch')
    if 'provider-ownership.json' in payloads:
        provider_state(json.loads(payloads['provider-ownership.json']))
    (destination / 'app').mkdir(mode=0o700)
    for name, data in payloads.items():
        write_file(destination / name, data)
    write_file(destination / 'bundle.json', json.dumps(manifest, sort_keys=True).encode())
    return validate_completed(destination)
