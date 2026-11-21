"""A test's own file as ``push_file`` takes a source: the path of a
descriptor the caller opened (``safe_fs.fd_path``), never a name."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path

from services.infra import safe_fs


@contextlib.contextmanager
def opened(path: str | os.PathLike[str]) -> Iterator[Path]:
    """The descriptor's path of ``path``, open for the block."""
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        yield Path(safe_fs.fd_path(fd))
    finally:
        os.close(fd)


def gone() -> Path:
    """A descriptor's path that names no open descriptor: a source that is gone."""
    return Path(safe_fs.fd_path(987_654))
