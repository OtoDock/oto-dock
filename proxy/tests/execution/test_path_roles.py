"""Tests for path role resolution (proxy/services/path_roles.py).

Covers every (role × scope × access-level) combination plus the manifest
path_env parser (shorthand + multi-value forms). Pure unit tests — no DB
required.
"""

import pytest

from services import path_roles
from services.path_roles import (
    SESSION_ID_TOKEN,
    expand_session_id,
    expand_session_id_in_env,
    get_multi_value_envs,
    resolve_path_env,
    resolve_path_env_entry,
    resolve_role,
)


# ---------------------------------------------------------------------------
# resolve_role: workspace
# ---------------------------------------------------------------------------


def test_workspace_user_scoped():
    assert resolve_role("workspace", username="alice") == "/users/alice/workspace"


def test_workspace_agent_scoped():
    assert resolve_role("workspace", username="") == "/workspace"


def test_workspace_subpath_appended():
    """Optional subpath is appended under the workspace dir."""
    assert resolve_role("workspace", username="alice", subpath="downloads/google-drive") == "/users/alice/workspace/downloads/google-drive"


def test_workspace_subpath_appended_agent_scoped():
    assert resolve_role("workspace", username="", subpath="downloads") == "/workspace/downloads"


def test_workspace_subpath_strips_leading_slash():
    assert resolve_role("workspace", username="alice", subpath="/downloads") == "/users/alice/workspace/downloads"


def test_workspace_empty_subpath_equivalent_to_no_subpath():
    assert resolve_role("workspace", username="alice", subpath="") == "/users/alice/workspace"


def test_workspace_ignores_user_role():
    """workspace resolution doesn't depend on access level."""
    for role in ("viewer", "manager", "admin", ""):
        assert resolve_role("workspace", username="alice", user_role=role) == "/users/alice/workspace"


# ---------------------------------------------------------------------------
# resolve_role: user_root
# ---------------------------------------------------------------------------


def test_user_root_user_scoped():
    assert resolve_role("user_root", username="alice") == "/users/alice"


def test_user_root_agent_scoped_returns_empty():
    assert resolve_role("user_root", username="") == ""


def test_user_root_ignores_user_role():
    """user_root depends on scope (username), not access level."""
    for role in ("viewer", "manager", "admin"):
        assert resolve_role("user_root", username="alice", user_role=role) == "/users/alice"


def test_user_root_subpath_appended():
    """Optional subpath is appended under the user root dir."""
    assert resolve_role("user_root", username="alice", subpath="workspace/downloads") == "/users/alice/workspace/downloads"


def test_user_root_subpath_agent_scoped_still_empty():
    """user_root is empty for agent-scoped sessions even with subpath."""
    assert resolve_role("user_root", username="", subpath="anything") == ""


# ---------------------------------------------------------------------------
# resolve_role: shared_workspace
# ---------------------------------------------------------------------------


def test_shared_workspace_manager_user_scoped():
    assert resolve_role("shared_workspace", username="alice", user_role="manager") == "/workspace"


def test_shared_workspace_admin_user_scoped():
    assert resolve_role("shared_workspace", username="alice", user_role="admin") == "/workspace"


def test_shared_workspace_viewer_user_scoped_empty():
    """Viewers have no access to the agent-shared workspace mount."""
    assert resolve_role("shared_workspace", username="alice", user_role="viewer") == ""


def test_shared_workspace_unknown_role_user_scoped_empty():
    """Unknown access level treated as restrictive (no access)."""
    assert resolve_role("shared_workspace", username="alice", user_role="") == ""


def test_shared_workspace_agent_scoped():
    """Agent-scoped sessions have /workspace as their only mount."""
    assert resolve_role("shared_workspace", username="") == "/workspace"


def test_shared_workspace_agent_scoped_role_ignored():
    """For agent-scoped, user_role is irrelevant — always /workspace."""
    for role in ("viewer", "manager", "admin", ""):
        assert resolve_role("shared_workspace", username="", user_role=role) == "/workspace"


def test_shared_workspace_subpath_appended_for_manager():
    """Optional subpath is appended for users with access."""
    assert resolve_role("shared_workspace", username="alice", user_role="manager", subpath="shared/cache") == "/workspace/shared/cache"


def test_shared_workspace_subpath_ignored_for_viewer():
    """Viewers still get empty string — subpath doesn't override access gating."""
    assert resolve_role("shared_workspace", username="alice", user_role="viewer", subpath="anything") == ""


