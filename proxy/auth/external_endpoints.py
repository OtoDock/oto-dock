"""The route lists a session's tokens are judged by (``middleware._session_refusal``):
every session token's list (``SESSION_ROUTES``: the routes the platform's
tools and hooks call, and no other), the Docker tools' callback token's five
routes, and the two narrower lists of an EXTERNAL session (a phone caller who
is not a platform user) and of the wrap-up after a conversation.

The counterpart of ``auth/service_endpoints.py`` for the other end of the
trust scale. A session on an external route carries a JWT like any agent
session — it sits in the CLI process env, and shell (where a role still has
one) can ``curl`` the proxy with it — so the tool-level rules alone would be
bypassable. The session refusal (``middleware._session_refusal``) therefore:

  1. rejects (401) any session token whose session is not live, or that was
     minted for an earlier life of its session id
     (``session_state.session_token_refusal``) — a token lifted from a call
     is dead once the hangup's closing window passes, a token lifted from a
     chat once the chat's session closes;
  2. confines (403) tokens whose caller is not a platform user (``ext`` and
     no ``user_sub``) to the endpoints below, read against the session's
     registered security context (``is_external_endpoint_allowed``): the
     hook callbacks always; the file and media callbacks only when the
     session has an attached tool; the credential gateway only for a server
     the session's Outside people picks attached (``outside_grants``); the
     call tools; a method of an app a manager opened to them, only with
     the apps tool attached. No memory route: the person's memory is read
     into the prompt and written after the conversation. Everything else — tasks,
     continuations, triggers, notifications, meetings, delegation, chats
     and transcripts, files, media, uploads, agent discovery, subscriptions,
     pins — is unreachable by construction, and a session with no
     registered context reaches the hook callbacks alone;
  3. after the session list, refuses (403) a first-party tool's route when
     the session's tool limits leave off every tool that calls it
     (``ROUTE_TOOLS``, ``session_route_refusal``), with the sentence the
     hook gives the tool call, so a limit holds on the route as on the call.

Contributor contract — when do you add an entry here?
  - Only when a hook script or an MCP that is attached to EXTERNAL sessions
    (``mcp_registry.EXTERNAL_DENIED_MCPS``, the ``"external"`` exclusion
    context and the Outside people picks decide that) calls a NEW proxy
    endpoint. Use an anchored ``^...$`` pattern scoped to the HTTP method(s)
    actually used, and ask whether an anonymous caller may drive that
    endpoint at all.
  - ``verify_session_match_async`` still binds the token's ``sid`` to the
    request body on the hook endpoints; this list only decides WHICH
    endpoints exist for an external caller.
"""

import re

import config
from auth.request_path import has_traversal

# (pattern, allowed-methods) — anchored, method-scoped: the routes every
# outside session reaches whatever was picked.
_ALLOWLIST: list[tuple[re.Pattern, frozenset[str]]] = [
    # Hook scripts (permission_gate / tool_result_forwarder / stop_tracker /
    # subagent_tracker) and the stdio interceptor's path resolvers.
    (
        re.compile(r"^/v1/hooks/(permission|tool-result|stop|subagent|resolve-path|resolve-tool-arg-paths)$"),
        frozenset({"POST"}),
    ),
    # The call tools (call-control-mcp): a caller ends or transfers their
    # OWN call; the binding is the token's session, the body names no call.
    (re.compile(r"^/v1/phone/call-control$"), frozenset({"POST"})),
]
# The session-scoped file / media callbacks of file-tools, image-gen,
# music-gen and video-gen: only a session with an attached tool has a
# process that calls them, and each binds the token's session by its route.
_TOOL_CALLBACKS = re.compile(
    r"^/v1/hooks/(file|file-written|document-preview|images|image-generating|image-gen-failed|media)$"
)
# A credentialed HTTP MCP attached to the session reaches its server through
# the credential gateway (api/mcp/gateway.py), which binds the token's
# session to the MCP's stored credential: only for a server the picks
# attached (the gateway judges each call by function name after).
_GATEWAY = re.compile(r"^/v1/mcp-gateway/([^/]+)/.*$")
_GATEWAY_METHODS = frozenset({"GET", "POST", "DELETE"})

# The apps a manager opened to outside people (the apps tool,
# ``outside_picks.APPS_MCP``): a method of an app, by the app's id and the
# method's name (``app_deploy.EXPORT_NAME_RE``'s characters); the app's door
# (``app_proxy._outside_caller``) judges the pick and the method.
_APP_METHOD = re.compile(r"^/v1/apps/[0-9a-f-]{36}/api/[a-z0-9][a-z0-9-]{0,63}$")

