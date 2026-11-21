"""The memo of what each machine holds of the platform's tools
(``services/remote/machine_tools.py``): what the machine last said, who is
left out of a session while its compute machine has not built a tool, and
what a relayed open is told."""

import asyncio
from types import SimpleNamespace

import pytest

from core import placement
from services.mcp import mcp_registry, mcp_sync
from services.remote import machine_tools, tools_ahead
from tests._compute import COMPUTE

MID = COMPUTE["id"]
_PAIRED = placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="laptop")


def placed() -> placement.PlacementCapabilities:
    """A session placed on the compute machine beside the platform."""
    return placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id=MID)


def _tool(name="pkg-mcp", *, category="community", runtime="node", transport="stdio", server_name=""):
    return SimpleNamespace(name=name, label="Pkg", category=category, server_name=server_name,
                           mcp_dir=f"/opt/otodock/mcps/{category}/{name}",
                           server=SimpleNamespace(runtime=runtime, transport=transport))


@pytest.fixture(autouse=True)
def rig(monkeypatch):
    """The compute machine beside the platform builds its own tools; the
    platform's version of every tool hashes to ``now``."""
    monkeypatch.setattr(machine_tools, "_machines", {})
    monkeypatch.setattr(machine_tools, "_wanted", set())
    state = SimpleNamespace(builds=True, named=[])
    monkeypatch.setattr(
        "core.remote.satellite_connection.get_connection_manager",
        lambda: SimpleNamespace(
            builds_own_tools=lambda mid: state.builds and mid == MID,
            building_machine=lambda: MID if state.builds else "",
            get_connection=lambda mid: SimpleNamespace(capabilities={"installed_mcps": list(state.named)})))
    monkeypatch.setattr(mcp_sync, "_version_hash", lambda manifest, **kw: "now")
    return state