def test_shared_workspace_subpath_appended_agent_scoped():
    assert resolve_role("shared_workspace", username="", subpath="dl") == "/workspace/dl"


# ---------------------------------------------------------------------------
# resolve_role: config (changed semantics)
# ---------------------------------------------------------------------------


def test_config_manager_user_scoped():
    assert resolve_role("config", username="alice", user_role="manager") == "/config"


def test_config_admin_user_scoped():
    assert resolve_role("config", username="alice", user_role="admin") == "/config"


def test_config_viewer_user_scoped_empty():
    """Viewers don't have /config mounted."""
    assert resolve_role("config", username="alice", user_role="viewer") == ""


def test_config_agent_scoped_empty():
    """Agent-scoped sessions don't have /config (no longer falls back)."""
    assert resolve_role("config", username="") == ""


def test_config_agent_scoped_with_role_still_empty():
    """Even with a recognized role, agent-scoped has no /config."""
    for role in ("manager", "admin"):
        assert resolve_role("config", username="", user_role=role) == ""


def test_config_unknown_role_empty():
    assert resolve_role("config", username="alice", user_role="") == ""


# ---------------------------------------------------------------------------
# resolve_role: credentials_dir (moved under .credentials/ subdir)
# ---------------------------------------------------------------------------


def test_credentials_dir_user_scoped():
    """User-scope tokens land under the user's own .credentials/ dir."""
    assert resolve_role(
        "credentials_dir", username="alice", subpath="google-tokens",
    ) == "/users/alice/.credentials/google-tokens"


def test_credentials_dir_agent_scoped():
    """Agent-scope tokens land under the agent's knowledge/.credentials/ —
    universal across all agent-scope sessions (phone/task/trigger).
    """
    assert resolve_role(
        "credentials_dir", username="", subpath="google-tokens",
    ) == "/knowledge/.credentials/google-tokens"


def test_credentials_dir_strips_leading_slash():
    """Subpath with leading slash should not produce double slash."""
    assert resolve_role(
        "credentials_dir", username="alice", subpath="/google-tokens",
    ) == "/users/alice/.credentials/google-tokens"


def test_credentials_dir_requires_subpath():
    with pytest.raises(ValueError, match="credentials_dir.*subpath"):
        resolve_role("credentials_dir", username="alice")


def test_credentials_dir_requires_subpath_agent_scope():
    with pytest.raises(ValueError, match="credentials_dir.*subpath"):
        resolve_role("credentials_dir", username="")


def test_credentials_dir_ignores_user_role():
    """credentials_dir doesn't gate on access level."""
    for role in ("viewer", "editor", "manager", "admin", ""):
        assert resolve_role(
            "credentials_dir", username="alice", subpath="google-tokens", user_role=role,
        ) == "/users/alice/.credentials/google-tokens"


# ---------------------------------------------------------------------------
# resolve_role: knowledge_dir (new role)
# ---------------------------------------------------------------------------


def test_knowledge_dir_user_scoped():
    """User-scope sessions read knowledge from /knowledge — the SAME path
    agent-scope sessions read from. Universal regardless of session scope.
    """
    assert resolve_role("knowledge_dir", username="alice") == "/knowledge"


def test_knowledge_dir_agent_scoped():
    """Agent-scope sessions also see /knowledge."""
    assert resolve_role("knowledge_dir", username="") == "/knowledge"


def test_knowledge_dir_subpath_appended():
    assert resolve_role(
        "knowledge_dir", username="alice", subpath="refs/templates",
    ) == "/knowledge/refs/templates"


def test_knowledge_dir_subpath_strips_leading_slash():
    assert resolve_role(
        "knowledge_dir", username="alice", subpath="/refs",
    ) == "/knowledge/refs"


def test_knowledge_dir_ignores_user_role():
    """knowledge_dir resolves to the same path regardless of access level
    (bwrap decides RW vs RO — see core/sandbox/sandbox.py)."""
    for role in ("viewer", "editor", "manager", "admin", ""):
        assert resolve_role(
            "knowledge_dir", username="alice", user_role=role,
        ) == "/knowledge"


# ---------------------------------------------------------------------------
# resolve_role: editor in WORKSPACE_TIER
# ---------------------------------------------------------------------------


def test_shared_workspace_editor_user_scoped():
    """Editor now resolves shared_workspace alongside manager/admin.
    The bwrap mount decides RW vs RO — editor gets RW, viewer would get nothing.
    """
    assert resolve_role(
        "shared_workspace", username="alice", user_role="editor",
    ) == "/workspace"