# The wrap-up after a conversation (``SecurityContext.wrapup``): the hooks,
# its memory op (the memory API maps it to that one person's memory), its
# record and the conversations tool's question, the tool callbacks and the
# credential gateway of the business tools it attached.
_WRAPUP_ALLOWLIST: list[tuple[re.Pattern, frozenset[str]]] = [
    _ALLOWLIST[0],
    (re.compile(r"^/v1/internal/memory/op$"), frozenset({"POST"})),
    (re.compile(r"^/v1/conversations/record$"), frozenset({"POST"})),
    (re.compile(r"^/v1/conversations/kind$"), frozenset({"GET"})),
]

EXTERNAL_BLOCKED_DETAIL = "This endpoint is not available to external sessions"
WRAPUP_BLOCKED_DETAIL = "This endpoint is not available after a conversation"
SESSION_DEAD_DETAIL = "Session is no longer active"
SESSION_ROUTE_DETAIL = "A session's token does not reach this route"
CALLBACK_OUTSIDE_DETAIL = "A tool's callback token works only on the file callbacks"

# --- every session token's list ----------------------------------------------
#
# A session's own token works on the routes the platform's tools and hooks
# call and on no other (the standing security decision 4). Each row names its
# caller so a review sees why it exists; a test sweeps the callers' sources for
# every ``/v1/`` path they name and the route table for every route, so a
# caller without a row, or a row without a route, fails the suite. ``{A}`` is
# the token's own ``agent`` claim, judged after the match: a per-agent row
# never matches another agent's path, and a token with no agent claim matches
# none. Matched whole (``fullmatch``): the router's own ``$`` also matches
# before a final newline. The outside caller's and the wrap-up's lists below
# are subsets of this one.
_ID = r"[A-Za-z0-9_.:-]+"
_UUID = r"[0-9a-f-]{36}"
_A = r"(?P<agent>[A-Za-z0-9_-]+)"
_GET, _POST = frozenset({"GET"}), frozenset({"POST"})
_SESSION_ROWS: list[tuple[str, frozenset[str]]] = [
    # The engine hooks, both engines, local and on a machine (proxy/hooks/*,
    # the satellite's Codex bridges; a terminal's start asks for the start's
    # riding part); the command-line credential's route keeps a pre-1.9
    # session's grace (api/hooks/credentials.py).
    (r"/v1/hooks/(permission|codex-question|tool-result|subagent|stop|resolve-tool-arg-paths|"
     r"start-context|cli-credential)", _POST),
    # A terminal's status line command (hooks/usage_report.py): a report of
    # the session's own account, filed as a hint (api/hooks/usage.py).
    (r"/v1/hooks/usage-report", _POST),
    # A machine's satellite, for the session's SSH agent socket (the proxy
    # signs: core/credentials/ssh_signer.py).
    (r"/v1/hooks/ssh-agent", _POST),
    # The tools' callbacks: the Docker tools with their own kind, the stdio
    # tools (display, image-gen, music-gen, video-gen, tts) with the session's.
    (r"/v1/hooks/(resolve-path|file|file-written|document-preview|images|image-generating|"
     r"image-gen-failed|media|url|ui)", _POST),
    # display-mcp's apps and files hooks.
    (r"/v1/hooks/apps/(pin|unpin|list|rollback|describe|export|import|push|state|open|deploy|"
     r"check|screenshot|status|preview|logs|restart|purge)", _POST),
    (r"/v1/hooks/files/(pin|unpin)", _POST),
    # The credential gateway (api/mcp/gateway.py).
    (r"/v1/mcp-gateway/[^/]+/.*", frozenset({"GET", "POST", "DELETE"})),
    # The session's own (notifications-mcp, meetings-mcp; agent-config-mcp
    # and agent-creator-mcp read the engines).
    (r"/v1/session/current", _GET),
    (r"/v1/execution-layers", _GET),
    # schedules-mcp.
    (r"/v1/tasks/(scheduled|one-time)", _POST),
    (r"/v1/continuations", _POST),
    (r"/v1/tasks", _GET),
    (r"/v1/tasks/runs", _GET),
    (rf"/v1/tasks/runs/{_ID}/stream", _GET),
    (rf"/v1/tasks/runs/{_ID}/cancel", _POST),
    (rf"/v1/tasks/(?!runs$|stats$){_ID}", frozenset({"GET", "DELETE"})),
    (rf"/v1/tasks/{_ID}/(run|pause|resume|edit)", _POST),
    # notifications-mcp.
    (r"/v1/notifications", frozenset({"GET", "POST"})),
    (rf"/v1/notifications/{_ID}", frozenset({"DELETE"})),
    (rf"/v1/notifications/{_ID}/(pause|resume|edit)", _POST),
    # triggers-mcp.
    (r"/v1/triggers", frozenset({"GET", "POST"})),
    (rf"/v1/triggers/{_ID}", frozenset({"GET", "DELETE"})),
    (rf"/v1/triggers/{_ID}/(edit|pause|resume|fire)", _POST),
    (r"/v1/subscriptions", _GET),
    # meetings-mcp.
    (r"/v1/meetings", _POST),
    (rf"/v1/meetings/{_ID}/(start|end|propose-conclude|leave)", _POST),
    # delegation-mcp.
    (r"/v1/delegation/(spawn|send_files|attach_result_files|adopt)", _POST),
    (r"/v1/delegation/sessions", _GET),
    (rf"/v1/delegation/sessions/{_ID}/peek", _GET),
    (r"/v1/delegation/notebook", frozenset({"GET", "POST"})),
    (r"/v1/chats/agent-move", _POST),
    # memory-mcp and the wrap-up; agent-config-mcp's memory settings.
    (r"/v1/internal/memory/op", _POST),
    (rf"/v1/internal/memory/agent-settings/{_A}", frozenset({"GET", "PATCH"})),
    # checks-mcp.
    (rf"/v1/agents/{_A}/checks", _GET),
    (rf"/v1/agents/{_A}/(checks|user-checks)/{_ID}", frozenset({"PUT", "DELETE"})),
    (r"/v1/checks/attached", _GET),
    (r"/v1/checks/(attach|detach|run)", _POST),
    # agent-config-mcp.
    (rf"/v1/agents/{_A}/info", _GET),
    (rf"/v1/agents/{_A}", frozenset({"PATCH"})),
    (rf"/v1/agents/{_A}/files", _GET),
    (rf"/v1/agents/{_A}/files/config/agent\.md", frozenset({"PUT"})),
    (rf"/v1/agents/{_A}/outside", frozenset({"GET", "PUT"})),
    (rf"/v1/agents/{_A}/tool-limits", frozenset({"GET", "PUT"})),
    (rf"/v1/agents/{_A}/complete-setup", _POST),
    (rf"/v1/agents/{_A}/knowledge-attachments", frozenset({"GET", "PUT"})),
    (rf"/v1/agents/{_A}/knowledge-attachments/{_ID}", frozenset({"DELETE"})),
    (rf"/v1/agents/{_A}/knowledge-library", frozenset({"PUT"})),
    (r"/v1/knowledge-libraries", _GET),
    (r"/v1/departments", _GET),
    (rf"/v1/agents/{_A}/reload-session", _POST),
    # agent-creator-mcp.
    (r"/v1/community/(agents|departments)", _GET),
    (r"/v1/cards", _POST),
    (rf"/v1/cards/{_ID}", _GET),
    (r"/v1/agents/local-template/building-blocks", _GET),
    (r"/v1/agents/local-template/validate", _POST),
    (r"/v1/agents/install-from-local-template", _POST),
    # mcps-mcp.
    (rf"/v1/agents/{_A}/mcps", frozenset({"GET", "PUT"})),
    (rf"/v1/agents/{_A}/skills", _GET),
    (rf"/v1/agents/{_A}/skills/{_ID}", frozenset({"PATCH"})),
    (rf"/v1/agents/{_A}/mcp-requests", frozenset({"GET", "POST"})),
    (rf"/v1/agents/{_A}/mcp-requests/{_ID}/cancel", _POST),
    (r"/v1/community/(mcps|skills)", _GET),
    (r"/v1/mcps/local-package/validate", _POST),
    # settings-mcp, ssh-hosts, location-mcp.
    (r"/v1/settings/catalog", _GET),
    (r"/v1/settings/card", _POST),
    (r"/v1/cards/list", _POST),
    (rf"/v1/agents/{_A}/ssh-hosts", _GET),
    (r"/v1/location/request", _POST),
    # The media tools (tts-mcp, transcribe-mcp, image-search-mcp).
    (r"/v1/audio/tts/(voices|voices/search)", _GET),
    (r"/v1/audio/tts/(voices/add|generate)", _POST),
    (r"/v1/audio/transcribe", _POST),
    (r"/v1/images/temp", _POST),
    # The phone and outside-person tools (call-control-mcp, conversations-mcp,
    # phone-mcp).
    (r"/v1/phone/call-control", _POST),
    (r"/v1/conversations", _GET),
    (rf"/v1/conversations/(kind|people|{_ID})", _GET),
    (r"/v1/conversations/record", _POST),
    (r"/v1/phone/calls", _POST),
    (rf"/v1/phone/calls/{_ID}", _GET),
    (rf"/v1/phone/calls/{_ID}/wait", _GET),
    (rf"/v1/phone/calls/{_ID}/answer", _POST),
    # Agents calling apps (APPS.md "Agents call apps": the agent's shell and
    # outside-apps-mcp with the session's token).
    (rf"/v1/apps/{_UUID}/api(/.*)?", frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})),
    (rf"/v1/apps/{_UUID}/platform/(?!ws$)[A-Za-z0-9_.-]+", _POST),
    (rf"/v1/apps/{_UUID}/push", _POST),
    (rf"/v1/apps/{_UUID}/state", frozenset({"PATCH"})),
]
SESSION_ROUTES: list[tuple[re.Pattern, frozenset[str]]] = [
    (re.compile(pattern), methods) for pattern, methods in _SESSION_ROWS
]
# --- the tools behind the routes ----------------------------------------------
#
# Which first-party tool calls each route of the list, for the session's tool
# limits (``services/mcp/tool_limits.py``): a limit a manager sets on a tool
# holds on the tool's own route too, so the agent's shell, calling the route
# with the session's token, gets the sentence the hook gives the tool call
# (``session_route_refusal``). One row per route and method, naming the tool
# or the tools that call it: a route one tool calls is refused when that tool
# is; a route several tools share (a read before a tool's own call, a route
# whose body names the action) stays open while one of them is allowed, since
# the path does not tell them apart. A route of the list with no row keeps
# the list's answer: the engine hooks, the SSH agent, the Docker tools'
# callbacks (a callback token, judged before the limits), the credential
# gateway, a server's read at its start, and an app's own methods and
# platform methods. A row names the server key the pairs carry (``outside_picks.server_key``:
# the manifest's ``server_name``, else its name). A test sweeps the tools'
# sources, so a tool's route without a row, or a row naming no tool, fails
# the suite.
_Tools = tuple[tuple[str, str], ...]


