# Security policy

## Supported versions

relaybay is young; security fixes go into the latest release only.

| Version | Supported |
|---|---|
| 0.1.x (latest) | yes |
| older, unreleased `main` snapshots | no |

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Report it privately through GitHub's
private vulnerability reporting: on the repository page, open **Security → Report a
vulnerability** (<https://github.com/Codenator81/relaybay/security/advisories/new>).

Include what you found, how to reproduce it (relaybay version, OS, the steps or a proof of
concept) and what an attacker could do with it. Never include real tokens or personal data; use a
fresh data folder (`relaybay serve --home <temp folder>`) for any reproduction.

## What to expect

relaybay is maintained by one person in their spare time, so these are goals, not guarantees:

- an acknowledgement within 7 days;
- an assessment (accepted, needs more information, or not a vulnerability) within 30 days;
- for accepted reports, a fixed release and a published advisory, crediting you unless you ask
  not to be named.

Please give us a reasonable time to release a fix before you disclose the issue publicly.

## Scope

relaybay is a local server bound to `127.0.0.1` by default and protected by a token. The security
model, including what is out of scope (for example, a process running as the same OS user, or a
LAN bind without TLS), is described in the
[specification](https://codenator81.github.io/relaybay/SPEC/#spec-security).
