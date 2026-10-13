# Security Policy

OtoDock is built to be the safe way to run powerful agents: every server-side
agent works inside a locked-down sandbox with network isolation on by default,
credentials are encrypted at rest and brokered per session, and access to your
machines and services is granted one scope at a time. Reports that poke holes
in that story make the platform better for everyone who self-hosts it, and we
treat them that way.

## Supported versions

Security fixes ship for the latest release line only: the newest entry on the
[Releases](https://github.com/OtoDock/oto-dock/releases) page and the patch
releases that follow it. Older lines get no backports; the fix for them is the
upgrade.

<!-- supported-versions:begin -->
| Version | Supported |
| ------- | --------- |
| 1.8.x (the latest release line) | ✅ |
| 1.7.x and earlier | ❌ |
<!-- supported-versions:end -->

The table is rewritten by the release tooling at every release.

**Upgrading.** On a Docker install, run the upgrade script from the install
folder:

```sh
curl -fsSLO https://raw.githubusercontent.com/OtoDock/oto-dock/main/scripts/upgrade.sh
bash upgrade.sh              # the latest release; --to <version> for another
```

It prints the Upgrade notes of every release it crosses before it changes
anything, takes a backup, refuses a target older than the release that last
migrated your database, and waits for the new server to answer. A source
checkout pulls the release tag and rebuilds. Read the
[upgrading guide](https://docs.otodock.io/administration/upgrading) and the
release notes ([CHANGELOG](CHANGELOG.md)) before you upgrade.

## Reporting a vulnerability

Please report vulnerabilities **privately**. Do not open a public issue for
anything security-sensitive.

- **Preferred:** GitHub private vulnerability reporting. Go to the
  repository's **Security** tab and choose **Report a vulnerability**. The
  report reaches the maintainers privately.
- **Email:** [security@otodock.io](mailto:security@otodock.io)

You get an acknowledgment within **72 hours** and status updates as the report
is triaged. Fixes for confirmed vulnerabilities take priority over feature
work.

## Advisories and CVEs

Every confirmed vulnerability gets a GitHub Security Advisory on this
repository. We request its CVE through GitHub's advisory flow, credit the
reporter in the advisory and the release notes (unless you ask us not to),
and publish the advisory with the release that fixes it. The release notes'
Security section links the advisory. A dependency we bump for a security fix
is named with its CVE.

## Scope

Especially interesting to us:

- Sandbox escapes (filesystem or network) from an agent session
- Credential exposure: anything that lets an agent or a person read secrets,
  OAuth tokens or another person's credentials
- Authentication and authorization bypasses (roles, per-agent access, API
  keys, webhook keys, shares)
- Cross-user data access on a shared install
- Anything that reaches a paired machine beyond what its owner granted

### Not a vulnerability

- **Anything that needs the platform admin to act against the install.** The
  admin owns the install: they run its code, hold its keys and can change any
  setting. A report that starts from an admin account (or from root on the
  host) shows what the admin can do, which is everything.
- **Getting past the command floor as someone already allowed to run
  commands.** The floor stops destructive commands by mistake in every
  engine; it is a safety net, not a security boundary against a person or an
  agent the admin let run commands. The sandbox, the path policy and the
  credential broker are the boundaries.
- **Scanner output without a demonstrated impact**: a dependency version a
  tool flags, a missing header, a TLS setting on your own reverse proxy.
- **Denial of service against your own install** by someone you gave an
  account (the per-person caps are fairness controls, not a tenant boundary).
- **The WOPI access token in Collabora's own log lines.** Collabora Online
  writes the token of a document's storage calls into its logs whatever the
  host does; the token is short-lived and scoped to one document, and the
  logs belong to the admin.
- **Bugs in a community MCP's server code.** Report those upstream to the
  server's project; report to us how OtoDock installs, isolates or
  authorizes it.

## How a release is checked

No release reaches this repository until the maintainers' release gate has
passed on the exact commit that ships: the proxy, dashboard, audio, phone and
satellite test suites; ruff, shellcheck, hadolint, actionlint and gitleaks;
Dependabot alerts, pip-audit, npm audit and trivy on every lockfile and
Dockerfile; semgrep and a CodeQL run with the same suite this repository's
code scanning uses, where only findings already reviewed may remain; a
`reuse lint` of the licensing; a check of every pinned version against
upstream; and a dry run of the public export with its leak scans. The images
of a release are then built, started and probed before any tag points at
them, and the release is installed and walked through on a Docker install
before it is published. The same quick checks run on every pull
request here.

## Verifying a release image

From 1.8.0 every release image (`otodock-proxy`, `otodock-file-tools`,
`otodock-phone`) carries a build provenance attestation signed by this
repository's release workflow, and an SBOM. Each image is started and probed
by that workflow before its version tag is written. Check an image before you
run it (with the GitHub CLI):

```sh
gh attestation verify oci://ghcr.io/otodock/otodock-proxy:<version> \
  --repo OtoDock/oto-dock --source-ref refs/tags/v<version>
```

and read its SBOM with:

```sh
docker buildx imagetools inspect ghcr.io/otodock/otodock-proxy:<version> --format '{{ json .SBOM }}'
```

The `SHA256SUMS` file attached to each GitHub Release covers the install
script, the Compose files and `config.env.example` attached beside it.

OtoDock is fair source under the Functional Source License (see
[LICENSE](LICENSE)). There is no paid bounty program at this time: fast fixes,
honest credit, and our thanks.