def test_config_editor_user_scoped_empty():
    """Editor does NOT see /config (owner-only role)."""
    assert resolve_role("config", username="alice", user_role="editor") == ""


def test_config_owner_only_resolves_for_manager():
    """Sanity: only manager + admin get /config resolved."""
    assert resolve_role("config", username="alice", user_role="manager") == "/config"
    assert resolve_role("config", username="alice", user_role="admin") == "/config"
    assert resolve_role("config", username="alice", user_role="viewer") == ""
    assert resolve_role("config", username="alice", user_role="editor") == ""


# ---------------------------------------------------------------------------
# resolve_role: error paths
# ---------------------------------------------------------------------------


def test_unknown_role_raises():
    with pytest.raises(ValueError, match="Unknown path role"):
        resolve_role("nonexistent", username="alice")


def test_roles_constant_lists_all_roles():
    """Lock-step contract — keep ROLES list aligned with resolver branches."""
    assert path_roles.ROLES == (
        "workspace",
        "user_root",
        "shared_workspace",
        "config",
        "knowledge_dir",
        "credentials_dir",
    )


# ---------------------------------------------------------------------------
# resolve_path_env: dict-based input (matches manifest JSON shape)
# ---------------------------------------------------------------------------


def test_resolve_path_env_with_dict_input():
    raw = {
        "IMAGE_SAVE_DIR": {"role": "workspace"},
        "MY_CREDS": {"role": "credentials_dir", "subpath": "google-tokens"},
    }
    result = resolve_path_env(raw, username="alice", user_role="manager")
    assert result == {
        "IMAGE_SAVE_DIR": "/users/alice/workspace",
        "MY_CREDS": "/users/alice/.credentials/google-tokens",
    }


def test_resolve_path_env_agent_scoped():
    raw = {
        "IMAGE_SAVE_DIR": {"role": "workspace"},
        "MY_CREDS": {"role": "credentials_dir", "subpath": "google-tokens"},
    }
    result = resolve_path_env(raw, username="")
    assert result == {
        "IMAGE_SAVE_DIR": "/workspace",
        "MY_CREDS": "/knowledge/.credentials/google-tokens",
    }


def test_resolve_path_env_skips_invalid_role_silently():
    """Empty role entries return empty values (parser logs warnings earlier)."""
    raw = {"FOO": {"role": ""}, "BAR": {"role": "workspace"}}
    result = resolve_path_env(raw, username="alice")
    assert result == {"FOO": "", "BAR": "/users/alice/workspace"}


def test_resolve_path_env_with_dataclass_input():
    """Accepts PathEnvDecl objects too (production shape from McpManifest)."""
    from services.mcp.mcp_registry import PathEnvDecl
    raw = {"IMAGE_SAVE_DIR": PathEnvDecl(role="workspace")}
    result = resolve_path_env(raw, username="alice")
    assert result == {"IMAGE_SAVE_DIR": "/users/alice/workspace"}


# ---------------------------------------------------------------------------
# resolve_path_env_entry: multi-value entries
# ---------------------------------------------------------------------------


def test_resolve_path_env_entry_shorthand_dict():
    decl = {"role": "workspace"}
    assert resolve_path_env_entry(decl, username="alice") == "/users/alice/workspace"


def test_resolve_path_env_entry_multivalue_manager_user_scoped():
    decl = {
        "values": [
            {"role": "user_root"},
            {"role": "shared_workspace"},
            {"role": "config"},
        ],
        "join": ":",
    }
    result = resolve_path_env_entry(decl, username="alice", user_role="manager")
    assert result == "/users/alice:/workspace:/config"


def test_resolve_path_env_entry_multivalue_viewer_drops_empties():
    """Viewer's user_root resolves to /users/{u}; shared_workspace and config are empty."""
    decl = {
        "values": [
            {"role": "user_root"},
            {"role": "shared_workspace"},
            {"role": "config"},
        ],
        "join": ":",
    }
    result = resolve_path_env_entry(decl, username="viewer1", user_role="viewer")
    assert result == "/users/viewer1"


def test_resolve_path_env_entry_multivalue_agent_scoped():
    """Agent-scoped: user_root empty; shared_workspace + config: only shared_workspace returns."""
    decl = {
        "values": [
            {"role": "user_root"},
            {"role": "shared_workspace"},
            {"role": "config"},
        ],
        "join": ":",
    }
    result = resolve_path_env_entry(decl, username="", user_role="")
    assert result == "/workspace"


