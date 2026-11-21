"""CodexCLIExecutionLayer: the Codex engine's facts.

What a session of this engine starts with on a machine (the persistent
``codex app-server`` JSON-RPC daemon, or the TUI), its files, credentials
and usage windows. The platform holds no session of it: the remote layer
does, through this engine's adapter.

Event translation converts the daemon's JSON-RPC notifications (item/started /
item/completed carrying a ThreadItem, item/agentMessage/delta, turn/completed,
…) into the platform's CommonEvent stream so the ChatStreamPump processes them
identically to CLI or Direct LLM events.
"""

import json
import logging

import config as app_config
from core.execution_layer import (
    AUTO_REVIEW_MODE,
    LayerCapabilities,
    OAuthRefresh,
    SubscriptionHandle,
    Windows,
    WindowSpec,
)
from core.layers.codex import oauth as codex_oauth, usage as codex_usage
from core.layers.codex.capabilities import _CODEX_CAPABILITIES

logger = logging.getLogger("codex-layer")


from core.layers.codex.helpers import (
    LOCAL_ENDPOINT_KEY_ENV as _LOCAL_ENDPOINT_KEY_ENV,  # noqa: F401
)
from core.layers.codex.mcp_toml import (  # noqa: F401
    _TOML_ENV_LINE_RE,
    _TOML_ENV_LINES_RE,
    _TOML_MCP_SECTION_RE,
    _inject_enabled_tools_toml,
    _inject_fetch_tokens_toml,
    _inject_proxy_vars_toml,
    _inject_session_id_toml,
    _inject_session_jwt_toml,
    with_tool_approvals,
)
from core.layers.codex.resume import _CodexResume
from core.layers.codex.spawn import (  # noqa: F401
    _CODEX_TOOLS_TABLE,
    _Prepared,
    _resume_provider_changed,
    _tui_env,
    _tui_flags,
)
from core.layers.codex.translator import CodexEventTranslator  # noqa: F401

#: The mode word Codex's PreToolUse hook reports for a turn under an asking
#: approval policy (on-request; ``never`` reports ``bypassPermissions``).
CODEX_ON_REQUEST_WORD = "default"


# ---------------------------------------------------------------------------
# CodexCLIExecutionLayer
# ---------------------------------------------------------------------------


