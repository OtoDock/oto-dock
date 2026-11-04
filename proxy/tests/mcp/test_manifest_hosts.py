"""The bearer allowlist follows the manifest (``services/community/manifest_hosts.py``
and the ``source`` rows of ``storage/identity/bearer_allowlist.py``): what a
catalog install applies by itself, what waits for an admin's Allow, a ZIP's
narrowing, an update that moves a host, disable and enable, removal, an
MCP's row taking over the admin row of its pair, the handover of an MCP
installed before its origin was recorded, and the migration of an older table.
"""

from __future__ import annotations

import json
import uuid

import pytest

from services.community import manifest_hosts
from services.mcp import mcp_manifest_parse, mcp_registry
from storage.identity import bearer_allowlist
from storage.mcp import mcp_store
from storage.pg import get_conn


def _data(name, *, url="https://mcp.vendor-h7.example/mcp", hosts=("mcp.vendor-h7.example",),
          provider="vendor-h7", container=False):
    server = {"transport": "streamable_http", "url_template": url, "source": "remote:x"}
    if container:
        server = {"transport": "streamable_http", "url_template": "http://${docker_mcp_host}:${port}/mcp",
                  "runtime": "docker", "image": "ghcr.io/example/x:1", "port": 9000}
    return {
        "name": name, "label": name.title(), "description": "d", "version": "1.0.0", "category": "community",
        "server": server,
        "credentials": {"type": "per_user", "label": "A", "oauth": {
            "provider_id": provider, "flows": ["authorization_code"],
            "authorization_url": "https://vendor-h7.example/a", "token_url": "https://vendor-h7.example/t",
            "bearer_required": True, "proposed_hosts": list(hosts),
            "services": [{"key": "default", "label": "D", "description": "d", "scopes": ["s"]}]}},
    }


@pytest.fixture
def reg(monkeypatch, tmp_path):
    """A registry of scratch manifests; ``put`` installs or replaces one."""
    monkeypatch.setattr(mcp_registry, "_manifests", {})
    names: list[str] = []
    ticks: list[int] = []
    from core.remote import mcp_gateway_push
    monkeypatch.setattr(mcp_gateway_push, "tick_soon", lambda: ticks.append(1))

    def put(data, *, enabled=True):
        path = tmp_path / data["name"] / f"{uuid.uuid4().hex[:6]}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        m = mcp_manifest_parse._parse_manifest(path)
        assert m is not None, data
        mcp_registry._manifests[m.name] = m
        mcp_store.set_mcp_enabled(m.name, enabled)
        names.append(m.name)
        return m
    put.ticks = ticks  # type: ignore[attr-defined]
    yield put
    for n in set(names) - {bearer_allowlist.ADMIN_SOURCE}:
        bearer_allowlist.forget_mcp(n)
        with get_conn() as conn:
            conn.execute("DELETE FROM mcp_state WHERE name = %s", (n,))
            conn.commit()
    with get_conn() as conn:
        conn.execute("DELETE FROM oauth_bearer_allowlist WHERE provider_id LIKE 'vendor-h7%%'")
        conn.commit()


def _name():
    return f"h7-{uuid.uuid4().hex[:8]}"


def _rows(name):
    return bearer_allowlist.live_hosts(name)


