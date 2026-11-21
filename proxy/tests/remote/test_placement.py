"""The placement descriptor (core-seams phase 7): the leaf, its questions, the
store's one builder, and the dashboard mirror in lock-step.

``core/placement.py`` names the stored target value, the resolved kind, the
pairing scope and the check/app site once; ``remote_store.placement_of``
builds ``PlacementCapabilities`` from one read of the machine row;
``dashboard/src/lib/placement.ts`` mirrors the constants (read here by regex,
the way ``tests/auth/test_roles.py`` reads ``permissions.ts``).
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import sys

import pytest

from core import placement
from tests._paths import PROXY_DIR, REPO_ROOT

_MIRROR = REPO_ROOT / "dashboard" / "src" / "lib" / "placement.ts"


# ---------------------------------------------------------------------------
# the leaf
# ---------------------------------------------------------------------------

def test_the_leaf_imports_nothing_of_the_tree():
    script = ("import sys, json\nimport core.placement\n"
              "print(json.dumps(sorted(m for m in sys.modules if m.startswith("
              "('core.', 'services.', 'storage.', 'auth', 'config', 'ws.', 'api.')))))\n")
    out = subprocess.run([sys.executable, "-c", script], cwd=str(PROXY_DIR),
                         capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == ["core.host_os", "core.placement"]


@pytest.mark.parametrize("target, local, machine, sentinel, offline_machine", [
    ("", True, "", False, ""),
    (None, True, "", False, ""),
    ("local", True, "", False, ""),
    ("m-1", False, "m-1", False, ""),
    ("__offline__:m-1", False, "m-1", True, "m-1"),
])
def test_the_stored_value_predicates(target, local, machine, sentinel, offline_machine):
    assert placement.is_local(target) is local
    assert placement.machine_of(target) == machine
    assert placement.is_offline_sentinel(target) is sentinel
    assert placement.offline_machine_of(target) == offline_machine


def test_the_sentinel_round_trips_and_never_runs_on_a_machine():
    s = placement.offline_sentinel("abc")
    assert s == placement.OFFLINE_PREFIX + "abc"
    assert placement.offline_machine_of(s) == "abc" and placement.machine_of(s) == "abc"
    # a satellite-initiated session asks runs_on: a live id yes, a sentinel
    # naming the same machine never, the sandbox never, no machine never
    assert placement.runs_on("abc", "abc")
    assert not placement.runs_on(s, "abc")
    assert not placement.runs_on(placement.LOCAL, "abc")
    assert not placement.runs_on("", "abc")
    assert not placement.runs_on("abc", "")


def test_a_pin_agrees_with_a_session_on_the_platforms_own_machine_only_by_its_clause():
    """The platform's place (``local``) agrees with a session on a compute
    machine, and with no other machine; a pin naming a machine agrees with
    that machine alone, compute or not."""
    assert placement.runs_on(placement.LOCAL, "c-1", platform_machine=True)
    assert placement.runs_on("", "c-1", platform_machine=True)
    assert not placement.runs_on(placement.LOCAL, "c-1")
    assert placement.runs_on("c-1", "c-1", platform_machine=True)
    assert not placement.runs_on("m-2", "c-1", platform_machine=True)
    assert not placement.runs_on(placement.offline_sentinel("c-1"), "c-1", platform_machine=True)
    assert not placement.runs_on(placement.LOCAL, "", platform_machine=True)


def test_the_kinds_and_the_pairing_scopes():
    assert placement.KINDS == (placement.KIND_LOCAL, placement.KIND_ADMIN_REMOTE, placement.KIND_USER_REMOTE,
                               placement.KIND_COMPUTE)
    assert placement.PAIRING_SCOPES == (placement.PAIRING_ADMIN, placement.PAIRING_USER)
    assert placement.MACHINE_SCOPES == (placement.PAIRING_ADMIN, placement.PAIRING_USER, placement.PAIRING_COMPUTE)
    assert placement.machine_is_admin_paired({"pairing_scope": placement.PAIRING_ADMIN})
    assert not placement.machine_is_admin_paired({"pairing_scope": placement.PAIRING_USER})
    assert not placement.machine_is_admin_paired({"pairing_scope": placement.PAIRING_COMPUTE})
    assert not placement.machine_is_admin_paired({})
    assert not placement.machine_is_admin_paired(None)
    # the stored spellings are the frozen ones
    assert (placement.LOCAL, placement.KIND_LOCAL, placement.SITE_LOCAL) == ("local", "local", "local")
    assert (placement.KIND_ADMIN_REMOTE, placement.KIND_USER_REMOTE) == ("admin_remote", "user_remote")
    assert placement.SITE_MACHINE == "machine" and placement.OFFLINE_PREFIX == "__offline__:"
    assert (placement.PAIRING_ADMIN, placement.PAIRING_USER) == ("admin", "user")
    assert placement.KIND_COMPUTE == placement.PAIRING_COMPUTE == "compute"


@pytest.mark.parametrize("row, personal, compute", [
    ({"pairing_scope": "user", "registered_by": "sub-a"}, True, False),
    ({"pairing_scope": "admin", "registered_by": "sub-b"}, False, False),
    ({"pairing_scope": "compute", "registered_by": ""}, False, True),
    ({"pairing_scope": "other", "registered_by": "sub-c"}, False, False),
    ({}, False, False),
    (None, False, False),
])
def test_a_rows_scope_answers_once(row, personal, compute):
    """A compute row is no person's machine: every reader that took "not
    admin-paired" for "a person's" asks ``machine_is_personal``."""
    assert placement.machine_is_personal(row) is personal
    assert placement.machine_is_compute(row) is compute


