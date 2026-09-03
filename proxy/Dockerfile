# syntax=docker/dockerfile:1
#
# OtoDock proxy image — the containerised platform (deployment topology T2).
#
# Build from the REPO ROOT (it needs scripts/, mcps/, VERSIONS.md):
#     docker build -f proxy/Dockerfile -t otodock-proxy .
#
# This image is for T2 (Docker-Compose self-host) — the proxy runs as a
# container that drives sibling MCP containers through a docker-socket-proxy.
# The live bare-metal install (T1) does NOT use this image; it keeps running the
# proxy as a host process (systemd, proxy/setup.sh).
#
# Layout mirrors the bare-metal install root (/opt/otodock) so config.py's
# PLATFORM_ROOT-relative paths (mcps/, config.env, VERSIONS.md) resolve
# identically:
#     /opt/otodock/proxy   — app code + venv          (this build)
#     /opt/otodock/mcps     — MCP manifests (baked)     (this build)
#     /opt/otodock/agents   — user data                 (named volume, runtime)
#     /opt/otodock/config.env — secrets                 (bind/volume, runtime)
#     /opt/otodock/bin      — proxy-only ffmpeg/ffprobe (OUTSIDE the sandbox)
#
# RUNTIME MODEL: the proxy runs as the NON-ROOT `oto` user (uid 1000), matching
# the bare-metal install, so the bwrap'd agent is non-root with zero Linux
# capabilities. Rootless bwrap inside a container needs three Docker relaxations
# (set in the compose, not here):
#     security_opt: [seccomp=unconfined, apparmor=unconfined, systempaths=unconfined]
# for, respectively: user-namespace creation, mount-propagation, and mounting a
# fresh isolated /proc. NO cap_add (SYS_ADMIN dropped), NOT --privileged. Net a
# privilege REDUCTION vs a root-run proxy.

ARG PYTHON_IMAGE=python:3.13.14-slim-bookworm
ARG NODE_IMAGE=node:24.18.0-slim

# --- Node stage: source the pinned Node 24 (same bookworm/glibc base) ---
FROM ${NODE_IMAGE} AS node

# --- Dashboard build stage: compile the SPA (only dist/ is copied forward) ---
# The proxy serves the built dashboard from DASHBOARD_DIST when present (app.py).
# node_modules (~220 MB) stays in this throwaway stage; only the ~10 MB dist/
# lands in the final image. Uses relative API paths → no build-time env needed.
# (The ~18 MB wake-word wasm/model bundle is NOT here — it ships inside
# proxy/assets/kws via the `COPY proxy/` below, deliberately outside dist/ so
# `cap sync` never copies it into the APK.)
FROM ${NODE_IMAGE} AS dashboard
WORKDIR /build
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

