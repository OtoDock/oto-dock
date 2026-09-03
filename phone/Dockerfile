# syntax=docker/dockerfile:1
#
# OtoDock phone (telephony) daemon image — the otodock-phone service in the T2
# self-host stack (the docker-compose.phone.yml overlay). SEPARATE from the proxy image. It runs
# the AsyncIO AudioSocket server (Asterisk/FreePBX media → VAD→STT→LLM→TTS) plus
# the outbound-call HTTP API, and connects OUT to the proxy's /ws/phone-management
# to receive ALL runtime config (routes, providers, credentials) from the proxy DB
# — so it needs only PROXY_URL + the shared secrets (PROXY_API_KEY/PHONE_API_SECRET
# from config.env). Reached by the proxy at http://otodock-phone:9093.
#
# Build from the REPO ROOT (it needs audio/ + phone/):
#     docker build -f phone/Dockerfile -t otodock-phone .
#
# Telephony takes calls only once a PBX / SIP trunk or Twilio is configured
# in the dashboard (Setup → Phone) — until then the service idles fail-closed.

ARG PYTHON_IMAGE=python:3.13.14-slim-bookworm
FROM ${PYTHON_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# onnxruntime (Smart Turn + Silero VAD) needs libgomp at runtime; curl for the
# healthcheck. silero-vad-lite ships a manylinux cp312 wheel — no build tools.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# Phone Python deps into a venv (parity with bare-metal phone/setup.sh).
# requirements.txt first so the heavy pip layer (onnxruntime/transformers) caches
# across source edits.
COPY phone/requirements.txt /opt/otodock/phone/requirements.txt
RUN python -m venv /opt/otodock/phone/venv \
 && /opt/otodock/phone/venv/bin/pip install --upgrade pip \
 && /opt/otodock/phone/venv/bin/pip install -r /opt/otodock/phone/requirements.txt

# The `audio` package WITH the on-box [localmodels] stack (Silero VAD + Smart Turn
# ONNX/transformers) — an editable local package whose deps are already pinned in
# requirements.txt above but which is itself installed here (mirrors how the proxy
# installs the lean core). Includes the VENDORED models/ (no runtime HF download).
# Its own dir for layer caching.
COPY audio/ /opt/otodock/audio/
RUN /opt/otodock/phone/venv/bin/pip install -e "/opt/otodock/audio[localmodels]"

# Phone daemon source. .dockerignore strips its venv/__pycache__/logs.
COPY phone/ /opt/otodock/phone/

# Non-root runtime user (uid 1000 — the same cross-container ownership contract as
# the proxy + file-tools). The daemon binds 9092/9093 (>1024, no privilege) and
# reads the shared config.env (bind-mounted) for PROXY_API_KEY + PHONE_API_SECRET.
# Chown everything EXCEPT the venv (a chown -R over onnxruntime/transformers would
# copy-up hundreds of MB into a fat layer; the venv runs fine root-owned + world-
# readable). The phone dir itself is owned by 1000 so the daemon can write its log.
RUN groupadd -g 1000 oto && useradd -u 1000 -g 1000 -m -s /bin/bash oto \
 && chown 1000:1000 /opt/otodock/phone \
 && find /opt/otodock/phone -mindepth 1 -maxdepth 1 ! -name venv \
      -exec chown -R 1000:1000 {} + \
 && chown -R 1000:1000 /opt/otodock/audio

WORKDIR /opt/otodock/phone
# AudioSocket media (PBX → daemon) + the register/outbound-call HTTP API.
EXPOSE 9092 9093

# Liveness — the HTTP API serves /health (open) once the daemon has its config.
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=4 \
  CMD curl -fsS http://localhost:9093/health || exit 1

USER 1000:1000

CMD ["/opt/otodock/phone/venv/bin/python", "main.py"]