class TestWhatTheMachineHolds:
    def test_a_verify_replaces_what_it_held(self):
        machine_tools.note_verify(MID, {"a": {"version_hash": "now", "healthy": True, "category": "community"}})
        machine_tools.note_verify(MID, {"b": {"version_hash": "now", "healthy": True, "category": "community"}})
        assert not machine_tools.holds(MID, "a", "now") and machine_tools.holds(MID, "b", "now")

    @pytest.mark.parametrize("info,held", [
        ({"version_hash": "now", "healthy": True}, True),
        ({"version_hash": "older", "healthy": True}, False),     # another version
        ({"version_hash": "now", "healthy": False}, False),      # an install that stopped, no environment
        (None, False),
    ])
    def test_a_tool_is_held_healthy_and_at_the_platforms_version(self, info, held):
        machine_tools.note_verify(MID, {"pkg-mcp": info} if info else {})
        assert machine_tools.holds(MID, "pkg-mcp", "now") is held

    def test_an_unknown_platform_version_holds_nothing(self):
        machine_tools.note_verify(MID, {"pkg-mcp": {"version_hash": "", "healthy": True}})
        assert not machine_tools.holds(MID, "pkg-mcp", "")

    def test_before_its_first_verify_the_tools_it_named_at_sign_in_read_as_held(self, rig):
        """A platform restart starts with no memo: a chat that starts in the
        second before the verify answers is not told its tools are missing."""
        rig.named = ["pkg-mcp"]
        assert machine_tools.holds(MID, "pkg-mcp", "now") and not machine_tools.holds(MID, "other", "now")
        machine_tools.note_verify(MID, {})
        assert not machine_tools.holds(MID, "pkg-mcp", "now")

    def test_a_build_it_acknowledged_is_known_before_any_verify(self, rig):
        """The install's own build ends before the machine's first verify."""
        machine_tools.note_installed(MID, "pkg-mcp", "now", category="community")
        assert machine_tools.holds(MID, "pkg-mcp", "now") and not machine_tools.holds(MID, "pkg-mcp", "other")

    def test_an_acknowledged_build_and_a_removal_move_one_tool(self):
        machine_tools.note_verify(MID, {"kept": {"version_hash": "now", "healthy": True, "category": "community"}})
        machine_tools.note_installed(MID, "pkg-mcp", "now", category="community")
        assert machine_tools.holds(MID, "pkg-mcp", "now") and machine_tools.holds(MID, "kept", "now")
        machine_tools.note_removed(MID, "kept")
        assert not machine_tools.holds(MID, "kept", "now")
        machine_tools.forget(MID)
        assert MID not in machine_tools._machines

    def test_before_its_first_verify_a_tool_is_looked_up_by_its_folder(self, rig):
        """The machine names its folders at sign-in, and a tool uploaded
        before names had its rule is not named like its folder."""
        rig.named = ["caf-"]
        assert not machine_tools.holds(MID, "café", "now")
        assert machine_tools.holds(MID, "café", "now", key="caf-")

    def test_a_tools_key_is_its_folder(self):
        assert machine_tools.key_of(_tool()) == "pkg-mcp"
        odd = SimpleNamespace(name="café", mcp_dir="/opt/otodock/mcps/community/caf-")
        assert machine_tools.key_of(odd) == "caf-"

    @pytest.mark.parametrize("key,safe", [
        ("pkg-mcp", True), ("Pkg_2", True), ("a" * 64, True),
        ("a" * 65, False), ("_tool", False), ("-tool", False), ("café", False), ("a/b", False),
        ("..", False), ("", False), ("tool\n", False),
    ])
    def test_the_rule_for_a_key_is_the_machines_own(self, key, safe):
        import re

        from tests._paths import REPO_ROOT
        assert machine_tools.safe_key(key) is safe
        theirs = REPO_ROOT / "satellite" / "host" / "auth_paths.py"
        if not theirs.is_file():
            pytest.skip("no satellite tree beside the proxy")
        pattern = re.search(r'_SLUG_RE = re\.compile\(r"\^(.+)\$", re\.IGNORECASE\)', theirs.read_text())
        assert pattern and pattern.group(1) == machine_tools._KEY_RE.pattern

    def test_a_machines_words_are_one_line_of_bounded_length(self):
        assert machine_tools.said("  error:\n  no wheel\tfor x  ") == "error: no wheel for x"
        long = machine_tools.said("x" * 5000)
        assert len(long) == machine_tools.SAID_LIMIT and long.endswith("...")
        assert machine_tools.said(None) == "" and machine_tools.said(17) == "17"

    @pytest.mark.parametrize("line,shown", [
        ("error: 401 from https://builder:s3cr3t-index@index.corp/simple/pkg/",
         "error: 401 from https://***@index.corp/simple/pkg/"),
        ("Proxy http://builder:s3cr3t-proxy@proxy.corp:3128 refused the tunnel",
         "Proxy http://***@proxy.corp:3128 refused the tunnel"),
        ("fetch https://npm-s3cr3t-token@registry.corp/pkg failed, as did https://a:b@c.d/",
         "fetch https://***@registry.corp/pkg failed, as did https://***@c.d/"),
        # No sign-in: an address, a scoped package, an e-mail and a version stay.
        ("GET https://registry.npmjs.org/@scope/pkg@1.0.0 for dev@corp.example: 404",
         "GET https://registry.npmjs.org/@scope/pkg@1.0.0 for dev@corp.example: 404"),
    ])
    def test_a_sign_in_inside_an_address_is_masked_in_what_a_machine_said(self, line, shown):
        """A build's failure line goes to the platform's log, the machine's
        card, the admin's install job and a chat's prompt, which goes to the
        model's provider. The mask is the vendored installer's one."""
        from services.mcp import mcp_installer
        assert machine_tools.said(line) == shown == mcp_installer.mask_sign_ins(line)
        assert "s3cr3t" not in machine_tools.said(line)

    def test_a_removed_machines_memo_and_pending_run_go(self, monkeypatch):
        monkeypatch.setattr(tools_ahead, "_pending", {MID: object(), "other": object()})
        monkeypatch.setattr(tools_ahead, "_locks", {MID: object()})
        machine_tools.note_verify(MID, {})
        machine_tools._wanted.add(MID)
        tools_ahead.forget(MID)
        assert MID not in machine_tools._machines and MID not in machine_tools._wanted
        assert list(tools_ahead._pending) == ["other"] and not tools_ahead._locks


