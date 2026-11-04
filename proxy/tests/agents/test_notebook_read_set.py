"""The Notebook's read set: an agent reads
itself and every agent one delegation edge away, in either direction,
compiled and manual alike. The five fixtures of the plan (the four modes
and a manual edge outside a department), the department union, and the
edges' corners: three levels, reach subtree, two heads, a self edge, an
edge to a deleted agent, admin-only agents.

Run: cd proxy && venv/bin/pytest tests/agents/test_notebook_read_set.py -v
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

ADMIN = "user-admin"


def _mk(*slugs: str, admin_only: bool = False) -> None:
    from storage.agents import agent_store
    for slug in slugs:
        agent_store.create_agent(slug, slug.title(), created_by=ADMIN, admin_only=admin_only)


def _department(mode: str, levels: list[list[str]], *, reach: str = "adjacent") -> str:
    """A department whose level i holds ``levels[i]``, the edges compiled."""
    from services.departments import edge_compiler
    from storage.agents import db_departments
    dept = db_departments.create_department(
        f"Dept {mode} {reach}", ADMIN, mode=mode, reach=reach,
        level_names=[f"L{i}" for i in range(len(levels))],
    )
    placements = [
        (slug, dept["levels"][i]["id"])
        for i, tier in enumerate(levels) for slug in tier
    ]
    db_departments.place_members(dept["id"], placements)
    edge_compiler.recompile()
    return dept["id"]


def _rs(agent: str) -> set[str]:
    from services.notebook.read_set import read_set
    agents = read_set(agent)
    assert agents[0] == agent, "the agent itself comes first"
    return set(agents)


@pytest.fixture
def team(temp_db):
    _mk("head", "alpha", "beta")
    return ("head", "alpha", "beta")


def test_down_head_reads_every_member_each_member_the_head_alone(team):
    _department("down", [["head"], ["alpha", "beta"]])
    assert _rs("head") == {"head", "alpha", "beta"}
    assert _rs("alpha") == {"alpha", "head"}
    assert _rs("beta") == {"beta", "head"}


def test_down_across_adds_the_siblings(team):
    _department("down_across", [["head"], ["alpha", "beta"]])
    assert _rs("head") == {"head", "alpha", "beta"}
    assert _rs("alpha") == {"alpha", "head", "beta"}
    assert _rs("beta") == {"beta", "head", "alpha"}


def test_both_reads_what_the_two_way_edges_give(team):
    _department("both", [["head"], ["alpha", "beta"]])
    assert _rs("head") == {"head", "alpha", "beta"}
    assert _rs("alpha") == {"alpha", "head", "beta"}


def test_off_contributes_no_edges(team):
    _department("off", [["head"], ["alpha", "beta"]])
    assert _rs("head") == {"head"}
    assert _rs("alpha") == {"alpha"}


def test_a_manual_edge_outside_a_department_reads_both_ways(temp_db):
    from storage.agents import agent_store
    _mk("left", "right", "bystander")
    agent_store.set_delegation_targets("left", ["right"])
    assert _rs("left") == {"left", "right"}
    assert _rs("right") == {"right", "left"}, "the reverse direction reads too"
    assert _rs("bystander") == {"bystander"}


def test_three_levels_adjacent_is_one_hop(temp_db):
    _mk("top", "mid", "low")
    _department("down", [["top"], ["mid"], ["low"]])
    assert _rs("top") == {"top", "mid"}, "the head does not reach two levels down"
    assert _rs("mid") == {"mid", "top", "low"}
    assert _rs("low") == {"low", "mid"}


def test_reach_subtree_reads_every_level(temp_db):
    _mk("top", "mid", "low")
    _department("down", [["top"], ["mid"], ["low"]], reach="subtree")
    assert _rs("top") == {"top", "mid", "low"}
    assert _rs("low") == {"low", "mid", "top"}, "the bottom reads every level above"


def test_two_heads_in_down_do_not_read_each_other(temp_db):
    _mk("h1", "h2", "m")
    _department("down", [["h1", "h2"], ["m"]])
    assert _rs("h1") == {"h1", "m"}
    assert _rs("h2") == {"h2", "m"}
    assert _rs("m") == {"m", "h1", "h2"}


def test_a_self_edge_and_a_two_way_pair_count_once(temp_db):
    from services.notebook.read_set import read_set
    from storage.agents import agent_store
    _mk("a", "b")
    agent_store.set_delegation_targets("a", ["a", "b"])
    agent_store.set_delegation_targets("b", ["a"])
    assert read_set("a") == ["a", "b"]
    assert read_set("b") == ["b", "a"]


def test_an_edge_to_a_deleted_agent_is_dropped(temp_db):
    from storage.agents import agent_store
    from storage.pg import get_conn
    _mk("a")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_delegation_targets (agent_name, target_agent, source) "
            "VALUES ('a', 'ghost', 'manual')")
        conn.commit()
    agent_store._invalidate_cache()
    assert _rs("a") == {"a"}


def test_an_unknown_agent_reads_nothing(temp_db):
    from services.notebook.read_set import read_set
    assert read_set("nobody") == []


def test_admin_only_neighbours_stay_out_of_ordinary_read_sets(temp_db):
    from storage.agents import agent_store
    _mk("plain")
    _mk("vault", "vault2", admin_only=True)
    agent_store.set_delegation_targets("vault", ["plain", "vault2"])
    assert _rs("plain") == {"plain"}, "an ordinary agent never reads an admin-only one"
    assert _rs("vault") == {"vault", "plain", "vault2"}
    assert _rs("vault2") == {"vault2", "vault"}


def _person(*, admin: bool = False, agents: tuple[str, ...] = (), sub: str = "user-x"):
    return SimpleNamespace(
        is_admin=admin, acting_sub=sub,
        can_access_agent=lambda name: admin or name in agents,
    )


def test_the_department_union_is_the_members_whatever_the_mode(team):
    from services.notebook.read_set import department_members
    dept = _department("off", [["head"], ["alpha", "beta"]])
    admin = _person(admin=True)
    assert [m["slug"] for m in department_members(dept, admin)] == ["head", "alpha", "beta"]


def test_the_department_union_lists_only_the_members_a_person_can_open(team):
    from services.notebook.read_set import department_members, department_visible
    from storage.agents import db_departments
    dept_id = _department("down", [["head"], ["alpha", "beta"]])
    dept = db_departments.get_department(dept_id)
    viewer = _person(agents=("alpha",))
    members = [m["slug"] for m in department_members(dept_id, viewer)]
    assert members == ["alpha"]
    assert department_visible(viewer, dept, members)
    stranger = _person(agents=())
    assert not department_visible(stranger, dept, [])
    creator = _person(sub=ADMIN)
    assert department_visible(creator, dept, []), "its creator sees it"