class TestCatalog:
    def test_the_mcps_own_host_applies_by_itself(self, reg):
        n = _name()
        reg(_data(n))
        assert manifest_hosts.after_install(n, from_catalog=True) is None
        assert _rows(n) == {("vendor-h7", "mcp.vendor-h7.example")}
        assert bearer_allowlist.is_host_allowed("vendor-h7", "mcp.vendor-h7.example")

    def test_an_update_that_moves_the_host_moves_the_row(self, reg):
        n = _name()
        reg(_data(n))
        manifest_hosts.after_install(n, from_catalog=True)
        reg(_data(n, url="https://api.vendor-h7.example/mcp", hosts=("api.vendor-h7.example",)))
        manifest_hosts.after_install(n, from_catalog=True)
        assert _rows(n) == {("vendor-h7", "api.vendor-h7.example")}
        assert not bearer_allowlist.is_host_allowed("vendor-h7", "mcp.vendor-h7.example")
        assert reg.ticks  # a row went: paired machines are told

    def test_a_provider_id_that_changed_moves_the_rows(self, reg):
        n = _name()
        reg(_data(n))
        manifest_hosts.after_install(n, from_catalog=True)
        reg(_data(n, provider="vendor-h7b"))
        manifest_hosts.after_install(n, from_catalog=True)
        assert _rows(n) == {("vendor-h7b", "mcp.vendor-h7.example")}
        assert bearer_allowlist.approved_hosts(n) == {("vendor-h7b", "mcp.vendor-h7.example")}
        assert not bearer_allowlist.is_host_allowed("vendor-h7", "mcp.vendor-h7.example")

    def test_a_dropped_host_goes(self, reg):
        n = _name()
        reg(_data(n, hosts=("mcp.vendor-h7.example", "other.vendor-h7.example")))
        manifest_hosts.after_install(n, from_catalog=True)
        manifest_hosts.confirm(n, provider="vendor-h7", hosts=["other.vendor-h7.example"],
                               shown=manifest_hosts.digest(mcp_registry.get_manifest(n)), by="admin-sub")
        assert len(_rows(n)) == 2
        reg(_data(n))
        manifest_hosts.after_install(n, from_catalog=True)
        assert _rows(n) == {("vendor-h7", "mcp.vendor-h7.example")}

    def test_a_host_beside_the_server_waits_for_an_allow(self, reg):
        n = _name()
        reg(_data(n, hosts=("mcp.vendor-h7.example", "*.vendor-h7.example", "10.0.0.9", "*")))
        waiting = manifest_hosts.after_install(n, from_catalog=True)
        assert _rows(n) == {("vendor-h7", "mcp.vendor-h7.example")}
        assert waiting["hosts"] == ["*.vendor-h7.example", "10.0.0.9"]  # a bare * never applies
        assert waiting["provider"] == "vendor-h7" and waiting["mcp_name"] == n

    def test_an_http_server_host_waits(self, reg):
        n = _name()
        reg(_data(n, url="http://mcp.vendor-h7.example/mcp"))
        assert manifest_hosts.after_install(n, from_catalog=True)["hosts"] == ["mcp.vendor-h7.example"]
        assert _rows(n) == set()

    def test_a_templated_server_host_waits(self, reg):
        n = _name()
        reg(_data(n, url="https://${tenant}.vendor-h7.example/mcp", hosts=("acme.vendor-h7.example",)))
        assert manifest_hosts.after_install(n, from_catalog=True)["hosts"] == ["acme.vendor-h7.example"]
        assert _rows(n) == set()

    def test_a_container_applies_localhost(self, reg):
        n = _name()
        reg(_data(n, container=True, hosts=("localhost",)))
        manifest_hosts.after_install(n, from_catalog=True)
        assert _rows(n) == {("vendor-h7", "localhost")}

    def test_a_disabled_mcp_is_approved_but_not_live(self, reg):
        n = _name()
        reg(_data(n), enabled=False)
        manifest_hosts.after_install(n, from_catalog=True)
        assert _rows(n) == set() and bearer_allowlist.approved_hosts(n)