class TestASessionsBuild:
    def test_a_community_tool_not_built_yet_is_left_out_with_the_reason(self):
        machine_tools.note_verify(MID, {})
        assert machine_tools.refusal(_tool(), placed()) == (
            "Pkg is being installed on OtoDock's compute machine. A new chat can use it once it is done")

    def test_a_tool_whose_build_failed_says_so_with_the_cause(self):
        """The ahead run's record: the compute machine's card has no tools
        line, so the sentence carries the cause and names no card."""
        machine_tools.note_verify(MID, {})
        machine_tools.of(MID).failed["pkg-mcp"] = "error: no usable wheels\n for x"
        assert machine_tools.refusal(_tool(), placed()) == (
            "Pkg could not be installed on OtoDock's compute machine (error: no usable wheels for x)")
        machine_tools.of(MID).failed["pkg-mcp"] = "y" * 5000
        assert len(machine_tools.refusal(_tool(), placed())) < 720
        assert MID in machine_tools._wanted      # a start still asks: the cause may be gone

    def test_a_failure_of_another_tool_or_an_older_one_changes_nothing(self):
        machine_tools.note_verify(MID, {"pkg-mcp": {"version_hash": "now", "healthy": True}})
        machine_tools.of(MID).failed["pkg-mcp"] = "an older try"
        assert machine_tools.refusal(_tool(), placed()) == ""
        machine_tools.note_verify(MID, {})
        machine_tools.of(MID).failed = {"other-mcp": "broken"}
        assert "is being installed" in machine_tools.refusal(_tool(), placed())

    def test_a_tool_built_at_the_platforms_version_is_in(self):
        machine_tools.note_verify(MID, {"pkg-mcp": {"version_hash": "now", "healthy": True}})
        assert machine_tools.refusal(_tool(), placed()) == ""

    @pytest.mark.parametrize("tool", [
        _tool(category="custom"),                               # bundled: it comes with the machine's image
        _tool(category="core"),
        _tool(runtime="docker", transport="http"),              # a container the platform runs
        _tool(runtime="none", transport="streamable_http"),     # a vendor's server
    ])
    def test_only_a_community_program_is_the_machines_to_build(self, tool):
        machine_tools.note_verify(MID, {})
        assert machine_tools.refusal(tool, placed()) == ""

    def test_a_paired_computer_and_a_machine_that_builds_nothing_are_not_judged_here(self, rig):
        """Their own sync installs at the session's start, as before."""
        machine_tools.note_verify(MID, {})
        assert machine_tools.refusal(_tool(), _PAIRED) == ""
        assert machine_tools.refusal(_tool(), placement.LOCAL_PLACEMENT) == ""
        rig.builds = False
        assert machine_tools.refusal(_tool(), placed()) == ""

    @pytest.mark.asyncio
    async def test_the_sessions_start_asks_for_the_install_once(self, monkeypatch):
        asked: list = []
        monkeypatch.setattr(tools_ahead, "schedule", lambda mid, delay: asked.append((mid, delay)))
        machine_tools.note_verify(MID, {})
        machine_tools.ask(MID)
        assert asked == []                      # nothing was left out
        machine_tools.refusal(_tool(), placed())
        machine_tools.refusal(_tool("other-mcp"), placed())
        machine_tools.ask(MID)
        machine_tools.ask(MID)
        assert asked == [(MID, 0.0)]


