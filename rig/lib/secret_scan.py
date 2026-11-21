#!/usr/bin/env python3
"""The last step of a rig run: no secret of the install or of the run sits in the
run folder, which is copied off the rig to be read.

    python3 rig/lib/secret_scan.py <run folder> --env <install .env> --state-dir <dir>

It looks for the value of every secret in the install's ``.env`` (a name with
PASSWORD, SECRET, _KEY or TOKEN, a value of 12 characters or more, a public key
left out), every password and API key the run made (``secrets.json`` and the
``otok_`` keys in ``state.json`` of the state folder), and any line that sets a
machine's secret or sends a pairing token with a value. A hit is printed as the
file and what it is, never the value, and the exit is 1. Exit 0 when clean.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SECRET_NAME = re.compile(r"PASSWORD|SECRET|_KEY|TOKEN")
MIN_LENGTH = 12
LINES = (
    ("a machine's secret", re.compile(rb"MACHINE_SECRET\s*=\s*['\"]?[A-Za-z0-9_\-]{12,}")),
    ("a pairing token", re.compile(rb"X-Pairing-Token\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}")),
)


def env_secrets(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if not path.exists():
        return found
    for line in path.read_text(errors="replace").splitlines():
        name, sep, value = line.strip().partition("=")
        value = value.strip().strip("'\"")
        if sep and SECRET_NAME.search(name) and "PUBLIC" not in name and len(value) >= MIN_LENGTH:
            found[f".env {name}"] = value
    return found


def _strings(node, trail: str):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{trail}.{key}" if trail else str(key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _strings(value, f"{trail}[{i}]")
    elif isinstance(node, str):
        yield trail, node


def run_secrets(state_dir: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for name in ("secrets.json", "state.json"):
        path = state_dir / name
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        for trail, value in _strings(data, ""):
            if name == "secrets.json" and len(value) >= MIN_LENGTH:
                found[f"the run's password {trail}"] = value
            elif value.startswith("otok_"):
                found[f"the run's key {trail}"] = value
    return found


def scan(folder: Path, secrets: dict[str, str]) -> list[str]:
    hits: list[str] = []
    wanted = {what: value.encode() for what, value in secrets.items()}
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and not p.is_symlink()):
        data = path.read_bytes()
        rel = path.relative_to(folder)
        hits += [f"{rel}: {what}" for what, value in wanted.items() if value in data]
        hits += [f"{rel}: {what}" for what, pattern in LINES if pattern.search(data)]
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("folder")
    parser.add_argument("--env", default="")
    parser.add_argument("--state-dir", default="")
    args = parser.parse_args(argv)
    secrets = env_secrets(Path(args.env)) if args.env else {}
    if args.state_dir:
        secrets.update(run_secrets(Path(args.state_dir)))
    hits = scan(Path(args.folder), secrets)
    for hit in hits:
        print(f"secret in the run folder: {hit}")
    print(f"secret scan: {len(secrets)} values and {len(LINES)} line shapes, {len(hits)} hit(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
