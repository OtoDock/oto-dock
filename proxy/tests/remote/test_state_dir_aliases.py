"""The machine's own OtoDock state stays refused through the aliases a host
resolves and a lexical compare cannot see (``services/path_policy_v2.py``,
``core/placement.py``): a Windows 8.3 short name, a component Win32 strips
(a trailing dot or space), a stream name, a UNC or device path, a leading
double slash on POSIX, and a home reached through a link (the satellite
reports the home and its OtoDock root unresolved beside the resolved ones).

Run: cd proxy && venv/bin/pytest tests/remote/test_state_dir_aliases.py -v
"""

from __future__ import annotations

import json

import pytest

from core import placement
from services.path_policy_v2 import PathPolicyContext, resolve_path_for_session


def _windows(**kw) -> PathPolicyContext:
    caps = dict(kind=placement.KIND_ADMIN_REMOTE, machine_id="m", os="windows",
                home_dir="C:/Users/example-long", os_user="example",
                agents_dir="C:/Users/example-long/OtoDock/agents", allow_full_fs=True,
                claude_runtime_root="C:/Users/EXAMPL~1/AppData/Local/Temp/claude")
    caps.update(kw)
    return PathPolicyContext(agent_slug="my-agent", role="manager",
                             placement=placement.PlacementCapabilities(**caps))


def _linux(**kw) -> PathPolicyContext:
    caps = dict(kind=placement.KIND_ADMIN_REMOTE, machine_id="m", os="linux",
                home_dir="/data/home/bob", os_user="bob",
                agents_dir="/data/home/bob/.oto-dock/agents", allow_full_fs=True)
    caps.update(kw)
    return PathPolicyContext(agent_slug="my-agent", role="manager",
                             placement=placement.PlacementCapabilities(**caps))


def _refused(ctx: PathPolicyContext, path: str, *, writing: bool = False) -> str:
    r = resolve_path_for_session(ctx, path, writing=writing)
    assert not r.allowed and r.protected is True, (path, r)
    return r.error


def _allowed(ctx: PathPolicyContext, path: str, *, writing: bool = False) -> None:
    r = resolve_path_for_session(ctx, path, writing=writing)
    assert r.allowed, (path, r)


@pytest.mark.parametrize("path", [
    "C:\\Users\\EXAMPL~1\\OtoDock\\satellite.conf",
    "C:/Users/EXAMPL~1/OtoDock/agents/other-agent/workspace/x.md",
    "C:/Users/example-long/OTO-DO~1/browser-profiles/a/Default/Cookies",
    "C:/Users/example-long/OtoDock./satellite.conf",
    "C:/Users/example-long/OtoDock /satellite.conf",
    "C:/Users/example-long/OtoDock::$INDEX_ALLOCATION/satellite.conf",
    "//?/C:/Users/example-long/OtoDock/satellite.conf",
    "\\\\?\\C:\\Users\\example-long\\OtoDock\\satellite.conf",
    "//localhost/c$/Users/example-long/OtoDock/satellite.conf",
    "//./C:/Users/example-long/OtoDock/satellite.conf",
])
def test_a_windows_alias_of_the_state_is_refused(path):
    _refused(_windows(), path)
    _refused(_windows(), path, writing=True)


def test_a_windows_session_keeps_its_ordinary_paths():
    ctx = _windows()
    _allowed(ctx, "C:/Users/example-long/Documents/plan.docx")
    _allowed(ctx, "D:/data/report~final.xlsx")
    _allowed(ctx, "C:/Users/example-long/OtoDock/agents/my-agent/workspace/x.md")
    # The CLI's runtime tree under the temp folder as the machine spells it
    # (TEMP often carries the profile's short name).
    _allowed(ctx, "C:/Users/EXAMPL~1/AppData/Local/Temp/claude/c--work/"
                  "11111111-2222-3333-4444-555555555555/tasks/b1.output")


def test_a_posix_double_slash_is_refused():
    ctx = _linux()
    _refused(ctx, "//data/home/bob/.oto-dock/satellite.conf")
    _allowed(ctx, "/data/home/bob/notes/plan.md")


def test_a_home_reached_through_a_link_is_refused_its_state():
    """The satellite reports its home resolved; the CLI may name it through
    the link. Both spellings of the OtoDock root are refused."""
    ctx = _linux(unresolved_home_dir="/home/bob", unresolved_otodock_dir="/home/bob/.oto-dock")
    for path in ("/home/bob/.oto-dock/satellite.conf",
                 "/home/bob/.oto-dock/agents/other-agent/workspace/x.md",
                 "/data/home/bob/.oto-dock/satellite.conf"):
        assert "OtoDock folder" in _refused(ctx, path)
    _allowed(ctx, "/home/bob/notes/plan.md")


def test_the_state_dirs_carry_the_unresolved_roots():
    P = placement.PlacementCapabilities
    p = P(os="linux", home_dir="/data/home/bob", reported_otodock_dir="/data/home/bob/.oto-dock",
          unresolved_home_dir="/home/bob", unresolved_otodock_dir="/home/bob/.oto-dock")
    assert p.state_dirs == ("/data/home/bob/.oto-dock", "/home/bob/.oto-dock")
    win = P(os="windows", home_dir="C:/Users/example-long",
            unresolved_home_dir="C:/Users/EXAMPL~1",
            unresolved_otodock_dir="C:/Users/EXAMPL~1/OtoDock")
    assert win.state_dirs == ("C:/Users/example-long/OtoDock", "C:/Users/EXAMPL~1/OtoDock",
                              "C:/Users/example-long/.oto-dock", "C:/Users/EXAMPL~1/.oto-dock")
    # A report without them (0.5.130 and older): the resolved roots alone.
    assert P(os="linux", home_dir="/home/bob").state_dirs == ("/home/bob/.oto-dock",)


def test_from_machine_reads_the_unresolved_roots_and_tolerates_their_absence():
    caps = json.dumps({"os": "linux", "home_dir": "/data/home/bob",
                       "home_dir_unresolved": "/home/bob",
                       "otodock_dir_unresolved": "/home/bob/.oto-dock"})
    p = placement.from_machine(placement.KIND_ADMIN_REMOTE, {"id": "m", "capabilities": caps})
    assert (p.unresolved_home_dir, p.unresolved_otodock_dir) == ("/home/bob", "/home/bob/.oto-dock")
    old = placement.from_machine(placement.KIND_ADMIN_REMOTE, {
        "id": "m", "capabilities": json.dumps({"os": "linux", "home_dir": "/home/bob"})})
    assert (old.unresolved_home_dir, old.unresolved_otodock_dir) == ("", "")
