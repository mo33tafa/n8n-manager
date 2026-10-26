# Security

## Scope and support

This repository contains a deployment manager. n8n, PostgreSQL, Caddy, Docker and the host operating system are separate components that require their own security updates and configuration review.

There is currently no published maintenance window or guaranteed security-response timeframe. Review the current source and dependency advisories before production use.

## Reporting a vulnerability

Do not publish credentials, private workflow data, backup contents or unredacted vulnerability details in public issues.

If GitHub private vulnerability reporting is enabled for this repository, use **Security → Report a vulnerability**. Otherwise, use a private contact method explicitly published by the repository owner. No dedicated private reporting address is configured by this project; do not assume a public issue is confidential.

Include a redacted description, affected versions, impact and reproduction steps using a disposable environment. Never access systems or data without authorization.

## Operational guidance

- Run the manager only on a server and Docker daemon you control. Docker access grants extensive privileges.
- Create the n8n owner account immediately after initial deployment. There are no default application credentials.
- Use HTTPS for public deployments. Configure DNS and firewalls deliberately; the manager does not manage them.
- Local-only mode binds n8n to loopback and disables secure cookies for local HTTP access. Use an SSH tunnel; do not expose this mode through an insecure public proxy.
- Protect the installation directory. Generated `.env` and backup files contain sensitive material.
- Backups are not encrypted by this tool. Store protected off-server copies and test recovery separately.
- Logs can contain workflow data or other private information. Review and redact before sharing.
- Read release notes before upgrading. Backups and updates interrupt service, migrations may be irreversible and recovery is manual.
- Retained data is not erased by uninstall. Treat unused volumes, settings and backups as sensitive.
- Keep the host and Docker environment patched. An explicit n8n image tag is not an immutable digest; PostgreSQL and Caddy use major-version tags.

The included unit tests are not a production security audit or a substitute for real deployment and recovery testing.
