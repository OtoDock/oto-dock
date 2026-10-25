"""The waiting period before the weekly job applies an npm/pypi release
(``mcp_updater.RELEASE_MIN_AGE``): what the registry documents say, what the
check offers, what ``update_one(settled_only=True)`` applies, and what the
job skips. An admin's Update never waits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from services.community import community_catalog
from services.mcp import mcp_autoupdate as au, mcp_updater

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def _ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _npm_doc(times: dict[str, str | None], latest: str) -> dict:
    return {
        "dist-tags": {"latest": latest},
        "versions": {v: {} for v in times},
        "time": {"created": _ago(400), "modified": _ago(0),
                 **{v: t for v, t in times.items() if t is not None}},
    }


def _pypi_doc(releases: dict[str, list[dict]], latest: str) -> dict:
    return {"info": {"version": latest}, "releases": releases}


@pytest.fixture
def doc(monkeypatch):
    holder = {"doc": None}

    async def _doc(registry, package):
        return holder["doc"]
    monkeypatch.setattr(mcp_updater, "_registry_doc", _doc)
    return holder


class TestResolveRelease:
    @pytest.mark.asyncio
    async def test_a_young_latest_waits_and_the_settled_one_is_offered(self, doc):
        doc["doc"] = _npm_doc({"1.0.0": _ago(60), "1.1.0": _ago(10), "1.2.0": _ago(2)}, "1.2.0")
        rel = await mcp_updater.resolve_release("npm", "x", "", now=NOW)
        assert (rel.latest, rel.settled) == ("1.2.0", "1.1.0")
        assert rel.latest_at.startswith("2026-10-07T12:00")

    @pytest.mark.asyncio
    async def test_a_version_without_a_time_never_settles(self, doc):
        doc["doc"] = _npm_doc({"1.0.0": None, "1.1.0": None}, "1.1.0")
        rel = await mcp_updater.resolve_release("npm", "x", "", now=NOW)
        assert (rel.latest, rel.settled, rel.latest_at) == ("1.1.0", None, "")

    @pytest.mark.asyncio
    async def test_a_time_in_the_future_never_settles(self, doc):
        doc["doc"] = _npm_doc({"1.0.0": (NOW + timedelta(days=30)).isoformat()}, "1.0.0")
        assert (await mcp_updater.resolve_release("npm", "x", "", now=NOW)).settled is None

    @pytest.mark.asyncio
    async def test_settled_never_goes_above_latest_nor_takes_a_prerelease(self, doc):
        # The publisher's latest tag sits below a newer stable one.
        doc["doc"] = _npm_doc({"1.0.0": _ago(90), "2.0.0": _ago(30), "1.5.0": _ago(20),
                               "1.6.0-rc.1": _ago(15)}, "1.5.0")
        rel = await mcp_updater.resolve_release("npm", "x", "", now=NOW)
        assert (rel.latest, rel.settled) == ("1.5.0", "1.5.0")

    @pytest.mark.asyncio
    async def test_a_bound_picks_latest_and_settled_inside_it(self, doc):
        doc["doc"] = _npm_doc({"1.9.0": _ago(90), "2.0.0": _ago(3), "3.0.0": _ago(40)}, "3.0.0")
        rel = await mcp_updater.resolve_release("npm", "x", ">=1,<3", now=NOW)
        assert (rel.latest, rel.settled) == ("2.0.0", "1.9.0")

    @pytest.mark.asyncio
    async def test_pypi_times_a_release_by_its_newest_live_file(self, doc):
        doc["doc"] = _pypi_doc({
            "1.0.0": [{"upload_time_iso_8601": _ago(40)}],
            # an old release that got a wheel two days ago waits like a new one
            "1.1.0": [{"upload_time_iso_8601": _ago(30)}, {"upload_time_iso_8601": _ago(2)}],
            # yanked everywhere: never a candidate
            "1.2.0": [{"upload_time_iso_8601": _ago(20), "yanked": True}],
        }, "1.1.0")
        rel = await mcp_updater.resolve_release("pypi", "x", "", now=NOW)
        assert (rel.latest, rel.settled) == ("1.1.0", "1.0.0")

    @pytest.mark.asyncio
    async def test_an_unreadable_document_answers_latest_alone(self, doc, monkeypatch):
        doc["doc"] = None

        async def _latest(registry, package, constraint):
            return "4.0.0"
        monkeypatch.setattr(mcp_updater, "resolve_latest_in_bound", _latest)
        rel = await mcp_updater.resolve_release("npm", "x", "", now=NOW)
        assert (rel.latest, rel.settled) == ("4.0.0", None)

    def test_an_oversized_document_is_refused(self, monkeypatch):
        import io

        class _Resp(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(mcp_updater, "_REGISTRY_DOC_CAP", 10)
        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda req, timeout: _Resp(b'{"versions": {"1.0.0": {}}}'))
        assert mcp_updater._read_capped("https://registry.example/x", "application/json") is None

    def test_a_gzip_document_is_read(self, monkeypatch):
        import gzip
        import io

        body = gzip.compress(b'{"dist-tags": {"latest": "1.0.0"}}')

        class _Resp(io.BytesIO):
            headers = {"Content-Encoding": "gzip"}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: _Resp(body))
        assert mcp_updater._read_capped("https://r/x", "application/json") == {
            "dist-tags": {"latest": "1.0.0"}}


class TestWaitingFields:
    def test_waiting_latest(self):
        rel = mcp_updater.Release("1.2.0", "1.1.0", _ago(2).replace("Z", "+00:00"))
        assert mcp_updater._waiting_fields(rel, "1.0.0") == {
            "settled": "1.1.0", "auto_after": (NOW + timedelta(days=5)).isoformat()}

    def test_nothing_settled_newer_than_the_install(self):
        rel = mcp_updater.Release("1.2.0", "1.0.0", "")
        assert mcp_updater._waiting_fields(rel, "1.0.0") == {"settled": "", "auto_after": ""}

    def test_a_settled_latest_has_no_date(self):
        rel = mcp_updater.Release("1.2.0", "1.2.0", _ago(9))
        assert mcp_updater._waiting_fields(rel, "1.0.0") == {"settled": "1.2.0"}


# ---------------------------------------------------------------------------
# The check's offer and update_one(settled_only=True)
# ---------------------------------------------------------------------------

def _npm_manifest(tmp_path, version="1.0.0", name="pkg-mcp"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "manifest.json").write_text(f'{{"name": "{name}"}}')
    return NS(name=name, category="community", version=version, mcp_dir=str(d),
              server=NS(runtime="node", source=f"npm:pkg@{version}", version_constraint="",
                        image=""))


@pytest.fixture
def one_npm_mcp(tmp_path, monkeypatch):
    m = _npm_manifest(tmp_path)
    monkeypatch.setattr(mcp_updater.mcp_registry, "get_all_manifests", lambda: {m.name: m})
    monkeypatch.setattr(mcp_updater.mcp_registry, "get_manifest", lambda n: m if n == m.name else None)

    async def _registry():
        return {"mcps": [{"name": m.name, "version": "", "runtime": "node", "source": "npm:pkg"}]}
    monkeypatch.setattr(community_catalog, "fetch_registry", _registry)

    async def _skills():
        return {"skills": []}
    monkeypatch.setattr(community_catalog, "fetch_skills_registry", _skills)

    async def _persist(results, source_changes, unchanged, registry_ok):
        return {"updates": results, "checked": 0, "checked_at": ""}
    monkeypatch.setattr(mcp_updater, "_persist_check", _persist)

    async def _changes(manifests, catalog):
        return {}, set()
    from services.community import mcp_source_swap
    monkeypatch.setattr(mcp_source_swap, "detect_changes", _changes)
    return m


@pytest.mark.asyncio
async def test_the_check_offers_latest_and_names_what_the_job_applies(one_npm_mcp, doc, monkeypatch):
    doc["doc"] = _npm_doc({"1.0.0": _ago(60), "1.1.0": _ago(10), "1.2.0": _ago(1)}, "1.2.0")
    monkeypatch.setattr(mcp_updater, "_utcnow", lambda: NOW)
    info = (await mcp_updater.detect_available_updates())["updates"]["pkg-mcp"]
    assert info["latest"] == "1.2.0" and info["reason"] == "package"
    assert info["settled"] == "1.1.0"
    assert info["auto_after"].startswith("2026-10-15")


@pytest.mark.asyncio
async def test_a_held_mcp_says_so(one_npm_mcp, doc, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(mcp_updater, "_utcnow", lambda: NOW)
    (Path(one_npm_mcp.mcp_dir) / mcp_updater.HOLD_MARKER).write_text("")
    doc["doc"] = _npm_doc({"1.0.0": _ago(60), "1.1.0": _ago(30)}, "1.1.0")
    info = (await mcp_updater.detect_available_updates())["updates"]["pkg-mcp"]
    assert info["held"] is True and info["settled"] == "1.1.0"


@pytest.fixture
def converge(monkeypatch):
    calls = []

    async def _install(name, *, install_version=None, progress_cb=None):
        calls.append(install_version)
        return {"status": "updated", "name": name, "version": install_version}
    from services.community import community_installer
    monkeypatch.setattr(community_installer, "install_from_catalog", _install)

    async def _entry(name):
        return {"name": name, "version": "", "source": "npm:pkg", "version_constraint": ""}
    monkeypatch.setattr(mcp_updater, "_catalog_entry", _entry)
    return calls


@pytest.mark.asyncio
async def test_the_job_installs_the_settled_release_and_keeps_the_newer_offer(
    tmp_path, doc, converge, monkeypatch,
):
    monkeypatch.setattr(mcp_updater, "_utcnow", lambda: NOW)
    m = _npm_manifest(tmp_path)
    doc["doc"] = _npm_doc({"1.0.0": _ago(60), "1.1.0": _ago(10), "1.2.0": _ago(1)}, "1.2.0")
    kept = {}
    monkeypatch.setattr(mcp_updater, "_keep_check_result", lambda n, info: kept.update({n: info}))
    monkeypatch.setattr(mcp_updater, "_forget_check_result", lambda n: kept.update({n: None}))
    monkeypatch.setattr(mcp_updater.mcp_registry, "get_manifest", lambda n: m)
    result = await mcp_updater.update_one("pkg-mcp", settled_only=True)
    assert converge == ["1.1.0"] and result["version"] == "1.1.0"
    assert "pending_offer" not in result
    assert kept["pkg-mcp"]["current"] == "1.1.0" and kept["pkg-mcp"]["latest"] == "1.2.0"


@pytest.mark.asyncio
async def test_an_admin_update_installs_the_newest_release_at_once(tmp_path, doc, converge, monkeypatch):
    m = _npm_manifest(tmp_path)

    async def _latest(registry, package, constraint):
        return "1.2.0"
    monkeypatch.setattr(mcp_updater, "resolve_latest_in_bound", _latest)
    monkeypatch.setattr(mcp_updater, "_forget_check_result", lambda n: None)
    monkeypatch.setattr(mcp_updater.mcp_registry, "get_manifest", lambda n: m)
    await mcp_updater.update_one("pkg-mcp")
    assert converge == ["1.2.0"]


@pytest.mark.asyncio
async def test_a_manifest_change_beside_an_unsettled_release_waits_whole(
    tmp_path, doc, converge, monkeypatch,
):
    monkeypatch.setattr(mcp_updater, "_utcnow", lambda: NOW)
    m = _npm_manifest(tmp_path)
    doc["doc"] = _npm_doc({"1.0.0": _ago(60), "2.0.0": _ago(2)}, "2.0.0")

    async def _entry(name):
        return {"name": name, "version": "", "source": "npm:pkg",
                "version_constraint": ">=2", "manifest_hash": "new-hash"}
    monkeypatch.setattr(mcp_updater, "_catalog_entry", _entry)
    monkeypatch.setattr(mcp_updater.mcp_registry, "get_manifest", lambda n: m)
    result = await mcp_updater.update_one("pkg-mcp", settled_only=True)
    assert result["status"] == "waiting" and converge == []


@pytest.mark.asyncio
async def test_a_manifest_change_alone_converges_at_once(tmp_path, doc, converge, monkeypatch):
    m = _npm_manifest(tmp_path)
    doc["doc"] = _npm_doc({"1.0.0": _ago(60)}, "1.0.0")

    async def _entry(name):
        return {"name": name, "version": "", "source": "npm:pkg", "manifest_hash": "new-hash"}
    monkeypatch.setattr(mcp_updater, "_catalog_entry", _entry)
    monkeypatch.setattr(mcp_updater, "_forget_check_result", lambda n: None)
    monkeypatch.setattr(mcp_updater.mcp_registry, "get_manifest", lambda n: m)
    await mcp_updater.update_one("pkg-mcp", settled_only=True)
    assert converge == ["1.0.0"]


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("info, waits", [
    ({"reason": "package", "latest": "1.2.0", "settled": ""}, True),
    ({"reason": "package", "latest": "1.2.0", "settled": "1.1.0"}, False),
    ({"reason": "both", "latest": "1.2.0", "settled": "1.1.0"}, True),
    ({"reason": "both", "latest": "1.2.0", "settled": "1.2.0"}, False),
    ({"reason": "manifest", "latest": "1.0.0", "settled": ""}, False),
    ({"reason": "package", "latest": "2.0"}, False),   # a container's row
])
def test_which_offers_wait(info, waits):
    assert au._waits(info) is waits


COMMIT = "2cea8d566dde07fbac28a61d698909d69724e853"
NEXT_COMMIT = "87b38d3c453fbf6bb516259732296d5b6fcda376"


@pytest.mark.asyncio
async def test_the_job_builds_a_commit_pinned_git_source_and_leaves_a_tag(monkeypatch, caplog):
    rows, applied = [], []
    monkeypatch.setattr(au.log_store, "record_result", lambda *a, **k: rows.append(k))
    young = NS(name="young", category="community", server=NS(runtime="node", source="npm:y", image=""))
    gitm = NS(name="blender", category="community",
              server=NS(runtime="python", source=f"git+https://h/r.git@{COMMIT}", image=""))
    tagged = NS(name="tagged", category="community",
                server=NS(runtime="python", source="git+https://h/t.git@v1.0.0", image=""))
    ready = NS(name="ready", category="community", server=NS(runtime="node", source="npm:r", image=""))
    monkeypatch.setattr(au.mcp_updater, "community_targets", lambda: [young, gitm, tagged, ready])

    async def _detect():
        return {"updates": {
            "young": {"current": "1.0", "latest": "1.1", "reason": "package", "settled": ""},
            # A catalog bump is our reviewed change: built at once, no wait.
            "blender": {"current": "1.0.2", "latest": "1.0.3", "reason": "package", "registry": "git",
                        "pinned_commit": True, "catalog_ref": NEXT_COMMIT},
            # A tag can move upstream with no catalog change: never built.
            "tagged": {"current": "1.0.0", "latest": "1.1.0", "reason": "package", "registry": "git",
                       "pinned_commit": False, "catalog_ref": "v1.1.0"},
            "ready": {"current": "1.0", "latest": "1.1", "reason": "package", "settled": "1.1"},
        }}
    monkeypatch.setattr(au.mcp_updater, "detect_available_updates", _detect)

    async def _update_one(name, **kw):
        applied.append((name, kw))
        return {"status": "updated", "old_version": "1.0", "version": "1.1"}
    monkeypatch.setattr(au.mcp_updater, "update_one", _update_one)
    with caplog.at_level("INFO", logger="otodock.mcp-autoupdate"):
        summary = await au.run_auto_update()
    assert applied == [("blender", {"settled_only": True}), ("ready", {"settled_only": True})]
    assert summary["counts"]["updated"] == 2
    assert [r["status"] for r in rows] == ["updated", "updated"]
    assert f"building blender from {COMMIT[:12]} to the catalog's commit {NEXT_COMMIT[:12]}" in caplog.text
    assert "tagged's catalog source pins no commit" in caplog.text


@pytest.mark.asyncio
async def test_a_failed_git_build_is_recorded_and_the_run_goes_on(monkeypatch):
    rows, notified = [], []
    monkeypatch.setattr(au.log_store, "record_result", lambda *a, **k: rows.append(k))
    gitm = NS(name="blender", category="community",
              server=NS(runtime="python", source=f"git+https://h/r.git@{COMMIT}", image=""))
    monkeypatch.setattr(au.mcp_updater, "community_targets", lambda: [gitm])

    async def _detect():
        return {"updates": {"blender": {"current": "1.0.2", "latest": "1.0.3", "reason": "package",
                                        "registry": "git", "pinned_commit": True,
                                        "catalog_ref": "f" * 40}}}
    monkeypatch.setattr(au.mcp_updater, "detect_available_updates", _detect)

    async def _update_one(name, **kw):
        # A 40-hex ref that names no commit fails the build; the installer
        # has already put the old folder back.
        raise RuntimeError("git checkout ffffffffffff: reference is not a tree")
    monkeypatch.setattr(au.mcp_updater, "update_one", _update_one)

    async def _notify(run_id, failed):
        notified.append(failed)
    monkeypatch.setattr(au, "_notify_failures", _notify)
    summary = await au.run_auto_update()
    assert summary["counts"]["failed"] == 1
    assert rows[0]["status"] == "failed" and "reference is not a tree" in rows[0]["error"]
    assert notified and notified[0][0][0] == "blender"


@pytest.mark.asyncio
async def test_update_one_builds_a_git_source_for_the_job_only_at_a_catalog_commit(monkeypatch):
    from fastapi import HTTPException

    from services.community import community_installer
    m = NS(name="blender", category="community",
           server=NS(runtime="python", source="git+https://h/r.git@abc", image=""))
    monkeypatch.setattr(mcp_updater.mcp_registry, "get_manifest", lambda n: m)
    installed = []

    async def _install(name, **kw):
        installed.append(name)
        return {"status": "updated", "version": "1.0.3"}
    monkeypatch.setattr(community_installer, "install_from_catalog", _install)
    monkeypatch.setattr(mcp_updater, "_forget_check_result", lambda n: None)
    catalog = {"source": "git+https://h/r.git@v1.0.3#subdirectory=mcp"}

    async def _entry(name):
        return {"name": name, "version": "1.0.3", **catalog}
    monkeypatch.setattr(mcp_updater, "_catalog_entry", _entry)
    # A tag in the catalog: the job's call is refused, nothing is built.
    with pytest.raises(HTTPException) as ei:
        await mcp_updater.update_one("blender", settled_only=True)
    assert ei.value.status_code == 409 and "never build a tag or a branch" in ei.value.detail
    assert installed == []
    # A commit: built, with no waiting period.
    catalog["source"] = f"git+https://h/r.git@{NEXT_COMMIT}#subdirectory=mcp"
    result = await mcp_updater.update_one("blender", settled_only=True)
    assert result["status"] == "updated" and installed == ["blender"]
    # An admin's Update never asks the catalog first (the install refuses a
    # tag itself).
    catalog["source"] = "git+https://h/r.git@v1.0.3"
    await mcp_updater.update_one("blender")
    assert installed == ["blender", "blender"]


def test_a_git_ref_is_a_commit_only_at_40_lowercase_hex():
    from services.community.community_installer import git_ref_is_commit
    assert git_ref_is_commit(f"git+https://github.com/o/r.git@{COMMIT}")
    assert git_ref_is_commit(f"git+https://github.com/o/r.git@{COMMIT}#subdirectory=mcp")
    assert not git_ref_is_commit("git+https://github.com/o/r.git@v1.2.0")
    assert not git_ref_is_commit("git+https://github.com/o/r.git@main")
    assert not git_ref_is_commit("git+https://github.com/o/r.git")
    assert not git_ref_is_commit(f"git+https://github.com/o/r.git@{COMMIT.upper()}")
    assert not git_ref_is_commit(f"git+https://github.com/o/r.git@{COMMIT[:39]}")
    assert not git_ref_is_commit(f"npm:pkg@{COMMIT}")
    assert not git_ref_is_commit(None)


@pytest.mark.asyncio
async def test_a_repair_never_waits_for_a_young_release(tmp_path, doc, converge, monkeypatch):
    """A venv the sandbox cannot run is rebuilt at once, at the installed
    version, and the newer release keeps waiting on the page."""
    from services.mcp import mcp_venv_bootstrap
    monkeypatch.setattr(mcp_updater, "_utcnow", lambda: NOW)
    m = _npm_manifest(tmp_path)
    m.server.source = "pypi:pkg@1.0.0"
    doc["doc"] = _pypi_doc({"1.0.0": [{"upload_time_iso_8601": _ago(60)}],
                            "1.1.0": [{"upload_time_iso_8601": _ago(1)}]}, "1.1.0")

    async def _entry(name):
        return {"name": name, "version": "", "source": "pypi:pkg"}
    monkeypatch.setattr(mcp_updater, "_catalog_entry", _entry)
    monkeypatch.setattr(mcp_venv_bootstrap, "venv_outside_sandbox", lambda d: True)
    kept = {}
    monkeypatch.setattr(mcp_updater, "_keep_check_result", lambda n, info: kept.update({n: info}))
    monkeypatch.setattr(mcp_updater.mcp_registry, "get_manifest", lambda n: m)
    result = await mcp_updater.update_one("pkg-mcp", settled_only=True)
    assert converge == ["1.0.0"] and result["status"] == "updated"
    assert kept["pkg-mcp"]["latest"] == "1.1.0"
    assert au._waits({"reason": "both", "latest": "1.1.0", "settled": "", "rebuild": True}) is False


@pytest.mark.asyncio
async def test_an_unreadable_registry_keeps_the_offer(tmp_path, doc, converge, monkeypatch):
    m = _npm_manifest(tmp_path)
    doc["doc"] = None

    async def _latest(registry, package, constraint):
        return "1.2.0"
    monkeypatch.setattr(mcp_updater, "resolve_latest_in_bound", _latest)
    forgot = []
    monkeypatch.setattr(mcp_updater, "_forget_check_result", forgot.append)
    monkeypatch.setattr(mcp_updater.mcp_registry, "get_manifest", lambda n: m)
    result = await mcp_updater.update_one("pkg-mcp", settled_only=True)
    assert result["status"] == "waiting" and converge == [] and forgot == []


@pytest.mark.asyncio
async def test_no_rebuild_offer_without_a_catalog_entry(one_npm_mcp, doc, monkeypatch):
    from services.mcp import mcp_venv_bootstrap
    one_npm_mcp.server.source = "pypi:gone@1.0.0"
    one_npm_mcp.version = "1.0.0"
    monkeypatch.setattr(mcp_venv_bootstrap, "venv_outside_sandbox", lambda d: True)

    async def _registry():
        return {"mcps": []}
    monkeypatch.setattr(community_catalog, "fetch_registry", _registry)
    doc["doc"] = _pypi_doc({"1.0.0": [{"upload_time_iso_8601": _ago(60)}]}, "1.0.0")
    assert "pkg-mcp" not in (await mcp_updater.detect_available_updates())["updates"]


@pytest.mark.asyncio
async def test_a_probe_cancelled_on_its_own_drops_only_its_mcp(tmp_path, one_npm_mcp, monkeypatch):
    """One registry probe ending in its own cancellation (a CancelledError
    is a BaseException, not an Exception) loses that MCP's offer and keeps
    the others; the check itself completes."""
    import asyncio
    gone = _npm_manifest(tmp_path, name="gone-mcp")
    gone.server.source = "npm:gone@1.0.0"
    both = {one_npm_mcp.name: one_npm_mcp, gone.name: gone}
    monkeypatch.setattr(mcp_updater.mcp_registry, "get_all_manifests", lambda: both)
    monkeypatch.setattr(mcp_updater, "_utcnow", lambda: NOW)

    async def _doc(registry, package):
        if package == "gone":
            raise asyncio.CancelledError
        return _npm_doc({"1.0.0": _ago(60), "1.1.0": _ago(30)}, "1.1.0")
    monkeypatch.setattr(mcp_updater, "_registry_doc", _doc)
    updates = (await mcp_updater.detect_available_updates())["updates"]
    assert updates["pkg-mcp"]["latest"] == "1.1.0"
    assert "gone-mcp" not in updates