class TestZip:
    def test_nothing_applies_until_the_admin_allows_what_was_shown(self, reg):
        n = _name()
        m = reg(_data(n))
        waiting = manifest_hosts.after_install(n, from_catalog=False)
        assert _rows(n) == set() and waiting["hosts"] == ["mcp.vendor-h7.example"]
        with pytest.raises(manifest_hosts.Refused):
            manifest_hosts.confirm(n, provider="vendor-h7", hosts=["evil.example"], shown=waiting["digest"], by="a")
        with pytest.raises(manifest_hosts.Refused):
            manifest_hosts.confirm(n, provider="google", hosts=waiting["hosts"], shown=waiting["digest"], by="a")
        with pytest.raises(manifest_hosts.Refused):
            manifest_hosts.confirm(n, provider="vendor-h7", hosts=waiting["hosts"], shown="0" * 16, by="a")
        manifest_hosts.confirm(n, provider="vendor-h7", hosts=waiting["hosts"], shown=manifest_hosts.digest(m),
                               by="admin-sub")
        assert _rows(n) == {("vendor-h7", "mcp.vendor-h7.example")}
        assert manifest_hosts.pending(m) is None

    def test_an_update_narrows_and_adds_nothing(self, reg):
        n = _name()
        m = reg(_data(n, hosts=("mcp.vendor-h7.example", "b.vendor-h7.example")))
        w = manifest_hosts.after_install(n, from_catalog=False)
        manifest_hosts.confirm(n, provider="vendor-h7", hosts=w["hosts"], shown=manifest_hosts.digest(m), by="a")
        reg(_data(n, hosts=("mcp.vendor-h7.example", "c.vendor-h7.example")))
        waiting = manifest_hosts.after_install(n, from_catalog=False)
        assert _rows(n) == {("vendor-h7", "mcp.vendor-h7.example")}
        assert waiting["hosts"] == ["c.vendor-h7.example"]

    def test_a_host_an_admin_row_already_allows_still_waits(self, reg):
        """An admin's row no longer hides a ZIP's host: it keeps serving until
        the Allow, which takes it over."""
        n = _name()
        admin_id = bearer_allowlist.add_allowed("vendor-h7", "mcp.vendor-h7.example", "admin-sub")
        m = reg(_data(n))
        waiting = manifest_hosts.after_install(n, from_catalog=False)
        assert waiting["hosts"] == ["mcp.vendor-h7.example"]
        assert bearer_allowlist.is_host_allowed("vendor-h7", "mcp.vendor-h7.example")
        manifest_hosts.confirm(n, provider="vendor-h7", hosts=waiting["hosts"], shown=manifest_hosts.digest(m),
                               by="admin-sub")
        assert _rows(n) == {("vendor-h7", "mcp.vendor-h7.example")}
        assert bearer_allowlist.get_allowed(admin_id) is None
        assert manifest_hosts.pending(m) is None


class TestLifecycle:
    def test_disable_takes_the_rows_down_and_enable_puts_them_back(self, reg):
        n = _name()
        reg(_data(n))
        manifest_hosts.after_install(n, from_catalog=True)
        mcp_store.set_mcp_enabled(n, False)
        manifest_hosts.after_disable(n)
        assert _rows(n) == set() and bearer_allowlist.approved_hosts(n)
        assert not bearer_allowlist.is_host_allowed("vendor-h7", "mcp.vendor-h7.example")
        mcp_store.set_mcp_enabled(n, True)
        manifest_hosts.after_enable(n)
        assert _rows(n) == {("vendor-h7", "mcp.vendor-h7.example")}

    def test_removal_forgets_only_the_mcps_rows(self, reg):
        n = _name()
        other_id = bearer_allowlist.add_allowed("vendor-h7", "other.vendor-h7.example", "admin-sub")
        reg(_data(n))
        manifest_hosts.after_install(n, from_catalog=True)
        assert bearer_allowlist.has_origin(n)
        manifest_hosts.after_removal(n)
        assert _rows(n) == set() and bearer_allowlist.approved_hosts(n) == set()
        assert not bearer_allowlist.has_origin(n)
        assert bearer_allowlist.get_allowed(other_id)["source"] == "admin"
        assert not bearer_allowlist.is_host_allowed("vendor-h7", "mcp.vendor-h7.example")

    def test_an_mcp_named_admin_owns_no_rows(self, reg):
        reg(_data("admin"))
        assert manifest_hosts.after_install("admin", from_catalog=True) is None
        with pytest.raises(ValueError):
            bearer_allowlist.live_hosts("admin")

    def test_an_api_key_mcp_named_like_a_provider_waits(self, reg):
        n = "google"
        data = _data(n)
        data["credentials"] = {"type": "per_user", "label": "K", "api_key_header": {
            "name": "X-Api-Key", "value_from": "KEY", "proposed_hosts": ["mcp.vendor-h7.example"]},
            "fields": [{"key": "KEY", "label": "Key", "input_type": "password"}]}
        m = reg(data)
        if manifest_hosts.hosts_of(m) is None:
            pytest.skip("this manifest shape does not parse here")
        waiting = manifest_hosts.after_install(n, from_catalog=True)
        assert _rows(n) == set() and waiting["hosts"] == ["mcp.vendor-h7.example"]


def _admin_rows(host):
    return [r for r in bearer_allowlist.list_allowed() if r["source"] == "admin" and r["host_pattern"].lower() == host]