# --- Final image ---
FROM ${PYTHON_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Freeze the bundled CLIs (Claude Code auto-update off — see VERSIONS.md).
    DISABLE_AUTOUPDATER=1 \
    # Proxy-side media tooling lives OUTSIDE the sandbox-mounted system dirs.
    OTO_FFMPEG_PATH=/opt/otodock/bin/ffmpeg \
    OTO_FFPROBE_PATH=/opt/otodock/bin/ffprobe

# Node 24 from the pinned image (binaries + global modules; recreate npm/npx).
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version

# Docker CLI + compose plugin — the proxy drives sibling MCP containers via
# `docker compose` over the socket-proxy (DOCKER_HOST=tcp://...). CLI only; no
# daemon. Pinned to Debian bookworm's Docker apt channel.
# (wget is a prerequisite of install-baseline-tools.sh's gh-repo step — present
# on bare-metal/satellite hosts, absent from the slim base, so add it here.)
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl wget gnupg \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
 && rm -rf /var/lib/apt/lists/* \
 && docker --version && docker compose version

# Baseline agent tooling — the single shared installer (git, gh, uv, pnpm,
# bubblewrap, poppler-utils, sqlite3, the pinned claude/codex CLIs, the git
# credential helper, best-effort passt/iproute2). Same script the satellite +
# bare-metal installs run, so the sandbox sees identical tools everywhere.
# Copied first (small, changes rarely) for layer caching. (VERSIONS.md is NOT
# needed here — the installer carries its own pinned CLI defaults — so it's
# copied later to keep doc edits from busting this expensive layer.)
COPY scripts/ /opt/otodock/scripts/
RUN bash /opt/otodock/scripts/install-baseline-tools.sh \
 && rm -rf /var/lib/apt/lists/*

# Proxy-side ffmpeg/ffprobe — installed then MOVED out of /usr/bin into
# /opt/otodock/bin so the agent bwrap (which RO-mounts /usr,/bin,/lib,/sbin)
# cannot reach the binary. The shared libs stay in /usr/lib (harmless — not
# runnable tools). The proxy finds them via OTO_FFMPEG_PATH/OTO_FFPROBE_PATH.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /opt/otodock/bin \
 && mv /usr/bin/ffmpeg /usr/bin/ffprobe /opt/otodock/bin/ \
 && /opt/otodock/bin/ffmpeg -version | head -1

# Proxy Python deps into a venv (parity with bare-metal; keeps the proxy's heavy
# deps OUT of the sandbox-visible system site-packages). requirements.txt copied
# first so the pip layer caches across app-code edits.
COPY proxy/requirements.txt /opt/otodock/proxy/requirements.txt
RUN python -m venv /opt/otodock/proxy/venv \
 && /opt/otodock/proxy/venv/bin/pip install --upgrade pip \
 && /opt/otodock/proxy/venv/bin/pip install -r /opt/otodock/proxy/requirements.txt

# The proxy also needs the lean `oto-audio` CORE (cloud STT/TTS for chat audio +
# phone config) — an editable local package that is NOT in requirements.txt.
# Install the base only: the heavy on-box `[localmodels]` ONNX stack is
# phone-only. Its own dir (changes rarely) for layer caching. Mirrors
# proxy/setup.sh on bare-metal — both install paths now pull this.
COPY audio/ /opt/otodock/audio/
RUN /opt/otodock/proxy/venv/bin/pip install -e /opt/otodock/audio

# Satellite source — the proxy reads its SATELLITE_VERSION at boot (ws/satellite.py)
# and serves it as the pairing tarball (api/remote/remote_machines.py). Source only;
# .dockerignore strips its venv/__pycache__. Should the tree ever be absent,
# the proxy degrades gracefully — see ws/satellite.py::satellite_source_available.
COPY satellite/ /opt/otodock/satellite/

# App code + MCP manifests + VERSIONS.md (baked). Volumes/binds overlay agents/
# + config.env at runtime. .dockerignore strips secrets, user data, venvs,
# sessions, logs. VERSIONS.md last so doc edits don't bust the layers above.
COPY VERSIONS.md /opt/otodock/VERSIONS.md
COPY proxy/ /opt/otodock/proxy/
COPY mcps/ /opt/otodock/mcps/

# Built dashboard SPA — served by the proxy from DASHBOARD_DIST
# (=/opt/otodock/dashboard/dist). Only the compiled output is copied (the build
# stage's node_modules is discarded).
COPY --from=dashboard /build/dist /opt/otodock/dashboard/dist

# Create the non-root runtime user + hand it ownership of /opt/otodock — done
# LAST so the expensive apt/install-baseline/venv layers above stay cached (the
# user is needed only at runtime). uid/gid 1000 is the cross-container file-
# ownership CONTRACT for the shared agents volume — file-tools' `mcp` user is
# also uid 1000 (username cosmetic; only the number matters).
#
# Chown everything EXCEPT the venv: the proxy executes the venv read-only (root-
# owned + world-readable is fine for uid 1000), and a `chown -R` over it would
# copy-up ~400 MB into a fat layer. Everything else (app code, mcps, dashboard,
# bin, …) is small and is owned by 1000 so the runtime can read/execute it AND
# create its writable children — proxy/sessions, recover-bin, media-cache,
# host-media-cache (lazy mkdir), mcps/*/.env + bundled venvs. The agents volume +
# config.env are chowned at boot by the compose init sidecar (volumes start root-
# owned). Excluding venv also avoids __pycache__ EACCES noise on the app code.
RUN groupadd -g 1000 oto && useradd -u 1000 -g 1000 -m -s /bin/bash oto \
 && chown 1000:1000 /opt/otodock \
 && find /opt/otodock -mindepth 1 -maxdepth 1 ! -name proxy \
      -exec chown -R 1000:1000 {} + \
 && find /opt/otodock/proxy -mindepth 1 -maxdepth 1 ! -name venv \
      -exec chown -R 1000:1000 {} + \
 && chown 1000:1000 /opt/otodock/proxy

WORKDIR /opt/otodock/proxy
EXPOSE 8400

# Liveness — the proxy serves /health once uvicorn + DB are up.
HEALTHCHECK --interval=10s --timeout=5s --start-period=40s --retries=6 \
  CMD curl -fsS http://localhost:8400/health || exit 1

# Drop to the non-root runtime user for the proxy process (and thus the bwrap'd
# agents, which inherit this uid → uid 1000, capability-less).
USER 1000:1000

CMD ["/opt/otodock/proxy/venv/bin/python", "app.py"]
