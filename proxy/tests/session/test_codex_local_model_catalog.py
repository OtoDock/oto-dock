"""The per-session Codex model catalog for a local (Ollama) model — the entry
that makes Codex defer its MCP tools (``supports_search_tool``) while staying
the built-in fallback entry in every other respect, the instructions text
included; the provider's model rows ride the subscription env.
"""

import hashlib
import json

from core.layers.codex import local_model_catalog as cat
from core.layers.codex.helpers import LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS

# codex-rs/models-manager/prompt.md at rust-v0.149.1 == rust-v0.153.4 ==
# rust-v0.156.1 (the pin since 2026-09-24), and the
# ``instructions`` field of a fallback (no-catalog) request captured on 0.149.1
# (20,751 chars). A Codex bump that refreshes the data file updates these two
# constants on purpose — see VERSIONS.md "To bump a CLI version".
_INSTRUCTIONS_CHARS = 20_751
_INSTRUCTIONS_SHA12 = "ca8f958932a9"


def _sha12(text: str) -> str:
    return hashlib.sha256(
        json.dumps(text, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:12]


def test_base_instructions_are_the_pinned_codex_prompt():
    text = cat.base_instructions()
    assert len(text) == _INSTRUCTIONS_CHARS
    assert _sha12(text) == _INSTRUCTIONS_SHA12
    assert text.startswith("You are a coding agent running in the Codex CLI")


def test_entry_mirrors_the_fallback_and_turns_on_deferral():
    e = cat.local_model_catalog_entry("qwen3.6-35b-a3b", 131_072)
    assert e["slug"] == e["display_name"] == "qwen3.6-35b-a3b"
    # What the deferral needs.
    assert e["supports_search_tool"] is True
    assert e["tool_mode"] == "direct"
    assert e["use_responses_lite"] is False
    # The configured window; the fallback's when the row has none.
    assert e["context_window"] == e["max_context_window"] == 131_072
    e0 = cat.local_model_catalog_entry("m", 0)
    assert e0["context_window"] == e0["max_context_window"] == cat.FALLBACK_CONTEXT_WINDOW == 272_000
    # The fallback's shape (model_info_from_slug, the same on 0.149.1, 0.153.4
    # and the pinned 0.156.1 — its four new fields all default): no
    # reasoning levels, the config-following shell type
    # ("default", an alias of unified_exec from 0.153 on), no apply_patch
    # tool, bytes truncation, text web search, both input modalities, 95%.
    assert e["supported_reasoning_levels"] == []
    assert e["default_reasoning_level"] is None
    assert e["shell_type"] == "default"
    assert e["apply_patch_tool_type"] is None
    assert e["truncation_policy"] == {"mode": "bytes", "limit": 10_000}
    assert e["web_search_tool_type"] == "text"
    assert e["input_modalities"] == ["text", "image"]
    assert e["effective_context_window_percent"] == 95
    assert e["include_apps_usage_instructions"] is False
    assert e["multi_agent_version"] is None
    assert "multi_agent_reasoning_effort" not in e  # not a fallback field (None on 0.153)
    # The instructions ride the legacy top-level field Codex promotes into
    # model_messages.instructions_template — without it Codex sends EMPTY
    # instructions for the session.
    assert e["base_instructions"] == cat.base_instructions()
    assert e["model_messages"] is None
    # Listed: an unlisted single-entry catalog can resolve to no model at all
    # when the app-server falls back to "the first available model".
    assert e["visibility"] == "list"


def test_catalog_lists_the_session_model_first_then_the_providers_rows():
    rows = [("qwen3.5-9b", 32_768), ("qwen3.6-35b-a3b", 131_072), ("", 5), ("qwen3.5-9b", 1)]
    doc = json.loads(cat.local_model_catalog_json("qwen3.6-35b-a3b", "ollama", rows))
    assert [m["slug"] for m in doc["models"]] == ["qwen3.6-35b-a3b", "qwen3.5-9b"]
    assert doc["models"][0]["context_window"] == 131_072
    assert doc["models"][1]["context_window"] == 32_768   # first row wins
    # A session model without a row still leads, with the fallback window.
    doc = json.loads(cat.local_model_catalog_json("other", "Ollama", rows))
    assert [m["slug"] for m in doc["models"]] == ["other", "qwen3.5-9b", "qwen3.6-35b-a3b"]
    assert doc["models"][0]["context_window"] == 272_000
    assert json.loads(cat.local_model_catalog_json("m", "ollama"))["models"][0]["slug"] == "m"


def test_catalog_json_only_for_ollama():
    # openai_compatible (vLLM / LiteLLM / llama.cpp / LM Studio behind one URL)
    # keeps today's namespace round trip: no catalog, no tool_search.
    assert cat.local_model_catalog_json("qwen3.6-35b-a3b", "openai_compatible") == ""
    assert cat.local_model_catalog_json("qwen3.6-35b-a3b", "") == ""
    assert cat.local_model_catalog_json("", "ollama") == ""


def test_catalog_toml_line_escapes_windows_paths():
    assert cat.catalog_toml_line("/workspace/.codex/models.json") == (
        'model_catalog_json = "/workspace/.codex/models.json"'
    )
    assert cat.catalog_toml_line(r"C:\Users\d\.codex\models.json") == (
        'model_catalog_json = "C:\\\\Users\\\\d\\\\.codex\\\\models.json"'
    )


def test_model_rows_round_trip_through_the_subscription_env(monkeypatch):
    from storage.billing import subscription_store

    monkeypatch.setattr(subscription_store, "list_models", lambda layer=None: [
        {"model_id": "qwen3.6-35b-a3b", "provider": "ollama", "context_window": 131_072},
        {"model_id": "qwen3.5-9b", "provider": "ollama", "context_window": None},
        {"model_id": "vllm-model", "provider": "openai_compatible", "context_window": 8192},
        {"model_id": "", "provider": "ollama", "context_window": 1},
    ])
    text = cat.local_model_rows_json("ollama")
    assert cat.parse_local_model_rows(text) == [("qwen3.6-35b-a3b", 131_072), ("qwen3.5-9b", 0)]
    assert cat.parse_local_model_rows(cat.local_model_rows_json("openai_compatible")) == [("vllm-model", 8192)]
    # Fail-soft both ways: a store error → no rows; garbage → no rows.
    def _boom(layer=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(subscription_store, "list_models", _boom)
    assert cat.local_model_rows_json("ollama") == "[]"
    assert cat.parse_local_model_rows("") == []
    assert cat.parse_local_model_rows("not json") == []
    assert cat.parse_local_model_rows('[["a"], ["b", 2], 3]') == [("b", 2)]


def test_idle_timeout_is_a_long_first_token_budget():
    # Codex's default (300000) killed the desktop's first turn at 5m01s while
    # the model was still prefilling; 30 minutes covers a CPU-bound prefill of
    # the whole context and Stop still interrupts the turn.
    assert LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS == 1_800_000