def test_resolve_path_env_entry_multivalue_custom_separator():
    decl = {
        "values": [
            {"role": "user_root"},
            {"role": "shared_workspace"},
        ],
        "join": ",",
    }
    result = resolve_path_env_entry(decl, username="alice", user_role="admin")
    assert result == "/users/alice,/workspace"


def test_resolve_path_env_entry_multivalue_with_subpaths():
    """Each item carries its own subpath."""
    decl = {
        "values": [
            {"role": "credentials_dir", "subpath": "tokens"},
            {"role": "credentials_dir", "subpath": "keys"},
        ],
        "join": ":",
    }
    result = resolve_path_env_entry(decl, username="alice")
    assert result == "/users/alice/.credentials/tokens:/users/alice/.credentials/keys"


def test_resolve_path_env_entry_multivalue_all_empty_returns_empty():
    """If every item resolves empty, the entry is empty."""
    decl = {
        "values": [
            {"role": "user_root"},  # empty for agent-scoped
            {"role": "config"},     # empty for agent-scoped
        ],
        "join": ":",
    }
    result = resolve_path_env_entry(decl, username="")
    assert result == ""


def test_resolve_path_env_entry_dataclass_multivalue():
    from services.mcp.mcp_registry import PathEnvDecl, PathEnvValueRef
    decl = PathEnvDecl(
        values=[
            PathEnvValueRef(role="user_root"),
            PathEnvValueRef(role="shared_workspace"),
        ],
        join=":",
    )
    result = resolve_path_env_entry(decl, username="alice", user_role="manager")
    assert result == "/users/alice:/workspace"


# ---------------------------------------------------------------------------
# get_multi_value_envs
# ---------------------------------------------------------------------------


def test_get_multi_value_envs_mixed():
    """Returns separator only for multi-value entries; ignores shorthand."""
    from services.mcp.mcp_registry import PathEnvDecl, PathEnvValueRef
    path_env = {
        "IMAGE_SAVE_DIR": PathEnvDecl(role="workspace"),
        "ALLOWED_FILE_DIRS": PathEnvDecl(
            values=[PathEnvValueRef(role="user_root")], join=":",
        ),
        "OTHER_PATHS": PathEnvDecl(
            values=[PathEnvValueRef(role="workspace")], join=",",
        ),
    }
    result = get_multi_value_envs(path_env)
    assert result == {"ALLOWED_FILE_DIRS": ":", "OTHER_PATHS": ","}


def test_get_multi_value_envs_no_multi_returns_empty():
    from services.mcp.mcp_registry import PathEnvDecl
    assert get_multi_value_envs({"X": PathEnvDecl(role="workspace")}) == {}


# ---------------------------------------------------------------------------
# expand_session_id: late-binding token
# ---------------------------------------------------------------------------


def test_expand_session_id_replaces_token():
    val = f"/users/alice/workspace/.screenshots/{SESSION_ID_TOKEN}"
    assert expand_session_id(val, "abc-123") == "/users/alice/workspace/.screenshots/abc-123"


def test_expand_session_id_idempotent_when_no_token():
    val = "/users/alice/workspace"
    assert expand_session_id(val, "abc-123") == "/users/alice/workspace"


def test_expand_session_id_in_env_full_dict():
    env = {
        "IMAGE_SAVE_DIR": "/users/alice/workspace",
        "SCREENSHOTS": f"/users/alice/workspace/.screenshots/{SESSION_ID_TOKEN}",
        "OTHER": "no token here",
        "PROXY_API_KEY": "secret",
    }
    result = expand_session_id_in_env(env, "sid-xyz")
    assert result["SCREENSHOTS"] == "/users/alice/workspace/.screenshots/sid-xyz"
    assert result["IMAGE_SAVE_DIR"] == "/users/alice/workspace"
    assert result["OTHER"] == "no token here"
    assert result["PROXY_API_KEY"] == "secret"


def test_expand_session_id_in_env_returns_new_dict():
    """Doesn't mutate the input."""
    env = {"X": SESSION_ID_TOKEN}
    expand_session_id_in_env(env, "y")
    assert env == {"X": SESSION_ID_TOKEN}  # untouched


def test_expand_session_id_in_env_skips_non_strings():
    """Non-string values pass through unchanged."""
    env = {"X": SESSION_ID_TOKEN, "Y": 42, "Z": None}
    result = expand_session_id_in_env(env, "sid")
    assert result == {"X": "sid", "Y": 42, "Z": None}


