# Security

Tahor handles private email. Keep credentials and runtime data outside the public
checkout, use an app password, and expose the review app only through a trusted
local connection or HTTPS proxy. Access is restricted to one configured, verified
Google account. Hosted inference sends message excerpts to your selected provider.

Do not put real mail, tokens, passwords, or personal Sieve rules in public issues.
For a suspected vulnerability, use GitHub’s private vulnerability reporting for
this repository if enabled. Otherwise contact the maintainer through their GitHub
profile before publishing sensitive reproduction details.

The supported target is the current main branch. There is no security SLA.
Configuration mistakes, model classification errors, and external-provider
outages are operational risks even when the application behaves as designed.

Unsubscribe URLs are checked for non-public destinations and redirects are
checked again. These checks are not a replacement for network-level egress rules
in a hostile environment. Use a dedicated service account and keep cloud instance
metadata and other internal services inaccessible where possible.
