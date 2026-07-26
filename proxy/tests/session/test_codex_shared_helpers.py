"""Tests for the shared Codex helpers used by both the local layer and
RemoteExecutionLayer. Ensures effort mapping, permission→sandbox mapping,
and auth.json construction produce identical output on both paths.
"""

from __future__ import annotations

import json

from core.layers.codex.helpers import (
    build_auth_json,
    build_auth_json_from_env,
    map_effort_to_codex,
    permission_to_sandbox,
)


# ---------------------------------------------------------------------------
# permission_to_sandbox
# ---------------------------------------------------------------------------

def test_permission_dont_ask_and_auto_map_to_danger_full_access():
    # auto (task mode) ≡ dontAsk → full access, nothing prompts.
    assert permission_to_sandbox("dontAsk") == "danger-full-access"
    assert permission_to_sandbox("auto") == "danger-full-access"


def test_permission_accept_edits_maps_to_workspace_write():
    # The exec-era "workspace-write-auto" is NOT a valid app-server SandboxMode;
    # acceptEdits is workspace-write (Codex has no separate auto-edit tier).
    assert permission_to_sandbox("acceptEdits") == "workspace-write"


def test_permission_plan_maps_to_read_only():
    assert permission_to_sandbox("plan") == "read-only"


def test_permission_default_maps_to_workspace_write():
    for mode in ("default", "", "unknown"):
        assert permission_to_sandbox(mode) == "workspace-write"


def test_full_fs_pairing_lifts_default_tier_only():
    # A full-filesystem pairing removes Codex's own workspace confinement for
    # the modes that would otherwise re-confine what the admin granted —
    # matching Claude's unsandboxed behaviour on the same pairing. Plan stays
    # read-only; the already-full tiers are unchanged.
    for mode in ("default", "", "unknown", "acceptEdits"):
        assert permission_to_sandbox(mode, allow_full_fs=True) == "danger-full-access"
        assert permission_to_sandbox(mode, allow_full_fs=False) == "workspace-write"
    assert permission_to_sandbox("plan", allow_full_fs=True) == "read-only"
    assert permission_to_sandbox("dontAsk", allow_full_fs=True) == "danger-full-access"
    assert permission_to_sandbox("auto", allow_full_fs=True) == "danger-full-access"


# ---------------------------------------------------------------------------
# map_effort_to_codex
# ---------------------------------------------------------------------------

def test_map_effort_low_medium_high_passthrough():
    assert map_effort_to_codex("low") == "low"
    assert map_effort_to_codex("medium") == "medium"
    assert map_effort_to_codex("high") == "high"


def test_map_effort_max_clamps_on_pre_56_models():
    # Pre-5.6 wire scales top at xhigh — "max" must never reach them.
    assert map_effort_to_codex("max") == "xhigh"
    assert map_effort_to_codex("max", "gpt-5.5") == "xhigh"
    assert map_effort_to_codex("max", "gpt-5.3-codex") == "xhigh"


def test_map_effort_max_unlocks_on_gpt56_family():
    # The GPT-5.6 family's wire scale includes "max" (0.144+).
    assert map_effort_to_codex("max", "gpt-5.6-sol") == "max"
    assert map_effort_to_codex("max", "gpt-5.6-terra") == "max"
    assert map_effort_to_codex("max", "gpt-5.6-luna") == "max"
    assert map_effort_to_codex("xhigh", "gpt-5.6-sol") == "xhigh"


def test_map_effort_ultra_unlocks_on_sol_and_terra_only():
    # "ultra" = max reasoning + Codex-native proactive multi-agent
    # orchestration; OpenAI's manifest supports it on Sol/Terra only.
    assert map_effort_to_codex("ultra", "gpt-5.6-sol") == "ultra"
    assert map_effort_to_codex("ultra", "gpt-5.6-terra") == "ultra"


def test_map_effort_ultra_clamps_to_model_ceiling_elsewhere():
    # Luna's wire scale tops at "max"; pre-5.6 families top at "xhigh"; a
    # stored "ultra" must degrade to the actual ceiling, never be rejected.
    assert map_effort_to_codex("ultra", "gpt-5.6-luna") == "max"
    assert map_effort_to_codex("ultra", "gpt-5.5") == "xhigh"
    assert map_effort_to_codex("ultra") == "xhigh"


def test_map_effort_max_never_implies_ultra():
    # Ultra is an explicit user choice — "max" on an ultra-capable model must
    # stay plain wire "max" (no surprise multi-agent orchestration).
    assert map_effort_to_codex("max", "gpt-5.6-sol") == "max"


