# OtoDock Versions

This file is the **single source of truth** for all runtime versions used by the platform. Bumping a version here is the only place a version should change — Docker Compose, install scripts, CI/CD pipelines, and the platform's `/health` endpoint all read from this file.

> Status: The **CLI pins** (`CLAUDE_CODE_VERSION` / `CODEX_VERSION`) are wired end-to-end (install scripts + a runtime parser in `proxy/config.py` → fleet reconcile — see "CLI pin-and-freeze" below). The **base-image pins** (`PYTHON_IMAGE` / `NODE_IMAGE` / `POSTGRES_IMAGE` / `OTODOCK_VERSION`) are wired into the Docker build by `scripts/versions.sh` (a shell parser mirroring `config.py::_read_pinned_version`) → the `build.args` of `docker-compose.build.yml` (the build overlay), via the `scripts/compose.sh` wrapper. Bumping a version here rebuilds on the new base with no Dockerfile edit; the Dockerfile `ARG` defaults are the fallback for a bare `docker compose` build.

---

## Platform version

```
OTODOCK_VERSION=1.3.2
```

The platform's own version. Bumped on every minor/major release. Released versions follow semver (`v1.0.0`, `v1.0.1`, `v1.0.2`).

`-dev` suffix indicates work in progress between releases. Stripped on release.

## Docker base images

```
POSTGRES_IMAGE=postgres:16.14-alpine
PYTHON_IMAGE=python:3.13.14-slim-bookworm
NODE_IMAGE=node:24.18.0-slim
```

Pinned to exact minor+patch. For CI gold standard, bump these to digest pins (`@sha256:...`) when the deployment matures.

## Runtime binary versions

These are what the platform installer installs on the host machine (every install path that provisions a host runs `scripts/install-baseline-tools.{sh,ps1}`).

```
NODE_VERSION=24.18.0
PYTHON_VERSION=3.13.14
PYTHON_MIN_VERSION=3.13
UV_VERSION=0.11.24
NPM_VERSION=11.16.0
PNPM_VERSION=11.9.0
SYMPY_VERSION=1.14.0
CLAUDE_CODE_VERSION=2.1.215
CODEX_VERSION=0.144.6
GH_VERSION=2.94.0
BUBBLEWRAP_VERSION=0.6.1
BUBBLEWRAP_MIN_VERSION=0.6.0
PASST_VERSION=2026_05_26.038c51e
PASST_MIN_VERSION=2023_12_30
```

Notes:
- **Python**: minimum supported is 3.13. Install scripts target 3.13.14 (matches dev environment) but may use whatever 3.13+ is available on the host. The Docker `python:3.13.14-slim-bookworm` base image is exact.
- **Node**: minimum is 22 LTS. We default to 24.18.0 (current LTS — "Krypton" — as of platform v1.0.0). Capacitor 8 requires Node 22+.
- **pnpm**: installed via `npm install -g pnpm@${PNPM_VERSION}` by the installer (faster Node package manager for some MCP authors / agent workflows). v11 is the current major (new store format + supply-chain defaults).
- **sympy**: symbolic maths in the agents' python (the file-tools maths workflow). Installed by the baseline installers pip-first into whatever `python3` resolves to (exact pin); externally-managed system pythons (PEP 668) fall back to the distro package (`python3-sympy` / brew `sympy`), whose version floats with the distro — presence beats an exact pin there.
- **GitHub CLI**: `GH_VERSION` is a reference value only — the installer adds the cli.github.com apt repo and installs the **latest** `gh` (no pin).
- **Bubblewrap**: distro-provided (`apt install bubblewrap`), **not** pinned to an exact version — `BUBBLEWRAP_VERSION` is an informational reference (Ubuntu 22.04 LTS ships 0.6.1; Debian 12 ships ~0.8; latest upstream is 0.11.2). `BUBBLEWRAP_MIN_VERSION` (0.6.0) is the **documented floor** — the realistic oldest supported host (Ubuntu 22.04 LTS ships 0.6.1) for the namespace/seccomp/mount features the sandbox uses. It is documentation, *not* yet a runtime gate (no `bwrap --version` preflight); adding one is an optional polish. Don't raise it higher (0.8+ would exclude Ubuntu 22.04 LTS, still supported to 2027).
- **passt / pasta**: provides the user-mode network stack that wraps **every** local agent sandbox in an isolated network namespace (always on — there is no toggle). Date-versioned upstream (`YYYY_MM_DD.<short-sha>`); `PASST_MIN_VERSION` is a *feature* floor (the build must support `--no-map-gw` + per-port `-T` forwarding), not a staleness pin — leave it unless a needed feature lands in a newer build. The startup preflight checks pasta's **presence** + that an unprivileged user+net namespace can be created (hard-fails the proxy boot if `pasta`/`ip`/`bwrap` are missing or namespaces are blocked), not its version. **Not in Ubuntu 22.04 (jammy) main** — `scripts/install-baseline-tools.sh` installs it as a HARD requirement, auto-fetching the upstream **static** build from `https://passt.top/builds/` on jammy; Debian 12 (bookworm) / Ubuntu 23.10+ ship it as `apt install passt`. The proxy Dockerfile installs `passt iproute2 bubblewrap`.