# ---------------------------------------------------------------------------
# Cross-role: viewer / manager / admin correctness vs bwrap mounts
# ---------------------------------------------------------------------------


def test_viewer_workspace_lands_inside_user_mount():
    """Viewers only have /users/{own}/ mounted; workspace role must land there."""
    result = resolve_role("workspace", username="viewer1")
    assert result.startswith("/users/viewer1/")


def test_viewer_credentials_lands_inside_user_mount():
    result = resolve_role(
        "credentials_dir", username="viewer1", subpath=".some_creds",
    )
    assert result.startswith("/users/viewer1/")


def test_viewer_accessible_roots_via_multivalue():
    """An ALLOWED_FILE_DIRS-style multi-value yields exactly the viewer's mount set."""
    decl = {
        "values": [
            {"role": "user_root"},
            {"role": "shared_workspace"},
            {"role": "config"},
        ],
        "join": ":",
    }
    result = resolve_path_env_entry(decl, username="viewer1", user_role="viewer")
    # Viewer's only bwrap mount is /users/{own}/.
    assert result == "/users/viewer1"


def test_manager_accessible_roots_via_multivalue():
    """Manager: full mount set."""
    decl = {
        "values": [
            {"role": "user_root"},
            {"role": "shared_workspace"},
            {"role": "config"},
        ],
        "join": ":",
    }
    result = resolve_path_env_entry(decl, username="alice", user_role="manager")
    assert result == "/users/alice:/workspace:/config"


def test_agent_scoped_accessible_roots_via_multivalue():
    """Agent-scoped: only /workspace."""
    decl = {
        "values": [
            {"role": "user_root"},
            {"role": "shared_workspace"},
            {"role": "config"},
        ],
        "join": ":",
    }
    result = resolve_path_env_entry(decl, username="", user_role="")
    assert result == "/workspace"


# ---------------------------------------------------------------------------
# Manifest parsing (mcp_registry._parse_path_env)
# ---------------------------------------------------------------------------


def test_parse_path_env_valid_shorthand_entries():
    from services.mcp.mcp_registry import _parse_path_env

    raw = {
        "IMAGE_SAVE_DIR": {"role": "workspace"},
        "CREDS": {"role": "credentials_dir", "subpath": ".keys"},
    }
    result = _parse_path_env(raw, "test-mcp")
    assert "IMAGE_SAVE_DIR" in result
    assert result["IMAGE_SAVE_DIR"].role == "workspace"
    assert result["IMAGE_SAVE_DIR"].subpath == ""
    assert result["IMAGE_SAVE_DIR"].is_multi is False
    assert "CREDS" in result
    assert result["CREDS"].role == "credentials_dir"
    assert result["CREDS"].subpath == ".keys"


def test_parse_path_env_drops_unknown_role():
    from services.mcp.mcp_registry import _parse_path_env

    raw = {
        "GOOD": {"role": "workspace"},
        "BAD": {"role": "made_up_role"},
    }
    result = _parse_path_env(raw, "test-mcp")
    assert "GOOD" in result
    assert "BAD" not in result


def test_parse_path_env_drops_credentials_dir_without_subpath():
    from services.mcp.mcp_registry import _parse_path_env

    raw = {
        "BAD": {"role": "credentials_dir"},  # missing subpath
        "GOOD": {"role": "credentials_dir", "subpath": ".keys"},
    }
    result = _parse_path_env(raw, "test-mcp")
    assert "BAD" not in result
    assert "GOOD" in result


def test_parse_path_env_drops_non_dict_values():
    from services.mcp.mcp_registry import _parse_path_env

    raw = {
        "STRING_VALUE": "not a dict",
        "GOOD": {"role": "workspace"},
    }
    result = _parse_path_env(raw, "test-mcp")
    assert "STRING_VALUE" not in result
    assert "GOOD" in result


def test_parse_path_env_handles_empty_input():
    from services.mcp.mcp_registry import _parse_path_env
    assert _parse_path_env({}, "test-mcp") == {}
    assert _parse_path_env(None, "test-mcp") == {}  # type: ignore


# ---------------------------------------------------------------------------
# Manifest parsing — multi-value entries
# ---------------------------------------------------------------------------