class CodexCLIExecutionLayer(_CodexResume):
    """The Codex CLI as an engine: its facts, and the spawn plan a machine
    starts a session of it from."""

    @property
    def capabilities(self) -> LayerCapabilities:
        return _CODEX_CAPABILITIES

    def transcript_tailer(self):
        """The Ratatui TUI writes a rollout JSONL under
        ``<CODEX_HOME>/sessions/``; ``codex_rollout_tailer`` persists it."""
        from core.session import codex_rollout_tailer
        return codex_rollout_tailer

    _remote_adapter = None

    def remote_adapter(self):
        """This engine on a satellite — ``core/layers/codex/remote.py``
        (imported here, not at module level: it reads this module's class)."""
        if self._remote_adapter is None:
            from core.layers.codex.remote import CodexRemoteAdapter
            self._remote_adapter = CodexRemoteAdapter(self)
        return self._remote_adapter

    def engine_reviews(self, session_id: str, live_mode: str) -> bool:
        """Codex's reviewer judges what the hook leaves undecided when Codex
        reports its on-request policy with the call (``default``; a turn under
        ``never`` reports ``bypassPermissions``) and the session's mode is
        Auto: its turns then start with ``auto_review``."""
        if live_mode != CODEX_ON_REQUEST_WORD:
            return False
        from core.session.session_manager import _get_remote_layer
        remote = _get_remote_layer()
        info = remote._sessions.get(session_id) if remote is not None else None
        return bool(info is not None and info.mode == AUTO_REVIEW_MODE)

    def canonical_tool_name(self, session_id: str, native: str) -> str:
        """Codex's native names → the canonical set (``tool_names.py``), and
        its hook's MCP tool names restored to the manifest's server name:
        the PreToolUse hook sanitises the server key
        (``mcp__meetings_mcp__direct_to`` for ``meetings-mcp``) while every
        permission rule, tier lookup and meeting check keys on the manifest's
        ``server_name`` (a mangled name fell to the default tier and parked a
        meeting on a permission card, 2026-09-09). The name is restored
        against every manifest. Unchanged when the server part already names
        a manifest or nothing matches."""
        from core.layers.codex import tool_names
        if not native.startswith("mcp__"):
            return tool_names.canonical(native)
        parts = native.split("__", 2)
        if len(parts) < 3 or not parts[1] or not parts[2]:
            return native
        server, tool = parts[1], parts[2]
        from services.mcp import mcp_registry
        declared = [(m.server_name or m.name) for m in mcp_registry.get_all_manifests().values()]
        if server in declared:
            return native
        for name in declared:
            if tool_names.sanitized_server_name(name) == server:
                return f"mcp__{name}__{tool}"
        return native


    def pinned_cli_version(self) -> str:
        return app_config.PINNED_CODEX_VERSION


    # --- Credentials -------------------------------------------------------

    #: The env entry the pool's sign-in (the full auth.json: a ChatGPT login
    #: or an OpenAI key) rides in from the config builder to
    #: ``start_session`` (popped there).
    _AUTH_JSON_ENV = "_CODEX_AUTH_JSON"

    def subscription_env(self, handle: SubscriptionHandle) -> dict[str, str]:
        env: dict[str, str] = {}
        if handle.auth_type == "local_endpoint":
            # A key on a LOCAL endpoint rides its own variable to the custom
            # provider the layer writes: as Codex's own sign-in it would
            # authenticate the built-in OpenAI provider instead.
            if handle.api_key:
                env["_CODEX_LOCAL_API_KEY"] = handle.api_key
        elif handle.api_key and not self._is_local_provider(handle.provider):
            # An OpenAI key rides as the sign-in file too: the app-server
            # takes a key from ``auth.json`` alone, never from the environment.
            # Never a local provider's row: Codex would send its key to OpenAI.
            from core.layers.codex.helpers import api_key_auth_json
            env[self._AUTH_JSON_ENV] = json.dumps(api_key_auth_json(handle.api_key))
        if handle.oauth_access_token:
            payload = self.credential_file_payload(
                handle.oauth_access_token, handle.oauth_expires_at_ms, handle.credential,
            )
            if payload is not None:
                env[self._AUTH_JSON_ENV] = json.dumps(payload)
        if handle.endpoint_url:
            env["_CODEX_ENDPOINT_URL"] = handle.endpoint_url
            # The provider (ollama / openai_compatible) picks the AGENTS.md
            # note that tells the model whether its MCP tools reach it
            # (helpers.local_provider_note). Popped by start_session like the URL.
            env["_CODEX_ENDPOINT_PROVIDER"] = handle.provider or ""
            # The provider's codex-cli model rows (id + context window) feed
            # the per-session model catalog that makes Codex defer its MCP
            # tools (local_model_catalog). Read HERE, off the event loop —
            # the pool's resolver runs under to_thread — so start_session
            # never touches the store on the loop. The one adapter call that
            # reads storage.
            from core.layers.codex.local_model_catalog import (
                LOCAL_MODEL_ROWS_ENV,
                local_model_rows_json,
            )
            env[LOCAL_MODEL_ROWS_ENV] = local_model_rows_json(handle.provider or "")
        return env

    def _is_local_provider(self, provider: str) -> bool:
        from core.engine_records import PROVIDER_LOCAL, provider_entry
        entry = provider_entry(self.capabilities, provider)
        return entry is not None and entry.get("kind") == PROVIDER_LOCAL

    def credential_file_payload(
        self, access_token: str, expires_at_ms: int, stored: dict,
    ) -> dict | None:
        # auth.json needs the login's id_token and account_id, which only the
        # device-auth blob carries; a row without one has no file to write
        # (the pool skips it on rotation and the spawn writes nothing).
        blob = stored.get("codex_auth_blob")
        if not isinstance(blob, dict) or not blob:
            return None
        from core.layers.codex.helpers import build_auth_json
        return build_auth_json(access_token, auth_blob=blob)

    def credential_file_from_env(self, env: dict) -> dict | None:
        raw = env.pop(self._AUTH_JSON_ENV, "")
        return json.loads(raw) if raw else None

    def refresh_oauth(self, refresh_token: str, stored: dict) -> OAuthRefresh:
        return codex_oauth.refresh(refresh_token, stored)

    # --- Usage windows -----------------------------------------------------

    def _window_specs(self) -> dict[str, WindowSpec]:
        return {s.key: s for s in self.capabilities.usage.windows}

    def usage_request(self, stored: dict) -> tuple[str, dict] | None:
        return codex_usage.usage_request(stored)

    def parse_usage(self, payload) -> Windows | None:
        return codex_usage.from_usage(payload, self._window_specs())

    def record_usage_event(self, session_id: str, payload) -> None:
        codex_usage.record_snapshot_async(session_id, payload)


    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