## Deliberate pins — DO NOT bump (compatibility-locked)

These are pinned for correctness, not staleness. A "selective reconcile" /
"bump everything to latest" pass MUST skip them — bumping any one breaks a
runtime path. Each pin documents its reason at the pin site.

| Pin | Where | Why it's locked |
|-----|-------|-----------------|
| `playwright==1.59.0` + `@playwright/mcp@0.0.68` | `mcps/community/camoufox/Dockerfile` | camoufox 0.4.11's Firefox launch driver needs `browserServerImpl` (playwright-core **1.60+ dropped** it), and `@playwright/mcp@0.0.68`'s MCP code lives in a nightly playwright-core (`1.59.0-alpha-…`). The Dockerfile pins `playwright==1.59.0` and **overwrites** camoufox's driver `playwright-core` with the MCP's bundled alpha core so both ends speak the same connect protocol. Newer/mismatched pairs fail to launch the browser. |
| `cartesia>=3.2,<4.0` (lock 3.2.0) | `audio/pyproject.toml`, `phone/requirements.txt` | Cartesia 3.x exposes the public context/push API with a dict `output_format`; the old private `cartesia._types.OutputFormat` import (≤2.x) is gone and 4.x is a future break. The audio TTS provider is written against 3.x. |
| `silero-vad-lite==0.2.1` | `audio/pyproject.toml`, `phone/requirements.txt` | Exact-pin — VAD behaviour is runtime semantics (endpointing tuning depends on it), not a stable public API. |
| Base images: `postgres:16.14-alpine`, `python:3.13.14-slim-bookworm`, `node:24.18.0-slim` | (this file) | Exact for reproducible image builds; bump deliberately, not opportunistically. |

> **Per-MCP SDK version spread is expected, not drift.** Each MCP has its own
> venv (`mcps/{cat}/{name}/venv/`), so different `mcp` / `uvicorn` / `starlette`
> versions across MCPs are isolated by design (see MCP-FRAMEWORK.md "Runtime
> Isolation"). Don't "unify" them — pin each to a *validated* version (the one
> its venv actually runs) and leave it.

## Distro-default dev tools

The installers (`scripts/install-baseline-tools.sh`, run by the platform
install and every consumer that provisions a host)
`apt install` the following tools at whatever version the host's package
index provides — no version pinning needed because the CLI surface is
stable across years:

```
git jq ripgrep tree make gcc build-essential
poppler-utils sqlite3
```

These are baseline "every agent sandbox should have these" tools —
agents authoring a script, building a project, processing a PDF, or
querying a sqlite db all rely on them being present. See
`scripts/install-baseline-tools.sh` for the canonical
apt-install block. If a specific tool needs version-pinning later
(release introduces a flag break), promote it into the pinned table
above and document the reason.

## Proxy-side media tooling (NOT agent-facing)

```
ffmpeg ffprobe   # proxy-side only; any recent build (≥4.x); no pin needed
```

`ffmpeg` + `ffprobe` are installed for the **proxy process only**, to transcode
non-web-native audio/video (e.g. iPhone HEVC `.mov`) into browser-playable MP4
for inline playback in chat + workspace (`proxy/services/media_pipeline.py`).

**Unlike the distro-default tools above, these must NOT be reachable by agent
sandboxes or installed on satellites.** They are deliberately excluded from
`scripts/install-baseline-tools.sh` (which is mounted into the bwrap sandbox).
Install them to a path OUTSIDE the sandbox-mounted system dirs
(`/usr`,`/bin`,`/lib`,`/sbin`) — e.g. `/opt/otodock/bin/` — and point the proxy
at them via `OTO_FFMPEG_PATH` / `OTO_FFPROBE_PATH` (see `config.env.example`).
The proxy falls back to a `PATH` lookup when those are unset (dev convenience).
No version pin — the CLI surface is stable across years; any recent ffmpeg
works. (A future `video-mcp` will use its own Dockerized ffmpeg for agent-driven
editing/transcription — unrelated to this playback path.)

## CLI pin-and-freeze

The platform runs against **exact** Claude Code / Codex CLI versions. `CLAUDE_CODE_VERSION` / `CODEX_VERSION` above are the single source of truth; everything reads them so a one-line bump here propagates fleet-wide.

**Source → consumers**
- `scripts/install-baseline-tools.sh` (Linux/macOS) and `scripts/install-baseline-tools.ps1` (Windows) both **install-or-upgrade** the global npm package to the pin (mismatch → upgrade, not skip). The `.sh`/`.ps1` defaults mirror the values above; override per-run via the `CLAUDE_CODE_VERSION` / `CODEX_VERSION` env vars.
- `proxy/config.py` parses these out of this file at startup → `PINNED_CLAUDE_CODE_VERSION` / `PINNED_CODEX_VERSION` (missing/garbled → "" → reconcile becomes a no-op, never a crash).