def _by(server: str, *functions: str) -> _Tools:
    return tuple((server, function) for function in functions)


_IMAGE_GEN = _by("image-gen-mcp", "generate_image", "edit_image_ai")
_ENGINE_READERS = _by("agent-config-mcp", "list_available_models", "update_default_model",
                      "update_execution_layers", "update_default_layer", "update_default_execution_mode")
_PUT, _PATCH, _DELETE = frozenset({"PUT"}), frozenset({"PATCH"}), frozenset({"DELETE"})
_ROUTE_TOOL_ROWS: list[tuple[str, frozenset[str], _Tools]] = [
    # The stdio tools' callbacks.
    (r"/v1/hooks/file", _POST, _by("display", "send_file") + _IMAGE_GEN),
    (r"/v1/hooks/images", _POST, _by("display", "display_images") + _IMAGE_GEN),
    (r"/v1/hooks/(image-generating|image-gen-failed)", _POST, _IMAGE_GEN),
    (r"/v1/hooks/media", _POST, _by("display", "display_video", "display_audio")
     + _by("tts-mcp", "generate_speech") + _by("music-gen-mcp", "compose_music", "sound_effect")
     + _by("video-gen-mcp", "generate_video", "generate_transition", "edit_video")),
    (r"/v1/hooks/url", _POST, _by("display", "send_url")),
    (r"/v1/hooks/ui", _POST, _by("display", "display_ui")),
    # display-mcp's apps and files hooks, one tool per op.
    *((rf"/v1/hooks/apps/{op}", _POST, _by("display", tool)) for op, tool in (
        ("pin", "pin_app"), ("unpin", "unpin_app"), ("list", "list_apps"), ("rollback", "rollback_app"),
        ("describe", "describe_app"), ("export", "export_app"), ("import", "import_app"),
        ("push", "app_push"), ("state", "app_state"), ("open", "open_app"), ("deploy", "deploy_app"),
        ("check", "check_app"), ("screenshot", "screenshot_app"), ("status", "deploy_status"),
        ("preview", "preview_app"), ("logs", "app_logs"), ("restart", "restart_app"), ("purge", "purge_app"))),
    (r"/v1/hooks/files/pin", _POST, _by("display", "pin_file")),
    (r"/v1/hooks/files/unpin", _POST, _by("display", "unpin_file")),
    # display's live tools' REST twins (app_proxy.push_rest, state_rest), which the shell calls.
    (rf"/v1/apps/{_UUID}/push", _POST, _by("display", "app_push")),
    (rf"/v1/apps/{_UUID}/state", _PATCH, _by("display", "app_state")),
    # The session's own, read before a tool's own call.
    (r"/v1/session/current", _GET, _by("notifications-mcp", "create_notification") + _by("meetings-mcp", "start_meeting")),
    (r"/v1/execution-layers", _GET, _ENGINE_READERS + _by("agent-creator-mcp", "list_building_blocks")),
    # schedules-mcp.
    (r"/v1/tasks/scheduled", _POST, _by("schedules-mcp", "create_scheduled_task")),
    (r"/v1/tasks/one-time", _POST, _by("schedules-mcp", "create_one_time_task")),
    (r"/v1/continuations", _POST, _by("schedules-mcp", "schedule_continuation")),
    (r"/v1/tasks", _GET, _by("schedules-mcp", "list_tasks")),
    (r"/v1/tasks/runs", _GET, _by("schedules-mcp", "get_task_result", "get_task_history")),
    (rf"/v1/tasks/runs/{_ID}/stream", _GET, _by("schedules-mcp", "run_task")),
    (rf"/v1/tasks/runs/{_ID}/cancel", _POST, _by("schedules-mcp", "cancel_task_run")),
    (rf"/v1/tasks/(?!runs$|stats$){_ID}", _GET, _by("schedules-mcp", "get_task")),
    (rf"/v1/tasks/(?!runs$|stats$){_ID}", _DELETE, _by("schedules-mcp", "delete_task")),
    (rf"/v1/tasks/{_ID}/run", _POST, _by("schedules-mcp", "run_task")),
    (rf"/v1/tasks/{_ID}/pause", _POST, _by("schedules-mcp", "pause_task")),
    (rf"/v1/tasks/{_ID}/resume", _POST, _by("schedules-mcp", "resume_task")),
    (rf"/v1/tasks/{_ID}/edit", _POST, _by("schedules-mcp", "edit_task")),
    # notifications-mcp.
    (r"/v1/notifications", _POST, _by("notifications-mcp", "create_notification")),
    (r"/v1/notifications", _GET, _by("notifications-mcp", "list_notifications")),
    (rf"/v1/notifications/{_ID}", _DELETE, _by("notifications-mcp", "cancel_notification")),
    (rf"/v1/notifications/{_ID}/pause", _POST, _by("notifications-mcp", "pause_notification")),
    (rf"/v1/notifications/{_ID}/resume", _POST, _by("notifications-mcp", "resume_notification")),
    (rf"/v1/notifications/{_ID}/edit", _POST, _by("notifications-mcp", "edit_notification")),
    # triggers-mcp.
    (r"/v1/triggers", _POST, _by("triggers-mcp", "create_trigger")),
    (r"/v1/triggers", _GET, _by("triggers-mcp", "list_triggers")),
    (rf"/v1/triggers/{_ID}", _GET, _by("triggers-mcp", "get_trigger")),
    (rf"/v1/triggers/{_ID}", _DELETE, _by("triggers-mcp", "delete_trigger")),
    (rf"/v1/triggers/{_ID}/edit", _POST, _by("triggers-mcp", "edit_trigger")),
    (rf"/v1/triggers/{_ID}/pause", _POST, _by("triggers-mcp", "pause_trigger")),
    (rf"/v1/triggers/{_ID}/resume", _POST, _by("triggers-mcp", "resume_trigger")),
    (rf"/v1/triggers/{_ID}/fire", _POST, _by("triggers-mcp", "fire_trigger")),
    (r"/v1/subscriptions", _GET, _by("triggers-mcp", "list_subscriptions")),
    # meetings-mcp.
    (r"/v1/meetings", _POST, _by("meetings-mcp", "start_meeting")),
    (rf"/v1/meetings/{_ID}/start", _POST, _by("meetings-mcp", "start_meeting")),
    (rf"/v1/meetings/{_ID}/end", _POST, _by("meetings-mcp", "end_meeting")),
    (rf"/v1/meetings/{_ID}/propose-conclude", _POST, _by("meetings-mcp", "propose_conclude")),
    (rf"/v1/meetings/{_ID}/leave", _POST, _by("meetings-mcp", "leave_meeting")),
    # delegation-mcp.
    (r"/v1/delegation/spawn", _POST, _by("delegation-mcp", "delegate")),
    (r"/v1/delegation/send_files", _POST, _by("delegation-mcp", "send_files")),
    (r"/v1/delegation/attach_result_files", _POST, _by("delegation-mcp", "attach_result_files")),
    (r"/v1/delegation/adopt", _POST, _by("delegation-mcp", "adopt_project")),
    (r"/v1/delegation/sessions", _GET, _by("delegation-mcp", "list_sessions")),
    (rf"/v1/delegation/sessions/{_ID}/peek", _GET, _by("delegation-mcp", "peek_session")),
    (r"/v1/delegation/notebook", _GET, _by("delegation-mcp", "notebook_read")),
    (r"/v1/delegation/notebook", _POST, _by("delegation-mcp", "notebook_post")),
    (r"/v1/chats/agent-move", _POST, _by("delegation-mcp", "move_chat")),
    # memory-mcp; agent-config-mcp's memory settings.
    (r"/v1/internal/memory/op", _POST, _by("memory-mcp", "memory")),
    (rf"/v1/internal/memory/agent-settings/{_A}", _GET, _by("agent-config-mcp", "get_memory_settings")),
    (rf"/v1/internal/memory/agent-settings/{_A}", _PATCH,
     _by("agent-config-mcp", "update_user_memory_enabled", "update_agent_memory_enabled")),
    # checks-mcp.
    (rf"/v1/agents/{_A}/checks", _GET, _by("checks-mcp", "list_checks")),
    (rf"/v1/agents/{_A}/checks/{_ID}", _PUT, _by("checks-mcp", "set_check")),
    (rf"/v1/agents/{_A}/checks/{_ID}", _DELETE, _by("checks-mcp", "delete_check")),
    (rf"/v1/agents/{_A}/user-checks/{_ID}", _PUT, _by("checks-mcp", "create_private_check")),
    (rf"/v1/agents/{_A}/user-checks/{_ID}", _DELETE, _by("checks-mcp", "delete_private_check")),
    (r"/v1/checks/attached", _GET, _by("checks-mcp", "list_checks")),
    (r"/v1/checks/attach", _POST, _by("checks-mcp", "attach_check")),
    (r"/v1/checks/detach", _POST, _by("checks-mcp", "detach_check")),
    (r"/v1/checks/run", _POST, _by("checks-mcp", "run_check")),
    # agent-config-mcp.
    (rf"/v1/agents/{_A}/info", _GET, _by("agent-config-mcp", "get_agent_config") + _ENGINE_READERS),
    (rf"/v1/agents/{_A}", _PATCH, _by(
        "agent-config-mcp", "update_display_name", "update_description", "update_color", "update_default_scope",
        "set_visibility_mode", "update_default_model", "update_execution_layers", "update_default_layer",
        "update_default_execution_mode", "set_department")),
    (rf"/v1/agents/{_A}/files", _GET, _by("agent-config-mcp", "list_context_files")),
    (rf"/v1/agents/{_A}/files/config/agent\.md", _PUT, _by("agent-config-mcp", "update_persona")),
    (rf"/v1/agents/{_A}/outside", _GET, _by("agent-config-mcp", "get_outside_people", "set_outside_people")),
    (rf"/v1/agents/{_A}/outside", _PUT, _by("agent-config-mcp", "set_outside_people")),
    (rf"/v1/agents/{_A}/tool-limits", _GET, _by("agent-config-mcp", "get_tool_limits")),
    (rf"/v1/agents/{_A}/tool-limits", _PUT, _by("agent-config-mcp", "set_tool_limits")),
    (rf"/v1/agents/{_A}/complete-setup", _POST, _by("agent-config-mcp", "complete_setup")),
    (rf"/v1/agents/{_A}/knowledge-attachments", _GET, _by("agent-config-mcp", "list_knowledge_libraries")),
    (rf"/v1/agents/{_A}/knowledge-attachments", _PUT, _by("agent-config-mcp", "attach_knowledge_library")),
    (rf"/v1/agents/{_A}/knowledge-attachments/{_ID}", _DELETE, _by("agent-config-mcp", "detach_knowledge_library")),
    (rf"/v1/agents/{_A}/knowledge-library", _PUT, _by("agent-config-mcp", "share_knowledge_folder")),
    (r"/v1/knowledge-libraries", _GET, _by("agent-config-mcp", "list_knowledge_libraries")),
    (r"/v1/departments", _GET, _by("agent-config-mcp", "set_department") + _by("agent-creator-mcp", "list_departments")),
    (rf"/v1/agents/{_A}/reload-session", _POST, _by("agent-config-mcp", "reload_session", "switch_account")),
    # agent-creator-mcp; settings-mcp shares the cards.
    (r"/v1/community/(agents|departments)", _GET, _by("agent-creator-mcp", "search_community_agents")),
    (r"/v1/cards", _POST, _by("agent-creator-mcp", "propose_install", "propose_department_install", "propose_move")
     + _by("settings-mcp", "show_card")),
    (rf"/v1/cards/{_ID}", _GET, _by("agent-creator-mcp", "check_card") + _by("settings-mcp", "check_card")),
    (r"/v1/agents/local-template/building-blocks", _GET, _by("agent-creator-mcp", "list_building_blocks")),
    (r"/v1/agents/local-template/validate", _POST, _by("agent-creator-mcp", "validate_agent_template")),
    (r"/v1/agents/install-from-local-template", _POST, _by("agent-creator-mcp", "create_agent")),
    # mcps-mcp.
    (rf"/v1/agents/{_A}/mcps", _GET, _by("mcps-mcp", "list_enabled_mcps", "list_available_mcps", "request_mcp_install",
                                        "request_mcp_access", "disable_mcp_for_agent")),
    (rf"/v1/agents/{_A}/mcps", _PUT, _by("mcps-mcp", "request_mcp_install", "request_mcp_access", "disable_mcp_for_agent")),
    (rf"/v1/agents/{_A}/skills", _GET, _by("mcps-mcp", "list_community_skills", "request_skill_install")),
    (rf"/v1/agents/{_A}/skills/{_ID}", _PATCH, _by("mcps-mcp", "request_skill_install")),
    (rf"/v1/agents/{_A}/mcp-requests", _GET, _by("mcps-mcp", "get_request_status")),
    (rf"/v1/agents/{_A}/mcp-requests", _POST, _by("mcps-mcp", "request_skill_install", "request_mcp_install",
                                                 "request_mcp_access")),
    (rf"/v1/agents/{_A}/mcp-requests/{_ID}/cancel", _POST, _by("mcps-mcp", "cancel_my_request")),
    (r"/v1/community/mcps", _GET, _by("mcps-mcp", "list_community_mcps")),
    (r"/v1/community/skills", _GET, _by("mcps-mcp", "list_community_skills")),
    (r"/v1/mcps/local-package/validate", _POST, _by("mcps-mcp", "validate_mcp_package")),
    # settings-mcp, ssh-hosts, location-mcp.
    (r"/v1/settings/catalog", _GET, _by("settings-mcp", "find_settings", "get_setting")),
    (r"/v1/settings/card", _POST, _by("settings-mcp", "get_setting")),
    (r"/v1/cards/list", _POST, _by("settings-mcp", "show_list")),
    (rf"/v1/agents/{_A}/ssh-hosts", _GET, _by("ssh-hosts", "list_ssh_hosts")),
    (r"/v1/location/request", _POST, _by("location-mcp", "get_user_location")),
    # The media tools.
    (r"/v1/audio/tts/voices", _GET, _by("tts-mcp", "list_voices")),
    (r"/v1/audio/tts/voices/search", _GET, _by("tts-mcp", "search_voice_library")),
    (r"/v1/audio/tts/voices/add", _POST, _by("tts-mcp", "add_library_voice")),
    (r"/v1/audio/tts/generate", _POST, _by("tts-mcp", "generate_speech")),
    (r"/v1/audio/transcribe", _POST, _by("transcribe-mcp", "transcribe_audio_file", "transcribe_audio_url")),
    (r"/v1/images/temp", _POST, _by("image-search-mcp", "search_by_image")),
    # The phone and outside-person tools; the call-control body names the action.
    (r"/v1/phone/call-control", _POST, _by("call-control", "end_call", "transfer_call", "ask_initiating_agent")),
    (r"/v1/conversations", _GET, _by("conversations", "list_conversations")),
    (r"/v1/conversations/people", _GET, _by("conversations", "list_people")),
    (rf"/v1/conversations/(?!kind$|people$){_ID}", _GET, _by("conversations", "read_conversation")),
    (r"/v1/conversations/record", _POST, _by("conversations", "record_outcome")),
    (r"/v1/phone/calls", _POST, _by("make-call", "make_call")),
    (rf"/v1/phone/calls/{_ID}", _GET, _by("make-call", "get_call_status")),
    (rf"/v1/phone/calls/{_ID}/wait", _GET, _by("make-call", "wait_for_call")),
    (rf"/v1/phone/calls/{_ID}/answer", _POST, _by("make-call", "answer_call_question")),
    # The apps tool of an outside session; a session the tool is not attached
    # to carries no pair for it, so the agent's own shell calls apps as before.
    (rf"/v1/apps/{_UUID}/api(/.*)?", frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"}),
     _by("outside-apps", "use_app")),
]
ROUTE_TOOLS: list[tuple[re.Pattern, frozenset[str], _Tools]] = [
    (re.compile(pattern), methods, tools) for pattern, methods, tools in _ROUTE_TOOL_ROWS
]
# The Docker tools' callbacks (``server.proxy_callbacks``: file-tools,
# video-tools), the five POST routes a callback token reaches, on either
# listener (the container arrives on the public one).
CALLBACK_ROUTES = re.compile(r"/v1/hooks/(resolve-path|file|file-written|document-preview|images)")


