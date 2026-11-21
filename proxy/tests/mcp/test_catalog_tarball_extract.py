"""Catalog tarball extraction — folder-name match + manifest-name fallback.

The catalog repo's folder usually shares the MCP's name, but the authoritative
id is the ``name`` inside each folder's manifest.json and the two can differ
(folder ``workspace-mcp`` ships manifest name ``google-workspace``). The
extractor must resolve both, and return None for an unknown name.
"""

import gzip
import io
import json
import tarfile
import zlib
from pathlib import Path

import pytest

from services.community.community_installer import _extract_mcp_subfolder


def _make_tarball(folders: dict[str, dict]) -> bytes:
    """Build a GitHub-style repo tarball: <owner>-<repo>-<sha>/<folder>/..."""
    prefix = "OtoDock-community-mcps-abc1234"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for folder, files in folders.items():
            d = tarfile.TarInfo(name=f"{prefix}/{folder}")
            d.type = tarfile.DIRTYPE
            tf.addfile(d)
            for rel, content in files.items():
                data = content.encode()
                fi = tarfile.TarInfo(name=f"{prefix}/{folder}/{rel}")
                fi.size = len(data)
                tf.addfile(fi, io.BytesIO(data))
    return buf.getvalue()


def _manifest(name: str) -> str:
    return json.dumps({"name": name, "category": "community",
                       "server": {"runtime": "python"}})


def test_extracts_folder_matching_name(tmp_path: Path):
    tb = _make_tarball({
        "camoufox": {"manifest.json": _manifest("camoufox"), "run.py": "x"},
    })
    out = _extract_mcp_subfolder(tb, "camoufox", tmp_path)
    assert out is not None and out.name == "camoufox"
    assert (out / "manifest.json").is_file()
    assert (out / "run.py").read_text() == "x"


def test_falls_back_to_manifest_name_scan(tmp_path: Path):
    tb = _make_tarball({
        "camoufox": {"manifest.json": _manifest("camoufox")},
        "workspace-mcp": {"manifest.json": _manifest("google-workspace"),
                          "server.py": "y"},
    })
    out = _extract_mcp_subfolder(tb, "google-workspace", tmp_path)
    assert out is not None and out.name == "workspace-mcp"
    assert json.loads((out / "manifest.json").read_text())["name"] == "google-workspace"
    assert (out / "server.py").read_text() == "y"


def test_unknown_name_returns_none(tmp_path: Path):
    tb = _make_tarball({
        "camoufox": {"manifest.json": _manifest("camoufox")},
    })
    assert _extract_mcp_subfolder(tb, "no-such-mcp", tmp_path) is None


def test_malformed_manifest_is_skipped(tmp_path: Path):
    tb = _make_tarball({
        "broken": {"manifest.json": "{not json"},
        "workspace-mcp": {"manifest.json": _manifest("google-workspace")},
    })
    out = _extract_mcp_subfolder(tb, "google-workspace", tmp_path)
    assert out is not None and out.name == "workspace-mcp"


def test_a_template_tarball_takes_regular_files_only():
    # The agents catalog's extraction: a link member (a symlink or a
    # hardlink, its target in the archive or not) is skipped, never a
    # KeyError; the template's files come out.
    import shutil

    from services.community.community_agents_catalog import _extract_template_subfolder
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def add(name, data=b"", **kw):
            ti = tarfile.TarInfo(name)
            for k, v in kw.items():
                setattr(ti, k, v)
            ti.size = len(data) if ti.type == tarfile.REGTYPE else 0
            tf.addfile(ti, io.BytesIO(data) if ti.type == tarfile.REGTYPE else None)
        add("root-abc/tpl/agent.json", b"{}")
        add("root-abc/tpl/context/a.md", b"inner")
        add("root-abc/tpl/context/out.md", type=tarfile.SYMTYPE, linkname="/etc/hostname")
        add("root-abc/tpl/context/in.md", type=tarfile.SYMTYPE, linkname="a.md")
        add("root-abc/tpl/context/hard.md", type=tarfile.LNKTYPE, linkname="etc/passwd")
    d = _extract_template_subfolder(buf.getvalue(), "tpl")
    try:
        assert sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()) == ["agent.json", "context/a.md"]
        assert not any(p.is_symlink() for p in d.rglob("*"))
    finally:
        shutil.rmtree(d.parent, ignore_errors=True)


# The catalog's caps (``community_catalog``): an archive past any of them is
# refused before anything is written, by both extractors.

MB = 1024 * 1024


