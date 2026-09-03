"""Satellite child-env curation.

The satellite spawns the agent CLI (claude / codex / MCP warm-up) as a child of
the satellite daemon, which runs as the operator's OS user. The three spawn sites
seeded the child env from the operator's ENTIRE ``os.environ`` (``{**os.environ}``
/ ``dict(os.environ)``) and then overlaid the platform-shipped ``config["env"]``.
Net: the agent inherited the operator's personal shell secrets (``GH_TOKEN`` from
``gh auth``, ``*_API_KEY`` exports, ``AWS_SECRET_ACCESS_KEY``, …) — readable by a
prompt-injected agent via ``printenv`` / ``$env:``.

This is **defense-in-depth, NOT a boundary**: the agent runs as the operator's uid
with no bwrap on satellites, so it can read those same secrets from the operator's
shell rc files / ``/proc/<pid>/environ`` regardless. The curation removes the
*casual* ``printenv`` exposure (and the prompt-injection "paste your env" vector)
without pretending to be a sandbox.

Design = DENYLIST (keep everything except secret-VALUE name patterns). A denylist
(vs the proxy's allowlist in ``env_builder``) minimizes cross-platform breakage:
system / runtime / GUI vars (``SYSTEMROOT``, ``PATHEXT``, ``APPDATA``, ``DISPLAY``,
``WAYLAND_DISPLAY``, ``XAUTHORITY``, ``DBUS_SESSION_BUS_ADDRESS``, ``PATH``, …) are
kept so native CLIs + GUI device-MCPs (computer / browser) keep working.

The platform-injected ``config["env"]`` (the curated session env carrying the
intentional ``GH_TOKEN`` / ``PROXY_API_KEY`` / ``ANTHROPIC_API_KEY``) is
overlaid by the caller AFTER this runs, so those survive — only the operator's
*ambient* secrets are dropped.
"""

import os

# Secret-VALUE name patterns (case-insensitive). A var whose NAME contains any of
# these substrings, or ends with any of the suffixes, is assumed to hold a secret
# value and is dropped. Deliberately NOT included:
#   * broad vendor PREFIXES (AWS_/AZURE_/OPENAI_) — they also match non-secret
#     CONFIG (AWS_PROFILE/AWS_REGION/AWS_CONFIG_FILE/OPENAI_BASE_URL), breaking the
#     agent driving host CLIs; the substring patterns already catch the real
#     secrets (AWS_SECRET_ACCESS_KEY, etc.).
#   * "CREDENTIAL" — would strip GOOGLE_APPLICATION_CREDENTIALS, which is a PATH to
#     a key file (the operator's uid reads it regardless → stripping = zero
#     protection + breaks Google ADC).
_SECRET_SUBSTRINGS = (
    "SECRET", "TOKEN", "PASSWORD", "PASSWD", "APIKEY", "API_KEY",
    "ACCESS_KEY", "PRIVATE_KEY",
)
# Broadest pattern — catches SSH_KEY / HOST_KEY / LICENSE_KEY / DEPLOY_KEY as well
# as FOO_API_KEY. Kept (real secrets dominate) but operator-overridable via
# OTO_ENV_KEEP for the occasional false positive (e.g. SSH_KEY pointing to a path).
_SECRET_SUFFIXES = ("_KEY",)


def _operator_keep_set() -> set:
    """Operator-extensible allow-back set (empty by default). ``OTO_ENV_KEEP`` is a
    comma-separated list of var NAMES to keep even if they match a secret pattern —
    an escape hatch for false positives on the broad ``_KEY`` suffix."""
    raw = os.environ.get("OTO_ENV_KEEP", "")
    return {n.strip().upper() for n in raw.split(",") if n.strip()}


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    if any(sub in upper for sub in _SECRET_SUBSTRINGS):
        return True
    if any(upper.endswith(suf) for suf in _SECRET_SUFFIXES):
        return True
    return False


def curate_satellite_env(environ=None) -> dict:
    """Copy ``environ`` (default ``os.environ``) MINUS the operator's ambient
    secret-VALUE vars. The caller overlays the platform ``config["env"]`` AFTER
    this, so intentional platform secrets are unaffected.

    Returns a plain ``dict[str, str]`` (a fresh copy — never mutates the source).
    """
    src = os.environ if environ is None else environ
    keep = _operator_keep_set()
    out: dict = {}
    for name, value in src.items():
        if name.upper() in keep:
            out[name] = value
            continue
        if _is_secret_name(name):
            continue
        out[name] = value
    return out