def _via(name):
    with get_conn() as conn:
        rows = conn.execute("SELECT host_pattern, via FROM oauth_bearer_allowlist_mcp_approved WHERE mcp_name = %s",
                            (name,)).fetchall()
    return {r["host_pattern"]: r["via"] for r in rows}


class TestTakeover:
    def test_a_catalog_install_takes_over_the_admin_row(self, reg):
        n = _name()
        bearer_allowlist.add_allowed("vendor-h7", "MCP.Vendor-H7.example", "system")
        reg(_data(n))
        assert manifest_hosts.after_install(n, from_catalog=True) is None
        assert _rows(n) == {("vendor-h7", "mcp.vendor-h7.example")}
        assert _admin_rows("mcp.vendor-h7.example") == []
        assert bearer_allowlist.is_host_allowed("vendor-h7", "mcp.vendor-h7.example")
        assert not reg.ticks  # the pair stayed allowed: nothing to tell the paired machines

    def test_disable_turns_the_address_off_and_leaves_other_admin_rows(self, reg):
        n = _name()
        bearer_allowlist.add_allowed("vendor-h7", "mcp.vendor-h7.example", "system")
        other_id = bearer_allowlist.add_allowed("vendor-h7b", "api.vendor-h7.example", "admin-sub")
        reg(_data(n))
        manifest_hosts.after_install(n, from_catalog=True)
        mcp_store.set_mcp_enabled(n, False)
        manifest_hosts.after_disable(n)
        assert not bearer_allowlist.is_host_allowed("vendor-h7", "mcp.vendor-h7.example")
        assert bearer_allowlist.get_allowed(other_id)["source"] == "admin"
        mcp_store.set_mcp_enabled(n, True)
        manifest_hosts.after_enable(n)
        assert _rows(n) == {("vendor-h7", "mcp.vendor-h7.example")}

    def test_a_wildcard_admin_row_is_never_taken_over(self, reg):
        n = _name()
        wild_id = bearer_allowlist.add_allowed("vendor-h7", "*.vendor-h7.example", "admin-sub")
        reg(_data(n))
        manifest_hosts.after_install(n, from_catalog=True)
        assert _rows(n) == {("vendor-h7", "mcp.vendor-h7.example")}
        assert bearer_allowlist.get_allowed(wild_id)["source"] == "admin"

    def test_an_admin_wildcard_of_the_same_pattern_stays_the_admins(self, reg):
        """A declared wildcard the admin allowed goes live as the MCP's row;
        the admin's own row of the same pattern stays, so the MCP's disable
        leaves the wildcard allowed."""
        n = _name()
        wild_id = bearer_allowlist.add_allowed("vendor-h7", "*.vendor-h7.example", "admin-sub")
        m = reg(_data(n, hosts=("mcp.vendor-h7.example", "*.vendor-h7.example")))
        waiting = manifest_hosts.after_install(n, from_catalog=True)
        assert waiting["hosts"] == ["*.vendor-h7.example"]
        manifest_hosts.confirm(n, provider="vendor-h7", hosts=waiting["hosts"], shown=manifest_hosts.digest(m), by="a")
        assert ("vendor-h7", "*.vendor-h7.example") in _rows(n)
        assert bearer_allowlist.get_allowed(wild_id)["source"] == "admin"
        mcp_store.set_mcp_enabled(n, False)
        manifest_hosts.after_disable(n)
        assert bearer_allowlist.is_host_allowed("vendor-h7", "api.vendor-h7.example")

    def test_a_disabled_mcp_keeps_the_admin_row_until_its_enable(self, reg):
        n = _name()
        bearer_allowlist.add_allowed("vendor-h7", "mcp.vendor-h7.example", "system")
        reg(_data(n), enabled=False)
        manifest_hosts.after_install(n, from_catalog=True)
        assert _rows(n) == set() and len(_admin_rows("mcp.vendor-h7.example")) == 1
        mcp_store.set_mcp_enabled(n, True)
        manifest_hosts.after_enable(n)
        assert _rows(n) == {("vendor-h7", "mcp.vendor-h7.example")}
        assert _admin_rows("mcp.vendor-h7.example") == []

    def test_an_admin_row_another_mcp_leans_on_stays(self, reg):
        """B (a ZIP not yet allowed) leans on the admin row: A's takeover keeps
        it, so turning A off never strands B. Once B owns its row, A's next
        enable takes the admin row over."""
        a, b = _name(), _name()
        bearer_allowlist.add_allowed("vendor-h7", "mcp.vendor-h7.example", "system")
        mb = reg(_data(b))
        assert manifest_hosts.after_install(b, from_catalog=False)["hosts"] == ["mcp.vendor-h7.example"]
        reg(_data(a))
        manifest_hosts.after_install(a, from_catalog=True)
        assert _rows(a) == {("vendor-h7", "mcp.vendor-h7.example")}
        assert len(_admin_rows("mcp.vendor-h7.example")) == 1
        assert manifest_hosts.pending(mb) is None  # A's row covers B meanwhile
        mcp_store.set_mcp_enabled(a, False)
        manifest_hosts.after_disable(a)
        assert bearer_allowlist.is_host_allowed("vendor-h7", "mcp.vendor-h7.example")
        waiting = manifest_hosts.pending(mb)
        manifest_hosts.confirm(b, provider="vendor-h7", hosts=waiting["hosts"], shown=waiting["digest"], by="a")
        assert _rows(b) == {("vendor-h7", "mcp.vendor-h7.example")}
        assert _admin_rows("mcp.vendor-h7.example") == []  # A owns its pair while off: nobody leans

    def test_an_mcp_reaching_its_server_host_undeclared_keeps_the_admin_row(self, reg):
        """B's gateway forwards to its own server host, which it does not
        declare, on the admin's hand row: A's takeover leaves that row, so
        turning A off does not strand B."""
        a, b = _name(), _name()
        bearer_allowlist.add_allowed("vendor-h7", "mcp.vendor-h7.example", "admin-sub")
        reg(_data(b, hosts=("other.vendor-h7.example",)))
        reg(_data(a))
        manifest_hosts.after_install(a, from_catalog=True)
        assert _rows(a) == {("vendor-h7", "mcp.vendor-h7.example")}
        assert len(_admin_rows("mcp.vendor-h7.example")) == 1
        mcp_store.set_mcp_enabled(a, False)
        manifest_hosts.after_disable(a)
        assert bearer_allowlist.is_host_allowed("vendor-h7", "mcp.vendor-h7.example")

    def test_an_mcp_with_a_templated_host_keeps_every_admin_row_of_its_key(self, reg):
        """Which host a templated server reaches is known only at the session,
        so any exact admin row of its key may be the one it lives on."""
        a, b = _name(), _name()
        bearer_allowlist.add_allowed("vendor-h7", "mcp.vendor-h7.example", "admin-sub")
        reg(_data(b, url="https://${tenant}.vendor-h7.example/mcp", hosts=("acme.vendor-h7.example",)))
        reg(_data(a))
        manifest_hosts.after_install(a, from_catalog=True)
        assert len(_admin_rows("mcp.vendor-h7.example")) == 1

    def test_a_container_relies_on_localhost_undeclared(self, reg):
        a, b = _name(), _name()
        bearer_allowlist.add_allowed("vendor-h7", "localhost", "admin-sub")
        reg(_data(b, container=True, hosts=("api.vendor-h7.example",)))
        reg(_data(a, container=True, hosts=("localhost",)))
        manifest_hosts.after_install(a, from_catalog=True)
        assert _rows(a) == {("vendor-h7", "localhost")}
        assert len(_admin_rows("localhost")) == 1

    def test_two_catalog_mcps_of_one_provider_own_a_row_each(self, reg):
        a, b = _name(), _name()
        bearer_allowlist.add_allowed("vendor-h7", "mcp.vendor-h7.example", "system")
        reg(_data(a))
        reg(_data(b))
        manifest_hosts.after_install(a, from_catalog=True)
        manifest_hosts.after_install(b, from_catalog=True)
        assert _rows(a) == _rows(b) == {("vendor-h7", "mcp.vendor-h7.example")}
        assert _admin_rows("mcp.vendor-h7.example") == []
        mcp_store.set_mcp_enabled(a, False)
        manifest_hosts.after_disable(a)
        assert bearer_allowlist.is_host_allowed("vendor-h7", "mcp.vendor-h7.example")

    def test_a_row_for_a_pair_no_mcp_declares_is_untouched(self, reg):
        n = _name()
        hand_id = bearer_allowlist.add_allowed("vendor-h7-hand", "hand.vendor-h7.example", "admin-sub")
        reg(_data(n))
        manifest_hosts.after_install(n, from_catalog=True)
        mcp_store.set_mcp_enabled(n, False)
        manifest_hosts.after_disable(n)
        manifest_hosts.after_removal(n)
        assert bearer_allowlist.get_allowed(hand_id)["source"] == "admin"


