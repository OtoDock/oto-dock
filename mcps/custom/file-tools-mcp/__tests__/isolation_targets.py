"""Module-level targets for isolation worker tests — spawn children import
this module by name, so the functions must live here, not in the test file."""

import os
import signal
import time


def ok_target(text: str) -> str:
    return f"parsed:{text}"


def big_target(n: int) -> str:
    return "x" * n


def hog_target() -> str:
    chunks = []
    while True:
        chunks.append(bytearray(50 * 1024 * 1024))


def sleep_target(seconds: float) -> str:
    time.sleep(seconds)
    return "slept"


def selfkill_target() -> str:
    os.kill(os.getpid(), signal.SIGKILL)
    return "unreachable"


def raise_target(message: str) -> str:
    raise ValueError(message)


def bytes_target(n: int) -> dict:
    """Structured payload with raw bytes (the render-core result shape)."""
    return {"images": [{"data": b"\x89PNG" + b"x" * n, "mime": "image/png"}],
            "rendered": [{"page": 1}]}


def big_struct_target(n: int) -> dict:
    """Oversized structured payload — must come back as a clean error."""
    return {"data": b"x" * n}


def memerr_target() -> str:
    raise MemoryError


def kbint_target() -> str:
    raise KeyboardInterrupt


def partial_write_target(path: str) -> None:
    """Simulates a core dying mid-atomic-save: partial tmp, no replace."""
    with open(path + ".otodock-tmp", "w") as f:
        f.write("partial garbage")
    raise RuntimeError("boom-mid-save")