def _bearer_jwt(request) -> str:
    """The bearer when it has a JWT's shape and is not the master key, else
    ``""`` (no bearer, the master key, a cookie-only request)."""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() != "bearer ":
        return ""
    token = auth[7:].strip()
    # Three dot-separated segments = JWT shape; the master key is not one.
    if not token or token.count(".") != 2 or config.is_master_key(token):
        return ""
    return token


def session_token_claims(request) -> dict | None:
    """The validated payload of a SESSION JWT presented as the bearer, else
    None (no bearer, the master key, a cookie-only request, or a token that
    does not validate — the endpoint's own auth answers those)."""
    token = _bearer_jwt(request)
    if not token:
        return None
    from auth.session_token import validate_session_token
    return validate_session_token(token)


def callback_token_claims(request) -> dict | None:
    """The validated payload of a Docker tool's callback token presented as
    the bearer, else None."""
    token = _bearer_jwt(request)
    if not token:
        return None
    from auth.session_token import validate_callback_token
    return validate_callback_token(token)


def is_session_endpoint_allowed(method: str, path: str, agent: str) -> bool:
    """True if a session token of ``agent`` may call ``method path``: a row
    of ``SESSION_ROUTES`` matched whole, its ``{A}`` equal to the token's
    own agent."""
    base = path.split("?", 1)[0]
    if has_traversal(base):
        return False
    verb = method.upper()
    for pattern, methods in SESSION_ROUTES:
        if verb not in methods:
            continue
        m = pattern.fullmatch(base)
        if m is None:
            continue
        if "agent" in m.groupdict() and (not agent or m.group("agent") != agent):
            continue
        return True
    return False


