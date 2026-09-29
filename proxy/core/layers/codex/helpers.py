"""Shared Codex helpers used by both the local CodexCLIExecutionLayer and the
RemoteExecutionLayer when targeting a satellite.

Keeping effort mapping, sandbox-mode resolution, and auth.json construction
here ensures local-sandboxed and remote-unsandboxed sessions produce
identical inputs to the Codex CLI (the ``codex app-server`` daemon).
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.session import session_kind


# Child-env variable that carries a key-protected LOCAL endpoint's bearer into
# the Codex process — the custom provider's ``env_key`` names it. Never
# ``CODEX_API_KEY`` (that switches Codex into API-key auth against its built-in
# OpenAI provider). Shared by the local layer (sandbox env + config.toml) and the
# remote start payload (``local_model_provider.env_key`` + the satellite env).
LOCAL_ENDPOINT_KEY_ENV = "OTO_LOCAL_API_KEY"

# ``[model_providers.oto_local] stream_idle_timeout_ms`` for every LOCAL endpoint
# (both writers; the satellite receives it as ``local_model_provider.
# stream_idle_timeout_ms``). Codex kills a request that shows no event for this
# long — default 300000, counted while waiting for the FIRST token too — and a
# local model prefills at CPU speed: the desktop's 47k-token first prompt took
# three minutes at 280 tok/s and died at 5m01s before its first token. Thirty
# minutes covers a CPU-bound prefill of a whole context; Stop still interrupts.
LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS = 1_800_000

# AGENTS.md section appended ONLY for Codex sessions on a LOCAL endpoint. Codex
# sends every MCP server's tools to the model as one Responses-API ``namespace``
# tool and no ``tool_search`` on a custom provider (identical on 0.149.1 and
# 0.153.4, request captures 2026-09-07; nothing in Codex's config flattens it).
# Whether the model sees its MCP tools is the SERVER's business: Ollama 0.34+,
# vLLM 0.25+ and LiteLLM 1.98+ unpack the groups and restore the namespace on
# the model's calls; llama.cpp skips non-function tools by design and LM Studio
# rejects the request. A model that sees no tools goes looking for them in the
# shell (2026-09-06: "the HA tools are not in my function list"; 2026-09-07:
# placeholder bash commands), so the note tells it the truth for its server —
# picked by the subscription's provider (``local_provider_note``). See CODEX.md
# "Local models and MCP tools".
_NOTE_HEADER = "# Tools on this model provider"

_NEVER_WORK_AROUND = (
    "Never work around a missing tool: do not run an MCP server's command from "
    "the shell, do not guess at tool names, do not search the filesystem or read "
    "configuration files for credentials, and do not call the platform's HTTP "
    "API yourself."
)

# The ``openai_compatible`` provider: the platform cannot tell which server sits
# behind the URL, so the note names the ones that serve the tools and the ones
# that do not.
LOCAL_PROVIDER_TOOL_NOTE = f"""{_NOTE_HEADER}

This session runs on a local model server. Codex hands your MCP servers' tools
to the model in a grouped form that only some servers understand: Ollama 0.34
and newer, vLLM 0.25 and newer and LiteLLM 1.98 and newer unpack it, while
llama.cpp and LM Studio drop it. So the servers listed under "Available Tools
(MCPs)" may not be callable here: only the functions present in your function
list are available. If a task needs an MCP tool that is not in your function
list, stop and say so plainly, then suggest one of those servers, the Direct
LLM engine, or an OpenAI model. {_NEVER_WORK_AROUND}"""

# The ``ollama`` provider: the session carries a model catalog entry that makes
# Codex defer the MCP tools behind its ``tool_search`` (local_model_catalog),
# so the function list starts small and a search loads the matches; a missing
# tool_search or an unsupported call means the server predates 0.34.
OLLAMA_PROVIDER_TOOL_NOTE = f"""{_NOTE_HEADER}

This session runs on an Ollama server. Your MCP servers' tools load on demand:
the function list starts with the built-in tools and tool_search, and a search
brings in the tools that match. So the servers listed under "Available Tools
(MCPs)" are callable here after a search. Search before you say a tool is
missing. If tool_search is absent from your function list, or a call to a
found tool fails as unsupported, the Ollama server is older than 0.34: stop
and say so plainly, then suggest updating Ollama or running this agent on the
Direct LLM engine. {_NEVER_WORK_AROUND}"""


def local_provider_note(provider: str = "") -> str:
    """The note for a local endpoint's provider (``ollama`` or anything else)."""
    return OLLAMA_PROVIDER_TOOL_NOTE if (provider or "").lower() == "ollama" else LOCAL_PROVIDER_TOOL_NOTE


def with_local_provider_note(system_prompt: str, provider: str = "") -> str:
    """The AGENTS.md text for a local-endpoint Codex session: the system prompt
    followed by the provider's note (appended once)."""
    prompt = system_prompt or ""
    if _NOTE_HEADER in prompt:
        return prompt
    head = prompt.rstrip() + "\n\n" if prompt.strip() else ""
    return head + local_provider_note(provider) + "\n"


# ---------------------------------------------------------------------------
# Permission mode → Codex sandbox mode
# ---------------------------------------------------------------------------

