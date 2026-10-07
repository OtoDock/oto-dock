"""Tests for the shared Codex helpers used by both the local layer and
RemoteExecutionLayer. Ensures effort mapping, permission→sandbox mapping,
and auth.json construction produce identical output on both paths.
"""

from __future__ import annotations


from core.layers.codex.helpers import (
    build_auth_json,
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


def test_map_effort_ultra_unlocks_on_sol_terra_and_astra():
    # "ultra" = max reasoning + Codex-native proactive multi-agent
    # orchestration; OpenAI's manifest supports it on Sol/Terra (and Astra).
    assert map_effort_to_codex("ultra", "gpt-5.6-sol") == "ultra"
    assert map_effort_to_codex("ultra", "gpt-5.6-terra") == "ultra"


def test_map_effort_gpt6_astra_unlocks_max_and_ultra():
    # GPT-6 Astra (Codex 0.153.x catalog): low…xhigh, max and ultra (Codex
    # delegates at xhigh for ultra — still the wire value "ultra" we send).
    assert map_effort_to_codex("max", "gpt-6-astra") == "max"
    assert map_effort_to_codex("ultra", "gpt-6-astra") == "ultra"
    assert map_effort_to_codex("xhigh", "gpt-6-astra") == "xhigh"
    assert map_effort_to_codex("high", "gpt-6-astra") == "high"
    # The unlock is exact: a future GPT-6 tier without "max" keeps the clamp.
    assert map_effort_to_codex("max", "gpt-6-mini") == "xhigh"
    assert map_effort_to_codex("ultra", "gpt-6-mini") == "xhigh"


def test_map_effort_gpt61_sol_follows_the_0_160_catalog():
    # Codex 0.160.0's bundled catalog: GPT-6.1 Sol carries low…max + ultra
    # (ultra delegates at xhigh). The unlock is the exact id: "gpt-6-sol" is
    # not a prefix of "gpt-6.1-sol", so without its own entry max and ultra
    # would clamp to xhigh.
    assert map_effort_to_codex("max", "gpt-6.1-sol") == "max"
    assert map_effort_to_codex("ultra", "gpt-6.1-sol") == "ultra"
    assert map_effort_to_codex("xhigh", "gpt-6.1-sol") == "xhigh"
    assert map_effort_to_codex("high", "gpt-6.1-sol") == "high"


def test_map_effort_gpt6_sol_and_luna_follow_the_0_156_catalog():
    # Codex 0.156.1's bundled catalog (verified 2026-09-24): GPT-6 Sol carries
    # low…max + ultra like 5.6 Sol; GPT-6 Luna low…max, no ultra, like 5.6 Luna.
    assert map_effort_to_codex("max", "gpt-6-sol") == "max"
    assert map_effort_to_codex("ultra", "gpt-6-sol") == "ultra"
    assert map_effort_to_codex("max", "gpt-6-luna") == "max"
    assert map_effort_to_codex("ultra", "gpt-6-luna") == "max"
    # The retired 5.6 ids keep their ceilings (a custom re-add, a session
    # whose row remaps at the next boot).
    assert map_effort_to_codex("ultra", "gpt-5.6-sol") == "ultra"
    assert map_effort_to_codex("max", "gpt-5.6-luna") == "max"


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


# ---------------------------------------------------------------------------
# config-side ultra gate (registry flag + per-layer emission)
# ---------------------------------------------------------------------------

def test_supports_ultra_flags_match_openai_manifest():
    # Sol/Terra and Astra carry ultra; Luna is capped at max by OpenAI's own
    # manifest. These flags must stay in sync with helpers._ULTRA_EFFORT_MODEL_PREFIXES.
    import config as app_config
    assert app_config.get_model_supports_ultra("gpt-6.1-sol") is True
    assert app_config.get_model_supports_ultra("gpt-5.6-terra") is True
    assert app_config.get_model_supports_ultra("gpt-6-astra") is True
    assert app_config.get_model_supports_ultra("gpt-6-luna") is False
    assert app_config.get_model_supports_ultra("claude-opus-5-5") is False
    # Registry-only gate: the retired 5.6 Sol and GPT-6 Sol have no row any
    # more (the wire clamp in helpers still knows them).
    assert app_config.get_model_supports_ultra("gpt-5.6-sol") is False
    assert app_config.get_model_supports_ultra("gpt-6-sol") is False
    assert app_config.get_model_supports_ultra("no-such-model") is False


def test_gpt6_astra_registry_entry():
    # A NEW model next to the 5.6 family (not a rename): codex-cli only until
    # the hosted relay prices it; listed right after the Sol row (GPT-6.1 Sol
    # the tier-2 default) so it reads first; the 272k window.
    import config as app_config
    entry = app_config.MODEL_REGISTRY["gpt-6-astra"]
    assert entry["provider"] == "openai"
    assert entry["layers"] == ["codex-cli"]
    assert entry["context_window"] == 272_000
    assert entry["pricing"] == (10.0, 50.0, 12.50, 1.00)
    assert entry["supports_xhigh"] and entry["supports_ultra"] and entry["supports_reasoning"]
    codex_ids = [m["value"] for m in app_config.get_layer_models("codex-cli")]
    assert codex_ids.index("gpt-6.1-sol") < codex_ids.index("gpt-6-astra") < codex_ids.index("gpt-5.6-terra")
    assert "gpt-6-sol" not in codex_ids
    assert "gpt-6-astra" not in {m["value"] for m in app_config.get_layer_models("direct-llm")}
    assert "gpt-6-astra" not in app_config.MODEL_SUCCESSORS
    assert app_config.get_model_supports_xhigh("gpt-6-astra") is True


def test_layer_models_emit_ultra_only_on_codex_layer():
    # Terra ships on codex-cli AND direct-llm — only the codex engine can run
    # the multi-agent orchestration, so only its list may advertise the flag
    # (the dashboard's effort picker keys on this). The flag is per MODEL
    # (Luna never gets it) ANDed with the engine's offers_ultra — read from
    # the layers' own descriptors, the lists the API actually serves.
    from core.layers.codex.layer import _CODEX_CAPABILITIES
    from core.layers.direct.layer import _DIRECT_CAPABILITIES
    codex = {m["value"]: m for m in _CODEX_CAPABILITIES.models}
    direct = {m["value"]: m for m in _DIRECT_CAPABILITIES.models}
    assert codex["gpt-6.1-sol"]["supports_ultra"] is True
    assert codex["gpt-5.6-terra"]["supports_ultra"] is True
    assert codex["gpt-6-luna"]["supports_ultra"] is False
    assert direct["gpt-5.6-terra"]["supports_ultra"] is False
    assert direct["gpt-6-luna"]["supports_ultra"] is False