def test_owner_of_is_a_personal_machines_owner_and_nobody_else():
    assert placement.owner_of({"pairing_scope": "user", "registered_by": "sub-a"}) == "sub-a"
    # an admin's machine was paired by an admin, and is owned by the admin tier
    assert placement.owner_of({"pairing_scope": "admin", "registered_by": "sub-b"}) == ""
    assert placement.owner_of({"pairing_scope": "compute", "registered_by": None}) == ""
    assert placement.owner_of({"pairing_scope": "user", "registered_by": None}) == ""
    assert placement.owner_of(None) == ""


def test_a_scope_no_release_writes_refuses_even_who_registered_it():
    row = {"id": "x-1", "pairing_scope": "other", "registered_by": "sub-a"}
    assert not placement.may_run_on(row, run_sub="sub-a", run_can_manage=True, user_paired_allowed=True)
    assert not placement.may_run_on({"id": "x-2", "registered_by": "sub-a"}, run_sub="sub-a",
                                    run_can_manage=True, user_paired_allowed=True)


def test_nobody_may_run_on_a_compute_machine_yet():
    # the row may name the admin who registered it (the owner rule allows it)
    row = {"id": "c-1", "pairing_scope": "compute", "registered_by": "sub-a", "all_roles_run_here": True}
    for run_sub, manage in (("", False), ("sub-a", True), ("sub-a", False), ("", True)):
        assert not placement.may_run_on(row, run_sub=run_sub, run_can_manage=manage,
                                        user_paired_allowed=True, agent_default="c-1")
    assert not placement.admits_every_role(row, "c-1")

def test_parse_device_grants_fails_closed():
    assert placement.parse_device_grants(None) == set()
    assert placement.parse_device_grants('["computer", "browser"]') == {"computer", "browser"}
    assert placement.parse_device_grants(["app"]) == {"app"}
    assert placement.parse_device_grants("nope") == set()
    assert placement.parse_device_grants('{"a": 1}') == set()


