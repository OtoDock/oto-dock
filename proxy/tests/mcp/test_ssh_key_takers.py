"""Who takes the agent's SSH keys and what rides a machine's start for them:
the editor tier, never a judge or an outside caller, and the public halves
alone, on a satellite that opens the agent socket.
"""

import sys

import pytest

from tests._paths import PROXY_DIR

_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from core import placement


def _machine(version="0.5.173", os="linux"):
    return placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id="m-1",
                                           satellite_version=version, os=os)


def _remote_cfg(scope="user", username="alice", user_sub="sub-1", *, version="0.5.173", os="linux"):
    from types import SimpleNamespace

    from core.execution_layer import AgentConfig
    return AgentConfig(
        agent_name="agent", user_sub=user_sub,
        # A manager's session: the keys take the editor tier as well as the
        # admin-paired machine these cases are about.
        security_context=SimpleNamespace(
            session_scope=scope, username=username, role="manager",
            placement=_machine(version, os),
        ),
    )


def test_session_takes_ssh_keys():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from core.sandbox.session_config_dir import session_takes_ssh_keys
    # The editor tier, read off the context's role; anything else is no keys.
    assert session_takes_ssh_keys(SimpleNamespace(role="editor", read_only=False)) is True
    assert session_takes_ssh_keys(SimpleNamespace(role="manager", read_only=True)) is False
    assert session_takes_ssh_keys(SimpleNamespace(role="manager", principal="external")) is False
    assert session_takes_ssh_keys(SimpleNamespace(role="contributor")) is False
    assert session_takes_ssh_keys(SimpleNamespace(read_only=False)) is False  # no role: no keys
    assert session_takes_ssh_keys(None) is False
    assert session_takes_ssh_keys(MagicMock()) is False  # a mock is not a role


# ── the proxy signs, no private key leaves it ──────────────────────────────

@pytest.mark.asyncio
async def test_the_agent_socket_payload_follows_the_tier_and_the_satellite(monkeypatch):
    """The public halves ride the start for a session that takes the keys,
    on every machine at the platform's floor (each opens the socket or the
    pipe)."""
    from types import SimpleNamespace

    from core.credentials import ssh_signer
    from core.remote.remote_start_payload import ssh_agent_payload
    monkeypatch.setattr(ssh_signer, "public_lines", lambda agent: {"prod": "ssh-ed25519 AAAA prod\n"})
    assert await ssh_agent_payload(_remote_cfg()) == {
        "public_keys": {"prod": "ssh-ed25519 AAAA prod\n"}}
    assert await ssh_agent_payload(_remote_cfg(version="0.6.0", os="darwin")) is not None
    assert await ssh_agent_payload(_remote_cfg(os="windows")) is not None
    # A judge, and a person below the editor tier, take no keys and no socket.
    judge = _remote_cfg()
    judge.security_context = SimpleNamespace(session_scope="user", username="alice", role="manager",
                                             read_only=True, placement=_machine())
    assert await ssh_agent_payload(judge) is None
    viewer = _remote_cfg()
    viewer.security_context = SimpleNamespace(session_scope="user", username="alice", role="viewer",
                                              placement=_machine())
    assert await ssh_agent_payload(viewer) is None
    # No key authorised: nothing to open a socket for.
    monkeypatch.setattr(ssh_signer, "public_lines", lambda agent: {})
    assert await ssh_agent_payload(_remote_cfg()) is None
