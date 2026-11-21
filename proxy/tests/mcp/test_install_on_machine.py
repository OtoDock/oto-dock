"""A community tool's install, its build on the compute machine
(``community_installer`` over ``mcp_sync.build_on_machine``): what the
platform writes after the build so both sides hash the same folder, the one
string it takes from the machine, and that an install that fails changes no
installed tool on either side."""

from __future__ import annotations

import json
import shutil

import pytest
from fastapi import HTTPException

from services.community import community_installer as ci
from services.mcp import mcp_installer, mcp_pin, mcp_registry
from services.remote import tools_ahead
from tests._compute import builds
from tests.mcp.test_install_gate import (  # noqa: F401  # the fixture
    _install_packaged,
    _packaged,
    mcps_dir,
)

MID = "compute-own"


@pytest.fixture
def sent_back(monkeypatch):
    """The machines an ahead run is asked for (what puts a machine back to
    what the platform holds)."""
    asked: list[str] = []
    monkeypatch.setattr(tools_ahead, "schedule", lambda machine_id, delay: asked.append(machine_id))
    monkeypatch.setattr(tools_ahead, "after_tool_change", lambda name, *, update: None)
    return asked


def _installed(mcps_dir):  # noqa: F811
    folder = mcps_dir / "community" / "pkg-mcp"
    return json.loads((folder / "manifest.json").read_text()) if (folder / "manifest.json").is_file() else None


@pytest.mark.asyncio
async def test_the_platform_writes_what_the_build_left_and_hashes_as_the_machine_does(
        mcps_dir, tmp_path, monkeypatch, sent_back):  # noqa: F811
    """No build runs here. The manifest's pin and the package file are
    written by the function the machine used, on the same bytes."""
    asked = builds(monkeypatch, resolved="3.1.4")
    result = await _install_packaged(tmp_path, _packaged("npm:@scope/pkg"), "a")
    assert result["status"] == "installed" and result["version"] == "3.1.4"
    assert [(b["name"], b["source"]) for b in asked] == [("pkg-mcp", "npm:@scope/pkg")]
    folder = mcps_dir / "community" / "pkg-mcp"
    assert _installed(mcps_dir)["server"]["source"] == "npm:@scope/pkg@3.1.4"
    assert (folder / "package.json").read_bytes() == mcp_installer._node_package_json("@scope/pkg", "3.1.4")
    assert not (folder / "node_modules").exists() and not (folder / "venv").exists()

    # The machine's side of the same build: the tarball's files, the
    # installer's package file, then the pin.
    machine = tmp_path / "machine" / "pkg-mcp"
    shutil.copytree(tmp_path / "upload" / "pkg-mcp-a", machine)
    (machine / "package.json").write_bytes(mcp_installer._node_package_json("@scope/pkg", "latest"))
    mcp_pin.settle_built(machine, "npm:@scope/pkg", "3.1.4")
    assert mcp_installer.compute_version_hash(folder) == mcp_installer.compute_version_hash(machine)
    assert sent_back == []


@pytest.mark.asyncio
async def test_a_failed_build_leaves_the_installed_version(mcps_dir, tmp_path, monkeypatch, sent_back):  # noqa: F811
    builds(monkeypatch, resolved="1.0.0")
    await _install_packaged(tmp_path, _packaged("npm:pkg"), "a")
    before = _installed(mcps_dir)
    builds(monkeypatch, ok=False, log="npm ERR! 404")
    with pytest.raises(HTTPException) as ei:
        await _install_packaged(tmp_path, _packaged("npm:pkg"), "b")
    assert ei.value.status_code == 500 and "npm ERR! 404" in ei.value.detail
    assert "nothing was changed" in ei.value.detail
    assert _installed(mcps_dir) == before
    assert (mcps_dir / "community" / "pkg-mcp" / "README.md").read_text() == "Pkg a.\n"
    assert mcp_registry.get_manifest("pkg-mcp").version == "1.0.0"


@pytest.mark.asyncio
async def test_a_fresh_install_whose_build_fails_leaves_no_folder(mcps_dir, tmp_path, monkeypatch, sent_back):  # noqa: F811
    builds(monkeypatch, ok=False, log="No solution found")
    with pytest.raises(HTTPException):
        await _install_packaged(tmp_path, _packaged("pypi:pkg", runtime="python"), "a")
    assert not (mcps_dir / "community" / "pkg-mcp").exists()
    assert mcp_registry.get_manifest("pkg-mcp") is None


