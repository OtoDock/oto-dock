"""``services/infra/archive_read``: a gzipped tar held in memory, read
under a cap on its unpacked stream, its member count and each member's size.

Load-bearing: tarfile reads a pax or GNU long name header whole when its
data is really there, so a small archive of one such header is refused at
the cap and never read whole; a gzip or tar error is ``tarfile.ReadError``;
an archive at its caps reads.
"""

from __future__ import annotations

import gzip
import io
import tarfile
import tracemalloc
import zlib

import pytest

from services.infra import archive_read

MB = 1024 * 1024


def _plain_tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


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


def _pax_comment(size: int) -> list[bytes | tuple[bytes, int]]:
    """A pax header whose one record is a comment of ``size`` bytes."""
    body = len(" comment=") + size + 1
    length = body + len(str(body))
    length = body + len(str(length))
    info = tarfile.TarInfo("PaxHeaders/big")
    info.type = tarfile.XHDTYPE
    info.size = length
    return [info.tobuf(tarfile.USTAR_FORMAT), f"{length} comment=".encode(), (b"a", size),
            b"\n" + b"\0" * (-length % tarfile.BLOCKSIZE)]


def test_a_normal_archive_reads():
    files = {"root/a.txt": b"one", "root/b/c.json": b"{}"}
    with archive_read.open_tar_gz(gzip.compress(_plain_tar(files)), max_unpacked=MB) as tar:
        found = archive_read.members(tar, max_members=2, max_member_bytes=3)
        assert [m.name for m in found] == ["root/a.txt", "root/b/c.json"]
        reader = tar.extractfile(found[0])
        assert reader is not None and reader.read() == b"one"


def test_a_gzip_whose_unpacked_size_passes_the_cap_is_refused():
    with pytest.raises(archive_read.ArchiveTooLarge, match="more than 1 MB"):
        archive_read.open_tar_gz(_gz((b"\0", MB + 1)), max_unpacked=MB)
    # At the cap it reads (zero blocks are an empty archive).
    with archive_read.open_tar_gz(_gz((b"\0", MB)), max_unpacked=MB) as tar:
        assert archive_read.members(tar, max_members=1, max_member_bytes=1) == []


def test_a_pax_header_past_the_cap_is_refused_before_it_is_read_whole():
    data = _gz(*_pax_comment(16 * MB), _plain_tar({"a.txt": b"x"}))
    assert len(data) < MB
    tracemalloc.start()
    try:
        with pytest.raises(archive_read.ArchiveTooLarge):
            archive_read.open_tar_gz(data, max_unpacked=MB)
        _now, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * MB, peak
    # Under the cap the same header reads, its comment whole.
    with archive_read.open_tar_gz(data, max_unpacked=32 * MB) as tar:
        found = archive_read.members(tar, max_members=1, max_member_bytes=1)
        assert [m.name for m in found] == ["a.txt"]
        assert len(found[0].pax_headers["comment"]) == 16 * MB


def test_too_many_members_are_refused():
    data = gzip.compress(_plain_tar({f"f{i}": b"" for i in range(4)}))
    with archive_read.open_tar_gz(data, max_unpacked=MB) as tar:
        with pytest.raises(archive_read.ArchiveTooLarge, match="more than 3 entries"):
            archive_read.members(tar, max_members=3, max_member_bytes=1)
    with archive_read.open_tar_gz(data, max_unpacked=MB) as tar:
        assert len(archive_read.members(tar, max_members=4, max_member_bytes=1)) == 4


def test_a_member_over_its_cap_is_refused():
    data = gzip.compress(_plain_tar({"small": b"x" * 10, "big": b"x" * 11}))
    with archive_read.open_tar_gz(data, max_unpacked=MB) as tar:
        with pytest.raises(archive_read.ArchiveTooLarge, match="larger than 10 bytes"):
            archive_read.members(tar, max_members=10, max_member_bytes=10)
    with archive_read.open_tar_gz(data, max_unpacked=MB) as tar:
        assert len(archive_read.members(tar, max_members=10, max_member_bytes=11)) == 2


@pytest.mark.parametrize("data", [
    b"not a gzip at all",
    gzip.compress(_plain_tar({"a": b"x"}), mtime=0)[:-12],
    gzip.compress(b"plain words, not a tar", mtime=0),
    b"",
], ids=["not-a-gzip", "a-cut-gzip", "a-gzip-of-no-tar", "empty"])
def test_a_gzip_or_tar_error_is_a_read_error(data):
    with pytest.raises(tarfile.ReadError):
        archive_read.open_tar_gz(data, max_unpacked=MB)
