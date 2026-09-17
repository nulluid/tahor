"""Bounded Fastmail web authentication; an experimental, unpublished protocol.

Only this service reads the account password and separately enrolled TOTP seed.
Provider response bodies and exception details must never reach logs or status.
"""
from datetime import datetime
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import struct
import time
from urllib.parse import urlsplit

import requests
from data_changes import atomic_write

ORIGINS = frozenset(('https://app.fastmail.com', 'https://api.fastmail.com',
                     'https://phl.api.fastmail.com', 'https://slc.api.fastmail.com'))
MAIL = 'urn:ietf:params:jmap:mail'
MAX_RESPONSE = 2 * 1024 * 1024


class ConnectorError(Exception):
    code = 'provider_unavailable'


class AuthenticationRequired(ConnectorError):
    code = 'authentication_required'


class ProtocolError(ConnectorError):
    code = 'protocol_changed'


class RateLimited(ConnectorError):
    code = 'rate_limited'


class CredentialError(ConnectorError):
    code = 'credentials_required'


def endpoint(url, path):
    """Exact origins and paths: never follow a provider redirect with secrets."""
    if not isinstance(url, str):
        raise ProtocolError()
    p = urlsplit(url)
    if (p.scheme + '://' + p.netloc not in ORIGINS or p.path != path
            or p.query or p.fragment or p.username or p.password):
        raise ProtocolError()
    return url


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid not in (0, os.geteuid()) or info.st_size > MAX_RESPONSE):
            raise CredentialError()
        try:
            content = stream.read(MAX_RESPONSE + 1)
            if len(content) > MAX_RESPONSE:
                raise CredentialError()
            return json.loads(content)
        except (ValueError, UnicodeError):
            raise CredentialError() from None