def test_parse_path_env_multivalue_basic():
    from services.mcp.mcp_registry import _parse_path_env

    raw = {
        "ALLOWED_FILE_DIRS": {
            "values": [
                {"role": "user_root"},
                {"role": "shared_workspace"},
                {"role": "config"},
            ],
            "join": ":",
        },
    }
    result = _parse_path_env(raw, "test-mcp")
    assert "ALLOWED_FILE_DIRS" in result
    decl = result["ALLOWED_FILE_DIRS"]
    assert decl.is_multi is True
    assert decl.role == ""
    assert len(decl.values) == 3
    assert decl.values[0].role == "user_root"
    assert decl.values[1].role == "shared_workspace"
    assert decl.values[2].role == "config"
    assert decl.join == ":"


def test_parse_path_env_multivalue_default_join_colon():
    from services.mcp.mcp_registry import _parse_path_env

    raw = {
        "X": {
            "values": [{"role": "workspace"}],
        },
    }
    result = _parse_path_env(raw, "test-mcp")
    assert result["X"].join == ":"


def test_parse_path_env_multivalue_custom_join():
    from services.mcp.mcp_registry import _parse_path_env

    raw = {
        "X": {
            "values": [{"role": "workspace"}, {"role": "user_root"}],
            "join": ",",
        },
    }
    result = _parse_path_env(raw, "test-mcp")
    assert result["X"].join == ","


def test_parse_path_env_multivalue_empty_values_dropped():
    from services.mcp.mcp_registry import _parse_path_env

    raw = {
        "X": {"values": [], "join": ":"},
    }
    result = _parse_path_env(raw, "test-mcp")
    assert "X" not in result


def test_parse_path_env_multivalue_with_subpath():
    from services.mcp.mcp_registry import _parse_path_env

    raw = {
        "MULTI_CREDS": {
            "values": [
                {"role": "credentials_dir", "subpath": "tokens"},
                {"role": "credentials_dir", "subpath": "keys"},
            ],
            "join": ":",
        },
    }
    result = _parse_path_env(raw, "test-mcp")
    assert "MULTI_CREDS" in result
    assert result["MULTI_CREDS"].values[0].subpath == "tokens"
    assert result["MULTI_CREDS"].values[1].subpath == "keys"


def test_parse_path_env_multivalue_drops_invalid_inner_role():
    """If any inner entry has a bad role, drop the whole multi-value entry."""
    from services.mcp.mcp_registry import _parse_path_env

    raw = {
        "X": {
            "values": [
                {"role": "workspace"},
                {"role": "made_up"},  # invalid
            ],
            "join": ":",
        },
    }
    result = _parse_path_env(raw, "test-mcp")
    assert "X" not in result


def test_parse_path_env_multivalue_drops_invalid_inner_creds_no_subpath():
    """credentials_dir item without subpath inside multi-value drops the entry."""
    from services.mcp.mcp_registry import _parse_path_env

    raw = {
        "X": {
            "values": [{"role": "credentials_dir"}],
            "join": ":",
        },
    }
    result = _parse_path_env(raw, "test-mcp")
    assert "X" not in result


def test_parse_path_env_rejects_both_role_and_values():
    """Exactly one of role/values — both is invalid."""
    from services.mcp.mcp_registry import _parse_path_env

    raw = {
        "X": {
            "role": "workspace",
            "values": [{"role": "user_root"}],
        },
    }
    result = _parse_path_env(raw, "test-mcp")
    assert "X" not in result


def test_parse_path_env_rejects_neither_role_nor_values():
    from services.mcp.mcp_registry import _parse_path_env

    raw = {
        "X": {"subpath": "foo"},  # no role, no values
    }
    result = _parse_path_env(raw, "test-mcp")
    assert "X" not in result


def test_parse_path_env_mixed_shorthand_and_multi():
    from services.mcp.mcp_registry import _parse_path_env

    raw = {
        "IMAGE_SAVE_DIR": {"role": "workspace"},
        "ALLOWED_FILE_DIRS": {
            "values": [{"role": "user_root"}, {"role": "shared_workspace"}],
            "join": ":",
        },
    }
    result = _parse_path_env(raw, "test-mcp")
    assert result["IMAGE_SAVE_DIR"].is_multi is False
    assert result["ALLOWED_FILE_DIRS"].is_multi is True


def test_shared_workspace_contributor_user_scoped():
    """The workspace tier resolves /workspace; a viewer still gets nothing."""
    assert resolve_role("shared_workspace", username="alice", user_role="contributor") == "/workspace"
    assert resolve_role("shared_workspace", username="alice", user_role="viewer") == ""


def test_config_contributor_user_scoped_empty():
    assert resolve_role("config", username="alice", user_role="contributor") == ""
