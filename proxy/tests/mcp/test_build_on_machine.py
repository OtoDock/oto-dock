"""The platform builds no tool: an install's build runs on a compute machine
(``mcp_sync.build_on_machine``), every sync skips a tool an install is
writing, and the machine that builds its own tools is sent the community
tools alone and told to drop the ones the platform no longer has."""

import asyncio
import base64
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import concurrency, placement
from core.credentials import catalog_install_registry
from services.mcp import mcp_egress, mcp_installer, mcp_registry, mcp_sync
from services.remote import machine_tools

MID = "compute-own"


class _Machines:
    """A connection manager with one compute machine: beside the platform
    and building its own tools, or (``builds`` False) one on another host."""

    def __init__(self, answer=None, *, builds: bool = True, connected: bool = True):
        self.builds, self.connected = builds, connected
        self.answer = answer or (lambda frame: {"results": {}})
        self.frames: list[dict] = []
        self.lock = asyncio.Lock()
        self.listeners: dict = {}

    def building_machine(self):
        return MID if self.connected else ""

    def builds_own_tools(self, machine_id):
        return self.builds

    def is_connected(self, machine_id):
        return self.connected

    def get_install_lock(self, machine_id):
        return self.lock

    def get_connection(self, machine_id):
        return SimpleNamespace(satellite_version="0.5.180", capabilities={})

    def register_install_progress(self, command_id, cb):
        self.listeners[command_id] = cb

    def unregister_install_progress(self, command_id):
        self.listeners.pop(command_id, None)

    async def send_command(self, machine_id, frame, *, timeout, command_id=None):
        self.frames.append(frame)
        for cb in list(self.listeners.values()):
            await cb({"mcp": "pkg-mcp", "phase": "npm", "pct": 10, "message": "npm install"})
        out = self.answer(frame)
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def machines(monkeypatch):
    monkeypatch.setattr(machine_tools, "_machines", {})
    monkeypatch.setattr(mcp_egress, "build_egress_block", lambda: None)
    cm = _Machines()
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    return cm


_MANIFEST = {"name": "pkg-mcp", "label": "Pkg", "version": "", "category": "community", "server_name": "pkg",
             "server": {"runtime": "node", "transport": "stdio", "command": "node", "source": "npm:pkg",
                        "source_build": ["native-thing"]},
             "system_requirements": {"debian": ["libmagic1"], "node_min": "20"}}


def _folder(tmp_path) -> Path:
    folder = tmp_path / "community" / "pkg-mcp"
    (folder / "keys").mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps(_MANIFEST))
    (folder / "server.js").write_text("// tool\n")
    (folder / "keys" / "id_rsa").write_text("never shipped\n")
    return folder


def _built(resolved="3.1.4"):
    return lambda frame: {"results": {"pkg-mcp": {"status": "ok", "version_hash": "h-new",
                                                  "resolved_version": resolved}}}