@pytest.mark.asyncio
async def test_a_step_that_fails_after_the_build_puts_both_sides_back(mcps_dir, tmp_path, monkeypatch, sent_back):  # noqa: F811
    """The machine already swapped the new version in: the platform's folder
    comes back, the registry reads it again, and the machine is sent what
    the platform holds."""
    builds(monkeypatch, resolved="1.0.0")
    await _install_packaged(tmp_path, _packaged("npm:pkg"), "a")
    before = _installed(mcps_dir)
    builds(monkeypatch, resolved="2.0.0")

    async def _breaks(*a, **kw):
        await mcp_registry.rescan()        # the step read the new folder before it failed
        raise RuntimeError("the database went away")
    monkeypatch.setattr(ci, "_register_and_start", _breaks)
    restarts: list = []

    async def _restart(stopped, prior_enabled, name):
        restarts.append(name)
    monkeypatch.setattr(ci, "_restart_stopped", _restart)
    with pytest.raises(RuntimeError):
        await _install_packaged(tmp_path, _packaged("npm:pkg"), "b")
    assert _installed(mcps_dir) == before
    assert mcp_registry.get_manifest("pkg-mcp").version == "1.0.0"
    assert sent_back == [MID]
    # The old compose file is back too: a container a switch stopped starts again.
    assert restarts == ["pkg-mcp"]
    assert not list((mcps_dir / "community").glob("pkg-mcp.*"))       # no backup left behind


@pytest.mark.asyncio
async def test_a_first_install_that_fails_after_the_build_leaves_no_row(mcps_dir, tmp_path, monkeypatch, sent_back):  # noqa: F811
    """The step that failed had read the new folder, which made the tool's
    state row. The platform never came to have the tool: its row goes with
    its folder, so the machine that built it is asked to take it away (a
    tool with a row is never read as uninstalled, ``mcp_sync.remove_gone``)."""
    from storage.mcp import mcp_store
    builds(monkeypatch, resolved="1.0.0")

    async def _breaks(*a, **kw):
        await mcp_registry.rescan()
        assert mcp_store.get_mcp_state("pkg-mcp") is not None
        raise RuntimeError("the database went away")
    monkeypatch.setattr(ci, "_register_and_start", _breaks)
    with pytest.raises(RuntimeError):
        await _install_packaged(tmp_path, _packaged("npm:pkg"), "a")
    assert not (mcps_dir / "community" / "pkg-mcp").exists()
    assert mcp_registry.get_manifest("pkg-mcp") is None
    assert mcp_store.get_mcp_state("pkg-mcp") is None
    assert sent_back == [MID]
    # A tool the registry lost keeps the row an admin's choices are on.
    mcp_store.set_mcp_enabled("pkg-mcp", False)
    with pytest.raises(RuntimeError):
        await _install_packaged(tmp_path, _packaged("npm:pkg"), "b")
    assert mcp_store.get_mcp_state("pkg-mcp")["enabled"] is False


@pytest.mark.asyncio
async def test_with_no_compute_machine_the_install_is_refused_and_nothing_changes(
        mcps_dir, tmp_path, monkeypatch, sent_back):  # noqa: F811
    from core import placement
    from services.mcp import mcp_sync

    async def _none(*a, **kw):
        raise mcp_sync.NoBuildMachine(placement.NO_COMPUTE_SENTENCE)
    monkeypatch.setattr(mcp_sync, "build_on_machine", _none)
    with pytest.raises(HTTPException) as ei:
        await _install_packaged(tmp_path, _packaged("npm:pkg"), "a")
    assert ei.value.status_code == 503 and ei.value.detail == placement.NO_COMPUTE_SENTENCE
    assert not (mcps_dir / "community" / "pkg-mcp").exists()


@pytest.mark.asyncio
async def test_the_job_says_it_waits_for_the_compute_machine(mcps_dir, tmp_path, monkeypatch, sent_back):  # noqa: F811
    from services.mcp import mcp_sync
    phases: list[tuple[str, str]] = []

    async def _build(name, mcp_dir, manifest_data, source, *, progress_cb=None, on_wait=None):
        await on_wait()
        return mcp_sync.Built(True, "", "1.0.0", "h", MID)
    builds(monkeypatch)          # the registry's answer, never the network
    monkeypatch.setattr(mcp_sync, "build_on_machine", _build)
    src = tmp_path / "upload" / "pkg-mcp-a"
    src.mkdir(parents=True)
    (src / "manifest.json").write_text(json.dumps(_packaged("npm:pkg")))

    async def _progress(ev):
        phases.append((ev["phase"], ev["message"]))
    await ci.install_from_extracted_folder(src, progress_cb=_progress)
    assert ("wait", "Waiting for the compute machine.") in phases