# ---------------------------------------------------------------------------
# the questions, per kind
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind, machine_id, remote, admin, user, site", [
    (placement.KIND_LOCAL, "", False, False, False, placement.SITE_LOCAL),
    (placement.KIND_ADMIN_REMOTE, "m", True, True, False, placement.SITE_MACHINE),
    (placement.KIND_USER_REMOTE, "m", True, False, True, placement.SITE_MACHINE),
    # a remote kind on a missing row: still remote for the path gate (which
    # fail-closes on the empty facts), but the checks probe the platform copy
    (placement.KIND_ADMIN_REMOTE, "", True, True, False, placement.SITE_LOCAL),
])
def test_every_named_fact_equals_what_its_sites_read_before_on_the_three_kinds(
        kind, machine_id, remote, admin, user, site):
    """Before the facts apart every question was ``is_remote`` or its
    negation; on the three kinds a 1.9.1 install has, each named fact still
    answers exactly that, so no site's answer changed."""
    p = placement.PlacementCapabilities(kind=kind, machine_id=machine_id)
    assert p.is_remote is remote
    assert p.admin_paired is admin and p.user_paired is user
    assert p.isolates_with_bwrap is (not remote)
    assert p.needs_path_translation is remote
    assert p.files_synced is remote
    assert p.counts_toward_capacity is (not remote)
    assert p.offers_device_tools is remote
    # what the key-material gates read: "is_remote and not admin_paired"
    assert p.user_paired is (remote and not admin)
    assert p.site == site
    assert not hasattr(p, "is_local")


def test_a_compute_placement_is_a_machine_for_its_process_and_the_platform_for_the_rest():
    p = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1")
    assert p.is_remote  # the process: reached over the machine's link
    assert p.isolates_with_bwrap and not p.needs_path_translation and not p.files_synced
    assert p.counts_toward_capacity and not p.offers_device_tools
    assert not p.admin_paired and not p.user_paired
    assert p.site == placement.SITE_LOCAL  # its files are the platform's own


def test_an_unknown_kind_answers_with_a_persons_machine_rules():
    assert placement.facts_of("nope") == placement.facts_of(placement.KIND_USER_REMOTE)


def test_admin_paired_is_the_kind_not_the_row():
    """An admin-scoped machine a person attached as their own override
    resolves user-remote: the object says not admin-paired, the row says
    admin-paired — the prompt section and the key-material rules follow
    the kind (audit A3)."""
    row = {"id": "m", "name": "Office", "pairing_scope": placement.PAIRING_ADMIN, "capabilities": "{}"}
    p = placement.from_machine(placement.KIND_USER_REMOTE, row)
    assert p.user_paired and not p.admin_paired
    assert placement.machine_is_admin_paired(row)


def test_os_family_reads_the_report_first_then_the_path_shape():
    P = placement.PlacementCapabilities
    assert P().os_family == "linux"
    assert P(os="darwin", home_dir="/var/root").os_family == "darwin"
    assert P(os="linux", home_dir="/Users/x").os_family == "linux"
    assert P(home_dir="/Users/x").os_family == "darwin"
    assert P(agents_dir="C:/Users/e/OtoDock/agents").os_family == "windows"
    assert P(home_dir="c:\\Users\\e").os_family == "windows"
    assert P(os="plan9", home_dir="/home/x").os_family == "linux"  # an unknown report → the shape


# ---------------------------------------------------------------------------
# from_machine and placement_of
# ---------------------------------------------------------------------------

_ROW = {
    "id": "m-1", "name": "Alice MBP", "allow_full_fs": True, "device_grants": '["computer"]',
    "pairing_scope": "admin",
    "capabilities": json.dumps({
        "os": "darwin", "home_dir": "/Users/alice", "agents_dir": "/Users/alice/.oto-dock/agents",
        "os_user": "alice", "claude_runtime_root": "/tmp/claude-501",
        "user_dirs": {"desktop": "/Users/alice/Desktop"}, "display": {"has_display": True},
    }),
}


def test_from_machine_reads_the_row_once():
    p = placement.from_machine(placement.KIND_USER_REMOTE, _ROW)
    assert p == placement.PlacementCapabilities(
        kind=placement.KIND_USER_REMOTE, machine_id="m-1", label="Alice MBP", os="darwin",
        home_dir="/Users/alice", agents_dir="/Users/alice/.oto-dock/agents", os_user="alice",
        allow_full_fs=True, claude_runtime_root="/tmp/claude-501",
        user_dirs={"desktop": "/Users/alice/Desktop"}, device_grants={"computer"}, has_display=True,
    )