class TestBeforeTheOrigin:
    """An MCP installed before its origin was recorded (no after_install ran
    on this version) hands its own-shaped pairs over at its enable."""

    def test_the_enable_takes_over_the_admin_row_of_its_own_host(self, reg):
        n = _name()
        bearer_allowlist.add_allowed("vendor-h7", "mcp.vendor-h7.example", "system")
        m = reg(_data(n))
        assert manifest_hosts.pending(m) is None  # handed over at the enable, nothing waits
        manifest_hosts.after_enable(n)
        assert _rows(n) == {("vendor-h7", "mcp.vendor-h7.example")}
        assert _admin_rows("mcp.vendor-h7.example") == []
        assert _via(n) == {"mcp.vendor-h7.example": manifest_hosts.VIA_ADMIN_ROW}
        mcp_store.set_mcp_enabled(n, False)
        manifest_hosts.after_disable(n)
        mcp_store.set_mcp_enabled(n, True)
        manifest_hosts.after_enable(n)
        assert _rows(n) == {("vendor-h7", "mcp.vendor-h7.example")}

    def test_a_row_gone_since_the_read_is_not_handed_over(self, reg, monkeypatch):
        """The handover stands on the admin row as the enable's transaction
        finds it: a row the admin removed after the read approves nothing."""
        n = _name()
        reg(_data(n))
        monkeypatch.setattr(bearer_allowlist, "admin_pairs",
                            lambda key: {("vendor-h7", "mcp.vendor-h7.example")} if key == "vendor-h7" else set())
        manifest_hosts.after_enable(n)
        assert _rows(n) == set() and bearer_allowlist.approved_hosts(n) == set()
        assert not bearer_allowlist.is_host_allowed("vendor-h7", "mcp.vendor-h7.example")

    def test_an_admin_removal_in_flight_is_waited_for(self, reg):
        """An admin's removal of the row, uncommitted while the enable runs:
        the enable waits on the row and, once the removal commits, hands
        nothing over."""
        import threading
        n = _name()
        row_id = bearer_allowlist.add_allowed("vendor-h7", "mcp.vendor-h7.example", "system")
        reg(_data(n))
        with get_conn() as conn:
            conn.execute("DELETE FROM oauth_bearer_allowlist WHERE id = %s", (row_id,))
            enable = threading.Thread(target=manifest_hosts.after_enable, args=(n,))
            enable.start()
            enable.join(0.5)
            assert enable.is_alive()  # held on the row the removal holds
            conn.commit()
        enable.join(10)
        assert not enable.is_alive()
        assert _rows(n) == set() and bearer_allowlist.approved_hosts(n) == set()

    def test_a_handover_beside_an_mcp_that_leans_keeps_the_admin_row(self, reg):
        """The handover's row is locked, not removed, when another MCP still
        relies on it: the MCP owns its pair and the admin row stays."""
        n, b = _name(), _name()
        bearer_allowlist.add_allowed("vendor-h7", "mcp.vendor-h7.example", "system")
        reg(_data(b, hosts=("other.vendor-h7.example",)))
        reg(_data(n))
        manifest_hosts.after_enable(n)
        assert _rows(n) == {("vendor-h7", "mcp.vendor-h7.example")}
        assert _via(n) == {"mcp.vendor-h7.example": manifest_hosts.VIA_ADMIN_ROW}
        assert len(_admin_rows("mcp.vendor-h7.example")) == 1

    def test_a_container_hands_over_localhost(self, reg):
        n = _name()
        bearer_allowlist.add_allowed("vendor-h7", "localhost", "system")
        reg(_data(n, container=True, hosts=("localhost",)))
        manifest_hosts.after_enable(n)
        assert _rows(n) == {("vendor-h7", "localhost")} and _admin_rows("localhost") == []

    def test_no_admin_row_means_no_handover(self, reg):
        n = _name()
        m = reg(_data(n))
        manifest_hosts.after_enable(n)
        assert _rows(n) == set()
        assert manifest_hosts.pending(m)["hosts"] == ["mcp.vendor-h7.example"]

    def test_a_host_beside_the_server_still_waits(self, reg):
        n = _name()
        admin_id = bearer_allowlist.add_allowed("vendor-h7", "other.vendor-h7.example", "system")
        m = reg(_data(n, hosts=("mcp.vendor-h7.example", "other.vendor-h7.example")))
        manifest_hosts.after_enable(n)
        assert _rows(n) == set()
        assert manifest_hosts.pending(m)["hosts"] == ["mcp.vendor-h7.example", "other.vendor-h7.example"]
        assert bearer_allowlist.get_allowed(admin_id)["source"] == "admin"

    def test_an_mcp_installed_since_never_takes_the_handover(self, reg):
        """A ZIP installed on this version (its origin recorded) waits for the
        Allow even when an admin row allows its own host, toggled or not."""
        n = _name()
        bearer_allowlist.add_allowed("vendor-h7", "mcp.vendor-h7.example", "system")
        m = reg(_data(n))
        manifest_hosts.after_install(n, from_catalog=False)
        mcp_store.set_mcp_enabled(n, False)
        manifest_hosts.after_disable(n)
        mcp_store.set_mcp_enabled(n, True)
        manifest_hosts.after_enable(n)
        assert _rows(n) == set() and len(_admin_rows("mcp.vendor-h7.example")) == 1
        assert manifest_hosts.pending(m)["hosts"] == ["mcp.vendor-h7.example"]

    def test_a_catalog_update_records_the_origin(self, reg):
        n = _name()
        bearer_allowlist.add_allowed("vendor-h7", "mcp.vendor-h7.example", "system")
        reg(_data(n))
        manifest_hosts.after_install(n, from_catalog=True)
        assert bearer_allowlist.has_origin(n)
        assert _via(n) == {"mcp.vendor-h7.example": manifest_hosts.VIA_CATALOG}
        assert _admin_rows("mcp.vendor-h7.example") == []