@pytest.mark.asyncio
async def test_a_container_tool_is_never_sent_to_a_machine(mcps_dir, tmp_path, monkeypatch, sent_back):  # noqa: F811
    """Its image is the platform's to pull: nothing is built on a machine."""
    asked = builds(monkeypatch)

    async def _registered(*a, **kw):
        return None
    monkeypatch.setattr(ci, "_register_and_start", _registered)
    manifest = {**_packaged("docker:ghcr.io/x/y:1.0", runtime="docker"), "version": "1.0"}
    manifest["server"] |= {"transport": "http", "port": 8999, "image": "ghcr.io/x/y:1.0",
                           "url_template": "http://${docker_mcp_host}:${port}"}
    result = await _install_packaged(tmp_path, manifest, "a")
    assert result["status"] == "installed" and asked == []


class TestTheOneStringTakenFromTheMachine:
    """R8.4: the version a build read back goes into a manifest."""

    @pytest.fixture
    def listed(self, monkeypatch):
        from services.mcp import mcp_updater
        answers = {"npm": ["1.0.0", "2.0.0"], "pypi": ["0.9.0"]}

        async def _npm(_package):
            return answers["npm"]

        async def _pypi(_package):
            return answers["pypi"]
        monkeypatch.setattr(mcp_updater, "_all_npm_versions", _npm)
        monkeypatch.setattr(mcp_updater, "_all_pypi_versions", _pypi)
        return answers

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source,resolved,version,refused", [
        ("npm:pkg@2.0.0", "2.0.0", "2.0.0", ""),
        ("npm:pkg@2.0.0", "1.0.0", "", "built version 1.0.0 of pkg, not 2.0.0"),   # a pin is the platform's word
        ("npm:pkg@2.0.0", "", "", "built version unknown of pkg, not 2.0.0"),
        ("npm:pkg", "2.0.0", "2.0.0", ""),
        ("npm:pkg", "6.6.6", "", "The registry lists no version 6.6.6 of pkg."),
        ("npm:pkg", "", "", "Could not read the installed version of pkg."),
        ("pypi:pkg", "0.9.0", "0.9.0", ""),
        ("pypi:pkg", "9.9.9", "", "The registry lists no version 9.9.9 of pkg."),
        ("pypi:pkg", "", "", ""),                                  # left unpinned, as before
        ("git+https://example.com/x.git@" + "a" * 40, "7.7.7", "", ""),            # nothing to pin
        # A range in an uploaded manifest names no one version: judged as unpinned.
        ("npm:pkg@^1.0.0", "2.0.0", "2.0.0", ""),
        ("npm:pkg@^1.0.0", "6.6.6", "", "The registry lists no version 6.6.6 of pkg."),
        ("npm:pkg@latest", "1.0.0", "1.0.0", ""),
        ("pypi:pkg@>=0.9", "0.9.0", "0.9.0", ""),
    ])
    async def test_it_is_the_pin_or_a_version_the_registry_lists(self, listed, source, resolved, version, refused):
        got, why = await ci._accepted_version(source, resolved)
        assert got == version and (refused in why if refused else why == "")

    @pytest.mark.asyncio
    async def test_a_registry_that_cannot_be_read_takes_the_plain_version(self, listed):
        """An install whose only index is private."""
        listed["npm"] = []
        assert await ci._accepted_version("npm:pkg", "4.2.0") == ("4.2.0", "")

    @pytest.mark.asyncio
    async def test_a_version_the_registry_does_not_list_rolls_the_install_back(
            self, mcps_dir, tmp_path, monkeypatch, sent_back):  # noqa: F811
        builds(monkeypatch, resolved="6.6.6", listed=("1.0.0",))
        with pytest.raises(HTTPException) as ei:
            await _install_packaged(tmp_path, _packaged("npm:pkg"), "a")
        assert "The registry lists no version 6.6.6 of pkg." in ei.value.detail
        assert not (mcps_dir / "community" / "pkg-mcp").exists()
        assert sent_back == [MID]              # the machine built it: its copy is taken away


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["café", "_tool", "-tool", "t" * 65])
async def test_a_name_no_machine_can_keep_is_refused_before_any_file_moves(
        mcps_dir, tmp_path, monkeypatch, sent_back, name):  # noqa: F811
    """A machine keeps a tool under its folder's name, by its own rule for
    one, and a new tool's folder is its name."""
    asked = builds(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await _install_packaged(tmp_path, _packaged("npm:pkg", name=name), "a")
    assert ei.value.status_code == 400 and ei.value.detail == (
        f"The name {name!r} cannot be used for this tool. Use letters, digits, dashes and "
        "underscores, begin with a letter or a digit, and use 64 characters at most.")
    assert asked == [] and not list((mcps_dir / "community").iterdir())


async def _installed_before_the_rule(mcps_dir, name: str, folder: str):  # noqa: F811
    """A tool an earlier release installed from a zip: any letter or digit
    passed as a name, and its folder is the name with every character
    outside a to z, digits, dashes and underscores replaced."""
    target = mcps_dir / "community" / folder
    target.mkdir()
    (target / "README.md").write_text("Pkg.\n")
    (target / "manifest.json").write_text(json.dumps(_packaged("npm:pkg@1.0.0", name=name) | {"version": "1.0.0"}))
    await mcp_registry.rescan()
    assert mcp_registry.get_manifest(name).mcp_dir.name == folder
    return target


@pytest.mark.asyncio
async def test_a_tool_installed_before_the_rule_updates_under_its_folder(mcps_dir, tmp_path, monkeypatch, sent_back):  # noqa: F811
    """A released install's tool keeps working whatever its name: a machine
    keys it by its folder, which a session's config names too."""
    target = await _installed_before_the_rule(mcps_dir, "café", "caf-")
    asked = builds(monkeypatch, resolved="2.0.0")
    result = await _install_packaged(tmp_path, _packaged("npm:pkg@2.0.0", name="café"), "b")
    assert result["status"] == "updated"
    assert [(b["name"], b["mcp_dir"]) for b in asked] == [("café", target)]
    assert json.loads((target / "manifest.json").read_text())["server"]["source"] == "npm:pkg@2.0.0"


@pytest.mark.asyncio
async def test_a_tool_whose_folder_no_machine_takes_is_refused_at_its_update(mcps_dir, tmp_path, monkeypatch, sent_back):  # noqa: F811
    """It runs where it was copied. A machine installs nothing under such a
    folder, so the update says what to do instead of failing there."""
    target = await _installed_before_the_rule(mcps_dir, "_tool", "_tool")
    asked = builds(monkeypatch, resolved="2.0.0")
    with pytest.raises(HTTPException) as ei:
        await _install_packaged(tmp_path, _packaged("npm:pkg@2.0.0", name="_tool"), "b")
    assert ei.value.status_code == 400 and "cannot be used for this tool" in ei.value.detail
    assert asked == [] and json.loads((target / "manifest.json").read_text())["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_the_platform_keeps_no_environment_of_a_tool_a_machine_builds(mcps_dir, tmp_path, monkeypatch, sent_back):  # noqa: F811
    """An environment a 1.9 install built here would sit beside the new
    sources after an update and be copied to a new compute volume as if it
    were the new version's. It goes with the update, the keys stay, and a
    failed update brings it back."""
    builds(monkeypatch, resolved="1.0.0")
    await _install_packaged(tmp_path, _packaged("npm:pkg"), "a")
    folder = mcps_dir / "community" / "pkg-mcp"
    for name in ("node_modules", "venv", "keys"):
        (folder / name).mkdir()
        (folder / name / "left").write_text("from before\n")

    builds(monkeypatch, ok=False, log="npm ERR!")
    with pytest.raises(HTTPException):
        await _install_packaged(tmp_path, _packaged("npm:pkg"), "b")
    assert all((folder / name / "left").is_file() for name in ("node_modules", "venv", "keys"))

    builds(monkeypatch, resolved="2.0.0")
    await _install_packaged(tmp_path, _packaged("npm:pkg"), "c")
    assert not (folder / "node_modules").exists() and not (folder / "venv").exists()
    assert (folder / "keys" / "left").read_text() == "from before\n"


@pytest.mark.asyncio
async def test_an_uninstall_tells_the_machine_that_builds_its_own_tools(mcps_dir, tmp_path, monkeypatch, sent_back):  # noqa: F811
    """No session's sync reaches that machine: its copy of the tool goes at
    a run of its own."""
    from api.mcp import mcps
    builds(monkeypatch, resolved="1.0.0")
    await _install_packaged(tmp_path, _packaged("npm:pkg"), "a")
    told: list[bool] = []
    monkeypatch.setattr(tools_ahead, "after_uninstall", lambda: told.append(True))
    assert (await mcps._delete_mcp_locked("pkg-mcp"))["status"] == "deleted"
    assert told == [True] and mcp_registry.get_manifest("pkg-mcp") is None
    assert not (mcps_dir / "community" / "pkg-mcp").exists()
