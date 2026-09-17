"""Manage only individual Rule IDs created by this connector, never SieveBlocks."""
import hashlib
import json
import re
from pathlib import Path

from data_changes import atomic_write
from provider_connector.auth import ConnectorError, ProtocolError, endpoint, MAIL

CAPABILITIES = ['urn:ietf:params:jmap:core', MAIL, 'https://www.fastmail.com/dev/mail',
                'https://www.fastmail.com/dev/rules', 'https://www.fastmail.com/dev/user']


class Conflict(ConnectorError):
    code = 'rule_conflict'


def domains(values):
    if not isinstance(values, list) or len(values) > 250:
        raise ProtocolError()
    for value in values:
        if (not isinstance(value, str) or len(value) > 253 or '.' not in value
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in value.split('.'))):
            raise ProtocolError()
    if len(set(values)) != len(values):
        raise ProtocolError()
    return sorted(values)


def rule_for(domain, installation, enabled=True):
    domains([domain])
    return {'name': 'Tahor ' + installation + ': ' + domain, 'sortOrder': 0,
            'isEnabled': enabled, 'combinator': 'all',
            'conditions': [{'lookIn': 'sieve', 'lookHow': '',
                            'lookFor': f'address :domain :is "from" "{domain}"'}],
            'discard': True, 'stop': True, 'showNotification': False, 'skipInbox': False,
            'markRead': False, 'markFlagged': False, 'markSpam': False,
            'filter': None, 'search': None, 'redirectTo': None, 'fileIn': None,
            'snoozeUntil': None}


class RuleManager:
    def __init__(self, auth, state_dir, installation, guard=None):
        if not re.fullmatch(r'[a-f0-9]{32}', installation):
            raise ProtocolError()
        self.auth, self.installation = auth, installation
        self.guard = guard
        self.path = Path(state_dir) / 'rules.json'
        from provider_connector.auth import private_json
        try:
            self.journal = private_json(self.path)
        except FileNotFoundError:
            self.journal = {'owned': {}, 'pending': None}

    def save(self):
        atomic_write(self.path, json.dumps(self.journal, sort_keys=True))

    def call(self, method, args):
        if method not in ('Rule/get', 'Rule/set'):
            raise ProtocolError()
        if method == 'Rule/set' and self.guard is not None:
            self.guard()
        session = self.auth.session
        _, response = self.auth.request('POST', endpoint(session['apiUrl'], '/jmap/api/'),
            {'using': CAPABILITIES, 'methodCalls': [[method, dict(args, accountId=session['primaryAccounts'][MAIL]), 't']]},
            token=session['accessToken'])
        messages = response.get('methodResponses', [])
        if len(messages) != 1 or len(messages[0]) != 3 or messages[0][0] != method or messages[0][2] != 't':
            raise ProtocolError()
        result = messages[0][1]
        if result.get('accountId') != session['primaryAccounts'][MAIL]:
            raise ProtocolError()
        return result

    def get(self):
        result = self.call('Rule/get', {'ids': None})
        if not isinstance(result.get('list'), list):
            raise ProtocolError()
        return {r['id']: r for r in result['list']}

    @staticmethod
    def matches(actual, expected):
        return actual is not None and all(actual.get(k) == v for k, v in expected.items())

    def sync(self, requested):
        requested = domains(requested)
        owned = self.journal['owned']
        # Authenticate before recording a create intent or sending any mutation.
        # Then read provider state again; a challenge may take time to complete.
        if self.auth is not None and set(requested) != set(owned):
            self.auth.ensure_settings_auth()
        current = self.get()
        # Recover an uncertain create only from a previously persisted intent.
        pending = self.journal.get('pending')
        if pending:
            expected = rule_for(pending, self.installation)
            candidates = [r for r in current.values() if r.get('name') == expected['name']]
            if len(candidates) > 1 or (candidates and not self.matches(candidates[0], expected)):
                raise Conflict()
            if candidates:
                owned[pending] = candidates[0]['id']
                self.journal['pending'] = None
                self.save()
            else:
                # A timed-out create might still execute. Never create a duplicate blindly.
                raise Conflict()
        for domain, rule_id in list(owned.items()):
            expected = rule_for(domain, self.installation)
            if rule_id not in current:
                if domain in requested:
                    raise Conflict()  # A user deleted an owned rule: do not recreate it.
                del owned[domain]
                self.save()
                continue
            if not self.matches(current[rule_id], expected):
                raise Conflict()
            if domain not in requested:
                result = self.call('Rule/set', {'destroy': [rule_id]})
                if rule_id not in result.get('destroyed', []):
                    raise Conflict()
                if rule_id in self.get():
                    raise Conflict()
                del owned[domain]
                self.save()
        for domain in requested:
            if domain in owned:
                continue
            expected = rule_for(domain, self.installation)
            if any(r.get('name') == expected['name'] for r in current.values()):
                raise Conflict()
            self.journal['pending'] = domain
            self.save()
            result = self.call('Rule/set', {'create': {'new': expected}})
            rule_id = result.get('created', {}).get('new', {}).get('id')
            if not rule_id:
                if 'new' in result.get('notCreated', {}):
                    self.journal['pending'] = None
                    self.save()
                raise ProtocolError()
            # Persist returned identity before readback, so a read failure is recoverable.
            owned[domain] = rule_id
            self.journal['pending'] = None
            self.save()
            current = self.get()
            if not self.matches(current.get(rule_id), expected):
                raise Conflict()
        return len(owned)


def digest(requested):
    return hashlib.sha256(json.dumps(domains(requested), separators=(',', ':')).encode()).hexdigest()