class TestTheBuild:
    @pytest.mark.asyncio
    async def test_the_folder_goes_as_one_tool_and_the_version_comes_back(self, machines, tmp_path):
        machines.answer = _built()
        progress: list = []
        built = await mcp_sync.build_on_machine("pkg-mcp", _folder(tmp_path), _MANIFEST, "npm:pkg",
                                                progress_cb=progress.append)
        assert built == mcp_sync.Built(True, "", "3.1.4", "h-new", MID)
        frame, = machines.frames
        assert frame["type"] == "sync_mcps" and frame["mcps_to_remove"] == [] and "egress" not in frame
        spec, = frame["mcps_to_install"]
        assert (spec["name"], spec["category"], spec["runtime"], spec["source"]) == (
            "pkg-mcp", "community", "node", "npm:pkg")
        # No expected hash: the pin the machine writes after the build changes it.
        assert spec["pin"] is True and spec["version_hash"] == ""
        assert spec["source_build"] == ["native-thing"]
        assert spec["system_requirements"]["debian"] == ["libmagic1"] and spec["system_requirements"]["node_min"] == "20"
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(spec["tarball_b64"])), mode="r:gz") as tar:
            assert sorted(tar.getnames()) == ["manifest.json", "server.js"]   # the keys never ride
        assert progress and progress[0]["phase"] == "npm"
        assert machines.listeners == {}
        assert machine_tools.holds(MID, "pkg-mcp", "h-new")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source,pin", [
        ("npm:pkg@3.1.4", True), ("pypi:pkg[extra]", True),
        ("git+https://example.com/x.git@" + "a" * 40, False),     # its version of record is the commit
    ])
    async def test_only_a_registry_source_is_pinned_from_the_build(self, machines, tmp_path, source, pin):
        machines.answer = _built()
        await mcp_sync.build_on_machine("pkg-mcp", _folder(tmp_path), _MANIFEST, source)
        assert machines.frames[0]["mcps_to_install"][0]["pin"] is pin

    @pytest.mark.asyncio
    @pytest.mark.parametrize("said", ["1.0.0; rm -rf /", "../../etc/passwd", "", None, {"x": 1}])
    async def test_what_the_machine_says_is_taken_only_as_a_plain_version(self, machines, tmp_path, said):
        machines.answer = _built(said)
        built = await mcp_sync.build_on_machine("pkg-mcp", _folder(tmp_path), _MANIFEST, "npm:pkg")
        assert built.ok and built.resolved_version == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer,cause", [
        (lambda f: {"results": {"pkg-mcp": {"status": "error", "error": "No solution found"}}}, "No solution found"),
        (lambda f: {"results": {"pkg-mcp": {"status": "deferred", "error": "in use"}}}, "in use"),
        (lambda f: {"results": {}}, "The compute machine did not build the tool."),
        (lambda f: RuntimeError("Satellite not connected"), "The build on the compute machine did not finish"),
    ])
    async def test_a_build_that_did_not_end_well_answers_its_cause_and_notes_nothing(
            self, machines, tmp_path, answer, cause):
        """A failure, a kept old version, a silent machine, a lost link: the
        machine's installed version is as it was, and the memo says so."""
        machines.answer = answer
        built = await mcp_sync.build_on_machine("pkg-mcp", _folder(tmp_path), _MANIFEST, "npm:pkg")
        assert not built.ok and cause in built.log and built.machine_id == MID
        assert machine_tools.of(MID).held == {} and machines.listeners == {}

    @pytest.mark.asyncio
    async def test_with_no_machine_it_waits_and_then_refuses_with_the_sentence(self, machines, tmp_path, monkeypatch):
        machines.connected = False
        waited: list = []

        async def _wait(serves, **kw):
            waited.append(serves())
            return False
        monkeypatch.setattr(concurrency, "wait_for_compute", _wait)
        told: list = []

        async def _on_wait():
            told.append(True)
        with pytest.raises(mcp_sync.NoBuildMachine) as ei:
            await mcp_sync.build_on_machine("pkg-mcp", _folder(tmp_path), _MANIFEST, "npm:pkg", on_wait=_on_wait)
        assert str(ei.value) == placement.NO_COMPUTE_SENTENCE
        assert waited == [False] and told == [True] and machines.frames == []

    @pytest.mark.asyncio
    async def test_a_machine_that_signs_in_during_the_wait_builds(self, machines, tmp_path, monkeypatch):
        machines.connected = False
        machines.answer = _built()

        async def _wait(serves, **kw):
            machines.connected = True
            return serves()
        monkeypatch.setattr(concurrency, "wait_for_compute", _wait)
        built = await mcp_sync.build_on_machine("pkg-mcp", _folder(tmp_path), _MANIFEST, "npm:pkg")
        assert built.ok and len(machines.frames) == 1

    @pytest.mark.asyncio
    async def test_it_runs_under_the_machines_install_lock(self, machines, tmp_path):
        """One build at a time on a machine: an ahead run's install ends first."""
        machines.answer = _built()
        await machines.lock.acquire()
        job = asyncio.ensure_future(mcp_sync.build_on_machine("pkg-mcp", _folder(tmp_path), _MANIFEST, "npm:pkg"))
        await asyncio.sleep(0.2)
        assert machines.frames == [] and not job.done()
        machines.lock.release()
        assert (await job).ok

    @pytest.mark.asyncio
    @pytest.mark.parametrize("builds", [True, False])
    async def test_the_platforms_egress_rides_to_the_machine_beside_it_alone(
            self, machines, tmp_path, monkeypatch, builds):
        """A machine on another host keeps its own egress."""
        machines.builds = builds
        machines.answer = _built()
        monkeypatch.setattr(mcp_egress, "build_egress_block", lambda: {"env": {"HTTPS_PROXY": "http://10.9.9.9:3128"},
                                                            "trust": [], "forwards": [], "allow_hosts": ["10.9.9.9"]})
        await mcp_sync.build_on_machine("pkg-mcp", _folder(tmp_path), _MANIFEST, "npm:pkg")
        assert ("egress" in machines.frames[0]) is builds