def codex_hooks_floor(client_type: str, interactive: bool = False) -> bool:
    """Whether a Codex app-server session runs ``permission_gate.py`` as its
    PreToolUse hook (the command-level permission FLOOR).

    True for every kind ``core/session/session_kind.py`` declares unattended
    (task / phone / meeting / trigger / internal / app — ``session_kind.attended()``
    answers; an unrecorded kind counts as attended): nobody answers an
    approval, so under ``approvalPolicy: never``
    the JSON-RPC approval bridge never fires and the hook is the only gate);
    False for attended dashboard chats (the bridge alone gates — both would
    double-gate) and for the interactive TUI, which trusts its hook by CLI
    flag instead. ONE rule for the local layer (``layer.py``) and the remote
    start payload (``codex_hooks_floor`` field, satellite >= 0.5.118) so a
    session is floored the same way wherever it runs.
    """
    return not session_kind.attended(client_type) and not interactive


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
    if permission_mode in ("plan", "judge"):
        # ``judge`` (CHECKS.md): the same read-only sandbox, without the
        # plan collaboration mode — the session answers, it does not plan.
        return "read-only"
    return "danger-full-access" if allow_full_fs else "workspace-write"


# The approval policy + structured turn/start sandboxPolicy live in
# ``codex_approvals.py`` (``approval_for_sandbox`` / ``build_sandbox_policy``) —
# both are stdlib-only and shared verbatim with the satellite via vendoring.


# ---------------------------------------------------------------------------
# Platform effort → Codex effort
# ---------------------------------------------------------------------------

# Codex's wire scale (0.144+): low/medium/high/xhigh, plus "max" and "ultra"
# from the GPT-5.6 family on (GPT-6 Astra too). Platform "max" maps to wire
# "max" only on models that support it (gpt-5.6*, gpt-6*) and clamps to
# "xhigh" everywhere else — pre-5.6 models top out at xhigh and must never be
# sent an effort they reject. An empty string means "don't pass the flag"
# (Codex's default).
#
# Platform "ultra" is an EXPLICIT user choice, never an alias for "max":
# wire "ultra" is not a bigger reasoning budget but Codex-native multi-agent
# orchestration (the model proactively spawns parallel sub-agent workstreams;
# codex-rs sends the API the model's multi-agent effort — "max" on Sol/Terra,
# "xhigh" on Astra — and flips MultiAgentMode::Proactive). It is offered
# per-model in the dashboard (supports_ultra — GPT-6 Sol, gpt-5.6 Terra and
# gpt-6-astra; OpenAI's own manifest caps Luna at "max") and clamps to the
# model's ceiling everywhere else, so a stored "ultra" can never reach a
# model/CLI that rejects it. It complements the platform's own delegation
# feature: delegate coordinates OtoDock sessions, ultra parallelizes WITHIN
# one Codex turn.
_EFFORT_TO_CODEX: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",  # pre-5.6 clamp; 5.6+ / 6 overridden below
}

# Model-id prefixes per unlocked wire value. Prefix checks keep this
# stdlib-only (no MODEL_REGISTRY import — the module is satellite-vendorable).
# NOTE: keep _ULTRA in sync with the ``supports_ultra`` flags in
# config.MODEL_REGISTRY (the dashboard gate) — this is the wire-level truth.
# Exact GPT-6 ids rather than "gpt-6": a future GPT-6 tier without "max"
# must not inherit the unlock. The retired 5.6 Sol / Luna prefixes stay: a
# custom re-add, or a live session whose row remaps at the next boot, keeps
# its ceiling (Codex 0.156.1 catalog: GPT-6 Sol low…max + ultra, GPT-6 Luna
# low…max).
_MAX_EFFORT_MODEL_PREFIXES = ("gpt-5.6", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna")
_ULTRA_EFFORT_MODEL_PREFIXES = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra", "gpt-6-sol")


def map_effort_to_codex(effort: str, model: str = "") -> str:
    """Map platform effort level to Codex ``model_reasoning_effort`` value.

    ``model`` (when given) unlocks the wire values newer families support —
    platform "ultra" → wire "ultra" on gpt-5.6 Sol/Terra and gpt-6-astra,
    platform "max" → wire "max" on gpt-5.6* and gpt-6*; without it (or on
    older models) both clamp down the scale ("ultra" → the model's max tier,
    "max" → "xhigh"). Returns an empty string when the effort is
    unknown/empty so the caller can skip the ``-c model_reasoning_effort=...``
    flag entirely.
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

def build_auth_json(token: str, *, auth_blob: dict) -> dict:
    """Build the ``auth.json`` payload Codex expects in ``CODEX_HOME``.

    ``auth_blob`` is the login's own auth.json as the device-auth flow stored
    it in the subscription: its ``id_token`` and ``account_id`` are preserved
    (Codex needs them next to the access token) and ``access_token`` is
    updated to the current ``token``. Without a blob there is no file to
    build — ``CodexCLIExecutionLayer.credential_file_payload`` answers None.

    ``refresh_token`` is NEUTRALIZED (blank) in every session file: the pool is
    the platform's sole rotator — a CLI holding no refresh token physically
    cannot rotate (providers revoke older access tokens on rotation, which is
    what killed live sessions pre-2026-07-06). Codex cooperates natively: its
    guarded reload re-reads ``auth.json`` before refreshing and skips its own
    refresh when the on-disk token changed, and a blank refresh token just
    fails its refresh attempt while the fanned-out access token keeps working.
    The real refresh token only ever lives in the subscription store.
    """
    auth_data = dict(auth_blob)
    tokens = dict(auth_data.get("tokens", {}))
    tokens["access_token"] = token
    tokens["refresh_token"] = ""
    auth_data["tokens"] = tokens
    auth_data["last_refresh"] = datetime.now(timezone.utc).isoformat()
    return auth_data