def totp(seed, now=None):
    """RFC 6238 SHA-1, six digits, 30 seconds; no third-party secret service."""
    try:
        if not isinstance(seed, str) or len(seed) > 256:
            raise ValueError()
        normalized = seed.replace(' ', '').upper().rstrip('=')
        key = base64.b32decode(normalized + '=' * (-len(normalized) % 8), casefold=True)
        if len(key) < 20:
            raise ValueError()
        counter = struct.pack('>Q', int(time.time() if now is None else now) // 30)
    except (ValueError, TypeError, struct.error):
        raise CredentialError() from None
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f'{(struct.unpack(">I", digest[offset:offset+4])[0] & 0x7fffffff) % 1000000:06d}'


def validate_credentials(value):
    if not isinstance(value, dict) or set(value) != {'username', 'password', 'totp_seed'}:
        raise CredentialError()
    if any(not isinstance(v, str) or not v or len(v) > 4096 for v in value.values()):
        raise CredentialError()
    if '@' not in value['username'] or any(c in value['username'] for c in '\r\n\0'):
        raise CredentialError()
    totp(value['totp_seed'])
    return value


class FastmailAuth:
    def __init__(self, credentials, state_dir, transport=None, clock=time.time):
        self.credentials_path = Path(credentials)
        self.state_dir = Path(state_dir)
        self.clock = clock
        self.http = transport or requests.Session()
        self.http.trust_env = False  # Ignore proxy and .netrc credential injection.
        self.session = None
        self.auth_state = {}
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.state_dir.stat()
        if info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise CredentialError()
        try:
            saved = private_json(self.state_dir / 'session.json')
            self.session = saved.get('session')
            self.auth_state = saved.get('auth_state', {})
            for item in saved.get('cookies', []):
                # Cookie scope is retained, never copied as a generic Cookie header.
                if item['domain'].lstrip('.') not in ('fastmail.com', 'app.fastmail.com', 'api.fastmail.com', 'phl.api.fastmail.com', 'slc.api.fastmail.com'):
                    raise CredentialError()
                self.http.cookies.set_cookie(requests.cookies.create_cookie(**item))
        except FileNotFoundError:
            pass

    def save(self):
        cookies = [{k: getattr(c, k) for k in ('name', 'value', 'domain', 'path', 'secure', 'expires', 'version', 'port', 'discard', 'comment', 'comment_url', 'rfc2109')} for c in self.http.cookies]
        atomic_write(self.state_dir / 'session.json', json.dumps({
            'session': self.session, 'cookies': cookies, 'auth_state': self.auth_state}))

    def request(self, method, url, body=None, token=None):
        endpoint(url, urlsplit(url).path)
        if urlsplit(url).path not in ('/auth/login', '/auth/sudo', '/auth/sessions', '/jmap/api/'):
            raise ProtocolError()
        headers = {'Accept': 'application/json', 'Origin': 'https://app.fastmail.com'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            with self.http.request(method, url, json=body, headers=headers,
                                   allow_redirects=False, timeout=(10, 45), stream=True) as response:
                status = response.status_code
                if status in (401, 403, 410, 423):
                    raise AuthenticationRequired()
                if status == 429:
                    raise RateLimited()
                if status >= 500:
                    raise ConnectorError()
                if status not in (200, 201):
                    raise ProtocolError()
                chunks, size = [], 0
                for chunk in response.iter_content(16384):
                    size += len(chunk)
                    if size > MAX_RESPONSE:
                        raise ProtocolError()
                    chunks.append(chunk)
                try:
                    value = json.loads(b''.join(chunks))
                except (ValueError, UnicodeError):
                    raise ProtocolError() from None
                return status, value
        except requests.RequestException:
            raise ConnectorError() from None

    def accept(self, session, username, expected_user=None):
        if (not isinstance(session, dict) or session.get('username', '').lower() != username.lower()
                or not isinstance(session.get('accessToken'), str) or not session['accessToken']
                or not isinstance(session.get('userId'), str)
                or not isinstance(session.get('primaryAccounts', {}).get(MAIL), str)
                or (expected_user and session['userId'] != expected_user)):
            raise AuthenticationRequired()
        endpoint(session.get('apiUrl'), '/jmap/api/')
        if self.session and self.session.get('accessToken') != session['accessToken']:
            self.auth_state.pop('settings_until', None)
        self.auth_state['user_id'] = session['userId']
        self.session = {key: session[key] for key in ('username', 'userId', 'accessToken', 'apiUrl', 'primaryAccounts')}
        self.save()
        return self.session

    def login(self, credentials, revision):
        # Reserve before network I/O: process crashes cannot bypass the cooldown.
        now = self.clock()
        if self.auth_state.get('revision') == revision:
            if self.auth_state.get('blocked'):
                raise AuthenticationRequired()
            if now < self.auth_state.get('next_login', 0):
                raise RateLimited()
        bound_user = self.auth_state.get('user_id')
        settings_attempt = self.auth_state.get('settings_attempt')
        self.auth_state = {'revision': revision, 'next_login': now + 3600, 'blocked': False, 'user_id': bound_user}
        if settings_attempt:
            self.auth_state['settings_attempt'] = settings_attempt
        self.save()
        url = 'https://api.fastmail.com/auth/login'
        body = {'type': 'start'}
        try:
            for step in ('start', 'username', 'password', 'totp'):
                status, data = self.request('POST', endpoint(url, '/auth/login'), body)
                if status == 201:
                    if step != 'totp':
                        raise ProtocolError()
                    return self.accept(data, credentials['username'], self.auth_state.get('user_id'))
                if not isinstance(data, dict) or not isinstance(data.get('loginId'), str):
                    raise ProtocolError()
                if data.get('nextUrl'):
                    url = endpoint(data['nextUrl'], '/auth/login')
                next_step = {'start': 'username', 'username': 'password', 'password': 'totp'}.get(step)
                methods = data.get('methods')
                if not isinstance(methods, list):
                    raise ProtocolError()
                if not next_step or next_step not in [m.get('type') for m in methods if isinstance(m, dict)]:
                    raise AuthenticationRequired()
                body = {'type': next_step, 'loginId': data['loginId']}
                if next_step == 'username':
                    body['username'] = credentials['username']
                else:
                    body.update(value=credentials['password'] if next_step == 'password' else totp(credentials['totp_seed'], self.clock()), remember=False)
            raise ProtocolError()
        except (AuthenticationRequired, ProtocolError):
            self.auth_state['blocked'] = True
            self.save()
            raise

    def ensure_settings_auth(self):
        """Obtain Fastmail's short-lived settings authorization before a mutation.

        This is provider step-up authentication, not operating-system sudo.
        Never treat an arbitrary permission error as permission to elevate/replay.
        """
        if not self.session:
            raise AuthenticationRequired()
        now = self.clock()
        if self.auth_state.get('settings_until', 0) > now + 60:
            return
        credentials = validate_credentials(private_json(self.credentials_path))
        revision = hashlib.sha256(json.dumps(credentials, sort_keys=True).encode()).hexdigest()
        previous = self.auth_state.get('settings_attempt', {})
        if previous.get('revision') == revision:
            if previous.get('blocked'):
                raise AuthenticationRequired()
            if now < previous.get('next_attempt', 0):
                raise RateLimited()
        attempt = {'revision': revision, 'blocked': False, 'next_attempt': now + 900}
        self.auth_state['settings_attempt'] = attempt
        self.save()
        parsed = urlsplit(endpoint(self.session['apiUrl'], '/jmap/api/'))
        url = parsed.scheme + '://' + parsed.netloc + '/auth/sudo'
        body = {'type': 'start'}
        try:
            for step in ('start', 'password', 'totp'):
                status, data = self.request('POST', endpoint(url, '/auth/sudo'), body,
                                            token=self.session['accessToken'])
                if status == 201:
                    if not isinstance(data, dict):
                        raise ProtocolError()
                    try:
                        until = datetime.fromisoformat(data['sudoUntil'].replace('Z', '+00:00'))
                        if until.tzinfo is None or until.timestamp() <= now + 60:
                            raise ValueError()
                    except (KeyError, AttributeError, TypeError, ValueError):
                        raise ProtocolError() from None
                    self.auth_state['settings_until'] = until.timestamp()
                    self.save()
                    return
                if not isinstance(data, dict) or not isinstance(data.get('loginId'), str):
                    raise ProtocolError()
                if data.get('nextUrl'):
                    url = endpoint(data['nextUrl'], '/auth/sudo')
                next_step = {'start': 'password', 'password': 'totp'}.get(step)
                methods = data.get('methods')
                if not isinstance(methods, list):
                    raise ProtocolError()
                if not next_step or next_step not in [m.get('type') for m in methods if isinstance(m, dict)]:
                    raise AuthenticationRequired()
                body = {'type': next_step, 'loginId': data['loginId'], 'remember': False,
                        'value': credentials['password'] if next_step == 'password' else totp(credentials['totp_seed'], self.clock())}
            raise ProtocolError()
        except (AuthenticationRequired, ProtocolError):
            attempt['blocked'] = True
            self.save()
            raise

    def ensure_session(self):
        credentials = validate_credentials(private_json(self.credentials_path))
        # Root must reprovision or explicitly retry after a rejection. Hash stays private.
        revision = hashlib.sha256(json.dumps(credentials, sort_keys=True).encode()).hexdigest()
        if self.session:
            origin = urlsplit(endpoint(self.session.get('apiUrl'), '/jmap/api/'))
            try:
                _, sessions = self.request('GET', origin.scheme + '://' + origin.netloc + '/auth/sessions')
                if not isinstance(sessions, list):
                    raise ProtocolError()
                for candidate in sessions:
                    if isinstance(candidate, dict) and candidate.get('userId') == self.session['userId']:
                        return self.accept(candidate, credentials['username'], self.session['userId'])
            except AuthenticationRequired:
                pass
            self.session = None
            self.http.cookies.clear()
            self.save()
        return self.login(credentials, revision)
