# Security Policy

OtoDock is built to be the safe way to run powerful agents: every server-side
agent works inside a locked-down sandbox with network isolation on by default,
credentials are encrypted at rest and brokered per session, and access to your
machines and services is granted one scope at a time. We take reports that
poke holes in that story seriously — they make the platform better for
everyone who self-hosts it.

## Supported versions

Security fixes ship for the latest minor release line. Upgrading is
`docker compose pull && docker compose up -d` — see the
[CHANGELOG](CHANGELOG.md) before you pull.

| Version | Supported |
| ------- | --------- |
| 1.1.x   | ✅        |
| 1.0.x   | ✅        |
| < 1.0   | ❌        |

## Reporting a vulnerability

Please report vulnerabilities **privately** — do not open a public issue for
anything security-sensitive.

- **Preferred:** GitHub private vulnerability reporting — go to the
  repository's **Security** tab → **Report a vulnerability**. Reports land
  directly with the maintainer, privately.
- **Email:** [security@otodock.io](mailto:security@otodock.io)

You'll get an acknowledgment within **72 hours** and a status update as the
report is triaged. Fixes for confirmed vulnerabilities are prioritized ahead
of feature work and ship as patch releases with credit to the reporter (if
you'd like it).

## Scope

Especially interesting to us:

- Sandbox escapes (filesystem or network) from an agent session
- Credential exposure — anything that lets an agent or user read secrets,
  OAuth tokens, or another user's credentials
- Authentication / authorization bypasses (roles, per-agent access, API keys,
  webhook keys)
- Cross-user data access on a shared install

Out of scope: issues that require a malicious platform **admin** (the admin
owns the install), denial-of-service against your own self-hosted instance,
and reports from automated scanners without a demonstrated impact.

There is no paid bounty program at this time — just fast fixes, honest
credit, and our thanks.
