# Security Policy

transmission_manage removes torrents and moves or deletes files, and it reads
tracker announce URLs that can contain passkeys. Please report anything that
could lead to unintended data loss, leaked credentials or remote control of the
tool privately.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:
[Report a vulnerability](https://github.com/lmipsum/transmission_manage/security/advisories/new).
Please do not open a public issue for security problems.

Include what you found, how to reproduce it, and the version (commit) you
tested. Redact passkeys, tokens and personal paths from logs and configs.

## Supported versions

Only the latest commit on `main` receives fixes until the first release.