def _manifest(name, *, category="community", runtime="node", transport="stdio"):
    return SimpleNamespace(name=name, label=name, category=category, version="1.0.0", mcp_dir=Path("/nowhere") / name,
                           server=SimpleNamespace(runtime=runtime, transport=transport, source="npm:x@1.0.0"))


@pytest.fixture
def registry(monkeypatch):
    tools = {m.name: m for m in (_manifest("a-mcp"), _manifest("b-mcp"), _manifest("bundled", category="custom"),
                                 _manifest("sidecar", runtime="docker", transport="http"))}
    monkeypatch.setattr(mcp_registry, "get_manifest", tools.get)
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: dict(tools))

    async def _hashes(names, **kw):
        return dict.fromkeys(names, "now")
    monkeypatch.setattr(mcp_sync, "version_hashes_for", _hashes)
    return tools


@pytest.fixture
def held(monkeypatch):
    """What the machine's verify answers."""
    state: dict = {}

    async def _fetch(cm, machine_id):
        return dict(state)
    monkeypatch.setattr(mcp_sync, "_fetch_satellite_state", _fetch)
    return state


@pytest.fixture
def no_install(monkeypatch):
    monkeypatch.setattr(catalog_install_registry, "_name_locks", {})


class TestWhatAMachineIsSent:
    def test_the_machine_beside_the_platform_gets_the_community_programs_alone(self, machines, registry):
        """The bundled tools come with its image, in folders it never writes."""
        names = {"a-mcp", "bundled", "sidecar", "unknown"}
        assert mcp_sync._sent_here(machines, MID, names) == {"a-mcp"}
        machines.builds = False
        assert mcp_sync._sent_here(machines, MID, names) == names

    @pytest.mark.asyncio
    async def test_a_plan_never_names_a_tool_an_install_is_writing(self, machines, registry, held, no_install):
        """Its sources are not accepted yet: no machine is sent them."""
        held["b-mcp"] = {"version_hash": "older", "healthy": True, "category": "community"}
        assert await mcp_sync.pending_for(MID, {"a-mcp", "b-mcp"}) == {"a-mcp", "b-mcp"}
        async with catalog_install_registry.lock_for("a-mcp"), catalog_install_registry.lock_for("b-mcp"):
            assert await mcp_sync.pending_for(MID, {"a-mcp", "b-mcp"}) == set()

    @pytest.mark.asyncio
    async def test_a_sessions_sync_leaves_out_a_tool_being_installed_and_sends_nothing_for_it(
            self, machines, registry, held, no_install, monkeypatch):
        machines.builds = False        # a paired computer's session start
        monkeypatch.setattr("core.remote.remote_execution.get_remote_layer", lambda: SimpleNamespace(_sessions={}))
        held["b-mcp"] = {"version_hash": "older", "healthy": True, "category": "community"}
        async with catalog_install_registry.lock_for("a-mcp"), catalog_install_registry.lock_for("b-mcp"):
            result = await mcp_sync.sync_mcps_for_session(MID, "s1", ["a-mcp", "b-mcp"])
        assert machines.frames == []
        # The one it lacks is out, as not installed yet. The one it holds keeps its version.
        assert result.not_installed == {"a-mcp"} and result.excluded_names == {"a-mcp"} and not result.failed


