"""Tests for services.mcp.mcp_sync._diff — install/update/remove computation."""

import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _fresh_hash_cache():
    """The version-hash cache is keyed by the manifest's dir and version:
    every test here uses the same ones, so an entry one test filled must
    not answer the next one's patched ``compute_version_hash``."""
    from services.mcp import mcp_sync
    mcp_sync._version_hashes.clear()
    yield
    mcp_sync._version_hashes.clear()


def _fake_manifest(name: str, runtime: str = "python"):
    m = MagicMock()
    m.name = name
    m.server_name = name
    m.server = MagicMock()
    m.server.runtime = runtime
    m.server.source = f"pypi:{name}@1.0.0"
    m.mcp_dir = f"/tmp/{name}"
    m.category = "custom"
    m.version = "1.0.0"
    return m


def test_install_when_missing():
    """Desired but not installed → to_install."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=_fake_manifest), \
         patch("services.mcp.mcp_installer.compute_version_hash",
               return_value="abc123"):
        install, update, remove = mcp_sync._diff(
            desired={"foo", "bar"},
            installed={},
        )
        assert install == {"foo", "bar"}
        assert update == set()
        assert remove == set()


def test_update_when_hash_drifts():
    """Installed but version_hash differs → to_update."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=_fake_manifest), \
         patch("services.mcp.mcp_installer.compute_version_hash",
               return_value="NEW_HASH"):
        install, update, remove = mcp_sync._diff(
            desired={"foo"},
            installed={"foo": {"version_hash": "OLD_HASH", "healthy": True}},
        )
        assert install == set()
        assert update == {"foo"}
        assert remove == set()


def _known(*names):
    return patch("services.mcp.mcp_registry.get_all_manifests",
                 return_value={n: _fake_manifest(n) for n in names})


def test_a_known_mcp_outside_this_session_is_kept():
    """A task session leaves out ``exclude_from: task`` MCPs: they stay on
    the machine (the next chat used to rebuild them)."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=_fake_manifest), _known("foo", "bar"):
        install, update, remove = mcp_sync._diff(
            desired={"foo"},
            installed={
                "foo": {"version_hash": "h", "healthy": True},
                "bar": {"version_hash": "h", "healthy": True},
            },
        )
        assert remove == set()


def test_an_mcp_the_platform_no_longer_ships_is_removed():
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=_fake_manifest), _known("foo"):
        install, update, remove = mcp_sync._diff(
            desired={"foo"},
            installed={
                "foo": {"version_hash": "h", "healthy": True},
                "bar": {"version_hash": "h", "healthy": True},
            },
        )
        assert remove == {"bar"}


def test_nothing_is_removed_while_the_registry_is_empty():
    """A failed manifest scan must not wipe a working satellite."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest", return_value=None), _known():
        for gc in (False, True):
            _i, _u, remove = mcp_sync._diff(
                desired=set(),
                installed={"bar": {"version_hash": "h", "healthy": True}},
                gc=gc,
            )
            assert remove == set()


def test_sync_now_removes_a_shipped_mcp_nobody_wants():
    """The explicit clean-up (admin Sync Now, ``gc``) keeps its reach."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=_fake_manifest), _known("foo", "bar"):
        _i, _u, remove = mcp_sync._diff(
            desired={"foo"},
            installed={
                "foo": {"version_hash": "h", "healthy": True},
                "bar": {"version_hash": "h", "healthy": True},
            },
            gc=True,
        )
        assert remove == {"bar"}


def test_docker_mcp_never_installed_on_satellite():
    """Docker runtime MCPs stay on the platform — excluded from satellite install."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=lambda n: _fake_manifest(n, runtime="docker")):
        install, update, remove = mcp_sync._diff(
            desired={"file-tools"},
            installed={},
        )
        assert install == set()


def test_unhealthy_triggers_reinstall():
    """Unhealthy (mid-install marker) → reinstall."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=_fake_manifest):
        install, update, remove = mcp_sync._diff(
            desired={"foo"},
            installed={"foo": {"version_hash": "h", "healthy": False}},
        )
        assert install == {"foo"}


def test_same_hash_no_update():
    """Matching version_hash → no update needed."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=_fake_manifest), \
         patch("services.mcp.mcp_installer.compute_version_hash",
               return_value="HASH"):
        install, update, remove = mcp_sync._diff(
            desired={"foo"},
            installed={"foo": {"version_hash": "HASH", "healthy": True}},
        )
        assert install == set()
        assert update == set()
        assert remove == set()


