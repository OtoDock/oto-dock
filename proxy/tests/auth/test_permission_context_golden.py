"""The permission context rendered from the placement descriptor is
byte-identical to the one the disassembled fields rendered (core-seams
phase 7, D19). The fixture holds ``build_permission_context`` for six
placements × three engines, captured at ``30dca287`` before the first edit
of the phase; the six contexts below are the same ones spelled through
``PlacementCapabilities``.
"""

from __future__ import annotations

import json

import pytest

from auth.path_policy import SecurityContext  # noqa: I001 — path_policy first: path_prompt imports it
from auth import path_prompt
from core import placement
from tests._paths import PROXY_DIR

_GOLDEN = json.loads((PROXY_DIR / "tests" / "fixtures" / "permission_context_golden.json").read_text())
P = placement.PlacementCapabilities

CASES = {
    "local_manager": SecurityContext(role="manager", username="alice", agent="pa", is_admin_agent=False,
                                     display_name="Alice", email="a@x"),
    "local_viewer": SecurityContext(role="viewer", username="bob", agent="pa", is_admin_agent=False),
    "local_contributor": SecurityContext(role="contributor", username="cara", agent="pa", is_admin_agent=False),
    "admin_remote": SecurityContext(
        role="manager", username="alice", agent="pa", is_admin_agent=False,
        placement=P(kind=placement.KIND_ADMIN_REMOTE, label="Office-PC", agents_dir="/home/svc/.oto-dock/agents",
                    machine_id="m-1", home_dir="/home/svc", allow_full_fs=False, os_user="svc",
                    user_dirs={"desktop": "/home/svc/Desktop", "downloads": "/home/svc/Downloads"},
                    claude_runtime_root="/tmp/claude-1000", device_grants={"computer"})),
    "admin_remote_fullfs": SecurityContext(
        role="admin", username="ada", agent="ops", is_admin_agent=True,
        placement=P(kind=placement.KIND_ADMIN_REMOTE, label="Office-PC", agents_dir="/home/svc/.oto-dock/agents",
                    machine_id="m-1", home_dir="/home/svc", allow_full_fs=True, os_user="svc")),
    "user_remote": SecurityContext(
        role="manager", username="dave", agent="pa", is_admin_agent=False,
        placement=P(kind=placement.KIND_USER_REMOTE, label="Dave MBP", agents_dir="/Users/dave/.oto-dock/agents",
                    machine_id="m-2", home_dir="/Users/dave", allow_full_fs=False, os_user="dave",
                    user_dirs={"desktop": "/Users/dave/Desktop"})),
    "user_remote_windows": SecurityContext(
        role="editor", username="erin", agent="pa", is_admin_agent=False,
        placement=P(kind=placement.KIND_USER_REMOTE, label="Erin-PC", agents_dir="C:/Users/erin/OtoDock/agents",
                    machine_id="m-3", home_dir="C:/Users/erin", allow_full_fs=True, os_user="erin",
                    user_dirs={"documents": "C:/Users/erin/Documents"})),
}


@pytest.mark.parametrize("key", sorted(_GOLDEN))
def test_the_permission_context_is_byte_identical(key):
    name, engine = key.split("/")
    text = path_prompt.build_permission_context(CASES[name], assigned_mcp_names=("memory-mcp",),
                                                execution_path=engine)
    assert text == _GOLDEN[key]