class TestAToolIsKeyedByItsFolder:
    """A machine keeps a tool under its folder's name, which a session's
    config names too. It is the tool's name for every tool but one an
    earlier release took under any letters: that one keeps working."""

    @pytest.fixture
    def odd(self, registry):
        registry["café"] = _manifest("café")
        registry["café"].mcp_dir = Path("/nowhere/caf-")
        return registry["café"]

    def test_what_a_machine_answers_is_read_by_the_registrys_names(self, odd):
        said = {"caf-": {"healthy": True}, "a-mcp": {"healthy": True}, "stranger": {"healthy": False}}
        assert mcp_sync._named(said) == {"café": {"healthy": True}, "a-mcp": {"healthy": True},
                                         "stranger": {"healthy": False}}

    def test_a_folder_that_is_another_tools_name_stays_that_tools(self, registry):
        registry["twin"] = _manifest("twin")
        registry["twin"].mcp_dir = Path("/nowhere/a-mcp")
        assert mcp_sync._named({"a-mcp": 1}) == {"a-mcp": 1}

    @pytest.mark.asyncio
    async def test_the_verify_the_plan_and_the_memo_read_it_by_name(self, machines, odd, no_install):
        machines.answer = lambda frame: {"mcps": {"caf-": {"version_hash": "now", "healthy": True,
                                                           "category": "community"}}}
        assert await mcp_sync.pending_for(MID, {"café", "a-mcp"}) == {"a-mcp"}
        assert machine_tools.holds(MID, "café", "now")

    def test_it_is_sent_under_its_folder(self, odd, monkeypatch):
        tb = SimpleNamespace(tarball_b64="", version_hash="now")
        needs = SimpleNamespace(debian=[], ubuntu=[], rhel=[], arch=[], macos_brew=[], node_min="", notes="")
        for manifest in (odd, mcp_registry.get_manifest("a-mcp")):
            manifest.server_name, manifest.system_requirements = "", needs
        spec = mcp_sync._install_spec("café", odd, tb)
        assert spec["name"] == "caf-" and spec["manifest_data"]["name"] == "café"
        assert mcp_sync._install_spec("a-mcp", mcp_registry.get_manifest("a-mcp"), tb)["name"] == "a-mcp"

    @pytest.mark.asyncio
    async def test_its_removal_names_its_folder(self, machines, odd):
        frame = await mcp_sync._frame(machines, MID, [], {"café", "stranger"})
        assert frame["mcps_to_remove"] == ["caf-", "stranger"]

    @pytest.mark.asyncio
    async def test_an_installs_build_goes_and_comes_back_under_the_folder(self, machines, odd, tmp_path):
        folder = tmp_path / "community" / "caf-"
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps(_MANIFEST | {"name": "café"}))
        machines.answer = lambda frame: {"results": {"caf-": {"status": "ok", "version_hash": "h-new",
                                                              "resolved_version": "3.1.4"}}}
        progress: list = []
        built = await mcp_sync.build_on_machine("café", folder, _MANIFEST | {"name": "café"}, "npm:pkg",
                                                progress_cb=progress.append)
        assert built.ok and machines.frames[0]["mcps_to_install"][0]["name"] == "caf-"
        assert machine_tools.holds(MID, "café", "h-new")

    @pytest.mark.asyncio
    async def test_a_tool_whose_folder_no_machine_takes_is_never_sent_and_says_what_to_do(
            self, machines, registry, held, no_install, monkeypatch):
        """It keeps running where the update's copy put it. A machine
        installs nothing under such a folder."""
        machines.builds = False
        monkeypatch.setattr("core.remote.remote_execution.get_remote_layer", lambda: SimpleNamespace(_sessions={}))
        registry["_tool"] = _manifest("_tool")
        result = await mcp_sync.sync_mcps_for_session(MID, "s1", ["_tool"])
        assert machines.frames == [] and result.excluded_names == {"_tool"}
        assert result.failed == {"_tool": machine_tools.UNSAFE_KEY_CAUSE} and result.memoized == {"_tool"}
        held["_tool"] = {"version_hash": "now", "healthy": True, "category": "community"}
        result = await mcp_sync.sync_mcps_for_session(MID, "s2", ["_tool"])
        assert machines.frames == [] and not result.failed and not result.excluded_names


