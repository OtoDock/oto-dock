"""The tool vocabulary (``core/events/tool_roles``): the tables agree with
each other, the payload keys are the engines' real ones, the hook script's
twin and the dashboard's mirror equal the authority, and the persisted
snapshot name is a checklist tool. Core-seams phase 2."""

from __future__ import annotations

import ast
import re
import sys

from tests._paths import PROXY_DIR, REPO_ROOT

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

from core.events import tool_roles  # noqa: E402


def test_every_name_has_a_role_a_payload_and_only_shells_a_dialect():
    assert set(tool_roles.TOOL_ROLES) == set(tool_roles.TOOL_PAYLOADS)
    for name, role in tool_roles.TOOL_ROLES.items():
        assert role in tool_roles.ROLES, (name, role)
        payload = tool_roles.TOOL_PAYLOADS[name]
        assert payload == tool_roles.NONE or payload in tool_roles.PAYLOAD_KEYS, (name, payload)
        assert (name in tool_roles.SHELL_DIALECTS) == (role == tool_roles.SHELL), name
    assert set(tool_roles.SHELL_DIALECTS.values()) == {tool_roles.POSIX, tool_roles.POWERSHELL}
    assert len(set(tool_roles.ROLES)) == len(tool_roles.ROLES)


def test_the_questions():
    assert tool_roles.role_of("Bash") == tool_roles.SHELL
    assert tool_roles.role_of("PowerShell") == tool_roles.SHELL
    assert tool_roles.dialect_of("PowerShell") == tool_roles.POWERSHELL
    assert tool_roles.dialect_of("Monitor") == tool_roles.POSIX
    assert tool_roles.dialect_of("Read") == ""
    assert tool_roles.role_of("nope") == "" and tool_roles.payload_of("nope") == ""
    assert tool_roles.role_of("") == "" and tool_roles.role_of(None) == ""
    assert not tool_roles.is_known("mcp__x__y")
    assert tool_roles.writes("Write") and tool_roles.writes("Delete") and tool_roles.writes("apply_patch")
    assert not tool_roles.writes("Read") and not tool_roles.writes("Bash")
    assert set(tool_roles.names_of(tool_roles.SHELL)) == {"Bash", "Monitor", "PowerShell"}
    assert tool_roles.names_of("no-such-role") == ()


def test_payload_values_come_from_the_engines_real_keys():
    """A notebook edit names its file ``notebook_path``; a Codex patch arrives
    under ``command`` on the hook wire and ``input`` in the rollout; a shell
    command is ``command`` only."""
    assert tool_roles.payload_value("NotebookEdit", {"notebook_path": "/n.ipynb"}) == "/n.ipynb"
    assert tool_roles.payload_key("NotebookEdit", {"notebook_path": "/n.ipynb"}) == "notebook_path"
    assert tool_roles.payload_value("Write", {"file_path": "/f", "notebook_path": "/n"}) == "/f"
    assert tool_roles.payload_value("apply_patch", {"command": "*** Begin Patch"}) == "*** Begin Patch"
    assert tool_roles.payload_value("apply_patch", {"input": "*** Begin Patch"}) == "*** Begin Patch"
    assert tool_roles.payload_value("Bash", {"command": "ls"}) == "ls"
    assert tool_roles.payload_value("Bash", {"cmd": "ls"}) == ""
    assert tool_roles.payload_value("Grep", {"path": "/p", "pattern": "x"}) == "/p"
    assert tool_roles.payload_value("Grep", {"pattern": "x"}) == ""
    assert tool_roles.payload_value("WebFetch", {"url": "https://x"}) == "https://x"
    assert tool_roles.payload_value("Agent", {"description": "d"}) == "d"
    assert tool_roles.payload_value("TodoRead", {"anything": "x"}) == ""
    assert tool_roles.payload_value("Read", "not a dict") == ""
    assert tool_roles.payload_value("Read", {"file_path": 3}) == ""


def test_carries_command_is_the_fail_closed_test():
    assert tool_roles.carries_command({"command": "rm -rf /"})
    assert tool_roles.carries_command({"cmd": "ls"})
    assert tool_roles.carries_command({"script": "echo"})
    assert not tool_roles.carries_command({"command": ""})
    assert not tool_roles.carries_command({"command": ["ls"]})
    assert not tool_roles.carries_command({"file_path": "/x"})
    assert not tool_roles.carries_command(None)


def test_the_snapshot_name_is_a_checklist_tool():
    assert tool_roles.role_of(tool_roles.TODO_SNAPSHOT) == tool_roles.CHECKLIST


def _top_level_dict(path, name: str) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = ()
        if isinstance(node, ast.Assign):
            targets = tuple(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = (node.target.id,)
        if name in targets:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path}: no top-level {name}")


def test_the_hook_scripts_twin_equals_the_authority():
    """``hooks/tool_result_forwarder.py`` runs inside the CLI's sandbox and
    cannot import the proxy: its ``TOOL_ROLES`` is a byte twin — the gate's
    twin rule compares the two definitions in key order; this compares them
    by value so a spurious twin report is recognisable."""
    twin = _top_level_dict(PROXY_DIR / "hooks" / "tool_result_forwarder.py", "TOOL_ROLES")
    assert twin == tool_roles.TOOL_ROLES
    assert list(twin) == list(tool_roles.TOOL_ROLES), "the twin rule compares in key order"


_TS_ENTRY = re.compile(r"""^\s*(?:'([^']+)'|"([^"]+)"|([A-Za-z_]\w*))\s*:\s*'([^']*)'\s*,?\s*(?://.*)?$""")


def _ts_record(text: str, name: str) -> dict[str, str]:
    """The entries of ``export const <name>: … = { 'k': 'v', … }`` in a TS
    source, one per line (the mirror is written that way on purpose)."""
    start = text.index(f"export const {name}")
    body = text[text.index("{", start) + 1:]
    body = body[:body.index("\n}")]
    out: dict[str, str] = {}
    for line in body.splitlines():
        if not line.strip() or line.strip().startswith("//"):
            continue
        m = _TS_ENTRY.match(line)
        assert m, f"{name}: unreadable mirror line {line!r}"
        out[m.group(1) or m.group(2) or m.group(3)] = m.group(4)
    return out


def test_the_dashboards_mirror_equals_the_authority():
    """``dashboard/src/lib/tools/roles.ts`` is the platform's table on the
    other side of the wire (recipe step 3: one mirror, one lock-step test)."""
    text = (REPO_ROOT / "dashboard" / "src" / "lib" / "tools" / "roles.ts").read_text(encoding="utf-8")
    assert _ts_record(text, "TOOL_ROLES") == tool_roles.TOOL_ROLES
    assert _ts_record(text, "TOOL_PAYLOADS") == tool_roles.TOOL_PAYLOADS
    assert f"'{tool_roles.TODO_SNAPSHOT}'" in text
    from core.events.stream_pump import DELEGATE_TOOL
    assert f"'{DELEGATE_TOOL}'" in text
