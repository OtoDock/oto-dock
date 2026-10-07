"""The tool schemas of agent-config-mcp — pure data: every tool's
``description`` and ``inputSchema`` as the MCP ``tools/list`` serves them.
``server.py`` deep-copies the table and fills the engine-typed tools from the
platform's catalog at list time (``_engine_schemas``); nothing here reads
the platform. Kept beside ``server.py`` so the server stays readable — the
tool bodies live there, the schemas here.
"""

from __future__ import annotations

from typing import Any

_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "get_agent_config": {
        "description": (
            "Inspect this agent's current settings (display name, description, "
            "color, default model, execution layers, community-template "
            "provenance). Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "list_available_models": {
        "description": (
            "List the models available on the platform's enabled execution "
            "layers, grouped by layer and ranked by capability tier (1 frontier "
            "… 4 fast; untiered), each with what it is good at. Use it to "
            "choose a default by tier, and before `update_default_model` for "
            "the valid model IDs."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "update_display_name": {
        "description": (
            "Change this agent's display name (shown in agent picker, chat "
            "header, and cards). The slug stays the same — only the human "
            "label changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "new_name": {
                    "type": "string",
                    "minLength": 1, "maxLength": 80,
                    "description": "1–80 chars; any printable Unicode.",
                },
            },
            "required": ["new_name"],
            "additionalProperties": False,
        },
    },
    "update_description": {
        "description": "Change this agent's description (1–500 chars).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "new_description": {
                    "type": "string",
                    "minLength": 1, "maxLength": 500,
                },
            },
            "required": ["new_description"],
            "additionalProperties": False,
        },
    },
    "update_color": {
        "description": (
            "Change this agent's accent color (used in UI badges + cards). "
            "Pass a hex code like `#3B82F6`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hex_color": {
                    "type": "string",
                    "pattern": "^#[0-9A-Fa-f]{6}$",
                },
            },
            "required": ["hex_color"],
            "additionalProperties": False,
        },
    },
    "update_default_model": {
        "description": (
            "Set this agent's default model. Must be one of the model_id "
            "values from `list_available_models`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string"},
            },
            "required": ["model_id"],
            "additionalProperties": False,
        },
    },
    # The two engine-typed tools name no engine here: `{engines}` and the
    # `enum` are filled from the platform's catalog at tools/list
    # (_engine_schemas). Without a catalog the enum is left out — the proxy
    # validates every PATCH — and the description points at the listing tool.
    "update_execution_layers": {
        "description": (
            "Set which execution layers this agent can use. Must be a non-"
            "empty subset of the platform's engines: {engines}. The "
            "current default layer is preserved at position 0 if it survives "
            "the change; otherwise the new layers[0] becomes the default."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "layers": {
                    "type": "array", "minItems": 1,
                    "items": {"type": "string"},
                },
            },
            "required": ["layers"],
            "additionalProperties": False,
        },
    },
    "update_default_layer": {
        "description": (
            "Set the default execution layer (one of the platform's engines: "
            "{engines}). Must already be in this agent's execution_paths list."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "layer": {"type": "string"},
            },
            "required": ["layer"],
            "additionalProperties": False,
        },
    },
    "update_default_scope": {
        "description": (
            "Set this agent's `default_scope` — `user` for personal-leaning "
            "agents (tasks / notifications / memories default to the user), "
            "`agent` for operational agents where most work is shared across "
            "all users of this agent. (For the full mode, prefer "
            "`set_visibility_mode`.) If this makes the agent Shared only while "
            "people hold the viewer or contributor role, it is refused: a manager "
            "confirms that switch in the agent's settings."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "default_scope": {"type": "string", "enum": ["user", "agent"]},
            },
            "required": ["default_scope"],
            "additionalProperties": False,
        },
    },
    "update_default_execution_mode": {
        "description": (
            "Set this agent's default SESSION MODE for new chats & tasks — "
            "`interactive` runs the native CLI as a live terminal (TUI), `-p` "
            "is the normal headless stream, `` (empty) unsets it (platform "
            "default). Only valid when this agent's DEFAULT model runs on an "
            "engine with a native terminal ({interactive}); the other engines "
            "cannot run interactively. Meetings always run headless regardless."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["interactive", "-p", ""]},
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
    },
    "set_visibility_mode": {
        "description": (
            "Set this agent's visibility mode — how it relates to users. One of: "
            "`personal_shared` (each person private + a shared team space), "
            "`shared_personal` (one shared space + personal files too), "
            "`personal_only` (fully private per person, NO shared space), "
            "`shared_only` (ONE shared workspace + ONE shared chat history for "
            "everyone, no personal space). Changing modes never deletes folders. "
            "Shared only takes the editor role or above to chat, so switching to it "
            "while people hold the viewer or contributor role is refused here: a "
            "manager confirms, in the agent's settings, the people whose assignment "
            "is removed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["personal_shared", "shared_personal",
                             "personal_only", "shared_only"],
                },
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
    },
    "list_context_files": {
        "description": (
            "List files auto-loaded into this agent's context from "
            "`config/context/`. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "update_persona": {
        "description": (
            "Replace this agent's persona — `config/agent.md`, the first "
            "thing loaded into every session. Use it when the user says who "
            "this agent should be, what it is for, how it should work, or "
            "what standards it holds; facts about people, projects and "
            "state go to memory instead. REPLACES the whole file, so send "
            "the complete persona (the current one is at the top of your "
            "own prompt). Write role, working style, judgment and "
            "boundaries in the second person — never capability lists (each "
            "tool ships its own instructions). Takes effect in the next new "
            "session. Requires manager/admin role with a user present."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The complete new persona in markdown. Usually 20–60 "
                        "lines, starting with an `# <Agent name>` heading."
                    ),
                },
            },
            "required": ["content"],
            "additionalProperties": False,
        },
    },
    "get_memory_settings": {
        "description": (
            "Inspect this agent's memory toggle overrides (per-agent layer "
            "above the platform-wide default). Shows the state of "
            "`user_memory_enabled` and `agent_memory_enabled`. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "update_user_memory_enabled": {
        "description": (
            "Enable or disable per-user memory for this agent (the "
            "`/memories/user/` scope). Overrides the platform-wide default. "
            "Disabling stops the user-memory prompt section from injecting "
            "and rejects `memory` tool writes to that scope."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
            "additionalProperties": False,
        },
    },
    "update_agent_memory_enabled": {
        "description": (
            "Enable or disable the shared agent memory for this agent "
            "(the `/memories/agent/` scope). Overrides the platform-wide "
            "default. Disabling stops the agent-memory section from "
            "injecting in every session and rejects `memory` tool writes "
            "to that scope."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
            "additionalProperties": False,
        },
    },
    "complete_setup": {
        "description": (
            "Mark this agent's post-install setup complete. Two scopes: "
            "'agent' removes the agent-wide `config/context/setup.md` "
            "(manager-level; call ONLY when every checklist item in setup.md "
            "is verified done), 'user' removes the current user's own "
            "`user-setup.md` onboarding from their context (call when their "
            "personal onboarding is finished or they decline it). Omit scope "
            "when only one applies — it is resolved automatically."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Optional one-line summary of what was configured (for the audit trail).",
                },
                "scope": {
                    "type": "string",
                    "enum": ["agent", "user"],
                    "description": "Which setup to complete; omit to auto-resolve when only one applies.",
                },
            },
            "additionalProperties": False,
        },
    },
    "list_knowledge_libraries": {
        "description": (
            "Show this agent's shared-knowledge state: which libraries it "
            "shares (whole folder or a knowledge subfolder, and to whom), "
            "which libraries are attached here (at "
            "/knowledge/shared/<source>/<subdir>/), and — for platform "
            "admins/creators — every library on the installation. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
    },
    "share_knowledge_folder": {
        "description": (
            "Share (or un-share) THIS agent's knowledge folder — or one of "
            "its subfolders — as an installation-wide knowledge library that "
            "other agents can attach. An agent can share several disjoint "
            "subfolders as independent libraries. Platform admins/creators "
            "only — the server rejects everyone else. Un-sharing detaches "
            "that library's consumers and removes their mirrors."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "enable": {
                    "type": "boolean",
                    "description": "true = share, false = un-share.",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "The library's display name, e.g. 'Brand Guidelines' "
                        "— how people pick it out in the dashboard, and the "
                        "name of its bulletin file. REQUIRED when "
                        "enable=true, ignored when un-sharing. Re-share with "
                        "a different name to rename it. Never part of the "
                        "mirror path (consumers read "
                        "/knowledge/shared/<this-agent>/<subdir>/)."
                    ),
                },
                "subdir": {
                    "type": "string",
                    "description": (
                        "Knowledge subfolder to share, relative to this "
                        "agent's knowledge/ root (e.g. 'marketing' or "
                        "'docs/public'). Empty/omitted = the whole folder. "
                        "Identifies the library on un-share too. Subtrees "
                        "must be disjoint from the agent's other libraries."
                    ),
                },
            },
            "required": ["enable"],
            "additionalProperties": False,
        },
    },
    "attach_knowledge_library": {
        "description": (
            "Attach a shared knowledge library to THIS agent — its content "
            "mirrors to /knowledge/shared/<source>/<subdir>/ (read-only "
            "unless writable). Also updates the writable flag of an "
            "existing attachment. Platform admins/creators only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_agent": {
                    "type": "string",
                    "description": "Slug of the agent sharing the library.",
                },
                "subdir": {
                    "type": "string",
                    "description": (
                        "The library's subfolder as shown by "
                        "list_knowledge_libraries; empty/omitted for a "
                        "whole-folder library."
                    ),
                },
                "writable": {
                    "type": "boolean",
                    "description": "true = edits here flow back to the source library. Default false (read-only).",
                },
            },
            "required": ["source_agent"],
            "additionalProperties": False,
        },
    },
    "detach_knowledge_library": {
        "description": (
            "Detach a shared knowledge library from THIS agent and remove "
            "its mirror. Platform admins/creators only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_agent": {
                    "type": "string",
                    "description": "Slug of the attached library's source agent.",
                },
                "subdir": {
                    "type": "string",
                    "description": (
                        "The attachment's library subfolder; empty/omitted "
                        "for a whole-folder library."
                    ),
                },
            },
            "required": ["source_agent"],
            "additionalProperties": False,
        },
    },
    "set_department": {
        "description": (
            "Assign THIS agent to a department + level on the company map "
            "(names resolved case-insensitively), or clear the assignment "
            "by passing an empty department. Wires delegation edges per "
            "the department's structure. Platform admins/creators only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "department": {
                    "type": "string",
                    "description": "Department name or id; empty string clears the assignment.",
                },
                "level": {
                    "type": "string",
                    "description": "Level name or id within the department; may be omitted when the department has exactly one level.",
                },
            },
            "required": ["department"],
            "additionalProperties": False,
        },
    },
}