def _gz(*parts: bytes | tuple[bytes, int]) -> bytes:
    """A gzip stream of the parts: bytes, or ``(byte, count)`` for that many
    copies, compressed as they come so the test never holds them whole."""
    z = zlib.compressobj(6, zlib.DEFLATED, 31)
    out = []
    for part in parts:
        if isinstance(part, bytes):
            out.append(z.compress(part))
            continue
        byte, count = part
        block = byte * MB
        while count > 0:
            out.append(z.compress(block[:min(count, MB)]))
            count -= MB
    out.append(z.flush())
    return b"".join(out)


def _header(name: str, size: int = 0, kind: bytes = tarfile.REGTYPE) -> bytes:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = size
    return info.tobuf(tarfile.PAX_FORMAT)


def _catalog(*filler: bytes | tuple[bytes, int]) -> bytes:
    """A repo tarball whose ``camoufox`` folder is both an MCP and a
    template, then the filler (headers and data of other folders)."""
    plain = _make_tarball({"camoufox": {"manifest.json": _manifest("camoufox"), "agent.json": "{}"}})
    with tarfile.open(fileobj=io.BytesIO(plain)) as tf:
        tf.getmembers()
        end = tf.offset
    raw = gzip.decompress(plain)
    return _gz(raw[:end], *filler, b"\0" * (2 * tarfile.BLOCKSIZE))


def _mcp(tb: bytes, tmp_path: Path) -> object:
    return _extract_mcp_subfolder(tb, "camoufox", tmp_path)


def _template(tb: bytes, tmp_path: Path) -> object:
    # The extractor's own temporary folder is made in ``tmp_path``: a refusal
    # leaves none behind.
    import shutil
    import tempfile

    from services.community.community_agents_catalog import _extract_template_subfolder
    saved, tempfile.tempdir = tempfile.tempdir, str(tmp_path)
    try:
        d = _extract_template_subfolder(tb, "camoufox")
    finally:
        tempfile.tempdir = saved
    shutil.rmtree(d.parent, ignore_errors=True)
    return d


@pytest.mark.parametrize("extract", [_mcp, _template])
def test_an_honest_catalog_extracts(extract, tmp_path: Path):
    assert extract(_catalog(_header("OtoDock-community-mcps-abc1234/other/a.txt", 1), b"x".ljust(512, b"\0")),
                   tmp_path).name == "camoufox"


@pytest.mark.parametrize("extract", [_mcp, _template])
def test_a_catalog_of_too_many_entries_is_refused(extract, tmp_path: Path):
    from services.community import community_catalog
    filler = [_header(f"OtoDock-community-mcps-abc1234/filler/d{i}", kind=tarfile.DIRTYPE)
              for i in range(community_catalog.CATALOG_MAX_MEMBERS)]
    with pytest.raises(ValueError, match="more than 10000 entries"):
        extract(_catalog(*filler), tmp_path)
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("extract", [_mcp, _template])
def test_a_catalog_file_over_its_cap_is_refused(extract, tmp_path: Path):
    from services.community import community_catalog
    size = community_catalog.CATALOG_MEMBER_MAX_BYTES + 1
    big = _header("OtoDock-community-mcps-abc1234/filler/big.bin", size)
    with pytest.raises(ValueError, match="larger than 32 MB"):
        extract(_catalog(big, (b"\0", size + (-size % tarfile.BLOCKSIZE))), tmp_path)
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("extract", [_mcp, _template])
def test_a_catalog_past_the_unpacked_cap_is_refused(extract, tmp_path: Path):
    # Five files of 26 MB: each under its own cap, together past 128 MB.
    filler: list[bytes | tuple[bytes, int]] = []
    for i in range(5):
        filler += [_header(f"OtoDock-community-mcps-abc1234/filler/f{i}.bin", 26 * MB), (b"\0", 26 * MB)]
    with pytest.raises(ValueError, match="more than 128 MB"):
        extract(_catalog(*filler), tmp_path)
    assert not any(tmp_path.iterdir())


def test_an_oversized_template_catalog_is_one_sentence(monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from services.community import community_agents_catalog
    from services.infra import archive_read

    async def _too_big():
        raise archive_read.ArchiveTooLarge("The archive unpacks to more than 128 MB.")

    monkeypatch.setattr(community_agents_catalog, "_fetch_tarball", _too_big)
    with pytest.raises(HTTPException) as e:
        asyncio.run(community_agents_catalog.fetch_and_extract_template("tpl"))
    assert e.value.status_code == 502
    assert e.value.detail == "The community agents could not be read. The archive unpacks to more than 128 MB."


def test_an_oversized_mcp_catalog_is_one_sentence_in_the_install_job():
    from api.mcp import community as community_api
    from services.infra import archive_read
    said = community_api._install_failure(archive_read.ArchiveTooLarge("The archive holds more than 10000 entries."))
    assert said == "The community list could not be read. The archive holds more than 10000 entries."