**Propagation + reconcile (the freeze)**
- The CLIs' **own** auto-update is disabled so they can't drift: Claude Code via `autoUpdates: false` in `settings.json` (`core/sandbox.py`) **+** `DISABLE_AUTOUPDATER=1` env (`core/env_builder.py`). Codex has **no silent auto-updater** (only the manual `codex update` subcommand) and is strict about unknown `config.toml` keys, so nothing is added there — the npm pin is its freeze.
- The proxy host itself is checked at boot by `core/sandbox.cli_version_preflight()` — **warn-only** (logs drift so an operator can re-run the installer; never blocks startup).

**To bump a CLI version:** edit `CLAUDE_CODE_VERSION` / `CODEX_VERSION` above, update the matching defaults in `scripts/install-baseline-tools.{sh,ps1}` and the doc references (`CLAUDE-CODE-CLI.md`, `CODEX.md`), restart the proxy (re-reads the pin). **Codex bumps require a live app-server smoke test** (its turn/thread surface is only re-verified by hand — see `CODEX.md`). **Claude bumps: re-check the TUI diff colors** against `dashboard/src/lib/ptyBrandColors.ts` — the dashboard terminal brand-tints the file-edit rows by rewriting the theme's exact truecolor triples, which are read from the pinned binary; a change degrades silently to the stock colors (grep the new binary for `diffAdded:"rgb(`).

## Downstream community repos

```
COMMUNITY_MCPS_VERSION=v1.0.0
COMMUNITY_AGENTS_VERSION=v1.0.0
```

Tagged releases of the `OtoDock/community-mcps` and `OtoDock/community-agents` repos — recorded here as the catalog versions validated against this platform release. These are real tags on those repos, not placeholders: bump the value here only after tagging the catalog repo itself.


## Database schema version

The platform manages this internally via PostgreSQL `schema_version` table + idempotent `run_migrations()`. Not surfaced here — bumping migrations doesn't require a `VERSIONS.md` change.

---

## Update procedure

When bumping any version above, follow the full per-pin upgrade
cascade (a bump is rarely a one-line edit). The short form:

1. **Update this file**.
2. **Regenerate any affected lockfiles** (Python deps via `uv pip compile --python-version <ver>`, dashboard via `npm install`) — and, on a **Python** bump, the `mcps/custom/*` lockfiles too.
3. **Mirror toolchain pins into the install scripts** where they're carried as defaults (the Docker build runs `install-baseline-tools.{sh,ps1}` before VERSIONS.md is in the image): `UV_VERSION`, `PNPM_VERSION`, `CLAUDE_CODE_VERSION`, `CODEX_VERSION`, `SYMPY_VERSION`.
4. **Test** end-to-end: base images → rebuild + offline smoke; CLI versions → a dashboard chat through that path.
5. **Bump `OTODOCK_VERSION`** on a breaking change. **Tag a release** (`git tag v0.X.Y && git push --tags`).

## Versions pinned ELSEWHERE (index)

`VERSIONS.md` is the source only for the shared toolchain above. These are pinned
in their own files (one place to *see* them, but not owned here):

- **Library deps** — `proxy/`, `audio/pyproject.toml`, `mcps/custom/*/requirements.txt` (all `==`, regenerated from `requirements.in` via `uv pip compile`); `dashboard/package-lock.json` (npm-managed). **Regenerate, never hand-edit.** Watch for **major** bumps: backend `fastapi`/`starlette`/`sse-starlette`/`pydantic`; frontend `react`/`vite`/`tailwindcss`/`typescript`.
- **Third-party compose images** — pinned directly in `docker-compose.yml` (not via a `*_IMAGE` var): `collabora/code:25.04.9.4.1` (document preview), `tecnativa/docker-socket-proxy:v0.4.2` (Docker-socket allowlist shim — the Docker-access **security boundary**; v0.4.x rebases the engine on haproxy 3.x. The allowlist env semantics — `CONTAINERS/IMAGES/NETWORKS/VOLUMES/POST` + default-deny — are unchanged; verified by an allowlist-matrix + real container-lifecycle smoke before the bump. Note the tag is `v`-prefixed from 0.4.x on. Bump deliberately + re-smoke the boundary, never opportunistically). Bump deliberately, not opportunistically.
- **Docker-MCP runtimes** — each Docker MCP carries its own runtime, independent of the platform (the startup reconciliation skips them): `mcps/custom/file-tools-mcp` (python:3.13 — tracked here under `mcps/custom/`), `mcps/community/camoufox` (python:3.12 in the published GHCR image — **3.13 update pending a community-repo CI republish**; playwright 1.59.0 — locked; **sourced from the `OtoDock/community-mcps` repo + CI→GHCR**, the `mcps/community/` copy here is a gitignored runtime artifact), `mcps/community/github-mcp` (go1.25 + python:3.12), `m365-mcp`, etc. Only file-tools + camoufox are *ours* to track (file-tools in-repo; camoufox via the community repo); the rest are community MCPs wrapped for compat.
- **Custom MCP system requirements** — each MCP's `manifest.json` `system_requirements` (`node_min` + OS packages; no `python_min` — that comes from the package's upstream `requires-python`).