def test_a_compute_placement_carries_no_fact_of_its_row():
    """Its sessions see sandbox paths: no path, OS user, full access or
    device grant of the row reaches a reader that reads them ungated (the
    ``~`` expansion, the permission hook's device grants)."""
    row = {**_ROW, "id": "c-1", "name": "Compute one", "pairing_scope": "compute"}
    p = placement.from_machine(placement.KIND_COMPUTE, row)
    assert p == placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1", label="Compute one")
    assert p.home_dir == "" and p.agents_dir == "" and p.os_user == "" and p.user_dirs == {}
    assert not p.allow_full_fs and p.device_grants == set() and p.has_display is None
    assert p.state_dirs == ()


def test_from_machine_fails_closed_on_a_missing_or_unreadable_row():
    gone = placement.from_machine(placement.KIND_ADMIN_REMOTE, None)
    assert gone.kind == placement.KIND_ADMIN_REMOTE and gone.machine_id == "" and not gone.allow_full_fs
    assert gone.home_dir == "" and gone.agents_dir == "" and gone.device_grants == set()
    assert gone.has_display is None and gone.site == placement.SITE_LOCAL
    bad = placement.from_machine(placement.KIND_ADMIN_REMOTE,
                                 {"id": "m", "name": "Box", "allow_full_fs": True, "capabilities": "{not json"})
    assert bad.machine_id == "" and not bad.allow_full_fs and bad.label == "Box"
    no_caps = placement.from_machine(placement.KIND_ADMIN_REMOTE, {"id": "m", "name": "Box", "capabilities": None})
    assert no_caps.machine_id == "m" and no_caps.home_dir == "" and no_caps.has_display is None


def test_placement_of_resolves_the_kind_and_reads_once(monkeypatch):
    from storage import remote_store
    reads = []

    def _machine(mid):
        reads.append(mid)
        return dict(_ROW, id=mid)
    monkeypatch.setattr(remote_store, "get_remote_machine", _machine)
    monkeypatch.setattr(remote_store, "get_user_remote_target",
                        lambda sub, agent: {"machine_id": "m-1"} if sub == "u-1" else None)
    user = remote_store.placement_of("m-1", "u-1", "pa")
    assert user.user_paired and user.machine_id == "m-1" and user.label == "Alice MBP"
    admin = remote_store.placement_of("m-1", "u-2", "pa")       # the override names another machine
    assert admin.admin_paired
    service = remote_store.placement_of("m-1", None, "pa")      # no user: the agent default
    assert service.admin_paired
    assert reads == ["m-1", "m-1", "m-1"]                        # one row read per build
    reads.clear()
    assert remote_store.placement_of(placement.LOCAL, "u-1", "pa") is placement.LOCAL_PLACEMENT
    assert remote_store.placement_of("", "u-1", "pa") is placement.LOCAL_PLACEMENT
    # the offline sentinel: the local placement on purpose — every consumer
    # refuses before the session starts (tests/execution/test_offline_sentinel.py)
    assert remote_store.placement_of(placement.offline_sentinel("m-1"), "u-1", "pa") is placement.LOCAL_PLACEMENT
    assert reads == []
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda mid: None)
    deleted = remote_store.placement_of("m-9", None, "pa")
    assert deleted.admin_paired and deleted.machine_id == "" and deleted.site == placement.SITE_LOCAL


def test_a_personal_machine_is_user_remote_whatever_pinned_the_chat(monkeypatch):
    # A chat moved to its person's own machine (no default override for the
    # agent) is still a personal placement: the kind follows the row.
    from storage import remote_store
    monkeypatch.setattr(remote_store, "get_remote_machine",
                        lambda mid: dict(_ROW, id=mid, pairing_scope="user", registered_by="u-1"))
    monkeypatch.setattr(remote_store, "get_user_remote_target", lambda sub, agent: None)
    assert remote_store.placement_of("m-1", "u-1", "pa").user_paired
    assert remote_store.placement_of("m-1", None, "pa").user_paired


