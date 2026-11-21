"""What the Codex engine declares it can do (its ``LayerCapabilities``)."""

import config as app_config
from core.execution_layer import (
    PERMISSION_MODES,
    AuthProfile,
    BehaviourProfile,
    CredentialFileSpec,
    EngineIdentity,
    InBandSpec,
    LayerCapabilities,
    ModelPolicy,
    RuntimeProfile,
    SessionHomeSpec,
    UsageProfile,
    WindowSpec,
)

_CODEX_CAPABILITIES = LayerCapabilities(
    name="codex-cli",
    display_name="OpenAI Codex",
    supports_resume=True,
    supports_permissions=True,
    supports_plan_mode=True,          # `plan` mode → read-only sandbox + codex's
                                      # `plan` collaboration mode (turn/start's
                                      # collaborationMode); a turn's `plan`
                                      # item becomes the review card (CODEX.md)
    supports_todos=True,
    supports_subagents=True,          # collabAgentToolCall → SUBAGENT_START/END (0.5)
    supports_context_compression=False,
    supports_control_commands=True,   # app-server: model/mode via per-turn override
    supports_mcps=True,
    permission_modes=list(PERMISSION_MODES),
    control_commands=["set_model", "set_permission_mode"],
    models=app_config.get_layer_models("codex-cli", offers_ultra=True),
    # "max" is real wire vocabulary from the GPT-5.6 family on (GPT-6 too);
    # older models clamp to xhigh in map_effort_to_codex. "ultra" (Codex-
    # native proactive multi-agent orchestration on top of max/xhigh
    # reasoning) is per-MODEL — the Sols, Terra and the Astras, never
    # Luna — so the registry flag is emitted per model row (offers_ultra
    # above says this engine can run it at all) and map_effort_to_codex
    # clamps it everywhere else (see helpers).
    effort_levels=["low", "medium", "high", "xhigh", "max", "ultra"],
    effort_changeable_mid_session=True,   # effort is a per-turn override now
    compression_threshold_pct=None,
    mcp_delivery="external_config",
    mcp_config_format="toml",
    providers=[
        # map_effort_to_codex: max and ultra are unlocked by the model
        # FAMILY (prefix tables) — max has no row flag and clamps to xhigh
        # at the wire on an older id; ultra is the per-row supports_ultra.
        {"id": "openai", "label": "OpenAI", "requires_key": True,
         "kind": "vendor", "relay_path": "", "api_path": "",
         "effort_scale": ["low", "medium", "high", "xhigh", "max", "ultra"],
         "effort_per_model": ["ultra"]},
        # Local providers reach the operator's own network — unavailable on
        # hosted OtoDock (no operator LAN). Gated off at import on cloud.
        # A local model id matches no unlock prefix: its ladder tops at xhigh.
        # Ollama's OpenAI-compatible API answers under /v1 (its root is the
        # native API the model discovery reads).
        *([] if app_config.OTODOCK_CLOUD else [
            {"id": "ollama", "label": "Ollama (Local)", "requires_key": False,
             "kind": "local", "relay_path": "", "api_path": "/v1",
             "effort_scale": ["low", "medium", "high", "xhigh"],
             "effort_per_model": []},
            {"id": "openai_compatible", "label": "OpenAI-compatible server", "requires_key": False,
             "kind": "local", "relay_path": "", "api_path": "",
             "effort_scale": ["low", "medium", "high", "xhigh"],
             "effort_per_model": []},
        ]),
    ],
    identity=EngineIdentity(
        short_name="codex",
        vendor_id="openai",
        vendor_label="OpenAI",
        account_label="ChatGPT",
        role="coding",
        sort_order=20,
    ),
    runtime=RuntimeProfile(
        has_os_process=True,                  # one `codex app-server` daemon per session
        hard_abort_kills_process=False,       # turn/interrupt — the daemon + MCPs stay warm
        supports_remote_execution=True,
        supports_interactive_pty=True,        # the native Ratatui TUI under a PTY
        interactive_first_prompt_via_argv=True,    # `codex <PROMPT>` auto-runs after MCP warm
        supports_reattach_after_restart=True, # the satellite keeps the turn's notifications (0.5.171)
        readopts_idle_session=True,           # an idle app-server is taken back by its thread
        binary="codex",
        pin_key="codex",                      # cli_pins wire key — frozen
        config_dir_name=".codex",             # CODEX_HOME under the session's scope root
        self_wakes=False,                     # no self-wake turn; the bg monitors nudge at once
        event_queue_depth=4096,               # the router delivers between-turns terminal exits OOB — a drop is correctness-bearing
        interactive_submit_backstop=False,    # the Ratatui TUI takes the single Enter under ConPTY
        interactive_reports_model=True,       # a main turn_context on another model, same mode
        installed_name="codex",               # installed_clis wire value — frozen
    ),
    behaviour=BehaviourProfile(
        rebuilds_history_from_db=False,       # the daemon owns its rollout (thread/resume)
        attach_images_inline=False,           # photos ride as sandbox paths
        skills_delivery="materialized_dir",
        supports_bash=True,
        supports_plans_dir=False,             # no plan FILE — the plan is the turn's final message
        builtin_file_tools=False,
        has_shell_on_external_route=False,    # [features] shell_tool = false for external callers
        hides_denied_tools=True,              # the functions a limit leaves out are not offered (tool_limits)
        provider_pinned_per_session=True,     # model_provider is fixed in config.toml at spawn
        supports_steer=True,                  # turn/steer
        supports_compact=True,                # thread/compact/start
        supports_interrupt_for_queued=False,  # an engine with steer never reaches the queue branch
        supports_background_commands=True,  # a background terminal (unified_exec)
        supports_step_descriptions=False,  # a command carries none
        # The vendor promises no lifetime. A thread idle up to 70 minutes read its
        # cache in 17 of 18 probes, with one miss at 50 minutes, and OpenAI keeps an
        # idle prefix for up to an hour.
        prompt_cache_s=3600,
        # Codex's native names, by role — four spellings of the same calls
        # (the app-server item types, the rollout's function calls and custom
        # tools, its server-side call types, the hook wire's Bash /
        # apply_patch and the approval bridge's synthetic names); each maps
        # into the canonical set in tool_names.py (canonical_tool_name).
        tools={
            "shell": ("commandExecution", "exec_command", "exec", "Bash"),
            "write": ("fileChange", "apply_patch"),
            "web_search": ("webSearch", "web_search_call"),
            "discovery": ("tool_search_call",),
            "todo": ("update_plan",),
            "question": ("request_user_input", "request_user_input_async"),
            # The GPT-6.2 rows' message to the person mid-turn: Codex shows
            # it as an agentMessage, the chat's text.
            "message": ("send_message_to_user_async",),
            "escalation": ("CodexEscalation", "CodexTerminalInput"),
        },
        nonblocking_question_tools=("request_user_input_async",),  # its terminal picker holds no turn
        # Codex reads AGENTS.md only from the root down to its working folder,
        # so a repository cloned into the workspace is never on that path.
        repository_instruction_files=("AGENTS.md", "CLAUDE.md"),
        start_context_door="hook",
    ),
    model_policy=ModelPolicy(
        default_model="gpt-6.2-sol",          # tier 2; Astra (tier 1) is offered, never defaulted
        model_filter_policy="local_providers",# a personal ChatGPT account keeps the OpenAI builtins
        pricing_editable=False,
    ),
    auth=AuthProfile(
        auth_types=("oauth", "api_key", "local_endpoint"),  # a ChatGPT login, an OpenAI key, or a local server
        credential_file=CredentialFileSpec(   # the satellite clamp's row — frozen
            wire_kind="codex", dirname=".codex", filename="auth.json",
        ),
        oauth_flow="device_code",             # a verification URL + one-time code the page polls on
        # Every earlier reasoning item goes back with the first account's
        # encrypted_content; another account's refusal names this code.
        resume_refusal_codes=("invalid_encrypted_content",),
        session_home=SessionHomeSpec(
            # The rollouts, the skills and prompts, a thread's writer locks,
            # the "don't ask again" rules and the archived threads stay the
            # scope's (the platform's readers and a resume find them there;
            # the databases stay there by CODEX_SQLITE_HOME). The settings,
            # the hooks, the model catalog and the sign-in are the session's.
            # Proved on 0.169.0: a thread made in one home and resumed from
            # the same home made again keeps its whole conversation.
            links=("sessions", "skills", "prompts", "thread-writer-locks", "rules", "archived_sessions"),
            own_files=("auth.json", "config.toml", "hooks.json", "models.json"),
            seed=(("installation_id", ()),),
        ),
    ),
    usage=UsageProfile(
        windows=(
            # ChatGPT's consumer windows; the vendor reports them by length, and
            # the platform lists them under the same keys as Claude's.
            WindowSpec(key="five_hour", length_s=5 * 3600, role="session", label="session"),
            WindowSpec(key="seven_day", length_s=7 * 86400, role="quota", label="weekly"),
        ),
        in_band=(
            # The app server's account/rateLimits/updated, read from the
            # engine's own output.
            InBandSpec(kind="headless", source="codex_event", trusted=True),
            # A terminal's rollout lines (token_count.rate_limits), a file in
            # the engine's folder the agent can write: a hint.
            InBandSpec(kind="terminal", source="codex_rollout", trusted=False),
        ),
    ),
)
