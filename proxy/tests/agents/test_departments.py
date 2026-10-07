"""Departments backend — CRUD/permissions, edge compiler, assignment gate.

Covers the agents-map backend:
- Department CRUD with the FIRST created_by-ownership gate on a platform
  object (admins all / creators own-only / members none).
- Membership-scoped visibility (non-member departments entirely invisible;
  in-department non-accessible agents flagged, never omitted).
- The per-agent assignment field gate on PATCH /v1/agents/{name}
  (cookie admin/creator only — a manager reaches the endpoint and is
  rejected only on this field).
- The edge compiler: same-level mutual + one-level-down (adjacent),
  subtree reach, mode off, retraction on leave/delete — and the
  source-scoping regression: a manual checkbox save must NEVER wipe
  department-compiled edges, and vice versa.
- The delegation mode: 'down' compiles a strict hierarchy (no same-level,
  no upward edges), 'down_across' adds the same level, 'both' the symmetric
  wiring, 'off' nothing; the default on create, the value gate, the switch
  with manual edges coexisting, the fold of the two older columns on a
  pre-existing table, the prompt line.

Run: cd proxy && venv/bin/pytest tests/agents/test_departments.py -v
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# Platform roles per conftest._seed_users
ADMIN = "user-admin"      # admin
CREATOR = "user-manager"  # creator
MEMBER = "user-viewer"    # member


@pytest.fixture
def client(temp_db):
    from app import app
    return TestClient(app)


def _cookie(sub: str, email: str, role: str) -> dict[str, str]:
    from auth.providers import create_session_jwt
    return {"Cookie": f"session={create_session_jwt(sub, email, 'T User', role)}"}


ADMIN_H = lambda: _cookie(ADMIN, "admin@test.com", "admin")  # noqa: E731
CREATOR_H = lambda: _cookie(CREATOR, "manager@test.com", "creator")  # noqa: E731
MEMBER_H = lambda: _cookie(MEMBER, "viewer@test.com", "member")  # noqa: E731


def _mk_agent(slug: str) -> str:
    from storage.agents import agent_store
    agent_store.create_agent(slug, slug.title(), created_by=ADMIN)
    return slug


def _assign_user(sub: str, agents: dict[str, str]) -> None:
    """agents = {slug: agent_role}"""
    from storage import database as task_store
    task_store.set_user_agents(sub, list(agents), ADMIN, agent_roles=agents)


def _mk_dept(client, name="Engineering", levels=None, **extra):
    # 'both' pins the symmetric shapes the older tests assert; the API's
    # own default ('down') has its own test in TestDelegationDirection.
    r = client.post(
        "/v1/departments",
        json={"name": name, "levels": levels or ["Head", "Senior", "Junior"],
              "mode": "both", **extra},
        headers=ADMIN_H(),
    )
    assert r.status_code == 200, r.text
    return r.json()


def _assign_dept(slug: str, dept: dict, level_name: str) -> None:
    """Assign directly at the store + recompile (bypasses the HTTP gate —
    gate behavior has its own tests below)."""
    from services.departments import edge_compiler
    from storage.agents import agent_store
    level = next(lv for lv in dept["levels"] if lv["name"] == level_name)
    agent_store.update_agent(
        slug, department_id=dept["id"], department_level_id=level["id"],
    )
    edge_compiler.recompile()


def _edges() -> set[tuple[str, str, str]]:
    from storage.agents import agent_store
    return {
        (e["from"], e["to"], e["source"])
        for e in agent_store.get_all_delegation_edges()
    }


class TestDepartmentCrud:
    def test_admin_creates_with_levels(self, client):
        dept = _mk_dept(client)
        assert dept["name"] == "Engineering"
        assert [lv["name"] for lv in dept["levels"]] == ["Head", "Senior", "Junior"]
        assert [lv["rank"] for lv in dept["levels"]] == [0, 1, 2]
        assert dept["reach"] == "adjacent"
        assert dept["mode"] == "both"
        assert dept["can_edit"] is True

    def test_member_cannot_create(self, client):
        r = client.post(
            "/v1/departments", json={"name": "Nope"}, headers=MEMBER_H()
        )
        assert r.status_code == 403

    def test_creator_edits_own_only(self, client):
        own = client.post(
            "/v1/departments", json={"name": "Sales"}, headers=CREATOR_H()
        ).json()
        foreign = _mk_dept(client, name="Ops")

        r = client.patch(
            f"/v1/departments/{own['id']}",
            json={"name": "Sales EU"}, headers=CREATOR_H(),
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Sales EU"

        r = client.patch(
            f"/v1/departments/{foreign['id']}",
            json={"name": "Hijacked"}, headers=CREATOR_H(),
        )
        assert r.status_code == 403

        assert client.delete(
            f"/v1/departments/{foreign['id']}", headers=CREATOR_H()
        ).status_code == 403
        assert client.delete(
            f"/v1/departments/{own['id']}", headers=CREATOR_H()
        ).status_code == 200

    def test_admin_edits_creator_owned(self, client):
        own = client.post(
            "/v1/departments", json={"name": "Sales"}, headers=CREATOR_H()
        ).json()
        r = client.patch(
            f"/v1/departments/{own['id']}",
            json={"mode": "off"}, headers=ADMIN_H(),
        )
        assert r.status_code == 200
        assert r.json()["mode"] == "off"

    def test_validation(self, client):
        assert client.post(
            "/v1/departments", json={"name": "  "}, headers=ADMIN_H()
        ).status_code == 400
        assert client.post(
            "/v1/departments",
            json={"name": "X", "levels": [f"L{i}" for i in range(9)]},
            headers=ADMIN_H(),
        ).status_code == 400
        assert client.post(
            "/v1/departments",
            json={"name": "X", "reach": "everything"},
            headers=ADMIN_H(),
        ).status_code == 400

    def test_levels_replace_keeps_ids_and_unassigns_removed(self, client):
        dept = _mk_dept(client)
        _mk_agent("lvl-keeper")
        _mk_agent("lvl-orphan")
        _assign_dept("lvl-keeper", dept, "Head")
        _assign_dept("lvl-orphan", dept, "Junior")
        head = next(lv for lv in dept["levels"] if lv["name"] == "Head")

        r = client.put(
            f"/v1/departments/{dept['id']}/levels",
            json={"levels": [
                {"id": head["id"], "name": "Chief"},
                {"id": "", "name": "Crew"},
            ]},
            headers=ADMIN_H(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert [lv["name"] for lv in body["levels"]] == ["Chief", "Crew"]
        # kept id preserved the keeper's assignment; removed level dropped
        # the orphan out of the department entirely
        assert body["unassigned_agents"] == ["lvl-orphan"]
        from storage.agents import agent_store
        keeper = agent_store.get_agent("lvl-keeper")
        assert keeper["department_level_id"] == head["id"]
        orphan = agent_store.get_agent("lvl-orphan")
        assert orphan["department_id"] == ""
        assert orphan["department_level_id"] == ""


class TestVisibility:
    def test_member_sees_only_member_departments(self, client):
        dept_in = _mk_dept(client, name="Mine")
        dept_out = _mk_dept(client, name="NotMine")
        _mk_agent("vis-a")
        _mk_agent("vis-b")
        _mk_agent("vis-c")
        _assign_dept("vis-a", dept_in, "Head")
        _assign_dept("vis-b", dept_in, "Senior")
        _assign_dept("vis-c", dept_out, "Head")
        _assign_user(MEMBER, {"vis-a": "viewer"})

        r = client.get("/v1/departments", headers=MEMBER_H())
        assert r.status_code == 200
        depts = {d["name"]: d for d in r.json()["departments"]}
        assert "Mine" in depts and "NotMine" not in depts
        members = {m["name"]: m for m in depts["Mine"]["members"]}
        assert members["vis-a"]["accessible"] is True
        assert members["vis-b"]["accessible"] is False
        assert depts["Mine"]["can_edit"] is False

    def test_admin_sees_all(self, client):
        _mk_dept(client, name="A")
        _mk_dept(client, name="B")
        r = client.get("/v1/departments", headers=ADMIN_H())
        assert {d["name"] for d in r.json()["departments"]} == {"A", "B"}

    def test_creator_sees_own_empty_department(self, client):
        client.post("/v1/departments", json={"name": "MineEmpty"},
                    headers=CREATOR_H())
        names = {d["name"] for d in client.get(
            "/v1/departments", headers=CREATOR_H()
        ).json()["departments"]}
        assert "MineEmpty" in names
        # a member sees no departments at all
        assert client.get(
            "/v1/departments", headers=MEMBER_H()
        ).json()["departments"] == []


class TestAssignmentFieldGate:
    def test_manager_member_rejected_on_field(self, client):
        dept = _mk_dept(client)
        _mk_agent("gate-a")
        _assign_user(MEMBER, {"gate-a": "manager"})
        level_id = dept["levels"][0]["id"]
        # same principal may edit other fields through the endpoint…
        assert client.patch(
            "/v1/agents/gate-a", json={"description": "ok"},
            headers=MEMBER_H(),
        ).status_code == 200
        # …but the department field narrows within it
        r = client.patch(
            "/v1/agents/gate-a",
            json={"department_id": dept["id"], "department_level_id": level_id},
            headers=MEMBER_H(),
        )
        assert r.status_code == 403

    def test_creator_manager_assigns_and_clears(self, client):
        dept = _mk_dept(client)
        _mk_agent("gate-b")
        _assign_user(CREATOR, {"gate-b": "manager"})
        level_id = dept["levels"][0]["id"]
        r = client.patch(
            "/v1/agents/gate-b",
            json={"department_id": dept["id"], "department_level_id": level_id},
            headers=CREATOR_H(),
        )
        assert r.status_code == 200, r.text
        from storage.agents import agent_store
        assert agent_store.get_agent("gate-b")["department_id"] == dept["id"]
        r = client.patch(
            "/v1/agents/gate-b",
            json={"department_id": "", "department_level_id": ""},
            headers=CREATOR_H(),
        )
        assert r.status_code == 200
        assert agent_store.get_agent("gate-b")["department_id"] == ""

    def test_admin_needs_no_manager_role(self, client):
        dept = _mk_dept(client)
        _mk_agent("gate-c")
        r = client.patch(
            "/v1/agents/gate-c",
            json={"department_id": dept["id"],
                  "department_level_id": dept["levels"][1]["id"]},
            headers=ADMIN_H(),
        )
        assert r.status_code == 200

    def test_validation(self, client):
        dept = _mk_dept(client)
        other = _mk_dept(client, name="Other")
        _mk_agent("gate-d")
        # dept without level
        assert client.patch(
            "/v1/agents/gate-d", json={"department_id": dept["id"]},
            headers=ADMIN_H(),
        ).status_code == 400
        # unknown department
        assert client.patch(
            "/v1/agents/gate-d",
            json={"department_id": "dept-nope",
                  "department_level_id": dept["levels"][0]["id"]},
            headers=ADMIN_H(),
        ).status_code == 400
        # level from another department
        assert client.patch(
            "/v1/agents/gate-d",
            json={"department_id": dept["id"],
                  "department_level_id": other["levels"][0]["id"]},
            headers=ADMIN_H(),
        ).status_code == 400


class TestEdgeCompiler:
    def test_same_level_mutual_and_adjacent_down(self, client):
        dept = _mk_dept(client)
        for s in ("head-1", "sen-1", "sen-2", "jun-1"):
            _mk_agent(s)
        _assign_dept("head-1", dept, "Head")
        _assign_dept("sen-1", dept, "Senior")
        _assign_dept("sen-2", dept, "Senior")
        _assign_dept("jun-1", dept, "Junior")

        e = _edges()
        # same level: mutual
        assert ("sen-1", "sen-2", "department") in e
        assert ("sen-2", "sen-1", "department") in e
        # one level down
        assert ("head-1", "sen-1", "department") in e
        assert ("head-1", "sen-2", "department") in e
        assert ("sen-1", "jun-1", "department") in e
        # SYMMETRIC (2026-09-02): one level up too
        assert ("jun-1", "sen-1", "department") in e
        assert ("sen-1", "head-1", "department") in e
        # adjacent stays non-transitive: head ↮ junior
        assert ("head-1", "jun-1", "department") not in e
        assert ("jun-1", "head-1", "department") not in e

    def test_subtree_reach(self, client):
        dept = _mk_dept(client, name="Flat", reach="subtree")
        for s in ("st-head", "st-sen", "st-jun"):
            _mk_agent(s)
        _assign_dept("st-head", dept, "Head")
        _assign_dept("st-sen", dept, "Senior")
        _assign_dept("st-jun", dept, "Junior")
        e = _edges()
        # SYMMETRIC (2026-09-02): subtree = full department mesh
        dept_edges = {(a, b) for a, b, src in e if src == "department"}
        members = ("st-head", "st-sen", "st-jun")
        expected = {(a, b) for a in members for b in members if a != b}
        assert expected <= dept_edges

    def test_mode_off_compiles_nothing(self, client):
        dept = _mk_dept(client, name="Silent", mode="off")
        _mk_agent("sil-a")
        _mk_agent("sil-b")
        _assign_dept("sil-a", dept, "Head")
        _assign_dept("sil-b", dept, "Head")
        assert not {t for t in _edges() if t[2] == "department"}

    def test_leaving_retracts_only_compiled(self, client):
        from services.departments import edge_compiler
        from storage.agents import agent_store
        dept = _mk_dept(client)
        _mk_agent("lv-a")
        _mk_agent("lv-b")
        # manual edge FIRST (user explicitly checked it), then the
        # department also wants it — the pre-existing manual row wins the
        # compiler's ON CONFLICT DO NOTHING, so it must survive leaving.
        agent_store.set_delegation_targets("lv-a", ["lv-b"])
        _assign_dept("lv-a", dept, "Head")
        _assign_dept("lv-b", dept, "Head")
        assert ("lv-a", "lv-b", "manual") in _edges()
        assert ("lv-b", "lv-a", "department") in _edges()

        agent_store.update_agent(
            "lv-b", department_id="", department_level_id="",
        )
        edge_compiler.recompile()
        e = _edges()
        assert ("lv-b", "lv-a", "department") not in e
        assert ("lv-a", "lv-b", "manual") in e

    def test_delete_department_retracts(self, client):
        dept = _mk_dept(client, name="Doomed")
        _mk_agent("dd-a")
        _mk_agent("dd-b")
        _assign_dept("dd-a", dept, "Head")
        _assign_dept("dd-b", dept, "Senior")
        assert ("dd-a", "dd-b", "department") in _edges()
        r = client.delete(f"/v1/departments/{dept['id']}", headers=ADMIN_H())
        assert r.status_code == 200
        assert sorted(r.json()["unassigned_agents"]) == ["dd-a", "dd-b"]
        assert not {t for t in _edges() if t[2] == "department"}
        from storage.agents import agent_store
        assert agent_store.get_agent("dd-a")["department_id"] == ""


class TestManualCompiledCoexistence:
    """The audit's load-bearing finding: the config UI autosaves the FULL
    manual set on every checkbox toggle — that save must never wipe
    compiled rows, and un-checking a department-wanted edge must let the
    compiler re-assert it."""

    def test_manual_put_preserves_compiled(self, client):
        dept = _mk_dept(client)
        for s in ("mx-a", "mx-b", "mx-solo"):
            _mk_agent(s)
        _assign_dept("mx-a", dept, "Head")
        _assign_dept("mx-b", dept, "Head")
        # manual toggle via the endpoint (what the UI autosave does)
        r = client.put(
            "/v1/agents/mx-a/delegation-targets",
            json={"targets": ["mx-solo"]},
            headers=ADMIN_H(),
        )
        assert r.status_code == 200
        e = _edges()
        assert ("mx-a", "mx-solo", "manual") in e
        assert ("mx-a", "mx-b", "department") in e  # survived the save
        assert ("mx-b", "mx-a", "department") in e

    def test_unchecking_dept_wanted_edge_reasserts_compiled(self, client):
        from storage.agents import agent_store
        dept = _mk_dept(client)
        _mk_agent("re-a")
        _mk_agent("re-b")
        # manual edge FIRST, then the department also wants it
        agent_store.set_delegation_targets("re-a", ["re-b"])
        _assign_dept("re-a", dept, "Head")
        _assign_dept("re-b", dept, "Head")
        assert ("re-a", "re-b", "manual") in _edges()  # manual wins conflict
        # user un-checks the manual edge → compiler re-asserts it
        r = client.put(
            "/v1/agents/re-a/delegation-targets",
            json={"targets": []},
            headers=ADMIN_H(),
        )
        assert r.status_code == 200
        assert ("re-a", "re-b", "department") in _edges()

    def test_get_reports_manual_and_compiled_separately(self, client):
        dept = _mk_dept(client)
        for s in ("gs-a", "gs-b", "gs-solo"):
            _mk_agent(s)
        _assign_dept("gs-a", dept, "Head")
        _assign_dept("gs-b", dept, "Head")
        from storage.agents import agent_store
        agent_store.set_delegation_targets("gs-a", ["gs-solo"])
        r = client.get(
            "/v1/agents/gs-a/delegation-targets", headers=ADMIN_H()
        )
        assert r.status_code == 200
        body = r.json()
        assert body["targets"] == ["gs-solo"]
        assert [c["target"] for c in body["compiled"]] == ["gs-b"]
        assert body["compiled"][0]["department_name"] == "Engineering"
        # effective set (what sessions/spawn-authz see) is the union
        assert agent_store.get_delegation_targets("gs-a") == ["gs-b", "gs-solo"]


class TestDelegationMode:
    """mode='down' is a strict hierarchy, 'down_across' adds the same level,
    'both' is the symmetric wiring every department created before the
    column existed keeps, 'off' compiles nothing; reach applies to whatever
    the mode wires."""

    def test_new_department_defaults_to_down(self, client):
        r = client.post(
            "/v1/departments", json={"name": "Site"}, headers=ADMIN_H(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "down"
        both = _mk_dept(client, name="Both", mode="both")
        assert both["mode"] == "both"
        listed = {d["name"]: d for d in client.get(
            "/v1/departments", headers=ADMIN_H()
        ).json()["departments"]}
        assert listed["Site"]["mode"] == "down"
        assert listed["Both"]["mode"] == "both"

    def test_other_values_are_refused(self, client):
        assert client.post(
            "/v1/departments",
            json={"name": "X", "mode": "up"},
            headers=ADMIN_H(),
        ).status_code == 400
        dept = _mk_dept(client)
        for word in ("sideways", "", "DOWN"):
            r = client.patch(
                f"/v1/departments/{dept['id']}",
                json={"mode": word}, headers=ADMIN_H(),
            )
            assert r.status_code == 400, word
        assert client.get(
            "/v1/departments", headers=ADMIN_H()
        ).json()["departments"][0]["mode"] == "both"

    def test_down_adjacent_is_a_strict_hierarchy(self, client):
        dept = _mk_dept(client, name="Site", mode="down")
        agents = {
            "Head": ["dh-head"],
            "Senior": ["ds-1", "ds-2"],
            "Junior": ["dj-1", "dj-2", "dj-3"],
        }
        for level, slugs in agents.items():
            for s in slugs:
                _mk_agent(s)
                _assign_dept(s, dept, level)
        compiled = {(a, b) for a, b, src in _edges() if src == "department"}
        expected = {("dh-head", s) for s in agents["Senior"]} | {
            (s, j) for s in agents["Senior"] for j in agents["Junior"]
        }
        # exactly the downward adjacent edges: no same-level, no upward, no
        # head-to-junior
        assert compiled == expected

    def test_down_subtree_reaches_every_level_below(self, client):
        dept = _mk_dept(client, name="Site", reach="subtree", mode="down")
        agents = {
            "Head": ["sh-head"],
            "Senior": ["ss-1", "ss-2"],
            "Junior": ["sj-1", "sj-2", "sj-3"],
        }
        for level, slugs in agents.items():
            for s in slugs:
                _mk_agent(s)
                _assign_dept(s, dept, level)
        compiled = {(a, b) for a, b, src in _edges() if src == "department"}
        below_head = agents["Senior"] + agents["Junior"]
        expected = {("sh-head", s) for s in below_head} | {
            (s, j) for s in agents["Senior"] for j in agents["Junior"]
        }
        assert compiled == expected

    def test_down_across_adjacent_adds_the_peers_only(self, client):
        dept = _mk_dept(client, name="Site", mode="down_across")
        agents = {
            "Head": ["xh-head"],
            "Senior": ["xs-1", "xs-2"],
            "Junior": ["xj-1", "xj-2"],
        }
        for level, slugs in agents.items():
            for s in slugs:
                _mk_agent(s)
                _assign_dept(s, dept, level)
        compiled = {(a, b) for a, b, src in _edges() if src == "department"}
        downward = {("xh-head", s) for s in agents["Senior"]} | {
            (s, j) for s in agents["Senior"] for j in agents["Junior"]
        }
        peers = {
            ("xs-1", "xs-2"), ("xs-2", "xs-1"),
            ("xj-1", "xj-2"), ("xj-2", "xj-1"),
        }
        # the strict hierarchy plus each level's members both ways: still
        # nothing upward, still no head-to-junior
        assert compiled == downward | peers

    def test_down_across_subtree_reaches_every_level_below(self, client):
        dept = _mk_dept(
            client, name="Site", mode="down_across", reach="subtree",
        )
        agents = {
            "Head": ["yh-head"],
            "Senior": ["ys-1", "ys-2"],
            "Junior": ["yj-1"],
        }
        for level, slugs in agents.items():
            for s in slugs:
                _mk_agent(s)
                _assign_dept(s, dept, level)
        compiled = {(a, b) for a, b, src in _edges() if src == "department"}
        expected = (
            {("yh-head", s) for s in agents["Senior"] + agents["Junior"]}
            | {(s, "yj-1") for s in agents["Senior"]}
            | {("ys-1", "ys-2"), ("ys-2", "ys-1")}
        )
        assert compiled == expected

    def test_both_subtree_reaches_two_levels_up_and_down(self, client):
        # reach is independent of the mode: the whole department both ways
        # lets the bottom level reach the head and back
        dept = _mk_dept(client, name="Site", mode="both", reach="subtree")
        for s in ("bs-head", "bs-sen", "bs-jun"):
            _mk_agent(s)
        _assign_dept("bs-head", dept, "Head")
        _assign_dept("bs-sen", dept, "Senior")
        _assign_dept("bs-jun", dept, "Junior")
        compiled = {(a, b) for a, b, src in _edges() if src == "department"}
        members = ("bs-head", "bs-sen", "bs-jun")
        assert compiled == {(a, b) for a in members for b in members if a != b}

    def test_single_level_under_every_mode(self, client):
        dept = _mk_dept(client, name="Flat", levels=["Crew"], mode="down")
        members = ("one-a", "one-b", "one-c")
        for s in members:
            _mk_agent(s)
            _assign_dept(s, dept, "Crew")
        mutual = {(a, b) for a in members for b in members if a != b}
        assert not {t for t in _edges() if t[2] == "department"}
        for mode, expected in (
            ("both", mutual), ("off", set()), ("down_across", mutual),
            ("down", set()),
        ):
            r = client.patch(
                f"/v1/departments/{dept['id']}",
                json={"mode": mode}, headers=ADMIN_H(),
            )
            assert r.status_code == 200, r.text
            assert r.json()["mode"] == mode
            compiled = {
                (a, b) for a, b, src in _edges() if src == "department"
            }
            assert compiled == expected, mode

    def test_mode_switch_keeps_manual_edges(self, client):
        from storage.agents import agent_store
        dept = _mk_dept(client, name="Site", levels=["Head", "Team"])
        for s in ("sw-head", "sw-a", "sw-b"):
            _mk_agent(s)
        # the team member asked for the upward edge by hand FIRST, so the
        # compiler's row for it never existed (manual wins the conflict)
        agent_store.set_delegation_targets("sw-a", ["sw-head"])
        _assign_dept("sw-head", dept, "Head")
        _assign_dept("sw-a", dept, "Team")
        _assign_dept("sw-b", dept, "Team")
        e = _edges()
        assert ("sw-a", "sw-head", "manual") in e
        assert ("sw-b", "sw-head", "department") in e
        assert ("sw-a", "sw-b", "department") in e

        # the PATCH itself recompiles: peer and upward compiled rows go, the
        # manual row stays, the head keeps its team
        r = client.patch(
            f"/v1/departments/{dept['id']}",
            json={"mode": "down"}, headers=ADMIN_H(),
        )
        assert r.status_code == 200, r.text
        e = _edges()
        assert ("sw-a", "sw-head", "manual") in e
        assert ("sw-b", "sw-head", "department") not in e
        assert ("sw-a", "sw-b", "department") not in e
        assert ("sw-b", "sw-a", "department") not in e
        assert ("sw-head", "sw-a", "department") in e
        assert ("sw-head", "sw-b", "department") in e

        # and back: the symmetric rows return, the manual row still owns its
        # pair
        client.patch(
            f"/v1/departments/{dept['id']}",
            json={"mode": "both"}, headers=ADMIN_H(),
        )
        e = _edges()
        assert ("sw-a", "sw-head", "manual") in e
        assert ("sw-b", "sw-head", "department") in e
        assert ("sw-a", "sw-b", "department") in e

    def test_agent_moved_between_levels(self, client):
        dept = _mk_dept(client, name="Site", levels=["Head", "Team"],
                        mode="down")
        for s in ("mv-head", "mv-a"):
            _mk_agent(s)
        _assign_dept("mv-head", dept, "Head")
        _assign_dept("mv-a", dept, "Team")
        assert ("mv-head", "mv-a", "department") in _edges()
        # swap the two: the demoted head loses its edge, the promoted agent
        # gains the edge to it
        _assign_dept("mv-head", dept, "Team")
        _assign_dept("mv-a", dept, "Head")
        compiled = {(a, b) for a, b, src in _edges() if src == "department"}
        assert compiled == {("mv-a", "mv-head")}

    def test_edge_feed_is_one_way(self, client):
        dept = _mk_dept(client, name="Site", levels=["Head", "Team"],
                        mode="down")
        for s in ("fd-head", "fd-a"):
            _mk_agent(s)
        _assign_dept("fd-head", dept, "Head")
        _assign_dept("fd-a", dept, "Team")
        r = client.get("/v1/agents/delegation-edges", headers=ADMIN_H())
        edges = {(e["from"], e["to"]) for e in r.json()["edges"]}
        assert ("fd-head", "fd-a") in edges
        assert ("fd-a", "fd-head") not in edges
        # the config UI's locked rows follow: the head has one, the team none
        head = client.get(
            "/v1/agents/fd-head/delegation-targets", headers=ADMIN_H()
        ).json()
        assert [c["target"] for c in head["compiled"]] == ["fd-a"]
        team = client.get(
            "/v1/agents/fd-a/delegation-targets", headers=ADMIN_H()
        ).json()
        assert team["compiled"] == []


class TestModeMigration:
    """The fold of the 1.6.x auto_delegation toggle and the never-released
    direction column into mode, on both shapes a live install can have."""

    @staticmethod
    def _legacy_table(with_direction: bool) -> None:
        from storage.pg import get_conn
        # Simulate a pre-upgrade install: the table exists in the older
        # shape (the column CHECK goes with the dropped column).
        with get_conn() as conn:
            conn.execute("ALTER TABLE departments DROP COLUMN mode")
            conn.execute(
                "ALTER TABLE departments ADD COLUMN auto_delegation "
                "BOOLEAN NOT NULL DEFAULT TRUE"
            )
            if with_direction:
                conn.execute(
                    "ALTER TABLE departments ADD COLUMN direction "
                    "TEXT NOT NULL DEFAULT 'both'"
                )
            conn.commit()

    @staticmethod
    def _migrate_twice() -> None:
        from storage import pg
        from storage import schema as pg_schema
        from storage.pg import get_conn
        # Fresh pool: a pooled connection may hold a prepared ``SELECT *``
        # plan whose result type the DDL changes.
        pg.close_pool()
        for _ in range(2):  # idempotent: safe on every startup
            with get_conn() as conn:
                pg_schema.run_migrations(conn)
                conn.commit()
        pg.close_pool()

    @staticmethod
    def _columns() -> set[str]:
        from storage.pg import get_conn
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'departments'"
            ).fetchall()
        return {r["column_name"] for r in rows}

    def test_a_1_6_install_folds_the_toggle(self, temp_db):
        from storage import schema as pg_schema
        from storage.agents import db_departments
        from storage.pg import get_conn
        self._legacy_table(with_direction=False)
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO departments "
                "(id, name, auto_delegation, created_at, updated_at) VALUES "
                "('dept-on', 'On', TRUE, 'then', 'then'), "
                "('dept-off', 'Off', FALSE, 'then', 'then')"
            )
            conn.commit()
        self._migrate_twice()
        # the symmetric wiring it had, or off
        assert db_departments.get_department("dept-on")["mode"] == "both"
        assert db_departments.get_department("dept-off")["mode"] == "off"
        cols = self._columns()
        assert "mode" in cols
        assert not cols & {"auto_delegation", "direction"}
        # the store's default for a NEW department is unchanged by the
        # column default
        fresh = db_departments.create_department("Fresh", ADMIN)
        assert fresh["mode"] == "down"
        with get_conn() as conn:
            assert pg_schema.check_schema_drift(conn) == []

    def test_an_install_with_the_direction_column_folds_both(self, temp_db):
        from storage import schema as pg_schema
        from storage.agents import db_departments
        from storage.pg import get_conn
        self._legacy_table(with_direction=True)
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO departments "
                "(id, name, auto_delegation, direction, created_at, updated_at) "
                "VALUES "
                "('dept-both', 'Both', TRUE, 'both', 'then', 'then'), "
                "('dept-down', 'Down', TRUE, 'down', 'then', 'then'), "
                "('dept-silent', 'Silent', FALSE, 'down', 'then', 'then')"
            )
            conn.commit()
        self._migrate_twice()
        assert db_departments.get_department("dept-both")["mode"] == "both"
        assert db_departments.get_department("dept-down")["mode"] == "down"
        # off wins over the direction it had
        assert db_departments.get_department("dept-silent")["mode"] == "off"
        cols = self._columns()
        assert "mode" in cols
        assert not cols & {"auto_delegation", "direction"}
        with get_conn() as conn:
            assert pg_schema.check_schema_drift(conn) == []


class TestActivityEndpoint:
    def test_shape_and_scoping(self, client):
        _mk_agent("act-mine")
        _mk_agent("act-other")
        _assign_user(MEMBER, {"act-mine": "viewer"})
        r = client.get("/v1/agents/activity", headers=MEMBER_H())
        assert r.status_code == 200
        body = r.json()
        assert body["window_days"] == 7
        names = {a["name"] for a in body["agents"]}
        assert "act-mine" in names and "act-other" not in names
        row = next(a for a in body["agents"] if a["name"] == "act-mine")
        for key in ("messages_7d", "usage_records_7d", "task_runs_7d"):
            assert row[key] == 0
        assert row["live"] is False and row["streaming"] is False

    def test_admin_sees_all(self, client):
        _mk_agent("act-a")
        _mk_agent("act-b")
        r = client.get("/v1/agents/activity", headers=ADMIN_H())
        names = {a["name"] for a in r.json()["agents"]}
        assert {"act-a", "act-b"} <= names


class TestDelegationEdgesEndpoint:
    def test_member_sees_dept_scoped_edges(self, client):
        dept = _mk_dept(client)
        for s in ("ee-a", "ee-b", "ee-hidden"):
            _mk_agent(s)
        _assign_dept("ee-a", dept, "Head")
        _assign_dept("ee-b", dept, "Head")
        from storage.agents import agent_store
        agent_store.set_delegation_targets("ee-hidden", ["ee-a"])
        _assign_user(MEMBER, {"ee-a": "viewer"})
        r = client.get("/v1/agents/delegation-edges", headers=MEMBER_H())
        assert r.status_code == 200
        edges = {(e["from"], e["to"], e["source"]) for e in r.json()["edges"]}
        # dept-mate edges visible (both endpoints on the member's map)…
        assert ("ee-a", "ee-b", "department") in edges
        # …but an edge from an agent outside their map never appears
        assert all(e[0] != "ee-hidden" for e in edges)

    def test_admin_sees_everything(self, client):
        from storage.agents import agent_store
        _mk_agent("ea-a")
        _mk_agent("ea-b")
        agent_store.set_delegation_targets("ea-a", ["ea-b"])
        r = client.get("/v1/agents/delegation-edges", headers=ADMIN_H())
        assert ("ea-a", "ea-b", "manual") in {
            (e["from"], e["to"], e["source"]) for e in r.json()["edges"]
        }


class TestAdminAddUserAgent:
    def test_additive_and_idempotent(self, client):
        from storage import database as task_store
        _mk_agent("add-a")
        _mk_agent("add-b")
        _assign_user(MEMBER, {"add-a": "viewer"})
        r = client.post(
            f"/v1/admin/users/{MEMBER}/agents/add-b",
            json={"role": "viewer"}, headers=ADMIN_H(),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "added"
        # existing assignment untouched (the PUT sibling would have replaced)
        assert set(task_store.get_user_agents(MEMBER)) == {"add-a", "add-b"}
        r = client.post(
            f"/v1/admin/users/{MEMBER}/agents/add-b",
            json={"role": "viewer"}, headers=ADMIN_H(),
        )
        assert r.json()["status"] == "already_assigned"

    def test_gates(self, client):
        _mk_agent("add-c")
        # non-admin caller
        assert client.post(
            f"/v1/admin/users/{MEMBER}/agents/add-c",
            json={"role": "viewer"}, headers=CREATOR_H(),
        ).status_code == 403
        # unknown user / agent
        assert client.post(
            "/v1/admin/users/user-ghost/agents/add-c",
            json={"role": "viewer"}, headers=ADMIN_H(),
        ).status_code == 404
        assert client.post(
            f"/v1/admin/users/{MEMBER}/agents/agent-ghost",
            json={"role": "viewer"}, headers=ADMIN_H(),
        ).status_code == 404
        # admin-only agent to a non-admin user
        from storage.agents import agent_store
        agent_store.create_agent("add-secret", "Secret", created_by=ADMIN,
                                 admin_only=True)
        assert client.post(
            f"/v1/admin/users/{MEMBER}/agents/add-secret",
            json={"role": "viewer"}, headers=ADMIN_H(),
        ).status_code == 403


class TestPromptDepartmentLine:
    def test_roster_gets_department_framing(self, client):
        from services.mcp.dynamic_context import _delegation_mcp_context
        dept = _mk_dept(client)
        for s in ("pr-head", "pr-peer", "pr-jun"):
            _mk_agent(s)
        _assign_dept("pr-head", dept, "Head")
        _assign_dept("pr-peer", dept, "Head")
        _assign_dept("pr-jun", dept, "Senior")
        from storage.agents import agent_store
        targets = agent_store.get_delegation_targets("pr-head")
        text = _delegation_mcp_context("pr-head", delegation_targets=targets)
        assert "**Engineering** department" in text
        assert "level **Head**" in text
        assert "`pr-peer`" in text
        assert "`pr-jun`" in text
        # SYMMETRIC (2026-09-02): the lower level sees its level above.
        jun_targets = agent_store.get_delegation_targets("pr-jun")
        jun_text = _delegation_mcp_context("pr-jun", delegation_targets=jun_targets)
        assert "level(s) above" in jun_text
        assert "`pr-head`" in jun_text

    def test_down_only_line(self, client):
        from services.mcp.dynamic_context import _delegation_mcp_context
        from storage.agents import agent_store
        dept = _mk_dept(client, name="Site", mode="down")
        for s in ("dl-head", "dl-sen", "dl-peer", "dl-jun", "dl-out"):
            _mk_agent(s)
        _assign_dept("dl-head", dept, "Head")
        _assign_dept("dl-sen", dept, "Senior")
        _assign_dept("dl-peer", dept, "Senior")
        _assign_dept("dl-jun", dept, "Junior")
        # the middle level: only the level below is listed, and the clause
        # says why nothing else is
        targets = agent_store.get_delegation_targets("dl-sen")
        text = _delegation_mcp_context("dl-sen", delegation_targets=targets)
        assert "**Site** department" in text
        assert "runs downward only" in text
        assert "level(s) below" in text and "`dl-jun`" in text
        assert "same-level" not in text and "`dl-peer`" not in text
        assert "level(s) above" not in text and "`dl-head`" not in text
        # the bottom level with no target at all: the line alone, so the
        # agent still learns its place
        bare = _delegation_mcp_context("dl-jun", delegation_targets=[])
        assert "level **Junior**" in bare and "runs downward only" in bare
        assert "delegation targets" not in bare
        # the bottom level with a manual target elsewhere: the department
        # line still appears, with the clause and no buckets
        agent_store.set_delegation_targets("dl-jun", ["dl-out"])
        jun_text = _delegation_mcp_context(
            "dl-jun", delegation_targets=["dl-out"]
        )
        assert "level **Junior**" in jun_text
        assert "runs downward only" in jun_text
        assert "Department delegation" not in jun_text

    def test_down_across_line(self, client):
        from services.mcp.dynamic_context import _delegation_mcp_context
        from storage.agents import agent_store
        dept = _mk_dept(client, name="Site", mode="down_across")
        for s in ("da-head", "da-sen", "da-peer", "da-jun"):
            _mk_agent(s)
        _assign_dept("da-head", dept, "Head")
        _assign_dept("da-sen", dept, "Senior")
        _assign_dept("da-peer", dept, "Senior")
        _assign_dept("da-jun", dept, "Junior")
        targets = agent_store.get_delegation_targets("da-sen")
        text = _delegation_mcp_context("da-sen", delegation_targets=targets)
        assert "downward and across your own level, never upward" in text
        assert "same-level" in text and "`da-peer`" in text
        assert "level(s) below" in text and "`da-jun`" in text
        assert "level(s) above" not in text and "`da-head`" not in text

    def test_unassigned_agent_has_no_line(self, client):
        from services.mcp.dynamic_context import _delegation_mcp_context
        _mk_agent("pr-solo")
        _mk_agent("pr-solo2")
        from storage.agents import agent_store
        agent_store.set_delegation_targets("pr-solo", ["pr-solo2"])
        text = _delegation_mcp_context(
            "pr-solo", delegation_targets=["pr-solo2"]
        )
        assert "department" not in text
