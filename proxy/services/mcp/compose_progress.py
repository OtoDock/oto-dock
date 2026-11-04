"""The latest progress of a compose run, per MCP, in plain words.

``docker_manager._run_compose_streaming`` feeds every line compose prints to
a ``Tracker``; the MCP update job (``mcp_updater.job_state``) reads the
record back through ``progress_of`` so the MCP Servers page can say what a
long pull is doing. The record is written from the compose reader thread and
read on the event loop, so it sits behind a lock.

Compose prints one line per layer event when stderr is not a terminal:
``<layer> Downloading 13.55MB`` (v5: the layer's bytes so far) or
``<layer> Downloading [==>  ]  55MB/2.03GB`` (older releases), ``Download
complete``, ``Extracting``, ``Pull complete``; ``Image <ref> Pulling``;
``Container <name> Recreate`` and the like for an up. An unpack prints a line
per layer event and nothing for minutes, so a phase change is written at
once and only the byte count is throttled.
"""

from __future__ import annotations

import re
import threading
import time

PULLING = "pulling"
DOWNLOADING = "downloading"
EXTRACTING = "extracting"
BUILDING = "building"
STARTING = "starting"

_DETAIL = {
    PULLING: "Looking for the new image.",
    EXTRACTING: "Unpacking the image.",
    BUILDING: "Building the image.",
    STARTING: "Starting the container.",
}

# A layer's state, in the order compose walks it.
_WAITING, _DOWNLOADING, _DOWNLOADED, _EXTRACTING, _DONE = range(5)

_LAYER_RE = re.compile(
    r"^\s*([0-9a-f]{12})\s+(Pulling fs layer|Waiting|Downloading|Verifying Checksum|"
    r"Download complete|Extracting|Pull complete|Already exists)\b(.*)$",
    re.IGNORECASE,
)
_SIZE_RE = re.compile(r"([\d.]+)\s*([kKMG]?B)\b")
_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9}
_LAYER_STATE = {
    "pulling fs layer": _WAITING, "waiting": _WAITING, "downloading": _DOWNLOADING,
    "verifying checksum": _DOWNLOADING, "download complete": _DOWNLOADED,
    "extracting": _EXTRACTING, "pull complete": _DONE, "already exists": _DONE,
}
_IMAGE_RE = re.compile(r"\bImage\s+\S+\s+(Pulling|Building)\b", re.IGNORECASE)
_CONTAINER_RE = re.compile(
    r"^[^A-Za-z]*Container\s+\S+\s+(Recreat|Creat|Start|Stop|Remov|Renam|Waiting|Healthy)",
    re.IGNORECASE,
)
_BUILD_RE = re.compile(r"^\s*(#\d+\s|\[\+\] Building|.*\bBuilding\b)", re.IGNORECASE)

#: A byte count is written at most this often (a phase change at once).
WRITE_EVERY_S = 1.0

_records: dict[str, tuple[object, str, str]] = {}
_lock = threading.Lock()


def human_bytes(n: float) -> str:
    """Bytes the way Docker counts them (decimal units), rounded for a person."""
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.0f} MB"
    return f"{max(n, 0) / 1e3:.0f} kB"


def _size(text: str) -> float | None:
    """The first size in ``text`` (a layer's bytes so far), or None."""
    m = _SIZE_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1)) * _UNITS[m.group(2).lower()]
    except (KeyError, ValueError):
        return None


def progress_of(name: str) -> dict | None:
    """``{phase, detail}`` of the compose run of ``name`` still going, or None."""
    with _lock:
        rec = _records.get(name)
    return {"phase": rec[1], "detail": rec[2]} if rec else None


class Tracker:
    """Reads one compose run's lines and keeps its record for ``name``."""

    def __init__(self, name: str, *, clock=time.monotonic) -> None:
        self.name = name
        self._clock = clock
        self._token = object()
        self._layers: dict[str, list] = {}   # id -> [state, bytes so far]
        self._phase = ""
        self._written: tuple[str, str] | None = None
        self._written_at = 0.0

    def feed(self, line: str) -> None:
        """One line compose printed; a line that names nothing known is skipped."""
        if not self._read(line):
            return
        phase, detail = self._state()
        if not phase or (phase, detail) == self._written:
            return
        now = self._clock()
        same_phase = self._written is not None and self._written[0] == phase
        if same_phase and now - self._written_at < WRITE_EVERY_S:
            return
        self._written, self._written_at = (phase, detail), now
        with _lock:
            _records[self.name] = (self._token, phase, detail)

    def close(self) -> None:
        """The run ended: its record goes, unless another run's replaced it."""
        with _lock:
            rec = _records.get(self.name)
            if rec is not None and rec[0] is self._token:
                del _records[self.name]

    def _read(self, line: str) -> bool:
        m = _LAYER_RE.match(line)
        if m:
            layer, word, rest = m.group(1), m.group(2).lower(), m.group(3)
            entry = self._layers.setdefault(layer, [_WAITING, 0.0])
            state = _LAYER_STATE[word]
            entry[0] = max(entry[0], state)
            if word == "downloading":
                size = _size(rest)
                if size is not None:
                    entry[1] = max(entry[1], size)
            return True
        if _CONTAINER_RE.match(line):
            self._phase = STARTING
            return True
        m = _IMAGE_RE.search(line)
        if m:
            self._phase = PULLING if m.group(1).lower() == "pulling" else BUILDING
            return True
        if _BUILD_RE.match(line) and self._phase in ("", BUILDING):
            self._phase = BUILDING
            return True
        return False

    def _state(self) -> tuple[str, str]:
        """Downloading while a layer downloads (or waits after bytes came in),
        unpacking while one is downloaded and not done or once every layer
        is done, else the last image or container word."""
        if self._phase != STARTING and self._layers:
            states = [e[0] for e in self._layers.values()]
            total = sum(e[1] for e in self._layers.values())
            if _DOWNLOADING in states or (_WAITING in states and total > 0):
                return DOWNLOADING, f"Downloading the new image, {human_bytes(total)} so far."
            if _DOWNLOADED in states or _EXTRACTING in states or (
                    total > 0 and all(s == _DONE for s in states)):
                return EXTRACTING, _DETAIL[EXTRACTING]
            return PULLING, _DETAIL[PULLING]
        if not self._phase:
            return "", ""
        return self._phase, _DETAIL[self._phase]