class TestWhatAMachineSaysIsBounded:
    """R8.4: a machine that was broken out of says what it likes. The
    platform shows and keeps a bounded line of it."""

    def test_a_progress_events_words_are_cut_and_its_number_is_a_percentage(self, registry):
        ev = mcp_sync._event({"mcp": "a-mcp", "phase": "npm", "pct": 250, "message": "x" * 5000,
                              "error": "line one\nline two", "command_id": "c1"})
        assert len(ev["message"]) == machine_tools.SAID_LIMIT and ev["error"] == "line one line two"
        assert ev["pct"] == 100 and ev["mcp"] == "a-mcp" and ev["command_id"] == "c1"
        for junk in ("abc", None, float("nan"), [1], float("inf")):
            assert mcp_sync._event({"pct": junk})["pct"] == 0
        assert mcp_sync._event({"pct": -5})["pct"] == 0 and "pct" not in mcp_sync._event({"phase": "done"})

    def test_an_events_tool_is_the_registrys_name_also_for_the_installers_own_lines(self, registry):
        registry["café"] = _manifest("café")
        registry["café"].mcp_dir = Path("/nowhere/caf-")
        assert mcp_sync._event({"mcp": "caf-"})["mcp"] == "café"
        assert mcp_sync._event({"mcp": "caf-.new"})["mcp"] == "café.new"
        assert mcp_sync._event({"mcp": "a-mcp.new"})["mcp"] == "a-mcp.new"

    @pytest.mark.asyncio
    async def test_a_builds_progress_reaches_the_install_cut(self, machines, tmp_path):
        async def _send(machine_id, frame, *, timeout, command_id=None):
            for cb in list(machines.listeners.values()):
                await cb({"mcp": "pkg-mcp", "phase": "npm", "pct": "junk", "message": "y" * 5000})
            return _built()(frame)
        machines.send_command = _send
        progress: list = []
        await mcp_sync.build_on_machine("pkg-mcp", _folder(tmp_path), _MANIFEST, "npm:pkg",
                                        progress_cb=progress.append)
        assert len(progress[0]["message"]) == machine_tools.SAID_LIMIT and progress[0]["pct"] == 0

    @pytest.mark.asyncio
    async def test_a_failed_builds_cause_is_cut(self, machines, tmp_path):
        machines.answer = lambda f: {"results": {"pkg-mcp": {"status": "error", "error": "z" * 5000}}}
        built = await mcp_sync.build_on_machine("pkg-mcp", _folder(tmp_path), _MANIFEST, "npm:pkg")
        assert not built.ok and len(built.log) == machine_tools.SAID_LIMIT

    @pytest.mark.asyncio
    async def test_a_plans_failure_warning_and_deferral_are_cut(self, registry):
        ack = asyncio.get_running_loop().create_future()
        ack.set_result({"results": {
            "a-mcp": {"status": "error", "error": "e" * 5000},
            "b-mcp": {"status": "deferred", "error": "d" * 5000},
            "bundled": {"status": "ok", "warmup": "warn:" + "w" * 5000}}})
        watch = mcp_sync.InstallWatch(MID, [], {}, set())
        result = mcp_sync._concluded(watch, ack, machine_id=MID, to_install=set(), shipped_hashes={}, sat_ver="")
        assert {len(result.failed["a-mcp"]), len(result.deferred["b-mcp"]),
                len(result.warmup_failed["bundled"])} == {machine_tools.SAID_LIMIT}


