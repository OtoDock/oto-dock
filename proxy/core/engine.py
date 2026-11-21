"""Engine: what an AI engine is, apart from any session of it.

An engine (Claude Code, Codex, Direct LLM) answers its facts here: its
descriptor, the spawn a machine renders (``prepare_spawn``), its half of the
satellite protocol, its credentials and usage windows, its config folders
and session files, its resume check, its transcript tailer, the hook's
answers. Holding a session is another contract (``ExecutionLayer``, built on
this one): the remote layer holds the sessions of the process engines, and
Direct LLM's layer holds its own.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from core.engine_records import (
    AgentConfig,
    LayerCapabilities,
    OAuthRefresh,
    ResumeVerdict,
    SpawnPlan,
    SubscriptionHandle,
    Windows,
)
from core.remote_engine_adapter import RemoteEngineAdapter


class Engine(ABC):
    """An AI engine's facts. Implementations: the Claude Code and Codex
    engines, and Direct LLM's layer (which also holds its sessions)."""

    async def prepare_spawn(self, session_id: str, config: AgentConfig) -> SpawnPlan:
        """The session's spawn for a compute machine (``SpawnPlan``): the
        argv and the environment the machine starts the engine with. An
        engine that runs in the proxy alone has none."""
        raise NotImplementedError(
            f"{self.capabilities.display_name} does not run on a compute machine")

    @property
    def engine_name(self) -> str:
        """The engine's short name (``claude`` / ``codex`` / ``direct``) as
        ``core/session/session_events`` reports it — read from the
        descriptor, so a layer declares it once in ``EngineIdentity``. The
        remote layer answers per session from its execution path instead
        (``engine_for``)."""
        return self.capabilities.identity.short_name

    def remote_adapter(self) -> "RemoteEngineAdapter | None":
        """This engine's satellite-protocol half (``RemoteEngineAdapter``),
        or None for an engine that never runs on a satellite (a contract
        test binds this to ``runtime.supports_remote_execution``)."""
        return None

    def canonical_tool_name(self, session_id: str, native: str) -> str:
        """The platform's name for a tool this engine presented as ``native``
        (``core/events/tool_roles``): what the permission authority, the
        pump and the tailers key on. Identity by default — Claude's names
        ARE the canonical set; Codex maps its app-server item types, its
        rollout names and its sanitised MCP server keys here. ``session_id``
        lets an engine answer from the session (the MCP servers it
        declared); the remote placement delegates to the engine behind the
        session. Never raises: an unknown native name comes back as is."""
        return native

    def engine_reviews(self, session_id: str, live_mode: str) -> bool:
        """Whether this engine's own reviewer judges the call the hook is
        deciding now (Auto's review set), from the mode the engine reported
        with the call (``live_mode``, empty for a call no hook raised). False
        by default: an engine with no reviewer gets the platform's own
        answer. Never raises."""
        return False

    def hook_can_ask(self) -> bool:
        """Whether this engine's PreToolUse hook can make its terminal ask
        the person (an ``ask`` decision feeds the CLI's own prompt). False
        by default: Codex's hook only denies (``OTO_HOOK_DENY_ONLY``)."""
        return False

    def terminal_reports_mode(self) -> bool:
        """Whether this engine's interactive terminal reports its own mode
        word with each hook call (Claude Code's Shift+Tab), which the
        person's chat follows. False by default: Codex reports only its
        approval policy (``default`` / ``bypassPermissions``), which is not
        a mode the person picked."""
        return False

    def chat_session_files(self, home: Path, session_id: str, resume_handle: str) -> list[Path]:
        """The on-disk session files of ONE chat under a scope home
        (``users/<u>`` / ``workspace`` / an external caller's tree): what the
        retention sweeps delete when the chat ages out. ``session_id`` and
        ``resume_handle`` are the chat row's; an engine reads the one it
        keys its files by. Default: none — an engine that rebuilds from the
        DB keeps no files."""
        return []

    def iter_session_files(self, home: Path) -> Iterator[tuple[Path, str]]:
        """Every session file of this engine under a scope home, with the
        session id or resume handle it belongs to (raw — the caller keeps its
        own id shape and liveness guards): the orphan sweep. A folder the
        engine keeps for a session whose file is already gone is one too.
        Default: none."""
        return iter(())

    def session_file_companions(self, path: Path) -> list[Path]:
        """What the engine keeps beside one session file and goes with it
        (the caller checks each is a real folder, never a link). Default:
        none."""
        return []

    def transcript_tailer(self):
        """The module that persists this engine's INTERACTIVE transcript
        into ``chat_messages`` — ``core.session.transcript_tailer`` for the
        Claude TUI's session JSONL, ``codex_rollout_tailer`` for the Codex
        rollout; both expose ``resolve_and_tail(session_id, chat_id)`` and
        ``forget``. None for an engine with no TUI (a contract test binds
        this to ``runtime.supports_interactive_pty``)."""
        return None

    def pinned_cli_version(self) -> str:
        """The exact CLI version the platform pins for this engine
        (``VERSIONS.md``), or "" for an engine with no binary.

        A METHOD read at call time, not a descriptor field: the descriptors
        are module constants, so a field would freeze ``config.PINNED_*`` at
        import — and the satellite handshake, the machine cards and their
        tests read the live value. Layers with a binary override this.
        """
        return ""

    def prepare_config_dir(
        self, agent_name: str, *, username: str = "", scope: str = "user",
        external_home=None, no_shell: bool = False, read_only: bool = False,
        outside_picks=None, kind_rule=None,
    ) -> Path:
        """Create or refresh the persistent config dir a LOCAL session of this
        engine mounts (``.claude`` / ``.codex`` under the scope's root — the
        settings, the hook scripts, the skills dir) and return its host
        path. ``scope`` is ``user`` (``users/<username>/``) or ``agent``
        (``workspace/``); ``external_home`` is an external caller's private
        tree, whose skills dir follows ``outside_picks`` (the Outside people
        picks the session was built with) or ``kind_rule`` (a wrap-up's own
        scratch home, its kind's set); ``no_shell`` and ``read_only`` are
        the external-session and judge restrictions an engine writes into
        its settings where it can.

        Every REGISTERED engine overrides this (a contract test binds it):
        the default raises, so an engine that forgets fails CI, never a
        first spawn. An engine with no CLI config still answers — Direct
        LLM keeps its plans dir and its in-process MCP config home in the
        ``.claude`` tree the sandbox mounts.
        """
        raise NotImplementedError(
            f"{type(self).__name__} declares no config dir builder "
            "(ExecutionLayer.prepare_config_dir)"
        )

    def resumes_interactive(self, config: AgentConfig) -> bool:
        """Whether a fresh INTERACTIVE spawn of ``config`` resumes an earlier
        conversation. The warmup's ``config.resume`` flag by default — Claude
        resumes its session id with ``--resume``, so the flag stands. An
        engine that resumes by a handle (Codex: the thread id, whose rollout
        outlives the in-memory session and the flag) answers from the
        handle; for a non-interactive config every engine answers the flag.
        """
        return config.resume

    async def on_subscriptions_changed(self) -> None:
        """The set of subscriptions on this engine changed — a row added,
        updated, disabled or deleted through the admin API.

        Default: nothing. An engine whose credentials feed another platform
        surface overrides it (Direct LLM pushes the phone config, whose Groq
        turn classifier reuses this engine's Groq key). Async because the
        one implementation awaits a WebSocket broadcast; the admin endpoints
        await it after their store write.
        """
        return None

    # --- Credentials: how an acquired subscription reaches a session -------
    #
    # SYNCHRONOUS, all of them: the pool runs on ``to_thread`` workers and its
    # fan-out callback fires on a worker thread for local writes and on the
    # event loop for satellite acks — an async method could be awaited from
    # neither. Vendor knowledge lives here; policy (which account, when to
    # rotate, the lock, the backoff, the fan-out) stays in the pool.

    def subscription_env(self, handle: SubscriptionHandle) -> dict[str, str]:
        """The env entries that carry ``handle``'s credential into a session
        of this engine — the layer's own names, which its ``start_session``
        (and the remote start payload) consume. A key rides the env, or the
        credential FILE where the engine reads a key from there alone
        (Codex); an OAuth login rides as the credential FILE payload the
        layer writes (``credential_file_from_env`` pops it) — never as an env
        token, which is frozen at exec and would defeat rotation fan-out. A minted relay
        token arrives as ``api_key`` + ``endpoint_url`` like any BYO key.

        Every REGISTERED engine overrides this (a contract test binds it):
        the default raises rather than returning nothing, so an engine that
        forgets cannot spawn with no credentials and fail on its first turn.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not map a subscription to a session env"
        )

    def credential_file_payload(
        self, access_token: str, expires_at_ms: int, stored: dict,
    ) -> dict | None:
        """The full content of this engine's credential file for an OAuth
        ``access_token`` — what the pool writes under a live process on every
        rotation and what the satellite receives verbatim. ``stored`` is the
        decrypted ``credential_data`` (grant metadata, vendor blobs); the
        refresh token is never in the payload (the pool is the sole rotator).
        None for an engine without a credential file, or when ``stored``
        lacks what the file needs. ``expires_at_ms`` is the token's expiry,
        passed by the caller so the file and the caller's own runway snapshot
        cannot disagree."""
        return None

    def credential_file_from_env(self, env: dict) -> dict | None:
        """Pop this engine's credential-file payload out of a session env
        (the consumer half of ``subscription_env``) — the local layer writes
        it, the remote start payload ships it. None when the env carries no
        file (a key in the environment, a local-endpoint session)."""
        return None

    def refresh_oauth(self, refresh_token: str, stored: dict) -> OAuthRefresh:
        """One refresh against the vendor's token endpoint (the vendor call
        only — the lock, the backoff, the terminal verdict, the store write
        and the fan-out are the pool's). ``stored`` is the decrypted
        ``credential_data`` the vendor-shaped parts are rebuilt from. An
        engine that takes a login (``"oauth"`` among its auth types)
        overrides this; the pool treats the default — and any adapter
        exception — as a transient failure, never a dead grant."""
        raise NotImplementedError(
            f"{type(self).__name__} takes no OAuth login to refresh"
        )

    # --- Usage windows: how the vendor reports an account's rate limits -----

    def usage_request(self, stored: dict) -> tuple[str, dict] | None:
        """The vendor's usage endpoint and the headers a poll of ``stored``'s
        account needs (the poller adds the platform ``User-Agent``), or None
        when the engine declares no windows or the credential lacks what the
        vendor needs. An engine with ``usage.windows`` overrides this."""
        return None

    def parse_usage(self, payload) -> Windows | None:
        """The engine's reading of its vendor's usage payload against its
        declared windows, or None on a shape it does not recognise."""
        return None

    def usage_scope_key(self, model: str) -> str:
        """The per-model window a spawn of ``model`` counts against on this
        engine (the ``Scoped.key`` its parser files that window under — Claude
        scopes weekly windows by model family), or "" when the vendor has no
        per-model windows or the model falls under none."""
        return ""

    def record_usage_event(self, session_id: str, payload) -> None:
        """An in-band usage event from a live session of this engine (the
        headless stream's rate-limit info, the app-server's rate-limit
        notification) — parsed by the engine and recorded on the session's
        account, off the event loop. Shared code (the remote turn) calls this
        so it never names an engine's parser; the engine's own session and
        translator may call their sibling usage module directly. Default:
        nothing to record."""
        return None

    def parse_usage_report(self, payload) -> Windows | None:
        """A terminal's own report of its account's windows (what the engine's
        status line command received, posted to ``/v1/hooks/usage-report`` by
        ``hooks/usage_report.py``), read against the declared windows and
        stamped with the source the engine declares for its terminals
        (``usage.in_band``), or None on a shape the engine does not recognise.
        Default: the engine has no such report."""
        return None

    # --- Capabilities ---

    @property
    @abstractmethod
    def capabilities(self) -> LayerCapabilities:
        """Return the capability descriptor for this execution layer."""

    # Convenience accessors (backwards-compatible with old boolean properties)
    @property
    def supports_resume(self) -> bool:
        return self.capabilities.supports_resume

    @property
    def supports_permissions(self) -> bool:
        return self.capabilities.supports_permissions

    @property
    def supports_plan_mode(self) -> bool:
        return self.capabilities.supports_plan_mode

    @abstractmethod
    async def can_resume_session(
        self, session_id: str, *, agent_name: str = "", username: str = "",
        external_home: str = "",
    ) -> ResumeVerdict:
        """Whether a dead session has conversation data and can be resumed.

        CLI: checks session .jsonl file for user messages.  After proxy
        restart the in-memory session→claude-dir mapping is lost, so
        *agent_name* and *username* (or an external caller's
        *external_home*) are used to derive the .claude/ path.
        Direct LLM: always ``FRESH`` (in-memory history lost on death;
        recovery handled via chat_id in AgentConfig instead). The callers go
        through ``core/session/resume_probe.settle_resume``, which waits on
        an ``UNKNOWN`` answer within a budget and never reads it as fresh.
        """