class TestABuildThatJustFailed:
    """A build that fails every time was built again at every chat's start
    and at every open, each a full attempt, unless its cause had the
    resolver's words. A failure now rests for ten minutes."""

    @pytest.fixture(autouse=True)
    def manifests(self, monkeypatch):
        tools = {"pkg-mcp": _tool(server_name="pkg")}
        monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: tools)

    @pytest.fixture
    def builds(self, monkeypatch):
        """Each run the memo asks for, whose build of the tool fails with
        ``cause`` at the fake clock's time."""
        clock = SimpleNamespace(now=5000.0, cause="npm error code E404", runs=[])
        monkeypatch.setattr(machine_tools.time, "monotonic", lambda: clock.now)

        def _run(machine_id, delay):
            clock.runs.append(machine_id)
            machine_tools.note_failed(machine_id, "pkg-mcp", clock.cause)
        monkeypatch.setattr(tools_ahead, "schedule", _run)
        machine_tools.note_verify(MID, {})
        return clock

    @pytest.mark.parametrize("cause", ["No solution found when resolving dependencies", "npm error code E404",
                                       "a lifecycle script failed", "fetch failed"])
    def test_five_starts_and_five_opens_are_one_build(self, builds, cause):
        builds.cause = cause
        for _ in range(5):
            machine_tools.refusal(_tool(), placed())
            machine_tools.ask(MID)
            machine_tools.relay_gate(MID, "pkg")
            machine_tools.ask(MID)
            builds.now += 30
        assert builds.runs == [MID]
        # The chat still reads why, and the open its sentence.
        assert f"({cause})" in machine_tools.refusal(_tool(), placed())
        assert machine_tools.relay_gate(MID, "pkg")[1] == machine_tools.FAILED_SENTENCE

    def test_a_start_asks_again_once_the_failure_is_ten_minutes_old(self, builds):
        machine_tools.refusal(_tool(), placed())
        machine_tools.ask(MID)
        builds.now += machine_tools.RETRY_FAILED_S - 1
        machine_tools.refusal(_tool(), placed())
        machine_tools.ask(MID)
        assert builds.runs == [MID]
        builds.now += 1
        machine_tools.relay_gate(MID, "pkg")
        machine_tools.ask(MID)
        assert builds.runs == [MID, MID]

    def test_another_tool_that_is_missing_still_asks(self, builds, monkeypatch):
        machine_tools.refusal(_tool(), placed())
        machine_tools.ask(MID)
        machine_tools.refusal(_tool("other-mcp"), placed())
        machine_tools.ask(MID)
        assert builds.runs == [MID, MID]

    def test_a_door_builds_at_once(self, builds):
        """An update, an enable, an assignment and a sign-in start the
        machine's run themselves: the rest is a chat's start's and an
        open's alone."""
        machine_tools.refusal(_tool(), placed())
        machine_tools.ask(MID)
        tools_ahead.after_assignment(MID)
        assert builds.runs == [MID, MID]

    def test_a_build_that_worked_forgets_the_failure(self, builds):
        machine_tools.refusal(_tool(), placed())
        machine_tools.ask(MID)
        machine_tools.note_built(MID, "pkg-mcp")
        assert "is being installed" in machine_tools.refusal(_tool(), placed())
        machine_tools.ask(MID)
        assert builds.runs == [MID, MID]