class TestRoutes:
    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.mcp import bearer_allowlist as allowlist_api
        from auth.providers import UserContext, get_current_user
        admin = UserContext(sub="admin-sub", email="a@x", name="A", role="admin", agents=[], agent_roles={})

        async def _stub():
            return admin
        app = FastAPI()
        app.include_router(allowlist_api.router)
        app.dependency_overrides[get_current_user] = _stub
        return TestClient(app)

    def test_an_installed_mcps_row_is_not_removed_by_hand(self, reg, client):
        n = _name()
        reg(_data(n))
        manifest_hosts.after_install(n, from_catalog=True)
        row = next(r for r in bearer_allowlist.list_allowed() if r["source"] == n)
        resp = client.delete(f"/v1/admin/oauth-bearer-allowlist/{row['id']}")
        assert resp.status_code == 409 and "Turn the MCP off or remove it" in resp.json()["detail"]
        # once the MCP is gone from the registry its row can go
        mcp_registry._manifests.pop(n)
        ticks = len(reg.ticks)
        assert client.delete(f"/v1/admin/oauth-bearer-allowlist/{row['id']}").status_code == 200
        assert _rows(n) == set() and bearer_allowlist.approved_hosts(n) == set()
        assert len(reg.ticks) == ticks + 1  # the paired machines are told now

    def test_removing_the_admin_rows_tells_the_paired_machines_at_once(self, reg, client):
        """An admin's own row goes at the removal, so a machine holding a
        pushed token for its host is told now, as at a revoke, not at the
        next 60 s pass."""
        row_id = bearer_allowlist.add_allowed("vendor-h7-own", "own.vendor-h7.example")
        ticks = len(reg.ticks)
        resp = client.post("/v1/admin/oauth-bearer-allowlist/admin-rows/remove")
        assert resp.status_code == 200
        assert not any(r["id"] == row_id for r in resp.json()["entries"])
        assert not bearer_allowlist.is_host_allowed("vendor-h7-own", "own.vendor-h7.example")
        assert len(reg.ticks) == ticks + 1

    def test_the_removal_keeps_the_mcps_rows(self, reg, client):
        n = _name()
        bearer_allowlist.add_allowed("vendor-h7-hand", "hand.vendor-h7.example", "admin-sub")
        reg(_data(n))
        manifest_hosts.after_install(n, from_catalog=True)
        resp = client.post("/v1/admin/oauth-bearer-allowlist/admin-rows/remove")
        assert resp.status_code == 200
        assert [(r["host_pattern"], r["source"]) for r in resp.json()["entries"]] == [("mcp.vendor-h7.example", n)]

    def test_the_released_restore_path_refuses_and_changes_nothing(self, reg, client):
        """A released dashboard's one-click "Restore defaults" (no confirm)
        left open across the upgrade removes nothing and tells nobody."""
        n = _name()
        bearer_allowlist.add_allowed("vendor-h7-hand", "hand.vendor-h7.example", "admin-sub")
        reg(_data(n))
        manifest_hosts.after_install(n, from_catalog=True)
        before = bearer_allowlist.list_allowed()
        ticks = len(reg.ticks)
        resp = client.post("/v1/admin/oauth-bearer-allowlist/restore-defaults")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "No address is predefined any more. Reload the page to see the current list."
        assert bearer_allowlist.list_allowed() == before
        assert len(reg.ticks) == ticks

    def test_the_list_carries_the_source_and_what_waits(self, reg, client):
        n = _name()
        reg(_data(n))
        manifest_hosts.after_install(n, from_catalog=False)
        body = client.get("/v1/admin/oauth-bearer-allowlist").json()
        assert all("source" in r for r in body["entries"])
        waiting = next(w for w in body["pending"] if w["mcp_name"] == n)
        resp = client.post("/v1/admin/oauth-bearer-allowlist/allow", json={
            "mcp_name": n, "provider": waiting["provider"], "hosts": waiting["hosts"], "digest": waiting["digest"]})
        assert resp.status_code == 200 and _rows(n) == {("vendor-h7", "mcp.vendor-h7.example")}
        resp = client.post("/v1/admin/oauth-bearer-allowlist/allow", json={
            "mcp_name": n, "provider": waiting["provider"], "hosts": waiting["hosts"], "digest": waiting["digest"]})
        assert resp.status_code == 409


