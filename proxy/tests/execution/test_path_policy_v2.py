"""Tests for the path-policy framework (proxy/services/path_policy_v2.py).

Mirror of the path-resolution policy truth-table. Each
edge case from the design has at least one test below.
"""

import sys

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from services.path_policy_v2 import (  # noqa: E402
    PathPolicyContext,
    ResolveItem,
    SessionTargetRevoked,
    classify_path,
    expand_tilde,
    is_other_user_tilde,
    is_path_string,
    context_from_security,
    normalize_path,
    resolve_path_batch,
    resolve_path_for_session,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _local_ctx() -> PathPolicyContext:
    return PathPolicyContext(
        target_kind="local",
        agent_slug="my-agent",
        role="manager",
    )


def _user_remote_ctx(*, allow_full_fs: bool = False,
                      home_dir: str = "/home/dave",
                      target_os: str = "linux") -> PathPolicyContext:
    return PathPolicyContext(
        target_kind="user_remote",
        machine_id="machine-abc",
        home_dir=home_dir,
        os_user="dave",
        user_dirs={
            "desktop":   f"{home_dir}/Desktop",
            "downloads": f"{home_dir}/Downloads",
        },
        allow_full_fs=allow_full_fs,
        target_agents_dir=f"{home_dir}/.oto-dock/agents",
        target_os=target_os,
        agent_slug="my-agent",
        role="manager",
    )


def _admin_remote_ctx(*, allow_full_fs: bool = True) -> PathPolicyContext:
    return PathPolicyContext(
        target_kind="admin_remote",
        machine_id="machine-xyz",
        home_dir="/home/svcuser",
        os_user="svcuser",
        user_dirs={"desktop": "/home/svcuser/Desktop"},
        allow_full_fs=allow_full_fs,
        target_agents_dir="/home/svcuser/.oto-dock/agents",
        target_os="linux",
        agent_slug="ops-bot",
        role="admin",
    )


# ---------------------------------------------------------------------------
# Type detection
# ---------------------------------------------------------------------------

class TestIsPathString:
    def test_unix_abs(self):
        assert is_path_string("/etc/hosts") is True

    def test_sandbox_virtual(self):
        assert is_path_string("/users/alice/workspace/foo.png") is True

    def test_tilde(self):
        assert is_path_string("~/Desktop/foo.png") is True

    def test_windows_drive(self):
        assert is_path_string("C:/Users/dave/Desktop/foo.png") is True

    def test_windows_drive_backslash(self):
        assert is_path_string("C:\\Users\\dave\\Desktop\\foo.png") is True

    def test_relative(self):
        assert is_path_string("Desktop/foo.png") is True

    def test_url_http(self):
        assert is_path_string("http://example.com/foo.png") is False

    def test_url_https(self):
        assert is_path_string("https://example.com/foo.png") is False

    def test_data_uri(self):
        assert is_path_string("data:image/png;base64,iVBORw0KG...") is False

    def test_template_dollar(self):
        assert is_path_string("/users/${user}/workspace/x.png") is False

    def test_template_double_brace(self):
        assert is_path_string("/users/{{user}}/workspace/x.png") is False

    def test_empty(self):
        assert is_path_string("") is False

    def test_long_base64_blob(self):
        # 600 chars, no slash → likely base64
        blob = "A" * 600
        assert is_path_string(blob) is False

    def test_long_with_slash_is_path(self):
        # Longer than 500 but contains slash — still a path
        long_path = "/very/" + ("nested/" * 100) + "file.txt"
        assert is_path_string(long_path) is True


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestNormalizePath:
    def test_backslash_to_forward(self):
        assert normalize_path("C:\\Users\\erin\\Desktop") == "c:/Users/erin/Desktop"

    def test_drive_lowercase(self):
        assert normalize_path("C:/Users/erin") == "c:/Users/erin"

    def test_collapse_double_slash(self):
        assert normalize_path("/a//b//c") == "/a/b/c"

    def test_strip_trailing_slash(self):
        assert normalize_path("/foo/bar/") == "/foo/bar"

    def test_keep_root_slash(self):
        assert normalize_path("/") == "/"

    def test_keep_drive_root(self):
        assert normalize_path("C:/") == "c:/"

    def test_empty(self):
        assert normalize_path("") == ""

    def test_unix_passthrough(self):
        assert normalize_path("/home/dave/Desktop") == "/home/dave/Desktop"


# ---------------------------------------------------------------------------
# Tilde expansion
# ---------------------------------------------------------------------------

class TestExpandTilde:
    def test_tilde_slash(self):
        out, was = expand_tilde("~/Desktop/foo.png", "/home/dave")
        assert out == "/home/dave/Desktop/foo.png"
        assert was is True

    def test_tilde_alone(self):
        out, was = expand_tilde("~", "/home/dave")
        assert out == "/home/dave"
        assert was is True

    def test_no_tilde(self):
        out, was = expand_tilde("/etc/hosts", "/home/dave")
        assert out == "/etc/hosts"
        assert was is False

    def test_other_user_tilde_passthrough(self):
        # expand_tilde does not expand ~root — left as-is.
        out, was = expand_tilde("~root/foo", "/home/dave")
        assert out == "~root/foo"
        assert was is False

    def test_empty_home(self):
        out, was = expand_tilde("~/foo", "")
        assert out == "~/foo"
        assert was is False

    def test_is_other_user_tilde(self):
        assert is_other_user_tilde("~root/foo") is True
        assert is_other_user_tilde("~daemon") is True
        assert is_other_user_tilde("~/foo") is False
        assert is_other_user_tilde("~") is False
        assert is_other_user_tilde("/etc") is False


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class TestClassifyPath:
    def test_sandbox_virtual_users(self):
        assert classify_path("/users/alice/workspace/foo.png") == "sandbox_virtual"

    def test_sandbox_virtual_workspace(self):
        assert classify_path("/workspace/foo.png") == "sandbox_virtual"

    def test_sandbox_virtual_knowledge(self):
        assert classify_path("/knowledge/index.md") == "sandbox_virtual"

    def test_sandbox_virtual_config(self):
        assert classify_path("/config/prompt.md") == "sandbox_virtual"

    def test_satellite_host_unix(self):
        assert classify_path("/home/erin/Desktop/foo.png") == "satellite_host"

    def test_satellite_host_etc(self):
        assert classify_path("/etc/hosts") == "satellite_host"

    def test_satellite_host_windows(self):
        # Must be pre-normalized (lowercase drive)
        assert classify_path("c:/Users/erin/Desktop") == "satellite_host"

    def test_relative(self):
        assert classify_path("Desktop/foo.png") == "relative"

    def test_invalid_empty(self):
        assert classify_path("") == "invalid"

    def test_invalid_nul(self):
        assert classify_path("/foo\x00/bar") == "invalid"


# ---------------------------------------------------------------------------
# resolve_path_for_session — local sandbox
# ---------------------------------------------------------------------------

class TestResolveLocal:
    def test_sandbox_virtual_allowed(self):
        ctx = _local_ctx()
        r = resolve_path_for_session(ctx, "/users/alice/workspace/foo.png")
        assert r.allowed
        assert r.path_ref.kind == "agent_tree"
        assert r.sandbox_relative == "/users/alice/workspace/foo.png"

    def test_satellite_host_rejected_on_local(self):
        ctx = _local_ctx()
        r = resolve_path_for_session(ctx, "/etc/hosts")
        assert not r.allowed
        assert "absolute paths outside the sandbox" in r.error.lower()

    def test_relative_anchors_workspace(self):
        ctx = _local_ctx()
        r = resolve_path_for_session(ctx, "Desktop/foo.png")
        # Resolves to /workspace/Desktop/foo.png (sandbox-virtual)
        assert r.allowed
        assert r.path_ref.kind == "agent_tree"
        assert r.sandbox_relative == "/workspace/Desktop/foo.png"

    def test_relative_leading_dotdot_denied_with_hint(self):
        # `../x` has no knowable base cwd — it must not silently resolve
        # against the workspace anchor (either to a baffling denial or to a
        # sandbox path the author never meant). Deterministic denial + fix.
        ctx = _local_ctx()
        for p in ("..", "../secrets.txt", "../users/alice/workspace/x.png",
                  "..\\up.txt", "a/../../up.txt"):
            r = resolve_path_for_session(ctx, p)
            assert not r.allowed, p
            assert "absolute path" in r.error, p

    def test_relative_inner_dotdot_still_collapses(self):
        # A `..` that stays INSIDE the relative path is harmless — normpath
        # collapses it before anchoring, same result as writing `b/x.png`.
        ctx = _local_ctx()
        r = resolve_path_for_session(ctx, "a/../b/x.png")
        assert r.allowed
        assert r.sandbox_relative == "/workspace/b/x.png"

    def test_empty_rejected(self):
        ctx = _local_ctx()
        r = resolve_path_for_session(ctx, "")
        assert not r.allowed


# ---------------------------------------------------------------------------
# resolve_path_for_session — relative anchoring at the session cwd
# (otodock-CLI sessions carry work_cwd; everyone else keeps the
# /workspace anchor + deterministic `..` deny above)
# ---------------------------------------------------------------------------

class TestRelativeWorkCwdAnchor:
    def _ctx(self, work_cwd="/srv/proj", roots=("/srv/proj",)):
        import dataclasses
        base = _user_remote_ctx()
        return dataclasses.replace(
            base, work_cwd=work_cwd, session_allowed_roots=tuple(roots))

    def test_plain_relative_anchors_at_cwd(self):
        r = resolve_path_for_session(self._ctx(), "data/x.csv")
        assert r.allowed
        assert r.access_path == "/srv/proj/data/x.csv"

    def test_dotdot_resolves_then_checks_home(self):
        # cwd under home: `../` lands on a sibling dir — admitted by the
        # normal home branch, no special-casing.
        ctx = self._ctx(work_cwd="/home/dave/proj", roots=())
        r = resolve_path_for_session(ctx, "../other/notes.md")
        assert r.allowed
        assert r.access_path == "/home/dave/other/notes.md"

    def test_dotdot_escape_beyond_admitted_space_denied(self):
        # Resolve-then-check: `..` collapsed against the cwd gains no reach —
        # /srv/secret is neither under the session root nor under home.
        r = resolve_path_for_session(self._ctx(), "../secret/x")
        assert not r.allowed

    def test_protected_env_still_wins(self):
        r = resolve_path_for_session(self._ctx(), ".env")
        assert not r.allowed
        assert ".env" in r.error

    def test_no_work_cwd_keeps_dotdot_deny(self):
        # Sessions without a persisted cwd keep the deterministic denial.
        ctx = self._ctx(work_cwd="", roots=())
        r = resolve_path_for_session(ctx, "../x")
        assert not r.allowed
        assert "absolute path" in r.error

    def test_context_from_security_threads_work_cwd(self):
        from types import SimpleNamespace
        sec = SimpleNamespace(
            target_kind="user_remote", target_machine_id="m1",
            target_agents_dir="/home/dave/.oto-dock/agents",
            target_home_dir="/home/dave", target_allow_full_fs=False,
            role="manager", agent="my-agent",
            session_allowed_roots=("/srv/proj",),
            work_cwd="/srv/proj",
        )
        built = context_from_security(sec)
        assert built.work_cwd == "/srv/proj"

    def test_local_session_with_cwd_fails_closed(self):
        # A hypothetical local session carrying a host cwd must not gain
        # host-absolute reach — the local branch rejects the anchored path.
        import dataclasses
        ctx = dataclasses.replace(_local_ctx(), work_cwd="/srv/proj")
        r = resolve_path_for_session(ctx, "data/x.csv")
        assert not r.allowed


# ---------------------------------------------------------------------------
# resolve_path_for_session — user_remote, allow_full_fs=False (default)
# ---------------------------------------------------------------------------

class TestResolveUserRemoteHomeOnly:
    def test_sandbox_virtual_translated_to_host(self):
        ctx = _user_remote_ctx()
        r = resolve_path_for_session(ctx, "/users/alice/workspace/foo.png")
        assert r.allowed
        assert r.access_path == (
            "/home/dave/.oto-dock/agents/my-agent/"
            "users/alice/workspace/foo.png"
        )
        assert r.path_ref.kind == "agent_tree"
        assert r.is_remote_pull is True
        assert r.is_remote_push is False

    def test_sandbox_virtual_writing(self):
        ctx = _user_remote_ctx()
        r = resolve_path_for_session(
            ctx, "/workspace/out.png", writing=True,
        )
        assert r.allowed
        assert r.is_remote_pull is False
        assert r.is_remote_push is True

    def test_home_path_allowed(self):
        ctx = _user_remote_ctx()
        r = resolve_path_for_session(ctx, "/home/dave/Desktop/foo.png")
        assert r.allowed
        assert r.access_path == "/home/dave/Desktop/foo.png"
        assert r.path_ref.kind == "satellite_host"

    def test_tilde_expansion_to_home(self):
        ctx = _user_remote_ctx()
        r = resolve_path_for_session(ctx, "~/Desktop/foo.png")
        assert r.allowed
        assert r.access_path == "/home/dave/Desktop/foo.png"

    def test_etc_rejected_home_only(self):
        ctx = _user_remote_ctx()
        r = resolve_path_for_session(ctx, "/etc/hosts")
        assert not r.allowed
        assert "outside the OS user's home" in r.error
        assert "full filesystem access" in r.error

    def test_other_home_rejected(self):
        ctx = _user_remote_ctx()
        r = resolve_path_for_session(ctx, "/home/otheruser/secret.txt")
        assert not r.allowed

    def test_other_user_tilde_rejected_even_with_full_fs(self):
        # Cross-OS-user is never allowed, regardless of full_fs.
        ctx = _user_remote_ctx(allow_full_fs=True)
        r = resolve_path_for_session(ctx, "~root/secret")
        assert not r.allowed
        assert "another OS user" in r.error

    def test_satellite_host_inside_synced_tree_uses_virtual(self):
        # Stating the absolute path that maps to /users/alice/... should
        # behave identically to passing /users/alice/... directly.
        ctx = _user_remote_ctx()
        absolute = (
            "/home/dave/.oto-dock/agents/my-agent/"
            "users/alice/workspace/foo.png"
        )
        r = resolve_path_for_session(ctx, absolute)
        assert r.allowed
        assert r.path_ref.kind == "agent_tree"
        assert r.sandbox_relative == "/users/alice/workspace/foo.png"

    def test_dot_dot_escape_resolved_then_rejected(self):
        ctx = _user_remote_ctx()
        r = resolve_path_for_session(
            ctx, "/home/dave/Desktop/../../etc/passwd",
        )
        # normpath → /etc/passwd → outside home
        assert not r.allowed
        assert "outside the OS user's home" in r.error

    def test_no_home_fail_closed(self):
        ctx = PathPolicyContext(
            target_kind="user_remote",
            machine_id="m",
            home_dir="",  # missing
            allow_full_fs=False,
            target_agents_dir="/home/x/.oto-dock/agents",
            agent_slug="a",
            target_os="linux",
        )
        r = resolve_path_for_session(ctx, "/some/path")
        assert not r.allowed
        assert "home directory unknown" in r.error


# ---------------------------------------------------------------------------
# resolve_path_for_session — user_remote, allow_full_fs=True
# ---------------------------------------------------------------------------

class TestResolveUserRemoteFullFs:
    def test_etc_allowed(self):
        ctx = _user_remote_ctx(allow_full_fs=True)
        r = resolve_path_for_session(ctx, "/etc/hosts")
        assert r.allowed
        assert r.access_path == "/etc/hosts"
        assert r.path_ref.kind == "satellite_host"

    def test_home_still_works(self):
        ctx = _user_remote_ctx(allow_full_fs=True)
        r = resolve_path_for_session(ctx, "/home/dave/Desktop/foo.png")
        assert r.allowed

    def test_cross_user_tilde_still_rejected(self):
        ctx = _user_remote_ctx(allow_full_fs=True)
        r = resolve_path_for_session(ctx, "~root/secret")
        assert not r.allowed


# ---------------------------------------------------------------------------
# resolve_path_for_session — admin_remote
# ---------------------------------------------------------------------------

class TestResolveAdminRemote:
    def test_system_path_allowed_by_default(self):
        ctx = _admin_remote_ctx(allow_full_fs=True)
        r = resolve_path_for_session(ctx, "/var/log/syslog")
        assert r.allowed
        assert r.access_path == "/var/log/syslog"

    def test_admin_opted_out_falls_back_to_home_only(self):
        ctx = _admin_remote_ctx(allow_full_fs=False)
        r = resolve_path_for_session(ctx, "/etc/sudoers")
        assert not r.allowed

    def test_admin_opted_out_home_still_works(self):
        ctx = _admin_remote_ctx(allow_full_fs=False)
        r = resolve_path_for_session(ctx, "/home/svcuser/notes.md")
        assert r.allowed


# ---------------------------------------------------------------------------
# Windows / macOS path handling
# ---------------------------------------------------------------------------

class TestWindowsPaths:
    def test_windows_drive_lowercase(self):
        ctx = PathPolicyContext(
            target_kind="user_remote",
            machine_id="winbox",
            home_dir="c:/Users/dave",
            allow_full_fs=False,
            target_agents_dir="c:/Users/dave/OtoDock/agents",
            target_os="windows",
            agent_slug="my-agent",
        )
        # Backslash form should normalize and admit.
        r = resolve_path_for_session(
            ctx, "C:\\Users\\dave\\Desktop\\foo.png",
        )
        assert r.allowed
        assert "c:/users/dave/desktop/foo.png" in r.access_path.lower()

    def test_windows_other_user_rejected(self):
        ctx = PathPolicyContext(
            target_kind="user_remote",
            machine_id="winbox",
            home_dir="c:/Users/dave",
            allow_full_fs=False,
            target_agents_dir="c:/Users/dave/OtoDock/agents",
            target_os="windows",
            agent_slug="my-agent",
        )
        r = resolve_path_for_session(
            ctx, "C:/Users/admin/Documents/secret.txt",
        )
        assert not r.allowed


class TestMacOSPaths:
    def test_macos_home_with_users_prefix(self):
        ctx = PathPolicyContext(
            target_kind="user_remote",
            machine_id="mac",
            home_dir="/Users/dave",
            allow_full_fs=False,
            target_agents_dir="/Users/dave/.oto-dock/agents",
            target_os="darwin",
            agent_slug="my-agent",
        )
        r = resolve_path_for_session(
            ctx, "/Users/dave/Desktop/foo.png",
        )
        assert r.allowed

    def test_macos_other_user_rejected(self):
        ctx = PathPolicyContext(
            target_kind="user_remote",
            machine_id="mac",
            home_dir="/Users/dave",
            allow_full_fs=False,
            target_agents_dir="/Users/dave/.oto-dock/agents",
            target_os="darwin",
            agent_slug="my-agent",
        )
        r = resolve_path_for_session(ctx, "/Users/admin/secret.txt")
        assert not r.allowed


# ---------------------------------------------------------------------------
# Batched API
# ---------------------------------------------------------------------------

class TestBatched:
    def test_order_preserved(self):
        ctx = _user_remote_ctx(allow_full_fs=True)
        items = [
            ResolveItem(raw_path="/etc/hosts"),
            ResolveItem(raw_path="/users/a/workspace/x.png"),
            ResolveItem(raw_path=""),
        ]
        results = resolve_path_batch(ctx, items)
        assert len(results) == 3
        assert results[0].allowed is True
        assert results[1].allowed is True
        assert results[2].allowed is False

    def test_per_item_write_flag(self):
        ctx = _user_remote_ctx()
        items = [
            ResolveItem(raw_path="/users/a/workspace/x.png", write=False),
            ResolveItem(raw_path="/users/a/workspace/y.png", write=True),
        ]
        results = resolve_path_batch(ctx, items)
        assert results[0].is_remote_pull and not results[0].is_remote_push
        assert results[1].is_remote_push and not results[1].is_remote_pull

    def test_empty_batch(self):
        ctx = _user_remote_ctx()
        assert resolve_path_batch(ctx, []) == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_nul_character_rejected(self):
        ctx = _user_remote_ctx(allow_full_fs=True)
        r = resolve_path_for_session(ctx, "/foo\x00/bar")
        assert not r.allowed
        assert "NUL" in r.error

    def test_relative_anchors_workspace_on_remote(self):
        # Desktop/foo.png on remote → resolves to /workspace/Desktop/foo.png
        # (the LLM is expected to use absolute home paths or sandbox-virtual
        # for the OS Desktop, not relative).
        ctx = _user_remote_ctx()
        r = resolve_path_for_session(ctx, "Desktop/foo.png")
        assert r.allowed
        assert r.path_ref.kind == "agent_tree"
        assert r.sandbox_relative == "/workspace/Desktop/foo.png"


# ---------------------------------------------------------------------------
# SessionTargetRevoked
# ---------------------------------------------------------------------------

class TestSessionTargetRevoked:
    def test_default_reason(self):
        e = SessionTargetRevoked(old="m1", new="local")
        assert "mid-session" in str(e).lower()
        assert e.old == "m1"
        assert e.new == "local"

    def test_custom_reason(self):
        e = SessionTargetRevoked(old="m1", new="local", reason="admin unpaired")
        assert str(e) == "admin unpaired"


# ---------------------------------------------------------------------------
# Credential / secret denylist — mirrors auth.path_policy so the
# MCP path resolvers enforce it on remote satellites (no bwrap masking).
# ---------------------------------------------------------------------------

@pytest.fixture
def _protected_tokens(monkeypatch):
    """Stub the registered protected credential subpaths to {google-tokens}."""
    from services.mcp import mcp_registry
    monkeypatch.setattr(
        mcp_registry, "get_protected_credentials_subpaths",
        lambda: frozenset({"google-tokens"}),
    )


class TestCredentialDenylist:
    def test_oauth_token_path_denied_agent_tree(self, _protected_tokens):
        """LLM-supplied path into the synced OAuth token dir is rejected — the
        stdio interceptor / Docker MCP callers trust this verdict verbatim."""
        ctx = _user_remote_ctx()
        r = resolve_path_for_session(
            ctx, "/users/dave/.credentials/google-tokens/acct.json",
        )
        assert not r.allowed
        assert "credential" in r.error.lower()

    def test_oauth_token_path_denied_local(self, _protected_tokens):
        """Defense-in-depth: also denied on the local target."""
        ctx = _local_ctx()
        r = resolve_path_for_session(
            ctx, "/users/dave/.credentials/google-tokens/acct.json",
        )
        assert not r.allowed

    def test_ssh_denied_satellite_host(self):
        ctx = _user_remote_ctx()
        r = resolve_path_for_session(ctx, "/home/dave/.ssh/id_rsa")
        assert not r.allowed
        assert ".ssh" in r.error.lower()

    def test_ssh_denied_via_tilde(self):
        ctx = _user_remote_ctx()
        r = resolve_path_for_session(ctx, "~/.ssh/id_ed25519")
        assert not r.allowed

    def test_ssh_config_read_allowed(self):
        """``.ssh/config`` is host/alias metadata, not key material — a
        session allowed to RUN ssh needs it to know its destinations."""
        ctx = _user_remote_ctx()
        r = resolve_path_for_session(ctx, "/home/dave/.ssh/config")
        assert r.allowed, r.error

    def test_ssh_known_hosts_read_allowed(self):
        ctx = _user_remote_ctx()
        for p in ("/home/dave/.ssh/known_hosts",
                  "/home/dave/.ssh/known_hosts.old"):
            r = resolve_path_for_session(ctx, p)
            assert r.allowed, (p, r.error)

    def test_ssh_config_write_still_denied(self):
        """A config write would plant ProxyCommand (code execution on the
        operator's next ssh) — writes stay denied for EVERY .ssh path."""
        ctx = _user_remote_ctx()
        for p in ("/home/dave/.ssh/config", "/home/dave/.ssh/known_hosts"):
            r = resolve_path_for_session(ctx, p, writing=True)
            assert not r.allowed, p
            assert ".ssh" in r.error.lower()

    def test_ssh_other_files_still_denied(self):
        """The carve-out is exact — keys and everything else stay denied,
        including a 'config' NOT directly under .ssh."""
        ctx = _user_remote_ctx()
        for p in ("/home/dave/.ssh/authorized_keys",
                  "/home/dave/.ssh/id_rsa.pub",
                  "/home/dave/.ssh/backup/config"):
            r = resolve_path_for_session(ctx, p)
            assert not r.allowed, p

    def test_env_write_denied_in_workspace(self):
        """.env writes are blocked everywhere (parity with local)."""
        ctx = _user_remote_ctx()
        r = resolve_path_for_session(ctx, "/workspace/.env", writing=True)
        assert not r.allowed
        assert ".env" in r.error.lower()

    def test_env_read_allowed_in_workspace(self):
        """An agent's own in-tree workspace .env stays READABLE — parity with
        the local sandbox, which permits workspace .env reads."""
        ctx = _user_remote_ctx()
        r = resolve_path_for_session(ctx, "/workspace/.env", writing=False)
        assert r.allowed

    def test_env_read_denied_satellite_host(self):
        """Reading the OS user's real .env (outside the agent tree) is blocked
        even though it sits under the home dir (home-only mode)."""
        ctx = _user_remote_ctx()
        r = resolve_path_for_session(ctx, "/home/dave/project/.env", writing=False)
        assert not r.allowed
        assert ".env" in r.error.lower()

    def test_legit_paths_unaffected(self, _protected_tokens):
        """No false positives on normal workspace / knowledge / user paths,
        even with credential protection enabled."""
        ctx = _user_remote_ctx()
        for p in ("/workspace/images/diagram.png",
                  "/knowledge/manual.pdf",
                  "/users/dave/workspace/notes.md"):
            r = resolve_path_for_session(ctx, p)
            assert r.allowed, p

    def test_batch_enforces_denylist(self):
        """resolve_path_batch (the hook's actual entry point) inherits the gate."""
        ctx = _user_remote_ctx()
        results = resolve_path_batch(ctx, [
            ResolveItem(raw_path="/workspace/ok.png"),
            ResolveItem(raw_path="/home/dave/.ssh/id_rsa"),
        ])
        assert results[0].allowed
        assert not results[1].allowed
