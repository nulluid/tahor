"""Validate a single reply destination without contacting SMTP or sending mail."""
from email.utils import getaddresses
from email_validator import validate_email, EmailNotValidError, caching_resolver

_RESOLVER = caching_resolver(timeout=5)
NO_REPLY = ('no-reply', 'noreply', 'donotreply', 'do-not-reply', 'mailer-daemon')


def reply_recipient(message, owner, check_dns=True):
    # A present but invalid Reply-To is not permission to fall back to a different address.
    header = 'Reply-To' if 'Reply-To' in message else 'From'
    values = message.get_all(header, [])
    addresses = getaddresses(values)
    if len(addresses) != 1:
        return None
    address = addresses[0][1]
    if not address or any(c in address for c in '\r\n\0'):
        return None
    if any(pattern in address.split('@', 1)[0].lower() for pattern in NO_REPLY):
        return None
    try:
        result = validate_email(address, check_deliverability=check_dns, allow_smtputf8=False,
                                dns_resolver=_RESOLVER if check_dns else None)
    except EmailNotValidError:
        return None
    # DNS timeout/no nameserver results may be syntactically valid but have no MX result.
    if check_dns and not getattr(result, 'mx', None):
        raise RuntimeError('Reply destination DNS is inconclusive; retry later')
    normalized = result.ascii_email
    return None if normalized.lower() == owner.lower() else normalized
