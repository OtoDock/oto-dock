"""Cross-layer model poison guard (ws.dashboard._model_allowed_for_path).

A chat-switch race can deliver the PREVIOUS chat's model to this chat: the
dashboard's selector fires model_change while the connection still points at
the old chat. A Claude model applied to a codex thread made every turn fail
with OpenAI's 400 ("The 'claude-fable-5' model is not supported when using
Codex with a ChatGPT account") until the user flipped the selector back —
the model was applied to the live session AND persisted onto the chat row.
The guard refuses any model the chat's execution layer doesn't serve.
"""

from unittest.mock import patch

from ws.dashboard import _model_allowed_for_path


def _models(*ids):
    return [{"model_id": i} for i in ids]


def test_layer_model_allowed():
    with patch("storage.subscription_store.list_models", return_value=_models("gpt-5.5", "gpt-5.3-codex")):
        assert _model_allowed_for_path("gpt-5.5", "codex-cli") is True


def test_foreign_model_refused():
    with patch("storage.subscription_store.list_models", return_value=_models("gpt-5.5", "gpt-5.3-codex")):
        assert _model_allowed_for_path("claude-fable-5", "codex-cli") is False


def test_claude_layer_refuses_codex_model():
    with patch("storage.subscription_store.list_models", return_value=_models("claude-fable-5", "claude-opus-5")):
        assert _model_allowed_for_path("gpt-5.5", "claude-code-cli") is False


def test_empty_model_passes():
    """Empty = agent/layer default — always applicable."""
    assert _model_allowed_for_path("", "codex-cli") is True


def test_unknown_path_passes():
    assert _model_allowed_for_path("gpt-5.5", "") is True


def test_lookup_failure_fails_open():
    """The guard protects against cross-layer poison; a registry hiccup must
    not block legitimate model changes."""
    with patch("storage.subscription_store.list_models", side_effect=RuntimeError("db down")):
        assert _model_allowed_for_path("gpt-5.5", "codex-cli") is True
