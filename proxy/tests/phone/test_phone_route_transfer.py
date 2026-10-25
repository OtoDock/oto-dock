"""The route's transfer targets and language switch: the two columns on a
fresh and an upgraded install, the store round trip, the API validator, and
the daemon push."""

from __future__ import annotations

from services.phone.phone_config import assemble_phone_config
from storage import schema as pg_schema
from storage.pg import get_conn
from storage.phone import phone_route_store
from tests.phone.test_phone_route_identity import (  # noqa: F401 — the client fixture
    _make_agent,
    _make_route,
    client,
)

TARGETS = [{"label": "Reception", "number": "200"}, {"label": "Maria", "number": "+306912345678"}]


def test_fresh_install_defaults(temp_db):
    agent = _make_agent()
    route = _make_route(agent)
    assert route["transfer_targets"] == [] and route["language_detect"] is False
    assert route["voice_id"] == ""


def test_an_upgraded_install_gains_the_columns_with_the_same_defaults(temp_db):
    agent = _make_agent()
    route = _make_route(agent)
    with get_conn() as conn:
        conn.execute("ALTER TABLE phone_routes DROP COLUMN transfer_targets, "
                     "DROP COLUMN language_detect, DROP COLUMN voice_id")
        conn.commit()
    with get_conn() as conn:
        pg_schema.run_migrations(conn)
        conn.commit()
    # The re-added columns moved to the end of the row: a pooled session's
    # prepared ``SELECT *`` plan (an earlier test's read of the routes in
    # this worker, or the next test's) would answer "cached plan must not
    # change result type". Fresh connections from here on, the read below
    # included.
    from storage import pg
    pg.close_pool(timeout=2.0)
    row = phone_route_store.get_route(route["id"])
    assert row["transfer_targets"] == [] and row["language_detect"] is False
    assert row["voice_id"] == ""
    pg.close_pool(timeout=2.0)


def test_store_round_trip_and_push(temp_db):
    agent = _make_agent()
    route = _make_route(agent, transfer_targets=TARGETS, language_detect=True)
    assert route["transfer_targets"] == TARGETS and route["language_detect"] is True
    updated = phone_route_store.update_route(route["id"], {"transfer_targets": TARGETS[:1]})
    assert updated["transfer_targets"] == TARGETS[:1]
    pushed = {r["id"]: r for r in assemble_phone_config()["routes"]}
    assert pushed[route["id"]]["transfer_targets"] == TARGETS[:1]
    assert pushed[route["id"]]["language_detect"] is True


def test_the_route_voice_rides_the_row_and_the_push(client):  # noqa: F811
    """A voice for the route alone: stored trimmed, pushed to the daemon,
    cleared with an empty string; whitespace inside or an oversized id is
    refused and the row untouched."""
    route = _make_route(_make_agent())
    r = client.put(f"/v1/admin/phone/routes/{route['id']}", json={"voice_id": " a7beff01-8f8b "})
    assert r.status_code == 200 and r.json()["voice_id"] == "a7beff01-8f8b"
    pushed = {x["id"]: x for x in assemble_phone_config()["routes"]}
    assert pushed[route["id"]]["voice_id"] == "a7beff01-8f8b"
    for bad in ("two words", "x" * 121):
        assert client.put(f"/v1/admin/phone/routes/{route['id']}", json={"voice_id": bad}).status_code == 400
    assert phone_route_store.get_route(route["id"])["voice_id"] == "a7beff01-8f8b"
    r = client.put(f"/v1/admin/phone/routes/{route['id']}", json={"voice_id": ""})
    assert r.status_code == 200 and r.json()["voice_id"] == ""


class TestValidator:
    def _put(self, client, route_id, body):  # noqa: F811
        return client.put(f"/v1/admin/phone/routes/{route_id}", json=body)

    def test_a_good_list_is_normalised_and_saved(self, client):  # noqa: F811
        route = _make_route(_make_agent())
        r = self._put(client, route["id"], {"transfer_targets": [
            {"label": "  Reception ", "number": " 200 ", "extra": "dropped"}]})
        assert r.status_code == 200
        assert r.json()["transfer_targets"] == [{"label": "Reception", "number": "200"}]
        r = self._put(client, route["id"], {"language_detect": True})
        assert r.status_code == 200 and r.json()["language_detect"] is True

    def test_bad_lists_are_refused(self, client):  # noqa: F811
        route = _make_route(_make_agent())
        # The model's own shape check answers a non-list and a list of
        # non-objects (422); the rules below are the validator's (400).
        for shape in ({"transfer_targets": "Reception"}, {"transfer_targets": ["200"]}):
            assert self._put(client, route["id"], shape).status_code == 422
        cases = [
            ({"transfer_targets": [{"label": "", "number": "200"}]}, "needs a label"),
            ({"transfer_targets": [{"label": "x" * 41, "number": "200"}]}, "needs a label"),
            ({"transfer_targets": [{"label": "Reception", "number": "ext 200"}]}, "needs a number"),
            ({"transfer_targets": [{"label": "A", "number": "200"}, {"label": "a", "number": "201"}]}, "used twice"),
            ({"transfer_targets": [{"label": f"t{i}", "number": "200"} for i in range(21)]}, "at most 20"),
        ]
        for body, detail in cases:
            r = self._put(client, route["id"], body)
            assert r.status_code == 400 and detail in r.json()["detail"], (body, r.json())
        assert phone_route_store.get_route(route["id"])["transfer_targets"] == []

    def test_non_ascii_digits_are_refused(self, client):  # noqa: F811
        # Python's \d takes every Unicode digit; the AMI Exten and the TwiML
        # must get ASCII ones.
        route = _make_route(_make_agent())
        arabic_digits = "+30\u0661\u0662\u0663\u0664"
        r = self._put(client, route["id"], {"transfer_targets": [{"label": "A", "number": arabic_digits}]})
        assert r.status_code == 400 and "number" in r.json()["detail"]

    def test_a_put_without_the_field_leaves_it(self, client):  # noqa: F811
        route = _make_route(_make_agent(), transfer_targets=TARGETS)
        assert self._put(client, route["id"], {"name": "renamed"}).status_code == 200
        assert phone_route_store.get_route(route["id"])["transfer_targets"] == TARGETS