class TestARelayedOpen:
    @pytest.fixture(autouse=True)
    def manifests(self, monkeypatch):
        tools = {"pkg-mcp": _tool(server_name="pkg"), "bundled": _tool("bundled", category="custom")}
        monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: tools)

    def test_a_tool_the_machine_built_opens_with_its_folder_to_check(self):
        machine_tools.note_verify(MID, {"pkg-mcp": {"version_hash": "now", "healthy": True}})
        assert machine_tools.relay_gate(MID, "pkg") == ("/opt/otodock/mcps/community/pkg-mcp", "")

    def test_a_tool_it_has_not_built_answers_the_sentence_and_wants_the_install(self, monkeypatch):
        asked: list = []
        monkeypatch.setattr(tools_ahead, "schedule", lambda mid, delay: asked.append(mid))
        machine_tools.note_verify(MID, {"pkg-mcp": {"version_hash": "older", "healthy": True}})
        assert machine_tools.relay_gate(MID, "pkg") == (
            "/opt/otodock/mcps/community/pkg-mcp", "This tool is being installed. Try again in a minute.")
        machine_tools.ask(MID)
        assert asked == [MID]

    def test_a_tool_whose_build_failed_answers_that_it_could_not_be_installed(self):
        machine_tools.note_verify(MID, {})
        machine_tools.of(MID).failed["pkg-mcp"] = "error: no usable wheels"
        assert machine_tools.relay_gate(MID, "pkg") == (
            "/opt/otodock/mcps/community/pkg-mcp",
            "This tool could not be installed on OtoDock's compute machine.")
        assert MID in machine_tools._wanted

    def test_a_folder_the_machine_says_is_not_there_is_forgotten_and_wanted(self):
        """The memo held it. The machine's own answer is the newer fact."""
        machine_tools.note_verify(MID, {"pkg-mcp": {"version_hash": "now", "healthy": True}})
        machine_tools.note_not_there(MID, "pkg")
        assert not machine_tools.holds(MID, "pkg-mcp", "now") and MID in machine_tools._wanted
        machine_tools._wanted.clear()
        machine_tools.note_not_there(MID, "nobody")
        assert not machine_tools._wanted

    def test_a_bundled_tool_an_unknown_server_and_another_machine_pass(self, rig):
        machine_tools.note_verify(MID, {})
        assert machine_tools.relay_gate(MID, "bundled") == ("", "")
        assert machine_tools.relay_gate(MID, "nobody") == ("", "")
        assert machine_tools.relay_gate("rack", "pkg") == ("", "")


class TestTheRebuildOffer:
    def test_a_tool_the_building_machine_lacks_or_holds_unhealthy_is_offered(self):
        machine_tools.note_verify(MID, {"broken": {"version_hash": "now", "healthy": False},
                                        "fine": {"version_hash": "older", "healthy": True}})
        assert machine_tools.missing_on_builder("broken") and machine_tools.missing_on_builder("absent")
        assert not machine_tools.missing_on_builder("fine")   # an older build is the sync's to update

    def test_unknown_reads_as_held(self, rig):
        """No verify yet, or no building machine: nothing is offered."""
        assert not machine_tools.missing_on_builder("absent")
        machine_tools.note_verify(MID, {})
        rig.builds = False
        assert not machine_tools.missing_on_builder("absent")


@pytest.mark.asyncio
async def test_the_sync_writes_the_memo_it_reads(monkeypatch):
    """The memo is the sync's own reads, never a second question."""
    monkeypatch.setattr(machine_tools, "_machines", {})
    sent: list = []

    async def _send(machine_id, frame, **kw):
        sent.append(frame["type"])
        return {"mcps": {"pkg-mcp": {"version_hash": "now", "healthy": True, "category": "community"}}}
    cm = SimpleNamespace(send_command=_send)
    assert "pkg-mcp" in await mcp_sync._fetch_satellite_state(cm, "m1")
    assert sent == ["sync_mcps_verify"] and machine_tools.of("m1").verified
    assert machine_tools.of("m1").held["pkg-mcp"]["version_hash"] == "now"
    await asyncio.sleep(0)