class TestWhatThePlatformNoLongerHas:
    @pytest.fixture(autouse=True)
    def rows(self, monkeypatch):
        """The tools that have a state row. An uninstall deletes a tool's
        row, a scan that lost the tool does not."""
        from storage.mcp import mcp_store
        kept: dict[str, bool] = {}
        monkeypatch.setattr(mcp_store, "get_all_mcp_states", lambda: dict(kept))
        return kept

    @pytest.mark.asyncio
    async def test_a_tool_the_registry_lost_is_no_uninstall(
            self, machines, registry, held, rows, no_install, monkeypatch, caplog):
        """The platform's community folder was not there at a scan (a volume
        not mounted, a restore in the middle), or one manifest did not
        parse. The bundled tools are always there, so the registry is never
        empty. What the machine holds of the lost tools is what they wrote
        there, in no backup: nothing of it goes, and one warning names them."""
        import logging
        caplog.set_level(logging.WARNING)
        bundled = {"bundled": registry["bundled"]}
        monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: dict(bundled))
        monkeypatch.setattr(mcp_registry, "get_manifest", bundled.get)
        held.update({name: {"category": "community"} for name in ("a-mcp", "b-mcp", "c-mcp")})
        rows.update({"a-mcp": True, "b-mcp": False, "c-mcp": True, "bundled": True})
        assert await mcp_sync.remove_gone(MID) == [] and machines.frames == []
        assert "a-mcp, b-mcp, c-mcp" in caplog.text
        # One manifest that does not parse, beside a tool an admin uninstalled.
        rows.pop("c-mcp")
        machines.answer = lambda frame: {"results": {"c-mcp": {"status": "removed"}}}
        assert await mcp_sync.remove_gone(MID) == ["c-mcp"]
        (frame,) = machines.frames
        assert frame["mcps_to_remove"] == ["c-mcp"]

    @pytest.mark.asyncio
    async def test_a_removal_frame_names_tools_only(self, machines, registry, held, no_install):
        """The machine's verify lists every folder with a manifest: a staged
        build, a quarantine, a stopped copy's folder, a 1.9.1 tool under a
        name no machine takes. The machine refuses each as a removal, so
        none is asked for: a frame for them alone is no frame."""
        left = ("x.new", ".quarantine-x-1a2b3c4d", ".copying-x", "-tool", "_tool", "a" * 65)
        held.update({name: {"category": "community"} for name in left})
        assert await mcp_sync.remove_gone(MID) == [] and machines.frames == []
        held["gone-mcp"] = {"category": "community"}
        machines.answer = lambda frame: {"results": {"gone-mcp": {"status": "removed"}}}
        assert await mcp_sync.remove_gone(MID) == ["gone-mcp"]
        (frame,) = machines.frames
        assert frame["mcps_to_remove"] == ["gone-mcp"]

    @pytest.mark.asyncio
    async def test_an_uninstalled_tool_is_taken_away(self, machines, registry, held, no_install):
        held.update({"a-mcp": {"category": "community"}, "gone-mcp": {"category": "community"}})
        machine_tools.note_installed(MID, "gone-mcp", "x", category="community")
        machines.answer = lambda frame: {"results": {"gone-mcp": {"status": "removed"}}}
        assert await mcp_sync.remove_gone(MID) == ["gone-mcp"]
        frame, = machines.frames
        assert frame["mcps_to_remove"] == ["gone-mcp"] and frame["mcps_to_install"] == []
        assert "remove_any_category" not in frame
        assert "gone-mcp" not in machine_tools.of(MID).held

    @pytest.mark.asyncio
    async def test_a_tool_an_install_is_writing_stays(self, machines, registry, held, no_install):
        """It is on the machine before the registry knows it."""
        held["fresh-mcp"] = {"category": "community"}
        async with catalog_install_registry.lock_for("fresh-mcp"):
            assert await mcp_sync.remove_gone(MID) == []
        assert machines.frames == []

    @pytest.mark.asyncio
    async def test_no_sessions_sync_takes_away_a_tool_an_install_is_writing(
            self, machines, registry, held, no_install, monkeypatch):
        """A machine on another server built it for the install, and a
        session starts there before the registry knows the tool."""
        machines.builds = False
        monkeypatch.setattr("core.remote.remote_execution.get_remote_layer", lambda: SimpleNamespace(_sessions={}))
        held["a-mcp"] = {"version_hash": "now", "healthy": True, "category": "community"}
        held["fresh-mcp"] = {"version_hash": "h", "healthy": True, "category": "community"}
        async with catalog_install_registry.lock_for("fresh-mcp"):
            result = await mcp_sync.sync_mcps_for_session(MID, "s1", ["a-mcp"])
        assert machines.frames == [] and not result.removed
        # The install ended without the tool (it failed here): the next start takes it away.
        machines.answer = lambda frame: {"results": {"fresh-mcp": {"status": "removed"}}}
        result = await mcp_sync.sync_mcps_for_session(MID, "s2", ["a-mcp"])
        frame, = machines.frames
        assert frame["mcps_to_remove"] == ["fresh-mcp"] and result.removed == ["fresh-mcp"]

    @pytest.mark.asyncio
    async def test_nothing_goes_while_the_registry_holds_no_manifest(self, machines, held, no_install, monkeypatch):
        """A failed scan must not empty a working machine."""
        monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: {})
        held["a-mcp"] = {"category": "community"}
        assert await mcp_sync.remove_gone(MID) == [] and machines.frames == []

    @pytest.mark.asyncio
    async def test_a_verify_that_failed_removes_nothing(self, machines, registry, held, no_install):
        """A machine that did not answer its verify is read from what it
        named at its sign-in: names with no category, its bundled tools and
        other names among them. None of them is a tool the platform no
        longer has."""
        held.update({name: {"version_hash": "", "healthy": True, "unverified": True}
                     for name in ("gone-mcp", "memory-mcp", "pkg")})
        assert await mcp_sync.remove_gone(MID) == [] and machines.frames == []

    @pytest.mark.asyncio
    async def test_a_machine_that_is_away_is_asked_nothing(self, machines, registry, held, no_install):
        machines.connected = False
        held["gone-mcp"] = {"category": "community"}
        assert await mcp_sync.remove_gone(MID) == [] and machines.frames == []


def test_the_folders_tarball_has_every_tarballs_exclusions(tmp_path):
    from services.mcp import mcp_tarball
    folder = _folder(tmp_path)
    (folder / "venv").mkdir()
    (folder / "venv" / "pyvenv.cfg").write_text("home = /x\n")
    (folder / "state").mkdir()
    (folder / "state" / "db.sqlite").write_text("local\n")
    packed = base64.b64decode(mcp_tarball.pack_folder(folder, {"state": "state/"}))
    with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as tar:
        assert sorted(tar.getnames()) == ["manifest.json", "server.js"]
    assert mcp_installer.COMMUNITY == "community"
