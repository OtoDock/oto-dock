"""Tests for the storage-quota write-path hardening:
  * EDQUOT/ENOSPC mid-write must drop the orphan ``.partial`` and re-raise
    (core/file_sync.apply_incoming_file), never leak a manifest-invisible temp;
  * the recover-bin per-agent byte cap evicts oldest-first.
"""

import base64

import config
import pytest
from core.remote import file_sync
from storage.files import recover_bin_store


# --- .partial cleanup on a failed write -----------------------------------

def test_write_cleans_partial_on_replace_oserror(tmp_path, monkeypatch):
    payload = base64.b64encode(b"hello").decode()

    def boom(src, dst):
        raise OSError("EDQUOT")

    monkeypatch.setattr(file_sync.os, "replace", boom)
    with pytest.raises(OSError):
        file_sync.apply_incoming_file(tmp_path, "notes.txt", "write", payload)
    assert not (tmp_path / "notes.txt").exists()
    assert list(tmp_path.rglob("*.partial")) == []  # orphan reaped


def test_write_chunk_cleans_partial_on_fsync_oserror(tmp_path, monkeypatch):
    payload = base64.b64encode(b"chunk").decode()

    def boom(fd):
        raise OSError("ENOSPC")

    monkeypatch.setattr(file_sync.os, "fsync", boom)
    with pytest.raises(OSError):
        file_sync.apply_incoming_file(tmp_path, "big.bin", "write_chunk", payload,
                                      final_chunk=False)
    assert list(tmp_path.rglob("*.partial")) == []


def test_write_success_leaves_no_partial(tmp_path):
    payload = base64.b64encode(b"ok").decode()
    file_sync.apply_incoming_file(tmp_path, "good.txt", "write", payload)
    assert (tmp_path / "good.txt").read_bytes() == b"ok"
    assert not (tmp_path / "good.txt.partial").exists()


# --- recover-bin per-agent cap --------------------------------------------

def test_recover_bin_cap_evicts_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RECOVER_BIN_DIR", tmp_path)
    monkeypatch.setattr(config, "RECOVER_BIN_AGENT_MAX_BYTES", 250)
    e1 = recover_bin_store.capture("ag", "workspace/a.txt", b"a" * 100, "deleted")
    e2 = recover_bin_store.capture("ag", "workspace/b.txt", b"b" * 100, "deleted")
    assert e1 and e2
    # third capture (100) would push total to 300 > 250 → evict the oldest (e1)
    e3 = recover_bin_store.capture("ag", "workspace/c.txt", b"c" * 100, "deleted")
    assert e3
    assert recover_bin_store.get(e1["entry_id"]) is None       # evicted
    assert recover_bin_store.get(e2["entry_id"]) is not None
    assert recover_bin_store.get(e3["entry_id"]) is not None


def test_recover_bin_cap_unlimited_when_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RECOVER_BIN_DIR", tmp_path)
    monkeypatch.setattr(config, "RECOVER_BIN_AGENT_MAX_BYTES", 0)
    ids = []
    for i in range(4):
        e = recover_bin_store.capture("ag", f"workspace/f{i}.txt",
                                      bytes([65 + i]) * 100, "deleted")
        assert e
        ids.append(e["entry_id"])
    assert all(recover_bin_store.get(i) is not None for i in ids)  # nothing evicted