class TestDirectLlmsOpen:
    """Direct LLM's client opens a tool on the compute machine through the
    gate (``core/layers/direct/mcp.py``)."""

    class _Sandbox:
        def get_env_overrides(self):
            return {}

        def describe(self):
            return SimpleNamespace(to_dict=dict)

    def _conn(self, loop):
        from core.layers.direct import mcp as direct_mcp
        return direct_mcp.MCPServerConnection("pkg", {"command": "node"}, session_id="s1",
                                              sandbox_builder=self._Sandbox(), machine_id=MID, main_loop=loop)

    @pytest.fixture
    def relay(self, monkeypatch):
        from core.credentials import credential_exit
        from core.remote import tool_relay
        opened: list[dict] = []

        class _Refused:
            def __init__(self, **kw):
                opened.append(kw)

            async def __aenter__(self):
                raise RuntimeError("the test opens nothing")

        monkeypatch.setattr(tool_relay, "relay_client", lambda *a, **kw: _Refused(**kw))
        monkeypatch.setattr(credential_exit, "machine_secret", lambda sid: "")
        return opened

    @pytest.mark.asyncio
    async def test_a_tool_not_built_yet_is_not_opened_and_its_call_answers_the_sentence(self, relay, monkeypatch):
        asked: list = []
        monkeypatch.setattr(machine_tools, "relay_gate",
                            lambda mid, server: ("/opt/otodock/mcps/community/pkg-mcp",
                                                 machine_tools.INSTALLING_SENTENCE))
        monkeypatch.setattr(machine_tools, "ask", asked.append)
        conn = self._conn(asyncio.get_running_loop())
        with pytest.raises(RuntimeError, match="This tool is being installed"):
            await conn._start_relayed("node", ["index.js"], {})
        await asyncio.sleep(0)
        assert relay == [] and asked == [MID]
        assert conn.refused == "This tool is being installed. Try again in a minute."

    @pytest.mark.asyncio
    async def test_a_built_tool_is_opened_with_its_folder_for_the_machine_to_check(self, relay, monkeypatch):
        monkeypatch.setattr(machine_tools, "relay_gate",
                            lambda mid, server: ("/opt/otodock/mcps/community/pkg-mcp", ""))
        conn = self._conn(asyncio.get_running_loop())
        with pytest.raises(RuntimeError, match="the test opens nothing"):
            await conn._start_relayed("node", ["index.js"], {})
        assert [o["tool_dir"] for o in relay] == ["/opt/otodock/mcps/community/pkg-mcp"] and conn.refused == ""

    @pytest.mark.asyncio
    async def test_the_machines_own_answer_is_the_same_sentence(self, monkeypatch):
        """The folder went between the memo's read and the open."""
        from core.credentials import credential_exit
        from core.remote import tool_relay

        class _NotThere:
            async def __aenter__(self):
                raise RuntimeError(f"Satellite error: {machine_tools.INSTALLING_SENTENCE}")
        monkeypatch.setattr(tool_relay, "relay_client", lambda *a, **kw: _NotThere())
        monkeypatch.setattr(credential_exit, "machine_secret", lambda sid: "")
        monkeypatch.setattr(machine_tools, "relay_gate", lambda mid, server: ("/x", ""))
        noted, asked = [], []
        monkeypatch.setattr(machine_tools, "note_not_there", lambda mid, server: noted.append((mid, server)))
        monkeypatch.setattr(machine_tools, "ask", asked.append)
        conn = self._conn(asyncio.get_running_loop())
        with pytest.raises(RuntimeError):
            await conn._start_relayed("node", ["index.js"], {})
        await asyncio.sleep(0)
        assert conn.refused == machine_tools.INSTALLING_SENTENCE
        # The memo forgets the tool and a run builds it again.
        assert noted == [(MID, "pkg")] and asked == [MID]

    @pytest.mark.asyncio
    async def test_a_tool_whose_build_failed_is_not_opened_and_its_call_says_so(self, relay, monkeypatch):
        monkeypatch.setattr(machine_tools, "relay_gate",
                            lambda mid, server: ("/opt/otodock/mcps/community/pkg-mcp", machine_tools.FAILED_SENTENCE))
        monkeypatch.setattr(machine_tools, "ask", lambda mid: None)
        conn = self._conn(asyncio.get_running_loop())
        with pytest.raises(RuntimeError, match="could not be installed"):
            await conn._start_relayed("node", ["index.js"], {})
        assert relay == []
        assert conn.refused == "This tool could not be installed on OtoDock's compute machine."

    @pytest.mark.asyncio
    async def test_the_first_call_of_a_tool_that_waited_answers_it(self, monkeypatch):
        conn = self._conn(asyncio.get_running_loop())
        conn.pending, conn.warm = True, True

        async def _start():
            conn.refused = machine_tools.INSTALLING_SENTENCE
        monkeypatch.setattr(conn, "start", _start)
        assert await conn.ensure_started() == "This tool is being installed. Try again in a minute."
        assert conn.pending           # the next call tries again
