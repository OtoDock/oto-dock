# `scripts/`

Repo-wide tooling — scripts that operate across the whole OtoDock stack rather
than inside any single service (`proxy/`, `dashboard/`, `audio/`). They live at the repo root alongside the other whole-stack artifacts
(`docker-compose*.yml`, `VERSIONS.md`, `config.env.example`).

## Install / host setup

| Script | Purpose | Consumed by |
|--------|---------|-------------|
| `install-baseline-tools.sh` | Installs the baseline dev toolchain (git, gh, python+uv, node+pnpm, ripgrep, …) every agent sandbox expects. | the platform `proxy/Dockerfile` |
| `install-baseline-tools.ps1` | Windows (winget) equivalent of the above. | Windows hosts |
| `oto-git-credential-helper` | Git credential helper that feeds `GH_TOKEN` (from the github-mcp manifest's `env_injection`) to `git push`/`fetch` for `github.com` only. Installed into `/usr/local/bin` + wired into `/etc/gitconfig`. | installed by `install-baseline-tools.sh`; relied on by `proxy/core/sandbox/sandbox.py` |
| `setup-apparmor-userns.sh` | One-time host setup for Ubuntu 24.04+ (`kernel.apparmor_restrict_unprivileged_userns=1`): installs + loads the scoped `otodock_userns` AppArmor profile so the containerised proxy can create the unprivileged user namespaces the agent sandbox is built on — WITHOUT disabling the system-wide hardening. Self-contained (the pull-only compose flow fetches just this file); idempotent; no-ops on hosts without the restriction. | self-host operators (`sudo`); run automatically by `compose.sh` when needed |

## Compose / version pinning (self-host)

| Script | Purpose | Consumed by |
|--------|---------|-------------|
| `versions.sh` | Parses the pinned image/CLI versions out of [`VERSIONS.md`](../VERSIONS.md) (the single source of truth) and exports them. Sourceable, or `versions.sh KEY` to print one. | `compose.sh`; mirrors `proxy/config.py::_read_pinned_version` |
| `compose.sh` | `docker compose` wrapper for the fully-containerised stack: checks for the `docker` CLI + compose v2 plugin up front (clear error instead of a raw `exec` failure), stacks `docker-compose.yml` + `docker-compose.build.yml` (+ `docker-compose.phone.yml` when present) and sources `versions.sh` so the image build stays in lockstep with `VERSIONS.md`. On Ubuntu 24.04+ it also selects the `otodock_userns` AppArmor profile (via `OTODOCK_APPARMOR_PROFILE`), running `setup-apparmor-userns.sh` (sudo) itself when the profile isn't installed yet. | self-host operators |

## Development bootstrap

| Script | Purpose | Consumed by |
|--------|---------|-------------|
| `dev-setup.sh` | One-shot DEV bootstrap for the bare-metal (host-process) install (`--service` = generate+enable systemd unit(s) for this checkout; `--phone` = include the telephony daemon): pinned Node/Python/uv/pnpm from `VERSIONS.md`, baseline tools + Docker, proxy venv + `proxy/setup.sh` (config.env), Postgres via `docker-compose.t1.yml`, dashboard build. Idempotent — re-run it after `git pull` to reconcile the toolchain. NOT the production installer (no sudoers/quota tooling; production = Docker Compose). | developers / contributors |

## Database backup / restore

| Script | Purpose | Consumed by |
|--------|---------|-------------|
| `backup.sh` | Timestamped, gzip'd `pg_dump` of the OtoDock Postgres DB via `docker exec` (works for both the bare-metal and containerised stack). Honours `OTODOCK_BACKUP_DIR` / `OTODOCK_BACKUP_RETAIN`. | operators (cron / systemd-timer) |
| `restore.sh` | Restores a `backup.sh` dump back into Postgres (destructive — prompts for confirmation). | operators |

## Maintainer tooling

| Script | Purpose | Consumed by |
|--------|---------|-------------|
| `loadtest_sessions.py` | Concurrency load-test + memory/CPU calibration harness for the live-RAM admission gates (`SESSION_EST_*_MB` in `proxy/config.py`). Spawns K concurrent sessions through the real dashboard WebSocket and measures the proxy process tree. | maintainers (sizing/calibration) |
