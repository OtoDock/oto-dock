"""Satellite stat-fingerprint (file_sync.compute_fingerprint).

A cheap STAT-only fingerprint that flips on any sync-relevant add / modify /
delete, used by the proxy to gate its idle-sync sweep. Same exclusions as the
manifest; NO content read; opaque to the proxy (same-satellite comparison only).
"""

from satellite.transport import file_sync


def test_empty_or_missing_dir_is_empty_string(tmp_path):
    assert file_sync.compute_fingerprint(tmp_path / "nope") == ""
    (tmp_path / "empty").mkdir()
    assert file_sync.compute_fingerprint(tmp_path / "empty") == ""


def test_deterministic_and_nonempty(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    (d / "f.txt").write_text("hello")
    fp1 = file_sync.compute_fingerprint(d)
    fp2 = file_sync.compute_fingerprint(d)
    assert fp1 == fp2 and fp1 != ""


def test_changes_on_add(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    (d / "f.txt").write_text("hello")
    fp1 = file_sync.compute_fingerprint(d)
    (d / "g.txt").write_text("world")
    assert file_sync.compute_fingerprint(d) != fp1


def test_changes_on_modify(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    p = d / "f.txt"
    p.write_text("hello")
    fp1 = file_sync.compute_fingerprint(d)
    p.write_text("hello, world")  # size (+ mtime) change
    assert file_sync.compute_fingerprint(d) != fp1


def test_changes_on_delete(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    (d / "f.txt").write_text("hello")
    (d / "g.txt").write_text("world")
    fp1 = file_sync.compute_fingerprint(d)
    (d / "g.txt").unlink()
    assert file_sync.compute_fingerprint(d) != fp1


def test_excludes_partial_and_skipdirs(tmp_path):
    # A `.partial` staging file + a SKIP_DIR (node_modules) must NOT affect the
    # fingerprint — same exclusions as the manifest, so the merge & the fingerprint
    # agree on what counts as a sync-relevant change.
    d = tmp_path / "a"
    d.mkdir()
    (d / "f.txt").write_text("hello")
    fp1 = file_sync.compute_fingerprint(d)
    (d / "f.txt.partial").write_text("aborted-transfer-staging")
    nm = d / "node_modules"
    nm.mkdir()
    (nm / "lib.js").write_text("x" * 100)
    assert file_sync.compute_fingerprint(d) == fp1