def route_tools(method: str, path: str) -> tuple[tuple[str, str], ...]:
    """The first-party tools that call ``method path`` (``ROUTE_TOOLS``,
    the first row that takes it), ``()`` for a route no row names."""
    base = path.split("?", 1)[0]
    verb = method.upper()
    for pattern, methods, tools in ROUTE_TOOLS:
        if verb in methods and pattern.fullmatch(base):
            return tools
    return ()


def session_route_refusal(method: str, path: str, ctx) -> str:
    """The sentence the session's tool limits refuse ``method path`` with,
    "" when it may go on: the route's tools against the registered
    context's pairs (``SecurityContext.tool_limits``), judged as the hook
    judges the tool call (``tool_limits.route_refusal``). A route no row
    names, and a session with no context, are open."""
    tools = route_tools(method, path)
    if not tools or ctx is None:
        return ""
    from services.mcp import tool_limits
    return tool_limits.route_refusal(getattr(ctx, "tool_limits", ()), tools)


def is_callback_endpoint_allowed(method: str, path: str) -> bool:
    """True if a Docker tool's callback token may call ``method path``: a
    POST to one of the five file callbacks."""
    base = path.split("?", 1)[0]
    return method.upper() == "POST" and CALLBACK_ROUTES.fullmatch(base) is not None


