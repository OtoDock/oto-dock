"""A gzipped tar held in memory, read under caps.

CPython 3.13.18's tarfile reads a GNU long name or a pax header in 1 MiB
chunks (``_safe_read``), which bounds what a header that only claims a size
costs. A header whose data is really there is still read whole, and a few
kilobytes of gzip unpack to gigabytes of it. So the only bound is the
unpacked stream: ``open_tar_gz`` unpacks it whole under ``max_unpacked`` and
refuses past it, before tarfile reads a header. ``members`` then counts the
entries and refuses a regular file over its own cap.

Constraint: the unpacked stream is never handed to tarfile short. tarfile
takes a short read as the end of the archive, so a cut stream would read as
an honest smaller one.
"""

from __future__ import annotations

import gzip
import io
import tarfile
import zlib

_CHUNK = 1024 * 1024
_MB = 1024 * 1024


class ArchiveTooLarge(ValueError):
    """The archive passes one of its caps. The message is for a person."""


def _size(n: int) -> str:
    return f"{n // _MB} MB" if n and n % _MB == 0 else f"{n} bytes"


def open_tar_gz(data: bytes, *, max_unpacked: int) -> tarfile.TarFile:
    """The tar inside the gzip ``data``, unpacked whole into memory first.
    ``ArchiveTooLarge`` when the unpacked stream passes ``max_unpacked``,
    ``tarfile.ReadError`` for a gzip or tar error."""
    raw = io.BytesIO()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            while chunk := gz.read(_CHUNK):
                if raw.tell() + len(chunk) > max_unpacked:
                    raise ArchiveTooLarge(f"The archive unpacks to more than {_size(max_unpacked)}.")
                raw.write(chunk)
    except (OSError, EOFError, zlib.error) as exc:
        # tarfile's own words for the same failure.
        raise tarfile.ReadError("not a gzip file") from exc
    raw.seek(0)
    return tarfile.open(fileobj=raw, mode="r:")


def members(tar: tarfile.TarFile, *, max_members: int, max_member_bytes: int) -> list[tarfile.TarInfo]:
    """Every entry of ``tar``, read one at a time: ``ArchiveTooLarge`` past
    ``max_members`` entries or at a regular file larger than
    ``max_member_bytes``. A tar error while reading is ``tarfile.ReadError``."""
    out: list[tarfile.TarInfo] = []
    for member in tar:
        if len(out) >= max_members:
            raise ArchiveTooLarge(f"The archive holds more than {max_members} entries.")
        if member.isreg() and member.size > max_member_bytes:
            raise ArchiveTooLarge(f"A file in the archive is larger than {_size(max_member_bytes)}.")
        out.append(member)
    return out