def test_may_run_on_is_the_placement_guard():
    admin = {"pairing_scope": "admin", "registered_by": "a"}
    mine = {"pairing_scope": "user", "registered_by": "u-1"}
    assert placement.may_run_on(admin, run_sub=None, run_can_manage=False, user_paired_allowed=True)
    assert placement.may_run_on(admin, run_sub="u-1", run_can_manage=True, user_paired_allowed=True)
    assert not placement.may_run_on(admin, run_sub="u-1", run_can_manage=False,
                                    user_paired_allowed=True)
    assert placement.may_run_on(mine, run_sub="u-1", run_can_manage=False, user_paired_allowed=True)
    assert not placement.may_run_on(mine, run_sub="u-2", run_can_manage=True,
                                    user_paired_allowed=True)
    assert not placement.may_run_on(mine, run_sub=None, run_can_manage=True,
                                    user_paired_allowed=True)
    assert not placement.may_run_on(mine, run_sub="u-1", run_can_manage=True,
                                    user_paired_allowed=False)
    assert not placement.may_run_on(None, run_sub="u-1", run_can_manage=True,
                                    user_paired_allowed=True)


def test_an_admin_machine_admits_every_role_of_its_own_agents_by_its_switch():
    on = {"id": "m-a", "pairing_scope": "admin", "registered_by": "a", "all_roles_run_here": True}
    off = dict(on, all_roles_run_here=False)
    assert placement.admits_every_role(on, "m-a")
    assert placement.admits_every_role(on, placement.offline_sentinel("m-a"))
    assert not placement.admits_every_role(off, "m-a")
    # Only the agent's own default: another agent's, the sandbox, or none.
    assert not placement.admits_every_role(on, "m-b")
    assert not placement.admits_every_role(on, placement.LOCAL)
    assert not placement.admits_every_role(on, "")
    assert not placement.admits_every_role(on, None)
    # A personal machine, a row with no id, no row.
    assert not placement.admits_every_role(dict(on, pairing_scope="user"), "m-a")
    assert not placement.admits_every_role(dict(on, id=""), "")
    assert not placement.admits_every_role(None, "m-a")
    # The guard: a person below the owner tier runs there only by the switch.
    kw = {"run_sub": "u-1", "run_can_manage": False, "user_paired_allowed": True}
    assert placement.may_run_on(on, agent_default="m-a", **kw)
    assert not placement.may_run_on(on, agent_default="m-b", **kw)
    assert not placement.may_run_on(on, **kw)
    assert not placement.may_run_on(off, agent_default="m-a", **kw)
    # A personal machine stays its owner's whatever the flag says.
    theirs = dict(on, pairing_scope="user", registered_by="u-2")
    assert not placement.may_run_on(theirs, agent_default="m-a", **kw)


def test_the_browser_settings_read_takes_the_placement(monkeypatch):
    from storage import remote_store
    assert remote_store.get_target_browser_settings(placement.LOCAL_PLACEMENT) == remote_store.BrowserTargetSettings()
    assert remote_store.get_target_browser_settings(
        placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE)) == remote_store.BrowserTargetSettings()


def test_the_object_replaces_but_never_mutates():
    p = placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m")
    q = dataclasses.replace(p, allow_full_fs=True, device_grants={"browser"})
    assert q.allow_full_fs and q.device_grants == {"browser"} and not p.allow_full_fs
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.kind = placement.KIND_LOCAL  # type: ignore[misc]


# ---------------------------------------------------------------------------
# the dashboard mirror
# ---------------------------------------------------------------------------

def _ts_const_strings(text: str, name: str) -> list[str]:
    m = re.search(rf"export const {name}\b[^=]*=\s*(.+?)(?:\n\n|\nexport|\Z)", text, re.S)
    assert m, f"{name} not found in the mirror"
    return re.findall(r"'([^']*)'", m.group(1))