def test_force_always_updates():
    """force=True re-installs even when hashes match."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=_fake_manifest), \
         patch("services.mcp.mcp_installer.compute_version_hash",
               return_value="HASH"):
        install, update, remove = mcp_sync._diff(
            desired={"foo"},
            installed={"foo": {"version_hash": "HASH", "healthy": True}},
            force=True,
        )
        assert update == {"foo"}


# --- Deferred-update backoff (Fix 2b: a swap blocked by an in-use old version
# is kept + deferred, not re-shipped/rebuilt every session) ------------------

def test_deferred_backoff_mark_check_clear():
    """mark → deferred; per-(machine,mcp) isolation; clear drops the backoff."""
    from services.mcp import mcp_sync

    mcp_sync._deferred_updates.clear()
    key = ("machine-1", "google-maps")
    assert mcp_sync._is_update_deferred(*key) is False
    mcp_sync._mark_update_deferred(*key)
    assert mcp_sync._is_update_deferred(*key) is True
    # Independent of other mcp / other machine.
    assert mcp_sync._is_update_deferred("machine-1", "other") is False
    assert mcp_sync._is_update_deferred("machine-2", "google-maps") is False
    # Clearing (as the ack loop does on a successful install) drops it.
    mcp_sync._deferred_updates.pop(key, None)
    assert mcp_sync._is_update_deferred(*key) is False


def test_deferred_backoff_expires_and_self_prunes():
    """A past deadline reads as not-deferred and is pruned from the store."""
    import time

    from services.mcp import mcp_sync

    mcp_sync._deferred_updates.clear()
    key = ("machine-1", "google-maps")
    mcp_sync._deferred_updates[key] = time.monotonic() - 1.0  # already expired
    assert mcp_sync._is_update_deferred(*key) is False
    assert key not in mcp_sync._deferred_updates  # self-pruned on read


def test_the_install_spec_carries_source_build():
    """The packages a manifest allows to build from source reach the
    satellite's installer; a manifest without the field ships an empty list."""
    from services.mcp import mcp_sync
    tb = MagicMock(tarball_b64="dGFy", version_hash="h1")
    m = _fake_manifest("unifi")
    m.server.source_build = ["unifi-network", "antlr4-python3-runtime"]
    spec = mcp_sync._install_spec("unifi", m, tb)
    assert spec["source_build"] == ["unifi-network", "antlr4-python3-runtime"]
    assert spec["name"] == "unifi" and spec["tarball_b64"] == "dGFy" and spec["version_hash"] == "h1"
    assert set(spec["system_requirements"]) == {
        "debian", "ubuntu", "rhel", "arch", "macos_brew", "node_min", "notes"}
    plain = _fake_manifest("plain")
    plain.server.source_build = None
    assert mcp_sync._install_spec("plain", plain, tb)["source_build"] == []


def test_the_version_hash_is_cached_per_manifest_key_and_for_a_minute(tmp_path, monkeypatch):
    """One hash walk per MCP per minute, unless its manifest (or another
    hash input file) changed on disk or the sync was forced."""
    from services.mcp import mcp_sync

    mcp_dir = tmp_path / "foo"
    mcp_dir.mkdir()
    (mcp_dir / "manifest.json").write_text("{}")
    m = _fake_manifest("foo")
    m.mcp_dir = mcp_dir
    walks = []
    with patch("services.mcp.mcp_installer.compute_version_hash",
               side_effect=lambda d: walks.append(d) or f"h{len(walks)}"):
        assert mcp_sync._version_hash(m) == "h1"
        assert mcp_sync._version_hash(m) == "h1"          # served from the cache
        assert len(walks) == 1
        assert mcp_sync._version_hash(m, force=True) == "h2"  # force walks again
        # A rewritten manifest (an install, update or switch) is a new key.
        (mcp_dir / "manifest.json").write_text('{"v": 2}')
        assert mcp_sync._version_hash(m) == "h3"
        # The entry ages out after the TTL (a source edit under an unchanged manifest).
        now = time.monotonic()
        monkeypatch.setattr(mcp_sync.time, "monotonic", lambda: now + mcp_sync._HASH_TTL_S + 1)
        assert mcp_sync._version_hash(m) == "h4"
        assert len(walks) == 4


@pytest.mark.asyncio
async def test_version_hashes_for_reads_every_manifest_off_the_loop():
    import asyncio

    from services.mcp import mcp_sync

    def _walk(d):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return "off-loop"
        raise AssertionError("the hash walk ran on the event loop")

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=lambda n: _fake_manifest(n) if n != "gone" else None), \
         patch("services.mcp.mcp_installer.compute_version_hash", side_effect=_walk):
        hashes = await mcp_sync.version_hashes_for({"foo", "bar", "gone"})
    assert hashes == {"foo": "off-loop", "bar": "off-loop"}
    with patch("services.mcp.mcp_registry.get_manifest", side_effect=_fake_manifest), \
         patch("services.mcp.mcp_installer.compute_version_hash",
               side_effect=AssertionError("_diff must take the hashes it was given")):
        install, update, remove = mcp_sync._diff(
            desired={"foo"}, installed={"foo": {"version_hash": "old", "healthy": True}},
            hashes={"foo": "new"},
        )
    assert update == {"foo"}


# --- The MCPs a session leaves out by its context alone (D6) ---------------