def is_external_endpoint_allowed(method: str, path: str, ctx=None) -> bool:
    """True if an external principal may call ``method path``: the
    allowlist for everyone; the tool callbacks and the gateway by the
    session's registered security context (``outside_grants``, the server
    keys the picks attached). No context: the allowlist alone."""
    from services.mcp import outside_picks
    base = path.split("?", 1)[0]
    if has_traversal(base):
        return False
    verb = method.upper()
    for pattern, methods in _ALLOWLIST:
        if pattern.match(base) and verb in methods:
            return True
    grants = getattr(ctx, "outside_grants", ()) if ctx is not None else ()
    attached = outside_picks.attached_keys(grants)
    if verb == "POST" and _TOOL_CALLBACKS.match(base):
        # Open with any attached tool (the tools that post them are stdio
        # and Docker alike), never for the channel's own tools alone.
        return bool(outside_picks.callback_keys(grants))
    m = _GATEWAY.match(base)
    if m and verb in _GATEWAY_METHODS:
        return m.group(1) in attached
    if verb == "POST" and _APP_METHOD.match(base):
        return outside_picks.APPS_SERVER in attached
    return False


def is_wrapup_endpoint_allowed(method: str, path: str) -> bool:
    """True if the wrap-up after a conversation may call ``method path``:
    its list, the tool callbacks and the credential gateway (which binds the
    token's session to the credentials of the servers it attached)."""
    base = path.split("?", 1)[0]
    if has_traversal(base):
        return False
    verb = method.upper()
    if any(pattern.match(base) and verb in methods for pattern, methods in _WRAPUP_ALLOWLIST):
        return True
    if verb == "POST" and _TOOL_CALLBACKS.match(base):
        return True
    return bool(_GATEWAY.match(base)) and verb in _GATEWAY_METHODS


