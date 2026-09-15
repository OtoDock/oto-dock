"""Schema for the phone domain: audio providers, phone servers and routes,
per-user audio preferences and the call log.

Called by ``storage.schema.init_schema`` in foreign-key order. Every
statement is idempotent (``CREATE ... IF NOT EXISTS``); see
``docs/architecture/DATABASE-SCHEMA.md``.
"""


def init_audio_telephony(conn) -> None:
    """Audio (STT / TTS) providers, phone servers / routes, audio prefs."""
    # --- Audio providers (STT / TTS) — shared by chat audio + telephony ---
    # Provider rows drive the admin pills and per-route / per-chat selection.
    # ``credential_key`` references ``infra_credentials.mcp_name`` (e.g.
    # ``audio-deepgram``); NULL for local / self-hosted providers. The partial
    # unique indexes enforce one default STT and one default TTS per context.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audio_providers (
            id SERIAL PRIMARY KEY,
            provider_type TEXT NOT NULL CHECK (provider_type IN ('stt', 'tts')),
            provider_name TEXT NOT NULL,
            label TEXT NOT NULL,
            credential_key TEXT,
            enabled_for_calls BOOLEAN NOT NULL DEFAULT TRUE,
            enabled_for_chat BOOLEAN NOT NULL DEFAULT TRUE,
            is_default_calls BOOLEAN NOT NULL DEFAULT FALSE,
            is_default_chat BOOLEAN NOT NULL DEFAULT FALSE,
            voices JSONB NOT NULL DEFAULT '{}',
            advanced JSONB NOT NULL DEFAULT '{}',
            last_health_check TEXT,
            last_health_status TEXT NOT NULL DEFAULT 'unknown',
            last_health_detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (provider_type, provider_name)
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_audio_default_calls "
        "ON audio_providers(provider_type) WHERE is_default_calls = TRUE"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_audio_default_chat "
        "ON audio_providers(provider_type) WHERE is_default_chat = TRUE"
    )

    # --- Phone servers (telephony adapters: Asterisk/FreePBX, Twilio, 3CX) ---
    # ``credentials`` + ``config`` are adapter-specific JSONB blobs. Bootstrap
    # tracks the one-time dialplan-snippet handshake. The control-plane adapters
    # live proxy-side in ``services/phone/phone_adapters/`` (the proxy talks to the PBX
    # directly); the phone server stays a pure media daemon. ``asterisk_manual``
    # is the no-automation reference adapter (snippet + admin-confirmed verify).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS phone_servers (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            adapter_type TEXT NOT NULL
                CHECK (adapter_type IN ('asterisk_manual', 'asterisk_freepbx', 'twilio', 'three_cx')),
            host TEXT NOT NULL DEFAULT '',
            credentials JSONB NOT NULL DEFAULT '{}',
            config JSONB NOT NULL DEFAULT '{}',
            bootstrap_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (bootstrap_status IN ('pending', 'snippet_provided', 'verified', 'failed', 'drift')),
            bootstrap_log TEXT NOT NULL DEFAULT '',
            last_health_check TEXT,
            last_health_status TEXT NOT NULL DEFAULT 'unknown',
            last_health_detail TEXT NOT NULL DEFAULT '',
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_phone_servers_default "
        "ON phone_servers(is_default) WHERE is_default = TRUE"
    )

    # --- Phone routes ---
    # ``id`` stays TEXT (UUID): it is a cross-process string key round-tripped
    # to the phone server in the warmup message (``phone_route_id``) and keyed
    # in the dashboard outbound-route dropdown — never coerce it to an int.
    # ``stt_provider_id`` / ``tts_provider_id`` NULL = use the context default;
    # ``phone_server_id`` is required (provisions the route on the adapter).
    # ``trigger_slug`` logically references ``triggers.slug`` with
    # ``scope='agent'`` and matching ``agent``. When a phone call lands on a
    # route, the warmup handler resolves the trigger row, builds a normalised
    # ``trigger_payload`` ({source: "phone", route, phone, did, body}) and
    # threads it into the agent session so manifest ``agent_context`` blocks
    # can resolve ``${trigger.*}`` tokens. NULL = no enrichment.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS phone_routes (
            id TEXT PRIMARY KEY,
            direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
            name TEXT NOT NULL DEFAULT '',
            agent TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'en',
            llm_mode TEXT NOT NULL DEFAULT 'proxy',
            phone_server_id INTEGER NOT NULL REFERENCES phone_servers(id) ON DELETE RESTRICT,
            stt_provider_id INTEGER REFERENCES audio_providers(id) ON DELETE RESTRICT,
            tts_provider_id INTEGER REFERENCES audio_providers(id) ON DELETE RESTRICT,
            greeting TEXT NOT NULL DEFAULT '',
            phone_context_override TEXT NOT NULL DEFAULT '',
            backchannel_mode TEXT NOT NULL DEFAULT 'on'
                CHECK (backchannel_mode IN ('on', 'off')),
            thinking_filler_mode TEXT NOT NULL DEFAULT 'on'
                CHECK (thinking_filler_mode IN ('on', 'off')),
            background_sound TEXT NOT NULL DEFAULT 'off',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            audiosocket_uuid TEXT UNIQUE,
            did TEXT,
            ami_caller_id TEXT NOT NULL DEFAULT '',
            ami_outbound_context TEXT NOT NULL DEFAULT '',
            dial_prefix TEXT NOT NULL DEFAULT '',
            adapter_data JSONB NOT NULL DEFAULT '{}',
            trigger_slug TEXT,
            -- Who the caller IS on this route and what the session may touch
            -- (external routes, 2026-09-06). 'caller' = every caller gets a
            -- private space and is an EXTERNAL principal; 'shared' = the
            -- agent's shared space, still external; 'user' = the call runs as
            -- identity_user_sub's own session (capped at manager). ``role``
            -- applies to the two external modes. remember_callers = FALSE makes
            -- every caller ephemeral (no private tree, no memory) — for public
            -- numbers where caller-ID cannot be trusted. Existing installs get
            -- the same defaults via run_migrations (no migration-only state).
            identity_mode TEXT NOT NULL DEFAULT 'caller'
                CHECK (identity_mode IN ('caller', 'shared', 'user')),
            identity_user_sub TEXT REFERENCES users(sub) ON DELETE SET NULL,
            role TEXT NOT NULL DEFAULT 'viewer'
                CHECK (role IN ('viewer', 'editor', 'manager')),
            remember_callers BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phone_routes_direction ON phone_routes(direction)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phone_routes_agent ON phone_routes(agent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phone_routes_server ON phone_routes(phone_server_id)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_phone_routes_did "
        "ON phone_routes(phone_server_id, did) WHERE direction = 'inbound'"
    )
    # NOTE: ``dial_prefix`` (outbound SIP-line selection) is in the CREATE TABLE
    # above for fresh installs. Existing DBs get it via a one-time manual
    # migration — an ALTER here deadlocks the test pool's concurrent init_schema.

    # --- Per-user audio preferences (chat sound / mic icons) ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_audio_prefs (
            user_sub TEXT PRIMARY KEY REFERENCES users(sub) ON DELETE CASCADE,
            stt_mode TEXT NOT NULL DEFAULT 'auto' CHECK (stt_mode IN ('native', 'platform', 'auto')),
            tts_mode TEXT NOT NULL DEFAULT 'auto' CHECK (tts_mode IN ('native', 'platform', 'auto')),
            tts_voice_map JSONB NOT NULL DEFAULT '{}',
            stt_language TEXT,
            wake_word_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TEXT NOT NULL
        )
    """)


def init_phone_call_log(conn) -> None:
    """Per-call outcome rows for the admin call log (route detail view).

    Written by the phone daemon's fire-and-forget teardown report
    (``POST /v1/phone/calls/report``) — one row per call, including calls
    that never warmed a session (PIN-refused, capacity-rejected). The
    ``route_name`` snapshot survives route deletion (FK goes NULL).
    Only attempt COUNTS are stored for PIN outcomes — never digits.
    ``session_id`` / ``identity`` / ``tools_run`` (2026-09-06) are the audit
    trail of the warmed session: the daemon reports the session id, the
    proxy fills the caller identity label and the distinct tools the call
    ran. Pre-existing installs get them via run_migrations.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS phone_call_log (
            id SERIAL PRIMARY KEY,
            route_id TEXT REFERENCES phone_routes(id) ON DELETE SET NULL,
            route_name TEXT NOT NULL DEFAULT '',
            phone_server_id INTEGER,
            agent TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
            from_number TEXT NOT NULL DEFAULT '',
            to_number TEXT NOT NULL DEFAULT '',
            transport TEXT NOT NULL DEFAULT '',
            call_uuid TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL,
            pin_attempts INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            duration_s INTEGER,
            session_id TEXT NOT NULL DEFAULT '',
            identity TEXT NOT NULL DEFAULT '',
            tools_run JSONB NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phone_call_log_route ON phone_call_log(route_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phone_call_log_started ON phone_call_log(started_at)")