def _manifest(name, *, exclude_from=("task",), audience="", remote_policy="",
              cred="none", instances=None, runtime="python"):
    from types import SimpleNamespace
    return SimpleNamespace(
        name=name, exclude_from=list(exclude_from), audience=audience,
        remote_policy=remote_policy, instances=instances, label=name,
        credentials=SimpleNamespace(type=cred),
        server=SimpleNamespace(runtime=runtime, transport="stdio"),
    )


def _session(*, client_type="task", role="manager", kind="admin_remote",
             permission_mode="default", external=False):
    from types import SimpleNamespace
    from core.placement import PlacementCapabilities
    sc = SimpleNamespace(placement=PlacementCapabilities(kind=kind), role=role,
                         principal="external" if external else "user")
    return SimpleNamespace(agent_name="agent-1", client_type=client_type,
                           permission_mode=permission_mode, security_context=sc)


async def _lifted(manifests, config, launched=(), *, excluded_creds=(), env_instance=True):
    from types import SimpleNamespace
    from services.mcp import mcp_sync
    resolved = SimpleNamespace(excluded_mcps=set(excluded_creds))
    with patch("services.mcp.mcp_registry.get_agent_mcps", return_value=manifests), \
         patch("services.oauth.credential_resolver.resolve_credentials",
               return_value=resolved) as creds, \
         patch("storage.mcp.mcp_store.get_instance_for_agent_env_delivery",
               return_value={"id": 1} if env_instance else None):
        got = await mcp_sync.lifted_by_context(config, set(launched))
    return got, creds


@pytest.mark.asyncio
async def test_a_task_session_keeps_its_context_excluded_mcps_wanted():
    got, creds = await _lifted(
        [_manifest("image-gen-mcp"), _manifest("file-x", exclude_from=())],
        _session(), launched={"file-x"},
    )
    assert got == {"image-gen-mcp"}
    creds.assert_not_called()          # no credential schema to resolve


@pytest.mark.asyncio
async def test_an_mcp_another_gate_left_out_is_not_lifted():
    """On a chat no context excludes it: something else did, it stays out."""
    got, _ = await _lifted([_manifest("image-gen-mcp")], _session(client_type="dashboard"))
    assert got == set()


@pytest.mark.asyncio
async def test_an_admin_paired_only_mcp_is_not_wanted_on_a_user_paired_machine():
    ssh = _manifest("ssh-hosts", remote_policy="admin_paired_only")
    got, _ = await _lifted([ssh], _session(kind="user_remote"))
    assert got == set()
    got, _ = await _lifted([ssh], _session(kind="admin_remote"))
    assert got == {"ssh-hosts"}


@pytest.mark.asyncio
async def test_the_audience_needs_the_sessions_role():
    gated = _manifest("mcps-mcp", audience="editor")
    assert (await _lifted([gated], _session(role="viewer")))[0] == set()
    assert (await _lifted([gated], _session(role="")))[0] == set()
    assert (await _lifted([gated], _session(role="editor")))[0] == {"mcps-mcp"}


@pytest.mark.asyncio
async def test_credentials_and_instances_never_over_lift():
    infra_ok = _manifest("img", cred="infra")
    infra_missing = _manifest("vid", cred="infra")
    per_user = _manifest("mail", cred="per_user")
    got, _ = await _lifted([infra_ok, infra_missing, per_user], _session(),
                           excluded_creds={"vid"})
    assert got == {"img"}
    from types import SimpleNamespace
    by_file = _manifest("ssh-server", instances=SimpleNamespace(delivery="config_file"))
    by_env = _manifest("music", instances=SimpleNamespace(delivery="env"))
    assert (await _lifted([by_file, by_env], _session()))[0] == {"music"}
    assert (await _lifted([by_env], _session(), env_instance=False))[0] == set()


@pytest.mark.asyncio
async def test_a_judge_or_a_session_without_its_context_lifts_nothing():
    from types import SimpleNamespace
    assert (await _lifted([_manifest("x")], _session(permission_mode="judge")))[0] == set()
    bare = SimpleNamespace(agent_name="agent-1", client_type="task",
                           permission_mode="default", security_context=None)
    assert (await _lifted([_manifest("x")], bare))[0] == set()


@pytest.mark.asyncio
async def test_a_meeting_lifts_what_meetings_exclude():
    got, _ = await _lifted([_manifest("agent-config-mcp", exclude_from=("meeting",))],
                           _session(client_type="meeting"))
    assert got == {"agent-config-mcp"}


@pytest.mark.asyncio
async def test_an_external_callers_session_lifts_what_the_external_context_left_out():
    """The principal is read from the security context as every other
    consumer reads it (``external_identity.is_external_ctx``)."""
    m = _manifest("crm-mcp", exclude_from=("external",))
    assert (await _lifted([m], _session(client_type="phone", external=True)))[0] == {"crm-mcp"}
    assert (await _lifted([m], _session(client_type="phone")))[0] == set()
