# Security

Tahor handles private email. Keep credentials and runtime data outside the public
checkout, use an app password, and expose the review app only through a trusted
local connection or HTTPS proxy. Access is restricted to one configured, verified
Google account. Hosted inference sends message excerpts to your selected provider.

Internet-facing services should run as a dedicated account without sudo access,
using administrator-owned application code and a separate writable data directory.
See [service isolation](docs/service-isolation.md) for the layout, hardened system
units, migration checks and limitations. A separate process under the same
administrator account is not a credential security boundary.

Do not put real mail, tokens, passwords, or personal Sieve rules in public issues.
For a suspected vulnerability, use GitHub’s private vulnerability reporting for
this repository if enabled. Otherwise contact the maintainer through their GitHub
profile before publishing sensitive reproduction details.

The supported target is the current main branch. There is no security SLA.
Configuration mistakes, model classification errors, and external-provider
outages are operational risks even when the application behaves as designed.

Unsubscribe URLs are checked for non-public destinations and redirects are
checked again. Connections use the validated address directly, preventing a second
DNS lookup from redirecting the connection, while retaining TLS hostname checks.
These checks are not a replacement for network-level egress rules
in a hostile environment. Use a dedicated service account and keep cloud instance
metadata and other internal services inaccessible where possible.

## Optional Fastmail account credentials

The [isolated connector](docs/fastmail-connector.md) has a separate service identity,
root-owned code, encrypted password/TOTP provisioning, private session storage, and no
public credential endpoint. Its narrow domain-rule interface does not reduce the
underlying Fastmail session's account privileges. Same-host root compromise defeats
this isolation. Never include connector credentials, cookies, seeds, or private journals
in reports. Full account authentication must be verified during enrollment.

## Reply rules and model output

Natural-language reply rules send the matching directions and up to 6,000
characters of message context through the classification provider. Draft writing
also sends the reply instructions and a longer excerpt. Keep private names,
signatures, mailbox samples, and rule settings out of the public repository.
Screenshots and tests should use synthetic data.

Email is untrusted input. Matching and writing instructions come from the mailbox
owner; message content must not authorize tool use or change rules. Uncertain
semantic matches wait for review. Drafts are saved in the mailbox and never sent
automatically; the owner must review recipients, facts, and commitments before
sending. Reply-address syntax and DNS validation do not guarantee deliverability
or authenticate the sender.
