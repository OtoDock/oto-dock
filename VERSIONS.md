# OtoDock Versions

This file is the **single source of truth** for all runtime versions used by the platform. Bump a version here first: the image build and CI (through `scripts/versions.sh`), `scripts/dev-setup.sh`, the release workflow and the proxy (`/health`, the CLI pins) read this file, and the defaults that cannot read it (the installers', the Dockerfile `ARG`s, the compose literals, CI's Postgres images) are mirrored by hand and held equal by the mirror tests ("Update procedure" step 3).

> Status: The **CLI pins** (`CLAUDE_CODE_VERSION` / `CODEX_VERSION`) are wired end-to-end (install scripts + a runtime parser in `proxy/config.py` → fleet reconcile — see "CLI pin-and-freeze" below). The **base-image pins** (`PYTHON_IMAGE` / `NODE_IMAGE` / `POSTGRES_IMAGE` / `OTODOCK_VERSION`) are wired into the Docker build by `scripts/versions.sh` (a shell parser mirroring `config.py::_read_pinned_version`) → the `build.args` of `docker-compose.build.yml` (the build overlay), via the `scripts/compose.sh` wrapper. Bumping a version here rebuilds on the new base with no Dockerfile edit; the Dockerfile `ARG` defaults are the fallback for a bare `docker compose` build.

---

## Platform version

```
OTODOCK_VERSION=1.9.0
```

The platform's own version. Bumped on every minor/major release. Released versions follow semver (`v1.0.0`, `v1.0.1`, `v1.0.2`).

`-dev` suffix indicates work in progress between releases. Stripped on release.
The Android app takes its version from this line: `versionName` is the value
as written, and `versionCode` is (major × 10000 + minor × 100 + patch) × 100 +
`OTODOCK_ANDROID_BUILD`, 0 to 99. A change here runs the Android build check.
Capacitor has no row in this file: `dashboard/package-lock.json` pins it.

## Docker base images

```
POSTGRES_IMAGE=postgres:16.15-alpine@sha256:f4610795587492cd4fb887c2dc3de301b813278bb6f616992061141b5f15ac36
PYTHON_IMAGE=python:3.13.17-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530
NODE_IMAGE=node:26.12.1-bookworm-slim@sha256:28147155d47dc31abe0a46e0d12b59b2fb3d0a2dcc5dc65fce76a074180b16fe
```

Pinned to the exact version AND the digest of its manifest list (`<image>:<tag>@sha256:<digest>`, the digest Docker Hub serves for the tag and `docker buildx imagetools inspect <image>:<tag>` prints): a build resolves exactly these bytes on either architecture, and a tag the upstream rebuilds under the same name (a patched base) changes nothing until the digest is bumped on purpose. The versions sweep compares each digest with the one the tag serves today and reports DIGEST when the tag moved; the tag part is compared with upstream as before.

## Published image architectures

| Image | linux/amd64 | linux/arm64 |
|-------|-------------|-------------|
| `otodock-proxy` | yes | yes |
| `otodock-file-tools` | yes | yes |
| `otodock-phone` | yes | yes |

The release tags are manifest lists, so `docker pull` resolves per host and no
Compose change is needed on either architecture; the release workflow fails
when a tag's list lacks either one, and the release tooling verifies the pull
of both before the release counts. The source flow (`scripts/compose.sh up -d
--build`) builds the host's own architecture. Every Python dependency of the
three images installs from a wheel on both (the Silero VAD model is vendored
and runs on onnxruntime, see the compatibility-locked table below).

## Runtime binary versions

These are what the platform installer installs on the host machine (every install path that provisions a host runs `scripts/install-baseline-tools.{sh,ps1}`). uv and pnpm are installed when absent and upgraded when the host's copy is older than the pin (a newer one is left alone; on macOS brew's current ones are taken). On Linux uv is judged by `/usr/local/bin/uv`, the copy the agents' sandboxes run, else by the `uv` on `PATH`, which is copied into `/usr/local/bin` when it is at or above the pin; an install runs `pipx install --force` for the installing user, which replaces whatever sits at `~/.local/bin/uv` (a pipx-managed copy or a standalone one there), while a copy elsewhere on `PATH` stays and wins wherever it comes first, and `/usr/local/bin/uv` ends at or above the pin. Node is installed when absent (the check is `npm` on Linux, where the NodeSource major floats within it, and `node` on Windows, winget's current LTS), while macOS takes brew's current `node` (`brew install` also upgrades an outdated one); Bun (Linux) and the two CLIs are installed or moved to the exact pin; sympy is installed when `python3` cannot import it (the `.ps1` re-pins it on every run).