def test_the_dashboard_mirror_equals_the_authority():
    text = _MIRROR.read_text(encoding="utf-8")
    assert _ts_const_strings(text, "TARGET_LOCAL") == [placement.LOCAL]
    assert _ts_const_strings(text, "SITE") == [placement.SITE_LOCAL, placement.SITE_MACHINE]
    assert _ts_const_strings(text, "PAIRING_SCOPE") == list(placement.MACHINE_SCOPES)
    for fn in ("isLocalTarget", "machineOf"):
        assert f"export function {fn}(" in text, fn
    assert "export type SiteKind" in text and "export type PairingScope" in text
    # the sentinel never reaches the dashboard
    assert placement.OFFLINE_PREFIX not in text


# ---------------------------------------------------------------------------
# The machine's own state directories
# ---------------------------------------------------------------------------

def test_the_state_root_derives_from_the_home_and_the_os_family():
    P = placement.PlacementCapabilities
    assert P(os="linux", home_dir="/home/office").otodock_dir == "/home/office/.oto-dock"
    assert P(os="darwin", home_dir="/Users/alice").otodock_dir == "/Users/alice/.oto-dock"
    assert P(os="windows", home_dir="C:/Users/e").otodock_dir == "C:/Users/e/OtoDock"
    # An unreported family takes the path shape, like os_family does.
    assert P(home_dir="C:/Users/e").otodock_dir == "C:/Users/e/OtoDock"
    assert P(home_dir="/home/office/").otodock_dir == "/home/office/.oto-dock"


def test_the_state_root_falls_back_to_the_agents_root_then_to_nothing():
    P = placement.PlacementCapabilities
    assert P(os="linux", agents_dir="/srv/box/.oto-dock/agents").otodock_dir == "/srv/box/.oto-dock"
    assert P(os="windows", agents_dir="D:/OtoDock/agents").otodock_dir == "D:/OtoDock"
    # An agents root that is not the platform's own shape names no state root.
    assert P(os="linux", agents_dir="/srv/agents").otodock_dir == ""
    assert P(os="linux", agents_dir="/srv/work/agents").otodock_dir == ""
    assert P().otodock_dir == ""


def test_the_state_dirs_cover_the_browser_profiles_on_windows_too():
    P = placement.PlacementCapabilities
    assert P(os="linux", home_dir="/home/office").state_dirs == ("/home/office/.oto-dock",)
    assert P(os="windows", home_dir="C:/Users/e").state_dirs == (
        "C:/Users/e/OtoDock", "C:/Users/e/.oto-dock")
    assert P().state_dirs == ()


def test_the_reported_state_root_wins_and_the_derived_one_stays_refused():
    """A 0.5.130 satellite reports its OtoDock root and its MCP folder; the
    placement keeps the derived root too (a symlinked home makes them
    differ) and refuses the MCP folder wherever the operator put it."""
    P = placement.PlacementCapabilities
    p = P(os="linux", home_dir="/home/office", reported_otodock_dir="/srv/real/.oto-dock",
          mcps_dir="/opt/mcps")
    assert p.otodock_dir == "/srv/real/.oto-dock"
    assert p.state_dirs == ("/srv/real/.oto-dock", "/home/office/.oto-dock", "/opt/mcps")
    # The same root reported and derived is listed once; a folder under a
    # listed root is not repeated.
    same = P(os="linux", home_dir="/home/office", reported_otodock_dir="/home/office/.oto-dock",
             mcps_dir="/home/office/.oto-dock/mcps")
    assert same.state_dirs == ("/home/office/.oto-dock",)
    win = P(os="windows", home_dir="C:/Users/e", reported_otodock_dir="c:/users/e/OtoDock",
            mcps_dir="D:/mcps")
    assert win.state_dirs == ("c:/users/e/OtoDock", "D:/mcps", "C:/Users/e/.oto-dock")
    # A 0.5.129 report carries neither: today's derivation, unchanged.
    assert P(os="linux", home_dir="/home/office").state_dirs == ("/home/office/.oto-dock",)


