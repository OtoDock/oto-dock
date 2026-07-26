"""Shared Codex helpers used by both the local CodexCLIExecutionLayer and the
RemoteExecutionLayer when targeting a satellite.

Keeping effort mapping, sandbox-mode resolution, and auth.json construction
here ensures local-sandboxed and remote-unsandboxed sessions produce
identical inputs to the Codex CLI (the ``codex app-server`` daemon).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Permission mode → Codex sandbox mode
# ---------------------------------------------------------------------------

def permission_to_sandbox(permission_mode: str, allow_full_fs: bool = False) -> str:
    """Map a platform permission mode to the Codex app-server ``SandboxMode`` enum.

    Valid values are exactly ``"read-only" | "workspace-write" |
    "danger-full-access"`` (verified vs codex 0.120.0 — the exec-era
    ``"workspace-write-auto"`` is NOT a valid app-server SandboxMode).

    - ``dontAsk`` / ``auto`` → ``danger-full-access`` (no boundary, nothing prompts)
    - ``plan``               → ``read-only`` (planning — reads only)
    - everything else        → ``workspace-write`` (default / acceptEdits — Codex
      has no separate auto-edit tier; in-workspace edits run, escapes prompt)

    ``allow_full_fs`` is the machine pairing's full-filesystem grant
    (``security_context.target_allow_full_fs`` — remote targets only). It
    lifts default/acceptEdits to ``danger-full-access``: Claude runs
    unsandboxed on such pairings and Codex's own workspace-write boundary
    would re-confine what the admin already granted (OtoDock's permission
    floor + path policy still gate every call). ``plan`` stays read-only.
    """
    if permission_mode in ("dontAsk", "auto"):
        return "danger-full-access"
    if permission_mode == "plan":
        return "read-only"
    return "danger-full-access" if allow_full_fs else "workspace-write"


# The approval policy + structured turn/start sandboxPolicy live in
# ``codex_approvals.py`` (``approval_for_sandbox`` / ``build_sandbox_policy``) —
# both are stdlib-only and shared verbatim with the satellite via vendoring.


# ---------------------------------------------------------------------------
# Platform effort → Codex effort
# ---------------------------------------------------------------------------

# Codex's wire scale (0.144+): low/medium/high/xhigh, plus "max" and "ultra"
# from the GPT-5.6 family on. Platform "max" maps to wire "max" only on
# models that support it (gpt-5.6*) and clamps to "xhigh" everywhere else —
# pre-5.6 models top out at xhigh and must never be sent an effort they
# reject. An empty string means "don't pass the flag" (Codex's default).
#
# Platform "ultra" is an EXPLICIT user choice, never an alias for "max":
# wire "ultra" is not a bigger reasoning budget but Codex-native multi-agent
# orchestration (the model reasons at max AND proactively spawns parallel
# sub-agent workstreams — codex-rs sends the API "max" and flips
# MultiAgentMode::Proactive). It is offered per-model in the dashboard
# (supports_ultra — gpt-5.6 Sol/Terra only; OpenAI's own manifest caps Luna
# at "max") and clamps to the model's ceiling everywhere else, so a stored
# "ultra" can never reach a model/CLI that rejects it. It complements the
# platform's own delegation feature: delegate coordinates OtoDock sessions,
# ultra parallelizes WITHIN one Codex turn.
_EFFORT_TO_CODEX: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",  # pre-5.6 clamp; 5.6+ overridden below
}

# Model-id prefixes per unlocked wire value. Prefix checks keep this
# stdlib-only (no MODEL_REGISTRY import — the module is satellite-vendorable).
# NOTE: keep _ULTRA in sync with the ``supports_ultra`` flags in
# config.MODEL_REGISTRY (the dashboard gate) — this is the wire-level truth.
_MAX_EFFORT_MODEL_PREFIXES = ("gpt-5.6",)
_ULTRA_EFFORT_MODEL_PREFIXES = ("gpt-5.6-sol", "gpt-5.6-terra")


def map_effort_to_codex(effort: str, model: str = "") -> str:
    """Map platform effort level to Codex ``model_reasoning_effort`` value.

    ``model`` (when given) unlocks the wire values newer families support —
    platform "ultra" → wire "ultra" on gpt-5.6 Sol/Terra, platform "max" →
    wire "max" on gpt-5.6*; without it (or on older models) both clamp down
    the scale ("ultra" → the model's max tier, "max" → "xhigh"). Returns an
    empty string when the effort is unknown/empty so the caller can skip the
    ``-c model_reasoning_effort=...`` flag entirely.
    """
    m = model or ""
    if effort == "ultra":
        if m.startswith(_ULTRA_EFFORT_MODEL_PREFIXES):
            return "ultra"
        effort = "max"  # Luna / older families: clamp to their ceiling below
    if effort == "max" and m.startswith(_MAX_EFFORT_MODEL_PREFIXES):
        return "max"
    return _EFFORT_TO_CODEX.get(effort, "")


# ---------------------------------------------------------------------------
# Auth.json construction (ChatGPT OAuth)
# ---------------------------------------------------------------------------

def build_auth_json(
    token: str,
    *,
    auth_blob: dict | None = None,
) -> dict:
    """Build the ``auth.json`` payload Codex expects in ``CODEX_HOME``.

    If ``auth_blob`` is provided (the original JSON stored in the subscription),
    we preserve ``id_token`` and ``account_id`` and update ``access_token`` to
    the current ``token``.  Otherwise we emit a minimal structure (may fail if
    Codex requires id_token — the subscription pool should always provide an
    auth_blob for OAuth subscriptions).

    ``refresh_token`` is NEUTRALIZED (blank) in every session file: the pool is
    the platform's sole rotator — a CLI holding no refresh token physically
    cannot rotate (providers revoke older access tokens on rotation, which is
    what killed live sessions pre-2026-07-06). Codex cooperates natively: its
    guarded reload re-reads ``auth.json`` before refreshing and skips its own
    refresh when the on-disk token changed, and a blank refresh token just
    fails its refresh attempt while the fanned-out access token keeps working.
    The real refresh token only ever lives in the subscription store.
    """
    if auth_blob:
        auth_data = dict(auth_blob)
        tokens = dict(auth_data.get("tokens", {}))
        tokens["access_token"] = token
        tokens["refresh_token"] = ""
        auth_data["tokens"] = tokens
        auth_data["last_refresh"] = datetime.now(timezone.utc).isoformat()
        return auth_data

    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": token,
            "id_token": "",
            "refresh_token": "",
            "account_id": "",
        },
        "last_refresh": datetime.now(timezone.utc).isoformat(),
    }


def build_auth_json_from_env(env: dict) -> dict | None:
    """Extract the OAuth token + auth_blob from a session env and build auth.json.

    Reads ``_CODEX_OAUTH_TOKEN`` and ``_CODEX_AUTH_BLOB`` (set by the
    subscription pool) from the provided env dict. Returns None if no OAuth
    token is present (API-key-only subscription uses ``CODEX_API_KEY``
    instead).

    Mutates `env` to pop the two consumed keys so the caller can pass the
    remaining env to the subprocess/satellite payload without leaking the
    blob.
    """
    token = env.pop("_CODEX_OAUTH_TOKEN", None)
    blob_json = env.pop("_CODEX_AUTH_BLOB", None)
    if not token:
        return None
    auth_blob = None
    if blob_json:
        try:
            auth_blob = json.loads(blob_json)
        except (json.JSONDecodeError, ValueError):
            auth_blob = None
    return build_auth_json(token, auth_blob=auth_blob)
