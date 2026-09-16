"""Execute user-requested unsubscribe actions with bounded, public-only URLs."""
import ipaddress
import smtplib
import socket
import urllib.request
from email.message import EmailMessage
from urllib.parse import parse_qs, unquote, urlsplit


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Unsubscribe URL must be a public HTTP or HTTPS address')
    if any(ord(c) < 32 for c in url):
        raise ValueError('Invalid unsubscribe URL')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('Unsubscribe URL points to a non-public address')
    return url


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_public(request):
    validate_url(request.full_url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicRedirect())
    return opener.open(request, timeout=15)


def execute(candidate, from_addr, app_password, smtp_host, smtp_port):
    url = candidate['unsubscribe_url']
    mailto = candidate['unsubscribe_mailto']
    if candidate['one_click'] and url:
        if urlsplit(url).scheme != 'https':
            raise ValueError('One-click unsubscribe requires HTTPS')
        request = urllib.request.Request(url, data=b'List-Unsubscribe=One-Click', headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
        with open_public(request) as response:
            return f'Unsubscribe request submitted (HTTP {response.status})'
    if mailto:
        if not from_addr or not app_password:
            raise RuntimeError('SMTP credentials are not configured')
        parsed = urlsplit(mailto if mailto.lower().startswith('mailto:') else 'mailto:' + mailto)
        recipient = unquote(parsed.path)
        if recipient.count('@') != 1 or any(c.isspace() or c in '<>,;"' for c in recipient):
            raise ValueError('Invalid unsubscribe email address')
        query = parse_qs(parsed.query)
        subject = query.get('subject', ['unsubscribe'])[0]
        if '\r' in subject or '\n' in subject:
            raise ValueError('Invalid unsubscribe email subject')
        message = EmailMessage()
        message['From'] = from_addr
        message['To'] = recipient
        message['Subject'] = subject
        message.set_content(query.get('body', [''])[0])
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as smtp:
            smtp.login(from_addr, app_password)
            smtp.send_message(message)
        return 'Unsubscribe email submitted'
    if url:
        with open_public(urllib.request.Request(url)) as response:
            return f'Unsubscribe link requested (HTTP {response.status}); the sender may require confirmation'
    raise ValueError('No unsubscribe mechanism is available')