def test_from_machine_reads_the_two_state_keys_and_tolerates_their_absence():
    caps = json.dumps({
        "os": "linux", "home_dir": "/home/office", "agents_dir": "/home/office/.oto-dock/agents",
        "otodock_dir": "/home/office/.oto-dock", "mcps_dir": "/home/office/.oto-dock/mcps",
    })
    p = placement.from_machine(placement.KIND_ADMIN_REMOTE, {"id": "m", "capabilities": caps})
    assert p.reported_otodock_dir == "/home/office/.oto-dock"
    assert p.mcps_dir == "/home/office/.oto-dock/mcps"
    old = placement.from_machine(placement.KIND_ADMIN_REMOTE, {
        "id": "m", "capabilities": json.dumps({"os": "linux", "home_dir": "/home/office"})})
    assert old.reported_otodock_dir == "" and old.mcps_dir == ""
    assert old.otodock_dir == "/home/office/.oto-dock"


def test_a_placement_carries_the_satellites_version_from_its_row():
    """The build leaves the SSH hosts tool out of a machine whose satellite
    cannot open the agent socket: the version rides the placement, every
    kind, and an unreadable row still answers it."""
    row = {**_ROW, "satellite_version": "0.5.173"}
    assert placement.from_machine(placement.KIND_ADMIN_REMOTE, row).satellite_version == "0.5.173"
    assert placement.from_machine(placement.KIND_COMPUTE, row).satellite_version == "0.5.173"
    bad = placement.from_machine(placement.KIND_USER_REMOTE,
                                 {**row, "capabilities": "{not json"})
    assert bad.satellite_version == "0.5.173"
    assert placement.from_machine(placement.KIND_ADMIN_REMOTE, _ROW).satellite_version == ""
    assert placement.LOCAL_PLACEMENT.satellite_version == ""


def test_from_machine_reads_the_exit_from_the_live_connection_first(monkeypatch):
    """The machine rule's input: the connection's report when it is
    connected, the row's capabilities when it is not, and nothing for a
    satellite that reports none."""
    import json

    from core.remote import satellite_connection
    row = {"id": "m-9", "name": "Laptop", "satellite_version": "0.5.175",
           "capabilities": json.dumps({"os": "linux", "exit_port": 40001, "exit_ok": True})}
    live = {"m-9": (0, False)}

    report: dict = {}

    class _Cm:
        def exit_state(self, mid):
            return live.get(mid, (0, False))

        def exit_report(self, mid):
            return report.get(mid, {})
    monkeypatch.setattr(satellite_connection, "get_connection_manager", _Cm)
    p = placement.from_machine(placement.KIND_USER_REMOTE, row)
    assert (p.exit_port, p.exit_ok) == (40001, True)
    assert (p.exit_why, p.exit_upstream) == ("", False)
    report["m-9"] = {"why": "installing", "upstream": True}
    p = placement.from_machine(placement.KIND_USER_REMOTE, row)
    assert (p.exit_why, p.exit_upstream) == ("installing", True)
    del report["m-9"]
    live["m-9"] = (40002, False)
    p = placement.from_machine(placement.KIND_USER_REMOTE, row)
    assert (p.exit_port, p.exit_ok) == (40002, False)
    c = placement.from_machine(placement.KIND_COMPUTE, {**row, "id": "m-9"})
    assert (c.exit_port, c.exit_ok) == (40002, False)
    none = placement.from_machine(placement.KIND_USER_REMOTE, {"id": "m-8", "capabilities": json.dumps({"os": "linux"})})
    assert (none.exit_port, none.exit_ok) == (0, False)
    bad = placement.from_machine(placement.KIND_USER_REMOTE, {"id": "m-7", "capabilities": "{not json"})
    assert (bad.exit_port, bad.exit_ok) == (0, False)
