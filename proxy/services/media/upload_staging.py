"""The chunked-upload staging tree's accounting: one directory per person
(a hash of the sub), an open upload's real occupancy, and the staged bytes
per person and in total that the quota monitor and the storage view read.
The upload routes (``api/media/uploads.py``) write the tree.
"""

import hashlib
import json
from pathlib import Path

import config


def _user_staging_dirname(sub: str) -> str:
    """One staging subdirectory per user, named by a hash of the sub (subs are
    opaque ids and never become directory names)."""
    return hashlib.sha256(sub.encode("utf-8")).hexdigest()[:32]


def _occupancy(meta: dict) -> int:
    """The bytes an open upload actually holds on disk (every received chunk
    is exactly chunk_size but the last), never its declared size."""
    try:
        size = int(meta.get("size", 0))
        chunk_size = int(meta.get("chunk_size", 0))
        received = len(meta.get("received") or [])
    except (TypeError, ValueError):
        return 0
    return max(0, min(received * chunk_size, size))


def _open_metas(user_dir: Path) -> list[tuple[Path, dict, float]]:
    """``(meta path, meta, mtime)`` for every open upload in a user dir."""
    out: list[tuple[Path, dict, float]] = []
    try:
        entries = list(user_dir.iterdir())
    except OSError:
        return out
    for p in entries:
        if p.suffix != ".json":
            continue
        try:
            out.append((p, json.loads(p.read_text()), p.stat().st_mtime))
        except (OSError, ValueError):
            continue
    return out


def staged_bytes_by_user() -> dict[str, int]:
    """Bytes actually staged per user dir (the hashed sub), for the quota view."""
    out: dict[str, int] = {}
    root = config.UPLOAD_STAGING_DIR
    if not root.is_dir():
        return out
    try:
        for d in root.iterdir():
            if d.is_dir():
                out[d.name] = sum(_occupancy(m) for _p, m, _t in _open_metas(d))
    except OSError:
        pass
    return out


def staged_bytes_total() -> int:
    return sum(staged_bytes_by_user().values())


def staged_bytes_for(sub: str) -> int:
    """Bytes one user has staged (their directory only), for the per-user
    quota bucket."""
    d = config.UPLOAD_STAGING_DIR / _user_staging_dirname(sub)
    if not d.is_dir():
        return 0
    try:
        return sum(_occupancy(m) for _p, m, _t in _open_metas(d))
    except OSError:
        return 0