def test_the_migration_marks_every_row_admin_and_keys_by_source():
    """A table of the shape 1.9.0 made, in a scratch schema: the rows become
    an admin's, the two-column key goes, an MCP row for the same host fits
    beside the admin's, and a second run changes nothing."""
    schema = f"h7mig_{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path TO {schema}")
        try:
            conn.execute("""CREATE TABLE oauth_bearer_allowlist (
                id SERIAL PRIMARY KEY, provider_id TEXT NOT NULL, host_pattern TEXT NOT NULL,
                added_by TEXT NOT NULL DEFAULT 'system', added_at TEXT NOT NULL,
                UNIQUE(provider_id, host_pattern))""")
            conn.execute("INSERT INTO oauth_bearer_allowlist (provider_id, host_pattern, added_by, added_at) "
                         "VALUES ('slack', 'mcp.slack.com', 'system', 'x'), ('p', 'h.example', 'sub-1', 'x')")
            bearer_allowlist.migrate(conn)
            rows = conn.execute("SELECT source FROM oauth_bearer_allowlist").fetchall()
            assert [r["source"] for r in rows] == ["admin", "admin"]
            conn.execute("INSERT INTO oauth_bearer_allowlist (provider_id, host_pattern, added_at, source) "
                         "VALUES ('p', 'h.example', 'x', 'some-mcp')")
            with pytest.raises(Exception):  # noqa: B017 — the three-column key refuses a duplicate
                conn.execute("SAVEPOINT dup")
                conn.execute("INSERT INTO oauth_bearer_allowlist (provider_id, host_pattern, added_at, source) "
                             "VALUES ('p', 'h.example', 'x', 'some-mcp')")
            conn.execute("ROLLBACK TO SAVEPOINT dup")
            bearer_allowlist.migrate(conn)
            keys = conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = 'oauth_bearer_allowlist'",
                (schema,)).fetchall()
            assert sorted(k["indexname"] for k in keys) == ["oauth_bearer_allowlist_pkey",
                                                            "oauth_bearer_allowlist_source_key"]
        finally:
            conn.rollback()
            conn.execute("SET search_path TO DEFAULT")
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.commit()
