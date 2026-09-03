"""Handshake-delivered sync file cap (satellite 0.5.103, Feature D 1.4.0).

The proxy config (OTODOCK_MAX_FILE_MB) is the single source of truth: it
arrives in the ``auth_result`` policy block as ``sync_max_file_bytes`` and is
cached in ``satellite_policy``. ``file_sync.max_file_size()`` returns it when
present, else the legacy 100MB module constant — a new satellite against an
OLD proxy must stay at 100MB or its manifest advertises files the old proxy's
pull leg hard-rejects (permanent churn).
"""

import base64
import hashlib

import pytest

from satellite.host import satellite_policy
from satellite.transport import file_sync
from satellite.transport.file_sync import apply_file_push


@pytest.fixture(autouse=True)
def _reset_policy():
    """Isolate the module-level policy cache between tests."""
    orig = dict(satellite_policy._state)
    yield
    with satellite_policy._lock:
        satellite_policy._state.clear()
        satellite_policy._state.update(orig)


def test_max_file_size_falls_back_to_legacy_without_policy():
    assert "sync_max_file_bytes" not in satellite_policy.get_policy()
    assert file_sync.max_file_size() == file_sync.MAX_FILE_SIZE


def test_max_file_size_reads_handshake_value():
    satellite_policy.set_policy({"sync_max_file_bytes": 123456})
    assert file_sync.max_file_size() == 123456


def test_policy_partial_update_preserves_cap():
    satellite_policy.set_policy({"sync_max_file_bytes": 999})
    # A later partial update (e.g. a live allow_full_fs toggle) must NOT
    # clobber the cap.
    satellite_policy.set_policy({"allow_full_fs": True})
    assert satellite_policy.get_policy().get("sync_max_file_bytes") == 999
    assert file_sync.max_file_size() == 999


def test_policy_invalid_cap_ignored():
    satellite_policy.set_policy({"sync_max_file_bytes": "not-an-int"})
    assert file_sync.max_file_size() == file_sync.MAX_FILE_SIZE


def _write_msg(rel_path: str, data: bytes) -> dict:
    return {
        "path": rel_path,
        "action": "write",
        "content_b64": base64.b64encode(data).decode(),
        "hash": "sha256:" + hashlib.sha256(data).hexdigest(),
    }


def test_inline_write_enforces_policy_cap(tmp_path):
    satellite_policy.set_policy({"sync_max_file_bytes": 8})
    agent = tmp_path / "agent"
    agent.mkdir()
    with pytest.raises(ValueError, match="MAX_FILE_SIZE"):
        apply_file_push(agent, _write_msg("workspace/big.bin", b"x" * 9))
    # At/below the cap commits fine.
    apply_file_push(agent, _write_msg("workspace/ok.bin", b"x" * 8))
    assert (agent / "workspace" / "ok.bin").read_bytes() == b"x" * 8


def test_chunked_write_enforces_policy_cap(tmp_path):
    satellite_policy.set_policy({"sync_max_file_bytes": 8})
    agent = tmp_path / "agent"
    agent.mkdir()
    data = b"x" * 12
    with pytest.raises(ValueError, match="MAX_FILE_SIZE"):
        apply_file_push(agent, {
            "path": "workspace/big.bin",
            "action": "write_chunk",
            "chunk_index": 0,
            "total_chunks": 1,
            "content_b64": base64.b64encode(data).decode(),
            "hash": "sha256:" + hashlib.sha256(data).hexdigest(),
        })
    # The staged .partial must not be left behind.
    assert not list(agent.rglob("*.partial"))


def test_manifest_respects_policy_cap(tmp_path):
    satellite_policy.set_policy({"sync_max_file_bytes": 8})
    agent = tmp_path / "agent"
    (agent / "workspace").mkdir(parents=True)
    (agent / "workspace" / "small.txt").write_bytes(b"x" * 4)
    (agent / "workspace" / "big.bin").write_bytes(b"x" * 20)
    paths = {e["path"] for e in file_sync.compute_manifest(agent)}
    assert paths == {"workspace/small.txt"}
    # Raise the cap → the big file appears.
    satellite_policy.set_policy({"sync_max_file_bytes": 1024})
    paths = {e["path"] for e in file_sync.compute_manifest(agent)}
    assert paths == {"workspace/small.txt", "workspace/big.bin"}
