"""Execute user-requested unsubscribe actions with bounded, public-only URLs."""
import ipaddress
import os
import ssl
import http.client
import smtplib
import socket
import urllib.request
import urllib.error
from email.message import EmailMessage
from urllib.parse import parse_qs, unquote, urlsplit


def public_addresses(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('Unsubscribe URL points to a non-public address')
    return addresses


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError('Unsubscribe URL must be a public HTTP or HTTPS address')
    if any(ord(c) < 32 for c in url):
        raise ValueError('Invalid unsubscribe URL')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    public_addresses(parsed.hostname, port)
    return url


class PublicHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        if self._tunnel_host:
            raise ValueError('Unsubscribe requests cannot use a proxy tunnel')
        addresses = public_addresses(self.host, self.port)
        last_error = None
        for family, kind, protocol, _, address in addresses:
            connection = socket.socket(family, kind, protocol)
            try:
                connection.settimeout(self.timeout)
                connection.connect(address)
                self.sock = connection
                return
            except OSError as exc:
                connection.close()
                last_error = exc
        raise last_error


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        PublicHTTPConnection.connect(self)
        try:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        except Exception:
            self.sock.close()
            raise


class PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(PublicHTTPConnection, request)


class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(PublicHTTPSConnection, request, context=self._context)


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_public(request):
    validate_url(request.full_url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicRedirect(), PublicHTTPHandler(), PublicHTTPSHandler())
    return opener.open(request, timeout=15)


class UnsubscribeError(RuntimeError):
    """An actionable, sanitized failure that can be displayed to the account owner."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def describe_failure(error):
    """Never expose tokenized URLs, recipient addresses or SMTP response bodies."""
    if isinstance(error, UnsubscribeError):
        return str(error)
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return ('Email unsubscribe could not sign in to the sending server. Verify the SMTP username and an app password with sending access. '
                'For Fastmail, use Mail (IMAP/POP/SMTP) access; working IMAP access alone does not verify SMTP access.')
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return 'The sender’s unsubscribe email address was rejected. Use its unsubscribe page or block marketing in Tahor.'
    if isinstance(error, smtplib.SMTPSenderRefused):
        return 'The sending server rejected your From address. Check Tahor’s SMTP account and permitted sender addresses.'
    if isinstance(error, urllib.error.HTTPError):
        status = error.code if type(error.code) is int and 100 <= error.code <= 599 else None
        return ('The sender rejected the unsubscribe request' + (f' (HTTP {status})' if status else '') +
                '. Open its unsubscribe page to finish, or block marketing in Tahor.')
    if isinstance(error, ssl.SSLError):
        return 'The unsubscribe connection failed TLS verification. No insecure connection was attempted; check the server configuration.'
    if isinstance(error, (smtplib.SMTPServerDisconnected, smtplib.SMTPDataError)):
        return 'Email unsubscribe was not confirmed. Check your mailbox or the sender’s unsubscribe page before retrying.'
    if isinstance(error, (OSError, TimeoutError, urllib.error.URLError)):
        return 'The unsubscribe service could not be reached. Try again later or use the sender’s unsubscribe page.'
    if isinstance(error, smtplib.SMTPException):
        return 'The sending server could not confirm the unsubscribe email. Check its SMTP configuration before retrying.'
    if isinstance(error, ValueError):
        return 'The advertised unsubscribe address is invalid or unsafe. Use your mail client to review it or block marketing in Tahor.'
    return 'The unsubscribe request could not be confirmed. Try again later or use the sender’s unsubscribe page.'


def _mailto_message(mailto, from_addr):
    parsed = urlsplit(mailto if mailto.lower().startswith('mailto:') else 'mailto:' + mailto)
    recipient = unquote(parsed.path)
    if (parsed.scheme != 'mailto' or parsed.netloc or parsed.fragment
            or recipient.count('@') != 1
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in '<>,;"' for c in recipient)):
        raise ValueError('Invalid unsubscribe email address')
    query = parse_qs(parsed.query, max_num_fields=20)
    subject = query.get('subject', ['unsubscribe'])[0]
    body = query.get('body', [''])[0]
    if ('\r' in subject or '\n' in subject or len(subject) > 1000
            or len(body) > 65536 or '\r' in from_addr or '\n' in from_addr):
        raise ValueError('Invalid unsubscribe email fields')
    message = EmailMessage()
    message['From'] = from_addr
    message['To'] = recipient
    message['Subject'] = subject
    message.set_content(body)
    return message


def _send_mailto(mailto, from_addr, app_password, smtp_host, smtp_port):
    # An optional dedicated sending credential avoids broadening the credential
    # used by the IMAP worker. Never substitute full web-login/TOTP credentials.
    username = os.environ.get('FASTMAIL_SMTP_USERNAME') or from_addr
    password = os.environ.get('FASTMAIL_SMTP_APP_PASSWORD') or app_password
    if not from_addr or not username or not password:
        raise UnsubscribeError('smtp_configuration', 'Email unsubscribe needs an SMTP username and an app password with sending access.')
    message = _mailto_message(mailto, from_addr)
    accepted = False
    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15, context=ssl.create_default_context()) as smtp:
            smtp.login(username, password)
            rejected = smtp.send_message(message)
            if rejected:
                raise UnsubscribeError('smtp_recipient', 'The sender’s unsubscribe email address was rejected. Use its unsubscribe page or block marketing in Tahor.')
            accepted = True
    except (smtplib.SMTPException, OSError):
        # A failed QUIT cannot undo the server's successful DATA response.
        # Do not invite another send after the request was already accepted.
        if not accepted:
            raise
    return 'Unsubscribe email submitted; the sender may take time to process it'


def execute(candidate, from_addr, app_password, smtp_host, smtp_port):
    url = candidate['unsubscribe_url']
    mailto = candidate['unsubscribe_mailto']
    try:
        if candidate['one_click'] and url:
            scheme = urlsplit(url).scheme
            if scheme == 'http':
                if mailto:
                    return _send_mailto(mailto, from_addr, app_password, smtp_host, smtp_port)
                validate_url(url)
                raise UnsubscribeError('manual_confirmation', 'This sender advertises an insecure HTTP one-click link. Tahor will not submit it automatically. Open the unsubscribe page in your browser to review it, or block marketing in Tahor.')
            if scheme != 'https':
                raise ValueError('One-click unsubscribe requires HTTPS')
            request = urllib.request.Request(url, data=b'List-Unsubscribe=One-Click', headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
            try:
                with open_public(request) as response:
                    if not 200 <= response.status < 300:
                        raise UnsubscribeError('http_rejected', 'The sender did not accept the one-click unsubscribe request. Use its unsubscribe page or block marketing in Tahor.')
                    return f'Unsubscribe request submitted (HTTP {response.status}); the sender may take time to process it'
            except urllib.error.HTTPError as error:
                status = error.code
                error.close()
                # Only a definite rejection permits this alternate submission.
                # Timeout/connection loss may mean the POST succeeded: do not
                # silently send another request over a second transport.
                if status != 403 or not mailto:
                    raise UnsubscribeError('http_rejected', describe_failure(error)) from None
                try:
                    result = _send_mailto(mailto, from_addr, app_password, smtp_host, smtp_port)
                except Exception as fallback_error:
                    raise UnsubscribeError('email_fallback', 'The one-click request was rejected (HTTP 403). ' + describe_failure(fallback_error)) from None
                return result + ' (the advertised email method was used after HTTP 403)'
        if mailto:
            return _send_mailto(mailto, from_addr, app_password, smtp_host, smtp_port)
        if url:
            validate_url(url)
            # Visiting a generic link does not establish that the subscription
            # changed; many pages require confirmation or a preference choice.
            raise UnsubscribeError('manual_confirmation', 'This sender requires confirmation on its unsubscribe page. Open that page to finish; Tahor has not marked you unsubscribed.')
        raise UnsubscribeError('no_mechanism', 'No unsubscribe mechanism is available. You can block marketing in Tahor instead.')
    except UnsubscribeError:
        raise
    except Exception as error:
        raise UnsubscribeError('submission_failed', describe_failure(error)) from None