# An app button's press that runs below the button's identity
# (``SecurityContext.press_reach``, ``services/apps/headless_exec.press_reach``):
# the routes its tool needs to find, show and write its files and to reach its
# credentials, and nothing that acts on the platform as the agent
# (memory, pins, tasks, notifications, triggers, delegation, the app hooks).
_PRESS_ALLOWLIST: list[tuple[re.Pattern, frozenset[str]]] = [
    (re.compile(r"^/v1/hooks/(resolve-path|resolve-tool-arg-paths)$"),
     _POST),
]
PRESS_BLOCKED_DETAIL = ("This button cannot use that tool when you press it. On a personal app, ask its "
                        "owner. On a shared app, ask an editor of the agent or an admin.")


def is_press_endpoint_allowed(method: str, path: str) -> bool:
    """True if a press confined below its button's identity may call
    ``method path``: its list, the tool callbacks and the credential
    gateway."""
    base = path.split("?", 1)[0]
    if has_traversal(base):
        return False
    verb = method.upper()
    if any(pattern.match(base) and verb in methods for pattern, methods in _PRESS_ALLOWLIST):
        return True
    if verb == "POST" and _TOOL_CALLBACKS.match(base):
        return True
    return bool(_GATEWAY.match(base)) and verb in _GATEWAY_METHODS