```
NODE_VERSION=26.12.1
PYTHON_VERSION=3.13.17
PYTHON_MIN_VERSION=3.10
UV_VERSION=0.13.5
NPM_VERSION=11.20.0
PNPM_VERSION=12.15.0
SYMPY_VERSION=1.14.0
CLAUDE_CODE_VERSION=2.1.296
CODEX_VERSION=0.163.0
GH_VERSION=2.103.2
BUBBLEWRAP_VERSION=0.6.1
BUBBLEWRAP_MIN_VERSION=0.6.0
PASST_VERSION=2026_10_02.cba3570
PASST_MIN_VERSION=2023_12_30
BUN_VERSION=1.4.4
```

Notes:
- **Python**: two pins. `PYTHON_VERSION` (3.13.17) is the platform's own interpreter — the proxy, phone and file-tools images (`python:3.13.17-slim-bookworm`, exact), the uv-managed dev venvs, and CI by its minor (`python-version: '3.13'`, the runner's newest 3.13 patch). `PYTHON_MIN_VERSION` (3.10) is the **host floor**: the satellite venv and the baseline tooling run on the host's own `python3` (Ubuntu 22.04 ships 3.10, Debian 12 3.11, Ubuntu 24.04 3.12; on Windows the installer provisions Python 3.12 via winget when no real interpreter at or above the floor is present). The satellite is written to 3.10 (no newer feature; `tomllib` is guarded), its lockfile is compiled at the floor, CI runs its suite on the floor and on the platform's Python, and `satellite/tests/test_python_floor.py` pins every site that states the floor. Auto-update never changes a host's interpreter, so raising the floor is a fleet compatibility event: every paired machine below the new floor needs a newer Python, and a re-pair, before the satellite release that uses it.
- **Node**: minimum is 22 LTS. We default to 26.12.1, the 26 line, which becomes LTS on 2026-10-28 (24 "Krypton" enters maintenance on 2026-10-20). `NODE_IMAGE` names the `-bookworm-slim` tag: 26's plain `-slim` tag is Debian 13 (trixie), and the image's node stage hands the binary to the bookworm Python base (Node's binaries need glibc 2.28 or later). A native install moves to the pinned major when `scripts/dev-setup.sh` runs; the installer adds Node only where `npm` is absent, so a paired machine keeps its own. Capacitor 8 requires Node 22+. `NPM_VERSION` is the npm that `NODE_VERSION` bundles (11.20.0 with 26.12.1, per the `npm` field of nodejs.org's dist index): nothing installs npm by version, and the image's npm comes from `NODE_IMAGE`. A host whose owner moved npm to 12 still installs the CLIs: the installers and the satellite allow the CLI package's own install scripts (`--allow-scripts`), which npm 12 blocks by default.
- **pnpm**: installed via `npm install -g --allow-scripts=pnpm pnpm@${PNPM_VERSION}` by the installer, which also upgrades an older copy to the pin (faster Node package manager for some MCP authors / agent workflows). The pinned major is 12, the Rust rewrite: its npm package carries the native binary in a per-platform optional dependency, and its install step puts it at the bin path (npm 12 skips install steps by default and leaves a shim that runs the binary through Node, hence the flag). It adds the `pn` and `pnx` command names, keeps the lockfile format (an existing project's first install may rewrite its lockfile once), fetches GitHub git dependencies over HTTPS, refuses `--frozen-lockfile false`, and needs its metadata cache for an `--offline` install even with a lockfile (pnpm 11 went on without it). In a local agent's sandbox the store sits at the mount's root (`/workspace/.pnpm-store`, as with 11) and its metadata cache beside the store (the sandbox sets `PNPM_CONFIG_CACHE_DIR` to the scope's `.cache/pnpm`, since pnpm 12 refuses an offline install without it).
- **sympy**: symbolic maths in the agents' python (the file-tools maths workflow). Installed by the baseline installers pip-first into whatever `python3` resolves to (exact pin); externally-managed system pythons (PEP 668) fall back to the distro package (`python3-sympy` / brew `sympy`), whose version floats with the distro — presence beats an exact pin there. The Windows installer runs `pip install --user sympy==<pin>` on the real Python on every run (no distro fallback).
- **Claude Code**: 2.1.296 (2026-10-13; the 2.1.289 → 2.1.296 review is in `CLAUDE-CODE-CLI.md`). Claude Opus 5.5 — the engine's declared default since 2026-09-24 — REQUIRES ≥2.1.280 (older CLIs answer `unrecognized_model`), as Fable 5.1 required ≥2.1.251. Claude Sonnet 5.5, the Direct LLM default since 2026-10-04, needs 2.1.284 or newer (an older CLI runs it with a 200K compaction window); Claude Haiku 5.5, a builtin on this engine and Direct LLM since 2026-10-13, needs 2.1.293 or newer the same way and 2.1.296 for its token counts. 2.1.306 was reviewed on 2026-10-21 and not pinned: from 2.1.299 a headless Plan chat is refused the calls the platform's gate allows there (the review notes named at the end of this row). Satellites reconcile installed CLIs to this pin on reconnect (pin-and-freeze), so a pin bump here propagates to the fleet without a satellite release.
- **Codex**: 0.163.0 (2026-10-21; the 0.162.1 → 0.163.0 review is in `CODEX.md`). GPT-6.1 Sol, the Codex default since 2026-10-04, needs 0.159.1 or newer (an older catalog refuses the id).
- **GitHub CLI**: `GH_VERSION` is a reference value only — the installer adds the cli.github.com apt repo and installs the **latest** `gh` (no pin). It also names the gh whose help `proxy/auth/gh_flags.py` was read from. An agent's GitHub reach reads gh commands by that table, so a newer gh's new flag stops a narrowed agent's command until the table is read again with `scripts/gen-gh-flags.py`.
- **Bubblewrap**: distro-provided (`apt install bubblewrap`), **not** pinned to an exact version — `BUBBLEWRAP_VERSION` is an informational reference (Ubuntu 22.04 LTS ships 0.6.1; Debian 12 ships ~0.8; latest upstream is 0.11.2). `BUBBLEWRAP_MIN_VERSION` (0.6.0) is the **documented floor** — the realistic oldest supported host (Ubuntu 22.04 LTS ships 0.6.1) for the namespace/seccomp/mount features the sandbox uses. It is documentation, *not* yet a runtime gate (no `bwrap --version` preflight); adding one is an optional polish. Don't raise it higher (0.8+ would exclude Ubuntu 22.04 LTS, still supported to 2027).
- **passt / pasta**: provides the user-mode network stack that wraps **every** local agent sandbox in an isolated network namespace (always on — there is no toggle). Date-versioned upstream (`YYYY_MM_DD.<short-sha>`). `PASST_VERSION` is the release the installer BUILDS from its git tag (`https://passt.top/passt`, the clone refused unless it is the tag's commit, `cba357068dc586e7540971eb40bb56c5e62c4de1` for the pin) wherever the host offers nothing at or above the floor: the proxy image (bookworm ships `0.0~git20230309`; the Dockerfile sets `OTODOCK_PASST_BUILD=required`, so the image build fails rather than ship another), Debian 12 and Ubuntu 22.04 hosts. passt.top keeps no versioned static build and none for aarch64, which is why it is a source build (gcc, make and git are baseline tools); the licence texts, the source it was built from (`passt-<version>.tar.gz`, about 400 KB, GPL-2.0-or-later) and a note naming the commit land in `/usr/local/share/doc/passt`. `PASST_MIN_VERSION` is a *feature* floor (the build must support `--no-map-gw` + per-port `-T` forwarding), not a staleness pin: a pasta at or above it stays, a distro passt at or above it is installed as is (Ubuntu 24.04, Debian 13). The startup preflight hard-fails the proxy boot if `pasta`/`ip`/`bwrap` are missing or namespaces are blocked, and WARNS (never refuses) when pasta is older than the floor: such a pasta still runs the launcher, and every 1.8.x image shipped one. Outside the image a failed build keeps the host's own pasta, else falls back to the distro's passt, then to upstream's moving static build on x86_64 (`OTODOCK_PASST_BUILD=distro`, which a paired machine's bootstrap sets, never builds or downloads: with no pasta and no distro passt it warns and goes on). The versions sweep compares the pin with the newest upstream tag. Bump it with the commit in the installer (`passt_commit`), then an image build and `scripts/image-boot-check.sh`, which runs the agent shape (pasta wrapping bwrap) in the built image.
- **Bun**: the runtime of app servers (apps with a `server/` folder): the proxy launches each app's server inside its own sandbox with the Bun binary bind-mounted in. PINNED exactly (`BUN_VERSION`; releases are tagged `bun-v<version>`): `scripts/install-baseline-tools.sh` downloads the release zip for the host architecture (`bun-linux-x64`, `-aarch64`, the `-baseline` build on CPUs without AVX2) into `/usr/local/bin/bun`; the Docker build inherits it from the same script. The proxy finds it through `BUN_BIN`, then `PATH`, then the official user install at `~/.bun/bin/bun`. A bump is the pin plus the installer's own default (`bun_ver` in `install_bun`, which the Docker build uses before this file is copied in), then a fresh `bun --version` on a host and a deploy of one app with a server. The download is verified by TLS only; no release asset is hash-pinned yet.

## Deliberate pins — DO NOT bump (compatibility-locked)

These are pinned for correctness, not staleness. A "selective reconcile" /
"bump everything to latest" pass MUST skip them — bumping any one breaks a
runtime path. Each pin documents its reason at the pin site.

| Pin | Where | Why it's locked |
|-----|-------|-----------------|
| `camoufox==0.5.8` + `playwright==1.62.0` + `@playwright/mcp@0.0.78` | the community catalog's `camoufox/Dockerfile` (`ARG`s) | `@playwright/mcp`'s MCP code lives in the nightly playwright-core it bundles (`1.62.0-alpha-…`), and camoufox caps Playwright (below 1.63 for 0.5.8). The Dockerfile **overwrites** camoufox's driver `playwright-core` with the MCP's bundled alpha core so both ends speak the same connect protocol. The three move together; the catalog's weekly upstream report computes the newest set that fits. |
| `cartesia>=3.2,<4.0` (lock 3.2.0) | `audio/pyproject.toml`, `phone/requirements.txt` | Cartesia 3.x exposes the public context/push API with a dict `output_format`; the old private `cartesia._types.OutputFormat` import (≤2.x) is gone and 4.x is a future break. The audio TTS provider is written against 3.x. |
| `audio/models/silero_vad.onnx` (Silero VAD v5.1.2, vendored) | `audio/models/README.md` | Not a package pin but a locked file: VAD behaviour is runtime semantics (the phone's endpointing tuning depends on these exact weights and the no-context feed in `audio/providers/vad/silero_model.py`). Swapping the model file is a tuning change; prove equivalence against the previous file, or re-tune, before shipping. |
| `audio/models/lang-id-ecapa-voxlingua107.onnx` (SpeechBrain ECAPA VoxLingua107, INT8 ONNX, vendored) | `audio/models/README.md` | A locked file, not a package pin: the language rule's thresholds (`audio/providers/lid/rule.py`: LEAN, SURE, the prior, the voice floors) were set by a benchmark (482 clips, six languages, 16 kHz and an 8 kHz line) against these exact weights and this quantization. A re-export or another model is a tuning change: re-run the benchmark's rule rows and re-set the thresholds before shipping it; the export recipe is in the README. |
| Base images: `postgres:16.15-alpine`, `python:3.13.17-slim-bookworm`, `node:26.12.1-bookworm-slim`, each with its manifest-list digest | (this file) | Exact for reproducible image builds; bump the tag or the digest deliberately, not opportunistically. |

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
- `scripts/install-baseline-tools.sh` (Linux/macOS) and `scripts/install-baseline-tools.ps1` (Windows) both **install-or-upgrade** the global npm package to the pin (mismatch → upgrade, not skip), with `--allow-scripts=<package>` so npm 12, which blocks install scripts by default, still runs the CLI's own install step (Claude Code's puts its native binary in place; npm 11 and 10 run it anyway). The `.sh`/`.ps1` defaults mirror the values above and are what every Docker image installs (`proxy/tests/execution/test_engine_descriptor.py::test_the_installer_defaults_mirror_the_pins` holds them equal); override per-run via the `CLAUDE_CODE_VERSION` / `CODEX_VERSION` env vars.
- `proxy/config.py` parses these out of this file at startup → `PINNED_CLAUDE_CODE_VERSION` / `PINNED_CODEX_VERSION` (missing/garbled → "" → reconcile becomes a no-op, never a crash).

**Propagation + reconcile (the freeze)**
- The proxy ships the pins to each satellite in the WS `auth_result` (`cli_pins`). The satellite records them synchronously at auth (`cli_versions.set_pins` — before any spawn command can arrive) and runs `reconcile_cli_versions()` off-thread (fail-open), **verify-first** (satellite ≥ 0.5.106): it scans candidate binaries — the `satellite.conf` hint, the service PATH, npm's global bin dirs — probing `--version` against the pin; a candidate already at the pin is recorded with NO install, and `npm i -g --allow-scripts=<pkg> <pkg>@<pin>` (the flag since 0.5.136) runs only when nothing satisfies the pin; a binary that prints no version, npm 12's stub `claude`, satisfies nothing. Every session spawn then resolves through the recorded pin-verified path (`resolve_spawn_bin*`, with a spawn-time fast-verify for the first session racing a cold install) — so PATH order and stale `satellite.conf` absolutes can never pick a shadowing install (e.g. a native-installer copy in `~/.local/bin`); with no pin shipped, the conf hint keeps full authority. On sudo-less Linux the global npm prefix (NodeSource `/usr`) isn't writable — the install falls back to `--prefix ~/.npm-global`, whose `bin/` the satellite prepends to PATH at startup (`ensure_user_npm_bin_on_path`). Windows/macOS prefixes are user-writable and never hit the fallback. Satellites ≥ 0.5.107 also report the resolved per-CLI `{version, path}` after every reconcile pass (`cli_status`) — surfaced on the admin Remote Machines cards with an amber highlight when a machine drifts from the pin. Design doc: `satellite/host/cli_versions.py` module docstring.
- The CLIs' **own** auto-update is disabled so they can't drift: Claude Code via `autoUpdates: false` in `settings.json` (`core/layers/cli/config_dir.py`) **+** `DISABLE_AUTOUPDATER=1` env (`core/sandbox/env_builder.py`). Codex has **no silent auto-updater** (only the manual `codex update` subcommand) and is strict about unknown `config.toml` keys, so nothing is added there — the npm pin is its freeze.
  - Satellites apply the same freeze on their side (`satellite/sessions/cli_session.py` writes `autoUpdates: false`; the satellite env carries `DISABLE_AUTOUPDATER=1`).
- The proxy host itself is checked at boot by `core/sandbox/sandbox.py::cli_version_preflight()` — **warn-only** (logs drift so an operator can re-run the installer; never blocks startup).

**To bump a CLI version:** edit `CLAUDE_CODE_VERSION` / `CODEX_VERSION` above, update the matching defaults in `scripts/install-baseline-tools.{sh,ps1}` and the doc references (`CLAUDE-CODE-CLI.md`, `CODEX.md`), run the installer on the proxy host or build the new image first (the boot preflight only warns, so a proxy restarted first runs the old CLIs), then restart the proxy (re-reads the pin) → satellites self-reconcile on reconnect. (The row's name is the engine's `pin_key` upper-cased plus `_VERSION` — `ADDING-AN-ENGINE.md` in the execution-layers docs, "The CLI version pin", lists every site a NEW engine's pin touches; the bootstrap headers, the boot preflight and `/health` read the registry.) **Codex bumps require a live app-server smoke test** (its turn/thread surface is only re-verified by hand — see `CODEX.md`), **a probe of the approval policies over RPC** (every app-server thread sends `on-request`, or `never` under `danger-full-access`, with `approvalsReviewer` `user`, or `auto_review` in Auto: `codex_approvals.approval_policy` / `approvals_reviewer`; `HOOKS.md` "Attended Codex chats"), **a re-read of the hooks crate** (the 12 events, the Stop output shape, `bypass_hook_trust`; `HOOKS.md` "Shapes per event") **and a refresh of the local-model catalog's instructions text**: `proxy/core/layers/codex/codex_base_instructions.md` must equal `codex-rs/models-manager/prompt.md` at the new tag (`https://raw.githubusercontent.com/openai/codex/rust-v<pin>/codex-rs/models-manager/prompt.md`), with the length/hash constants in `proxy/tests/session/test_codex_local_model_catalog.py` and the tag in `codex_base_instructions.NOTICE` updated (see `CODEX.md` "Binary + version"). **Claude bumps: re-check the TUI diff colors** against `dashboard/src/lib/ptyBrandColors.ts` — the dashboard terminal brand-tints the file-edit rows by rewriting the theme's exact truecolor triples, which are read from the pinned binary; a change degrades silently to the stock colors (grep the new binary for `diffAdded:"rgb(`). **Claude bumps: re-verify that `proxy/services/mcp/skill_format.split_frontmatter` still matches the CLI loader's frontmatter rule** — one leading byte-order mark dropped, the fence closed at the first `---` after the opening line (`/^---\s*\n([\s\S]*?)---\s*\n?/`) — because the SKILL.md scrub is a security boundary only while the platform and the CLI agree on what is frontmatter (SKILLS.md "Security"; `tests/mcp/test_skill_format.py`). **A new Claude default model needs the CLI that knows it**: probe `claude --model <id> -p` on the new binary (an older one answers `unrecognized_model`), and check the flags the spawns pass (`--system-prompt-snapshot off` since 2026-09-24, `--setting-sources user` and the Codex TUI's `--no-daemon` since 2026-10-04) exist on the previous pin too, so a host mid-reconcile still spawns. **Claude bumps: check the built-in mods.** An empty `enabledPlugins` leaves Claude Code's built-in plugins on (2.1.287 and later); every built-in but the four the platform keeps (`cc-plugin-agents-md`, `cc-plugin-diff`, `cc-plugin-telemetry` and the managed-only `cc-plugin-sec-default`) is off by name in `core/layers/cli/config_dir.DISABLED_BUILTIN_PLUGINS` and the satellite twin (`satellite/sessions/cli_session.py`), so a remote flag cannot switch on one that shipped dark. The probe is the `@builtin` ids the new binary names and the init frame's `plugins` and `skills` of a `-p` run under the platform's settings: a new built-in joins the list unless the platform decides to keep it on. **Codex bumps: check the TUI stays embedded.** `daemon_auto_start` is on by default since 0.157 (a shared background server with its own Codex install, outside the pin); both TUIs pass `--no-daemon`, and no `packages/` directory appears under the session's `CODEX_HOME` after a TUI start. **Codex bumps: re-run the inner-sandbox mountpoint test** with the new binary (`CODEX_BIN=<the new codex> proxy/venv/bin/python -m pytest proxy/tests/execution/test_codex_inner_sandbox.py`, from the repo root): Codex's own sandbox mounts over named folders of every writable root, and a folder it adds that the read-only user folder lacks breaks the first command of every chat (`.aws` from 0.160); it joins the pre-created list in `proxy/core/sandbox/sandbox.py`. **Codex bumps: re-read the summary request.** `turn/start` must still take `summary` (`ReasoningSummary`), and the bundled catalog's `default_reasoning_summary` and `supports_reasoning_summary_parameter` decide whether a model sends one (`codex debug models`); a model newly marked as not taking it shows no reasoning text, and a refusal wording that changes must still match `helpers.summary_refused` and its satellite twin (`proxy/core/layers/codex/helpers.py`, `satellite/sessions/codex_session.py`). **Codex bumps: read the approval kinds.** `item/commandExecution/requestApproval` carries `kind`; `writeStdin` is the terminal-input card (`CodexTerminalInput`), and a kind the bridge does not know falls to the command gate as `Bash` (`codex_approvals.approval_to_tool`). **A new Codex default model needs the CLI whose catalog lists it and its effort prefixes**: the pinned Codex's bundled catalog must list the id (an older catalog refuses it), and `core/layers/codex/helpers.py` must unlock its `max` and `ultra` by the exact id (`gpt-6.1-sol` does not match `gpt-6-sol`). **Codex bumps: diff `CodexErrorInfo`** (`v2/shared.rs`): a value that ends a turn for a reason the platform names joins `codex/translator._ENDING_INFO` (0.160's `tooManyDenials` → `declined`/`denials`); any other stays a plain ERROR. **Claude bumps: check the limit wording and the account tools**: a new usage-limit notice must match `turn_ending._LIMIT_RE`, `scheduler/lanes._limit_notice` and `subscription_pool._LIMIT_ERROR_MARKERS` ("You're out of usage credits", 2026-10-04), and a new claude.ai tool that acts on the signed-in account joins `DISALLOWED_BUILTIN_TOOLS` (the Artifact tools, 1.7.1). **Claude bumps: walk a headless Plan chat**: a call the gate allows in Plan (an open-tier MCP tool such as `display_ui`, a `memory` write) must run there. From 2.1.299 the CLI refuses it in the Plan spawn the platform starts today (`--permission-mode plan`, no prompt tool), and the 2.1.306 review notes name the fix for that bump: probe first a Plan spawn with bypass available (`--allow-dangerously-skip-permissions` beside the plan mode, which leaves the gate the only authority), and fall back to a prompt tool the platform answers. **A CLI bump on npm 12**: install it with `--allow-scripts=<package>` under npm 12 and run `--version`; a release that moves its binary into a dependency's install script needs that package allowed too. **A new direct-llm builtin needs its hosted-relay row first** (`otodock-commercial` `relay_vendors.py`, deployed before the release that lists it): the relay prices per model and rejects unknown ids.

## Downstream community repos

```
COMMUNITY_MCPS_VERSION=v1.0.0
COMMUNITY_AGENTS_VERSION=v1.0.0
```

Tagged releases of the `OtoDock/community-mcps` and `OtoDock/community-agents` repos — recorded here as the catalog versions validated against this platform release. These are real tags on those repos, not placeholders: bump the value here only after tagging the catalog repo itself.

## Protocol versions

```
SATELLITE_VERSION=0.5.158
MIN_SATELLITE_VERSION=0.5.76
```

Wire-protocol version between platform and satellite daemon, bumped on every wire-protocol change. `SATELLITE_VERSION` here mirrors the authoritative `satellite/config.py::SATELLITE_VERSION` — the proxy reads that file (not this one) for its latest-version push, so update both when bumping.

`MIN_SATELLITE_VERSION` is the minimum the platform will accept; older satellites are rejected with an upgrade prompt.

## Database schema version

The platform manages this internally via PostgreSQL `schema_version` table + idempotent `run_migrations()`. Not surfaced here — bumping migrations doesn't require a `VERSIONS.md` change.

## Docker floors

```
COMPOSE_FLOOR_VERSION=2.26.1
COMPOSE_FLOOR_SHA256=2f61856d1b8c9de29ffdaedaa1c6d0a5fc5c79da45068f1f4310feed8d3a3f61
```

The oldest Docker the install scripts accept is Docker Engine 26 (API 1.45) with Docker Compose 2.26: `install.sh`, `upgrade.sh`, `scripts/compose.sh` and `dev-setup.sh` check both (the document editor's key is one file of a volume, a `subpath`), and `proxy/tests/deploy/test_compose_topology.py` holds their Compose check to this pin's minor. Before each release the candidate is booted fresh with Compose `COMPOSE_FLOOR_VERSION` (`https://github.com/docker/compose/releases/download/v<version>/docker-compose-linux-x86_64`, refused unless its sha256 is `COMPOSE_FLOOR_SHA256`, the value the release's own `.sha256` file and `checksums.txt` carry), so the Compose floor is a tested promise; the engine floor is checked by the scripts and never booted there. A floor, never bumped with the other pins (the versions sweep has no upstream for it): it moves only with the floor the scripts check.

---

## Update procedure

When bumping any version above, follow the full per-pin upgrade
cascade (a bump is rarely a one-line edit). The short form:

1. **Update this file**.
2. **Regenerate any affected lockfiles** (Python deps via `uv pip compile --python-version <ver>`, dashboard via `npm install`) — and, on a **Python minor** bump, the `mcps/custom/*` lockfiles too (a Python patch release changes no lockfile; a `UV_VERSION` bump recompiles every lockfile once, with no upgrade flag).
3. **Mirror toolchain pins into the install scripts** where they're carried as defaults (the Docker build runs `install-baseline-tools.sh` before VERSIONS.md is in the image; the `.ps1` carries its own defaults for Windows hosts): `UV_VERSION`, `PNPM_VERSION`, `BUN_VERSION`, `SYMPY_VERSION`, `NODE_VERSION` (the `.sh` takes its major for the NodeSource line), `CLAUDE_CODE_VERSION`, `CODEX_VERSION`; `proxy/tests/execution/test_engine_descriptor.py` holds every one equal to its row. The image pins (tag and digest) have mirrored defaults too: the `ARG PYTHON_IMAGE` / `ARG NODE_IMAGE` of the three Dockerfiles, the `${POSTGRES_IMAGE:-…}` literals of both compose files and `scripts/dev-setup.sh`, CI's Postgres service images and `dev-setup.sh`'s `PYTHON_VERSION`/`NODE_VERSION` fallbacks (`test_the_image_defaults_mirror_the_pins`, same file).
4. **Test** end-to-end: base images → rebuild + offline smoke; CLI versions → a dashboard chat through that path.
   - Satellite bumps: pair a fresh satellite, run a turn.
5. **Bump `OTODOCK_VERSION`** on a breaking change. **Tag a release** (`git tag v0.X.Y && git push --tags`).

## Versions pinned ELSEWHERE (index)

`VERSIONS.md` is the source only for the shared toolchain above. These are pinned
in their own files (one place to *see* them, but not owned here):

- **Library deps** — `proxy/`, `phone/`, `satellite/requirements.txt`, `audio/pyproject.toml`, `mcps/custom/*/requirements.txt` (all `==`, regenerated from `requirements.in` via `uv pip compile`); `dashboard/package-lock.json` (npm-managed). **Regenerate, never hand-edit.** Watch for **major** bumps: backend `fastapi`/`starlette`/`sse-starlette`/`pydantic`; frontend `react`/`vite`/`tailwindcss`/`typescript`.
- **Third-party compose images** — pinned directly in `docker-compose.yml` (not via a `*_IMAGE` var), each by tag and manifest-list digest like the base images: `collabora/code:26.04.5.1.1` (document preview; `mcps/custom/file-tools-mcp/docker-compose.yml` carries the same tag and digest for a native install's Collabora and its key one-shot, and the two files move together; since 26.04 the image has no shell (user 1001, its own entrypoint `coolwsd --use-env-vars`), so both files pass every setting as environment, run in copy mode (`mount_jail_tree=false`) and mount the WOPI proof key from a volume as a single file, which needs Docker Engine 26 and Docker Compose 2.26 or later; both turn background save off, as a forked save on 25.04.10.3.1 could hang past its timeout and drop the edit; both mount a script over the image's `browser/dist/welcome/welcome.js` that closes the welcome dialog (on Docker a file of the key's volume, which the key's one-shot writes; on a native install `collabora-welcome.js` beside the compose file; never a compose `configs` entry, which Compose 2.26 to 2.3x copy into the created container before the one-shot has written the key, failing the first start), so a bump re-checks the welcome override's path), `tecnativa/docker-socket-proxy:v0.5.0` (Docker-socket allowlist shim — the Docker-access **security boundary**; v0.5.0 moves the engine to haproxy 3.4 and adds pause and unpause flags that default off, which changes nothing here: `CONTAINERS=1` with `POST=1` passes those two paths as before. The allowlist env semantics — `CONTAINERS/IMAGES/NETWORKS/VOLUMES/POST` + default-deny — are unchanged; verified by an allowlist-matrix + real container-lifecycle smoke before each bump (v0.5.0: the same answers on 32 probes as v0.4.2, a community Docker MCP installed, restarted and removed through it). Note the tag is `v`-prefixed from 0.4.x on. Bump deliberately + re-smoke the boundary, never opportunistically). Bump deliberately, not opportunistically.
- **Docker-MCP runtimes** — each Docker MCP carries its own runtime, independent of the platform (the startup reconciliation skips them): `mcps/custom/file-tools-mcp` (its Python base is `PYTHON_IMAGE`, passed by the release build, the Dockerfile's `ARG` default held equal to the row; plus **Playwright `playwright==1.63.0` with its `chromium-headless-shell` build** under `/opt/ms-playwright` — the headless browser behind the apps' rendered checks (`POST /render`); tracked here under `mcps/custom/`; the browser build is the one the pinned package asks for, so a Playwright bump is the `requirements.in` edit, the lockfile regeneration and an image rebuild; independent of camoufox's locked 1.62.0, a different image with a different driver), `mcps/community/camoufox` (python:3.13; camoufox, playwright and `@playwright/mcp` pinned together; **sourced from the `OtoDock/community-mcps` repo + CI→GHCR**, the `mcps/community/` copy here is a gitignored runtime artifact), `m365-mcp` (the vendor's own image), etc. Only file-tools + camoufox are *ours* to track (file-tools in-repo; camoufox via the community repo); the rest are community MCPs wrapped for compat.
- **Custom MCP system requirements** — each MCP's `manifest.json` `system_requirements` (`node_min` + OS packages; no `python_min` — that comes from the package's upstream `requires-python`).
