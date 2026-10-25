"""Tests for the ``credentials.oauth.env_injection`` manifest field and the
resolver path that emits its env vars.

Two surfaces:

* **Validator** — ``services/mcp_registry._validate_oauth_services``
  must accept a non-empty list of valid POSIX env-var names and reject
  empty lists, invalid names, and duplicates. Pure logic, no DB.

* **Resolver** — ``services/credential_resolver._resolve_oauth_mcp``
  must read the bound account's token file and populate each declared
  env var with the canonical ``access_token``. Works for both
  ``bearer_required: true`` MCPs (returns env-vars only) and stdio MCPs
  with ``credentials_dir`` (env-vars merged on top of the file-copy
  result). The DB / token-file pieces are stubbed via monkeypatch so
  the test focuses on the resolver branch.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.mcp.mcp_registry import _validate_oauth_services as _strict_validate
from services.oauth import credential_resolver


def _validate(raw, mcp_name="any"):
    """Wrapper that injects required fields so tests focus on env_injection."""
    if isinstance(raw, dict):
        if "provider_id" not in raw and "provider" not in raw:
            raw = {**raw, "provider_id": "github"}
        if "flows" not in raw:
            raw = {**raw, "flows": ["authorization_code"]}
    return _strict_validate(raw, mcp_name)


# ═══════════════════════════════════════════════════════════════════════════
# Validator — happy path
# ═══════════════════════════════════════════════════════════════════════════


class TestValidatorAccepts:
    def test_single_env_var(self):
        _validate({"env_injection": ["GH_TOKEN"]})

    def test_multiple_env_vars(self):
        _validate({"env_injection": ["GH_TOKEN", "GITHUB_TOKEN"]})

    def test_underscore_prefixed_name(self):
        _validate({"env_injection": ["_PRIVATE_TOKEN"]})

    def test_alphanumeric_names(self):
        _validate({"env_injection": ["OAUTH_TOKEN_V2", "X_API_KEY_3"]})

    def test_absent_field_is_fine(self):
        # env_injection is optional; omitting it must not raise.
        _validate({})

    def test_git_credential_helper_accepted(self):
        _validate({"git_credential_helper": {
            "host": "github.com", "helper": "!gh auth git-credential",
        }})


# ═══════════════════════════════════════════════════════════════════════════
# Validator — rejection paths
# ═══════════════════════════════════════════════════════════════════════════


class TestValidatorRejects:
    def test_not_a_list(self):
        with pytest.raises(ValueError, match="env_injection"):
            _validate({"env_injection": "GH_TOKEN"})

    def test_empty_list(self):
        with pytest.raises(ValueError, match="non-empty list"):
            _validate({"env_injection": []})

    def test_empty_string_element(self):
        with pytest.raises(ValueError, match="non-empty string"):
            _validate({"env_injection": [""]})

    def test_lowercase_name_rejected(self):
        with pytest.raises(ValueError, match="POSIX env-var name"):
            _validate({"env_injection": ["lowercase"]})

    def test_starts_with_digit_rejected(self):
        with pytest.raises(ValueError, match="POSIX env-var name"):
            _validate({"env_injection": ["1BAD"]})

    def test_hyphen_in_name_rejected(self):
        with pytest.raises(ValueError, match="POSIX env-var name"):
            _validate({"env_injection": ["GH-TOKEN"]})

    def test_dot_in_name_rejected(self):
        with pytest.raises(ValueError, match="POSIX env-var name"):
            _validate({"env_injection": ["GH.TOKEN"]})

    def test_duplicate_names_rejected(self):
        with pytest.raises(ValueError, match="duplicate"):
            _validate({"env_injection": ["GH_TOKEN", "GH_TOKEN"]})

    def test_non_string_element(self):
        with pytest.raises(ValueError, match="non-empty string"):
            _validate({"env_injection": [123]})

    def test_git_credential_helper_not_object_rejected(self):
        with pytest.raises(ValueError, match="git_credential_helper"):
            _validate({"git_credential_helper": "github.com"})

    def test_git_credential_helper_missing_field_rejected(self):
        with pytest.raises(ValueError, match="git_credential_helper.helper"):
            _validate({"git_credential_helper": {"host": "github.com"}})


# ═══════════════════════════════════════════════════════════════════════════
# Resolver — env_injection emission
# ═══════════════════════════════════════════════════════════════════════════


def _build_manifest(
    env_injection, *, bearer_required=True, git_credential_helper=None,
):
    """Build the minimal manifest shape `_resolve_oauth_mcp` walks."""
    oauth = {
        "provider_id": "github",
        "bearer_required": bearer_required,
    }
    if env_injection is not None:
        oauth["env_injection"] = env_injection
    if git_credential_helper is not None:
        oauth["git_credential_helper"] = git_credential_helper
    credentials = SimpleNamespace(oauth=oauth)
    return SimpleNamespace(credentials=credentials)


def _write_token_file(tmp_path: Path, account_label: str, access_token: str) -> Path:
    token = {
        "provider": "github",
        "account_id": account_label,
        "access_token": access_token,
        "expires_at": "2099-01-01T00:00:00Z",
    }
    f = tmp_path / f"{account_label}.json"
    f.write_text(json.dumps(token))
    return f


@pytest.fixture
def patch_resolver(monkeypatch, tmp_path):
    """Wire the resolver's collaborators so tests can drive it directly.

    Stubs:
      * `mcp_registry.get_manifest` returns the manifest the test passes via
        the `set_manifest` helper.
      * `task_store.get_username_by_sub` returns a fixed username for any sub.
      * `pick_account` returns "primary".
      * `oauth_account_store.get_token_dir` returns the per-test tmp dir.

    Yields a `Ctx` dataclass with `set_manifest()` + tmp_path so tests can
    write token files and swap manifests per-case.
    """
    holder: dict = {"manifest": None}

    def fake_get_manifest(mcp_name):
        return holder["manifest"]

    def fake_get_token_dir(username="", *, provider_id=""):
        return tmp_path

    monkeypatch.setattr(
        "services.mcp.mcp_registry.get_manifest", fake_get_manifest,
    )
    monkeypatch.setattr(
        "services.oauth.oauth_account_store.get_token_dir", fake_get_token_dir,
    )
    monkeypatch.setattr(
        credential_resolver, "pick_account",
        lambda mcp, agent, *, user_sub="": credential_resolver.AccountRef(
            label="primary", owner_sub=user_sub,
        ),
    )
    monkeypatch.setattr(
        "storage.database.get_username_by_sub", lambda sub: "alice",
    )

    return SimpleNamespace(
        token_dir=tmp_path,
        set_manifest=lambda m: holder.update(manifest=m),
    )


_GH_HELPER = {"host": "github.com", "helper": "!gh auth git-credential"}


class TestResolverEmitsEnvVars:
    def test_env_injection_alone_places_no_token_in_the_env(self, patch_resolver):
        """The names a manifest lists are what the command wrapper sets on
        the real command's own process; the session's environment never
        receives the token (the second private report's finding)."""
        patch_resolver.set_manifest(_build_manifest(["GH_TOKEN", "GITHUB_TOKEN"]))
        _write_token_file(patch_resolver.token_dir, "primary", "ghp_user_token_xyz")

        result = credential_resolver._resolve_oauth_mcp(
            mcp_name="github-mcp",
            cred_type_info={"oauth": {}},
            user_sub="user-sub-1",
            task_scope="user",
            agent_name="dev-agent",
        )

        assert result == {}

    def test_service_scope_records_the_bound_accounts_credential_by_reference(self, patch_resolver):
        """Agent scope (no user_sub) resolves the service account: the git
        entries name the platform's helper and the broker half carries the
        token file by reference, never the value."""
        patch_resolver.set_manifest(_build_manifest(["GH_TOKEN"], git_credential_helper=_GH_HELPER))
        _write_token_file(patch_resolver.token_dir, "primary", "ghp_service_token")
        cli: dict = {}

        result = credential_resolver._resolve_oauth_mcp(
            mcp_name="github-mcp",
            cred_type_info={"oauth": {}},
            user_sub=None,
            task_scope="agent",
            agent_name="dev-agent",
            cli_credentials=cli,
        )

        assert "ghp_service_token" not in json.dumps(result)
        assert result["GIT_CONFIG_VALUE_1"] == credential_resolver.PLATFORM_GIT_HELPER
        cred = cli["github-mcp"]
        assert cred.host == "github.com" and cred.env_names == ("GH_TOKEN",)
        assert cred.manifest == "github-mcp" and cred.username == "x-access-token"
        assert cred.token_ref.token_dir == str(patch_resolver.token_dir)
        assert cred.token_ref.account_label == "primary"

    def test_no_env_injection_returns_empty_for_bearer(self, patch_resolver):
        """Bearer-required MCP without env_injection still resolves but
        emits no env vars — the bearer-header injector handles auth."""
        patch_resolver.set_manifest(_build_manifest(None))
        _write_token_file(patch_resolver.token_dir, "primary", "ghp_token")

        result = credential_resolver._resolve_oauth_mcp(
            mcp_name="github-mcp",
            cred_type_info={"oauth": {}},
            user_sub="user-sub-1",
            task_scope="user",
            agent_name="dev-agent",
        )

        assert result == {}

    def test_no_token_file_returns_none(self, patch_resolver):
        """Even with env_injection declared, missing token file = excluded."""
        patch_resolver.set_manifest(_build_manifest(["GH_TOKEN"]))
        # Deliberately do NOT write a token file.

        result = credential_resolver._resolve_oauth_mcp(
            mcp_name="github-mcp",
            cred_type_info={"oauth": {}},
            user_sub="user-sub-1",
            task_scope="user",
            agent_name="dev-agent",
        )

        assert result is None

    def test_empty_access_token_delivers_nothing(self, patch_resolver):
        """Token file with no access_token = no git entries and no broker
        half (rare — only if writeback corrupted the file)."""
        patch_resolver.set_manifest(_build_manifest(["GH_TOKEN"], git_credential_helper=_GH_HELPER))
        # Token file present but access_token missing.
        f = patch_resolver.token_dir / "primary.json"
        f.write_text(json.dumps({"provider": "github", "account_id": "primary"}))
        cli: dict = {}

        result = credential_resolver._resolve_oauth_mcp(
            mcp_name="github-mcp",
            cred_type_info={"oauth": {}},
            user_sub="user-sub-1",
            task_scope="user",
            agent_name="dev-agent",
            cli_credentials=cli,
        )

        assert result == {} and cli == {}


class TestResolverEmitsGitCredentialHelper:
    def test_git_credential_helper_wires_git_config(self, patch_resolver):
        """git_credential_helper → GIT_CONFIG_* pointing git at the
        platform's credential helper (the manifest's own helper string is
        accepted and not read); no token anywhere in the answer."""
        patch_resolver.set_manifest(_build_manifest(["GH_TOKEN"], git_credential_helper=_GH_HELPER))
        _write_token_file(patch_resolver.token_dir, "primary", "ghp_tok")
        cli: dict = {}

        result = credential_resolver._resolve_oauth_mcp(
            mcp_name="github-mcp",
            cred_type_info={"oauth": {}},
            user_sub="user-sub-1",
            task_scope="user",
            agent_name="dev-agent",
            cli_credentials=cli,
        )

        assert "GH_TOKEN" not in result and "ghp_tok" not in json.dumps(result)
        # Two entries: an empty RESET then our helper — so an interactive system
        # helper (Windows Git Credential Manager) can't win over the token one.
        assert result["GIT_CONFIG_COUNT"] == "2"
        assert result["GIT_CONFIG_KEY_0"] == "credential.https://github.com.helper"
        assert result["GIT_CONFIG_VALUE_0"] == ""
        assert result["GIT_CONFIG_KEY_1"] == "credential.https://github.com.helper"
        assert result["GIT_CONFIG_VALUE_1"] == credential_resolver.PLATFORM_GIT_HELPER
        assert "gh auth git-credential" not in result["GIT_CONFIG_VALUE_1"]
        # The helper the entries name is the script every config dir carries,
        # run by the interpreter and from the dir each spawn path names.
        from core.sandbox.session_config_dir import HOOK_SCRIPTS
        assert "credential_helper.py" in HOOK_SCRIPTS
        assert result["GIT_CONFIG_VALUE_1"].startswith('!"$OTO_HOOK_PY" -I "$OTO_HOOKS_DIR/credential_helper.py"')
        assert cli["github-mcp"].host == "github.com"

    def test_cli_token_env_gives_a_step_the_token_and_the_manifests_helper(
        self, patch_resolver, monkeypatch,
    ):
        """An app step's script has no helper beside it: it receives the
        names with the token and the manifest's own git helper entries, for
        the MCPs enabled on the local placement, where a step runs; one
        enabled on a paired machine alone gives it nothing."""
        from core import placement
        manifest = _build_manifest(["GH_TOKEN", "GITHUB_TOKEN"], git_credential_helper=_GH_HELPER)
        manifest.name = "github-mcp"
        patch_resolver.set_manifest(manifest)
        _write_token_file(patch_resolver.token_dir, "primary", "ghp_step")
        asked: list = []

        def _local_mcps(agent, *, placement=None):
            asked.append(placement)
            return [manifest] if placement == self_placement.LOCAL_PLACEMENT else []
        self_placement = placement
        monkeypatch.setattr("services.mcp.mcp_registry.get_agent_mcps", _local_mcps)
        monkeypatch.setattr(
            "services.mcp.mcp_registry.get_agent_mcps_all_placements",
            lambda agent: pytest.fail("a step never walks the other placements"),
        )

        env = credential_resolver.cli_token_env(
            "dev-agent", "user-sub-1", task_scope="user", providers=["github"],
        )

        assert env["GH_TOKEN"] == env["GITHUB_TOKEN"] == "ghp_step"
        assert env["GIT_CONFIG_VALUE_1"] == "!gh auth git-credential"
        assert asked == [placement.LOCAL_PLACEMENT]
        assert credential_resolver.cli_token_env(
            "dev-agent", "user-sub-1", task_scope="user", providers=["slack"],
        ) == {}


# ═══════════════════════════════════════════════════════════════════════════
# mcp_env_injection — token into the MCP SERVER subprocess env (notion)
# ═══════════════════════════════════════════════════════════════════════════


def _build_mcp_env_manifest(
    mcp_env, *, bearer_required=False, env_injection=None, name="notion-mcp",
):
    """Manifest shape for token-via-env MCPs (notion: official stdio server)."""
    oauth = {"provider_id": "notion", "bearer_required": bearer_required}
    if mcp_env is not None:
        oauth["mcp_env_injection"] = mcp_env
    if env_injection is not None:
        oauth["env_injection"] = env_injection
    m = SimpleNamespace(credentials=SimpleNamespace(oauth=oauth))
    m.name = name
    return m


class TestValidatorMcpEnvInjection:
    def test_accepts_single(self):
        _validate({"mcp_env_injection": ["NOTION_TOKEN"]})

    def test_rejects_bad_name(self):
        with pytest.raises(ValueError, match="mcp_env_injection"):
            _validate({"mcp_env_injection": ["lowercase"]})

    def test_rejects_duplicates(self):
        with pytest.raises(ValueError, match="duplicate"):
            _validate({"mcp_env_injection": ["NOTION_TOKEN", "NOTION_TOKEN"]})

    def test_rejects_empty_list(self):
        with pytest.raises(ValueError, match="non-empty list"):
            _validate({"mcp_env_injection": []})


class TestResolverMcpEnvInjection:
    def test_stdio_no_credentials_dir_token_via_env(self, patch_resolver, monkeypatch):
        """notion shape: stdio, no credentials_dir, no bearer — the
        mcp_env_injection vars ARE the credential delivery."""
        monkeypatch.setattr(
            "services.mcp.mcp_registry.get_credentials_dirs", lambda m: [],
        )
        patch_resolver.set_manifest(_build_mcp_env_manifest(["NOTION_TOKEN"]))
        _write_token_file(patch_resolver.token_dir, "primary", "ntn_secret_1")

        result = credential_resolver._resolve_oauth_mcp(
            mcp_name="notion-mcp", cred_type_info={"oauth": {}},
            user_sub="user-sub-1", task_scope="user", agent_name="dev-agent",
        )
        assert result == {"NOTION_TOKEN": "ntn_secret_1"}

    def test_bash_only_injection_without_dirs_stays_disconnected(
        self, patch_resolver, monkeypatch,
    ):
        """Pre-existing behavior preserved: env_injection alone (no
        credentials_dir, no bearer, no mcp_env_injection) does NOT make an
        MCP 'connected'."""
        monkeypatch.setattr(
            "services.mcp.mcp_registry.get_credentials_dirs", lambda m: [],
        )
        patch_resolver.set_manifest(
            _build_mcp_env_manifest(None, env_injection=["SOME_TOKEN"]),
        )
        _write_token_file(patch_resolver.token_dir, "primary", "tok")

        result = credential_resolver._resolve_oauth_mcp(
            mcp_name="x-mcp", cred_type_info={"oauth": {}},
            user_sub="user-sub-1", task_scope="user", agent_name="dev-agent",
        )
        assert result is None

    def test_bearer_mcp_merges_both_injections(self, patch_resolver):
        patch_resolver.set_manifest(_build_mcp_env_manifest(
            ["VENDOR_TOKEN"], bearer_required=True, env_injection=["CLI_TOKEN"],
        ))
        _write_token_file(patch_resolver.token_dir, "primary", "tok_xyz")

        result = credential_resolver._resolve_oauth_mcp(
            mcp_name="some-mcp", cred_type_info={"oauth": {}},
            user_sub="user-sub-1", task_scope="user", agent_name="dev-agent",
        )
        # The MCP's own token env is delivered; the shell's names are not.
        assert result == {"VENDOR_TOKEN": "tok_xyz"}


class TestCallerSecretClassification:
    def test_mcp_env_names_classified_as_pure_secrets(
        self, patch_resolver, monkeypatch,
    ):
        """resolve_credentials puts mcp_env_injection names into secret_keys
        (broker-bundle-only, stripped from config files) and keeps them OUT
        of bash_env_keys; an env_injection name reaches no environment."""
        manifest = _build_mcp_env_manifest(
            ["NOTION_TOKEN"], env_injection=["CLI_TOKEN"],
        )
        patch_resolver.set_manifest(manifest)
        _write_token_file(patch_resolver.token_dir, "primary", "ntn_secret_2")
        monkeypatch.setattr(
            "services.mcp.mcp_registry.get_credentials_dirs", lambda m: [],
        )
        monkeypatch.setattr(
            "services.mcp.mcp_registry.get_agent_mcps_all_placements",
            lambda agent: [manifest],
        )
        # Real schema shape: `oauth` is a presence FLAG (bool), not the dict —
        # the regression that broke warmup on was assuming dict here.
        monkeypatch.setattr(
            "services.mcp.mcp_registry.get_credential_schema",
            lambda m: {"type": "per_user", "oauth": True, "label": "Notion"},
        )

        res = credential_resolver.resolve_credentials(
            "dev-agent", "user-sub-1", task_scope="user",
        )

        assert res.env_by_mcp["notion-mcp"]["NOTION_TOKEN"] == "ntn_secret_2"
        assert "NOTION_TOKEN" in res.secret_keys          # broker-only
        assert "NOTION_TOKEN" not in res.bash_env_keys    # never in bash env
        assert "CLI_TOKEN" not in res.env_vars and res.bash_env_keys == set()
        assert "notion-mcp" in res.available_mcps
        assert res.cli_credentials == {}                  # no git host named