def test_map_effort_xhigh_passthrough():
    assert map_effort_to_codex("xhigh") == "xhigh"


def test_map_effort_empty_or_unknown_returns_empty():
    assert map_effort_to_codex("") == ""
    assert map_effort_to_codex("wat") == ""


# ---------------------------------------------------------------------------
# build_auth_json
# ---------------------------------------------------------------------------

def test_build_auth_json_with_blob_preserves_ids_updates_access_token():
    blob = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "ID",
            "access_token": "OLD",
            "refresh_token": "RFR",
            "account_id": "ACC",
        },
        "last_refresh": "old-ts",
    }
    out = build_auth_json("NEW", auth_blob=blob)
    assert out["auth_mode"] == "chatgpt"
    assert out["tokens"]["id_token"] == "ID"
    assert out["tokens"]["access_token"] == "NEW"
    assert out["tokens"]["account_id"] == "ACC"
    assert out["last_refresh"] != "old-ts"  # refreshed
    # The session file must never carry a usable refresh token — the pool is
    # the sole rotator; a CLI-side rotation would revoke every other live
    # session's access token. The store keeps the real one.
    assert out["tokens"]["refresh_token"] == ""
    # The input blob is not mutated (it mirrors the stored credential).
    assert blob["tokens"]["refresh_token"] == "RFR"


def test_build_auth_json_no_blob_minimal_structure():
    out = build_auth_json("TOK")
    assert out["auth_mode"] == "chatgpt"
    assert out["tokens"]["access_token"] == "TOK"
    assert out["tokens"]["refresh_token"] == ""
    assert "id_token" in out["tokens"]
    assert "last_refresh" in out


# ---------------------------------------------------------------------------
# build_auth_json_from_env
# ---------------------------------------------------------------------------

def test_build_auth_json_from_env_pops_both_keys():
    env = {
        "OTHER": "x",
        "_CODEX_OAUTH_TOKEN": "NEW",
        "_CODEX_AUTH_BLOB": json.dumps({
            "auth_mode": "chatgpt",
            "tokens": {"id_token": "ID", "refresh_token": "RFR", "account_id": "A"},
        }),
    }
    out = build_auth_json_from_env(env)
    assert out is not None
    assert out["tokens"]["access_token"] == "NEW"
    assert out["tokens"]["id_token"] == "ID"
    # Both keys should be popped out of env
    assert "_CODEX_OAUTH_TOKEN" not in env
    assert "_CODEX_AUTH_BLOB" not in env
    assert env["OTHER"] == "x"


def test_build_auth_json_from_env_returns_none_when_no_token():
    env = {"FOO": "bar"}
    assert build_auth_json_from_env(env) is None
    assert env == {"FOO": "bar"}


def test_build_auth_json_from_env_handles_bad_json_blob():
    env = {"_CODEX_OAUTH_TOKEN": "TOK", "_CODEX_AUTH_BLOB": "{not json"}
    out = build_auth_json_from_env(env)
    # Falls back to minimal structure when blob is unparseable
    assert out is not None
    assert out["tokens"]["access_token"] == "TOK"


# ---------------------------------------------------------------------------
# config-side ultra gate (registry flag + per-layer emission)
# ---------------------------------------------------------------------------

def test_supports_ultra_flags_match_openai_manifest():
    # Sol/Terra carry ultra; Luna is capped at max by OpenAI's own manifest.
    # These flags must stay in sync with helpers._ULTRA_EFFORT_MODEL_PREFIXES.
    import config as app_config
    assert app_config.get_model_supports_ultra("gpt-5.6-sol") is True
    assert app_config.get_model_supports_ultra("gpt-5.6-terra") is True
    assert app_config.get_model_supports_ultra("gpt-5.6-luna") is False
    assert app_config.get_model_supports_ultra("claude-opus-5") is False
    assert app_config.get_model_supports_ultra("no-such-model") is False


def test_layer_models_emit_ultra_only_on_codex_layer():
    # Terra ships on codex-cli AND direct-llm — only the codex engine can run
    # the multi-agent orchestration, so only its list may advertise the flag
    # (the dashboard's effort picker keys on this).
    import config as app_config
    codex = {m["value"]: m for m in app_config.get_layer_models("codex-cli")}
    direct = {m["value"]: m for m in app_config.get_layer_models("direct-llm")}
    assert codex["gpt-5.6-sol"]["supports_ultra"] is True
    assert codex["gpt-5.6-terra"]["supports_ultra"] is True
    assert codex["gpt-5.6-luna"]["supports_ultra"] is False
    assert direct["gpt-5.6-terra"]["supports_ultra"] is False
