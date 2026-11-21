"""The sandbox of a session on a compute machine, run under bwrap: the
``own`` folder of every engine folder it sees is empty and read-only, its
own home is there and writable, and the files of its home that name its
hooks cannot be rewritten (the plan's security rules 1 and 5)."""

import os
import shutil
import subprocess

import pytest

from core.sandbox import sandbox_render as render, session_home

SID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
SIBLING = "11111111-2222-3333-4444-555555555555"
OTHER_SCOPE = "22222222-3333-4444-5555-666666666666"
A = render.ROOT_AGENT

pytestmark = pytest.mark.skipif(shutil.which("bwrap") is None, reason="requires bwrap on PATH")


@pytest.fixture
def tree(tmp_path):
    agents = tmp_path / "agents"
    agent = agents / "pa"
    ws_store = agent / "workspace" / ".claude"
    user_store = agent / "users" / "alice" / ".claude"
    for store, sid in ((ws_store, SID), (ws_store, SIBLING), (user_store, OTHER_SCOPE)):
        store.mkdir(parents=True, exist_ok=True)
        home = session_home.prepare(store, sid, ("projects",))
        (home / ".credentials.json").write_text(sid)
        (home / "settings.json").write_text("{}")
    empty = tmp_path / "empty"
    empty.mkdir()
    mcps = tmp_path / "mcps"
    mcps.mkdir()
    return agents, mcps, empty


def _run(tree, desc, script: str) -> str:
    agents, mcps, empty = tree
    host = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(empty),
                             launcher="/bin/true", uid=os.getuid(), gid=os.getgid())
    cmd = render.render(desc, host, ["/bin/sh", "-c", script])
    out = subprocess.run(cmd[cmd.index("bwrap"):], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _desc(home=True, masks=True):
    return render.SandboxDescription(
        agent="pa",
        binds=(render.Bind(A, "workspace", "/workspace", True),
               render.Bind(A, "users/alice", "/users/alice", True)),
        mcp_binds=(), mcps_root="/opt/none", extra_binds=(), cwd="/workspace", proxy_port=8400,
        forwards=(render.PROXY_FORWARD,),
        home_masks=(render.Bind(A, "workspace/.claude/own", "/workspace/.claude/own", False),
                    render.Bind(A, "users/alice/.claude/own", "/users/alice/.claude/own", False))
        if masks else (),
        home=render.Bind(A, f"workspace/.claude/own/{SID}", f"/workspace/.claude/own/{SID}", True)
        if home else None)


SCRIPT = f"""
echo "ws=$(ls /workspace/.claude/own | tr '\\n' ' ')"
echo "user=$(ls /users/alice/.claude/own | tr '\\n' ' ')"
echo "mine=$(cat /workspace/.claude/own/{SID}/.credentials.json)"
(echo x > /workspace/.claude/own/{SID}/settings.json) 2>/dev/null && echo settings=written || echo settings=refused
(echo x > /workspace/.claude/own/{SID}/new.txt) 2>/dev/null && echo home=writable || echo home=refused
(mkdir /workspace/.claude/own/planted) 2>/dev/null && echo own=writable || echo own=refused
"""


def test_a_session_sees_its_own_home_alone(tree):
    out = _run(tree, _desc(), SCRIPT)
    lines = dict(line.split("=", 1) for line in out.strip().splitlines())
    assert lines["ws"].split() == [SID]
    assert lines["user"].split() == []
    assert f"mine={SID}" in out
    assert "settings=refused" in out
    assert "home=writable" in out
    assert "own=refused" in out
    agents = tree[0]
    assert (agents / "pa" / "workspace" / ".claude" / "own" / SID / "new.txt").exists()
    assert not (agents / "pa" / "workspace" / ".claude" / "own" / "planted").exists()


def test_a_session_with_no_home_of_its_own_sees_none(tree):
    out = _run(tree, _desc(home=False), SCRIPT)
    lines = dict(line.split("=", 1) for line in out.strip().splitlines())
    assert lines["ws"].strip() == "" and lines["user"].strip() == ""


def test_without_the_masks_every_home_shows_which_is_what_they_close(tree):
    """The control: the same sandbox with no mask shows the sibling's home."""
    out = _run(tree, _desc(home=False, masks=False), SCRIPT)
    assert SIBLING in out


@pytest.mark.parametrize("has_home", [True, False])
def test_a_session_in_its_own_home_reads_no_stores_sign_in(tree, has_home):
    """The store's own sign-in was written by a session without a home (a
    judge, Codex, one from before the upgrade), maybe for another account:
    a session that runs from a home reads it empty and cannot write it. One
    without a home keeps the store's file, which is its own."""
    agents = tree[0]
    name = ".credentials" + ".json"
    for scope in ("workspace", "users/alice"):
        (agents / "pa" / scope / ".claude" / name).write_text(f"store-{scope}")
    out = _run(tree, _desc(home=has_home), f'echo "ws=$(cat /workspace/.claude/{name})"; '
               f'echo "user=$(cat /users/alice/.claude/{name})"; '
               f'(echo x > /workspace/.claude/{name}) 2>/dev/null && echo store=written || echo store=refused')
    lines = dict(line.split("=", 1) for line in out.strip().splitlines())
    if has_home:
        assert lines == {"ws": "", "user": "", "store": "refused"}
        assert (agents / "pa" / "workspace" / ".claude" / name).read_text() == "store-workspace"
    else:
        assert lines["ws"] == "store-workspace" and lines["user"] == "store-users/alice"


def test_a_stores_sign_in_written_after_the_start_stays_hidden(tree, monkeypatch):
    """The store held no sign-in when the session started; a session without
    a home (a judge) writes one later, in place, as the platform's writer
    does beneath the agent's folder (the agents root is this tree, so that is
    the branch that runs). The session that runs from a home still reads it
    empty."""
    import config
    agents = tree[0]
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    name = ".credentials" + ".json"
    store = agents / "pa" / "workspace" / ".claude"
    marker = agents / "pa" / "workspace" / "written"
    host = render.RenderHost(agents_root=str(agents), mcps_root=str(tree[1]), empty_dir=str(tree[2]),
                             launcher="/bin/true", uid=os.getuid(), gid=os.getgid())
    script = (f'i=0; while [ ! -e /workspace/written ] && [ $i -lt 100 ]; do sleep 0.05; i=$((i+1)); done; '
              f'echo "late=$(cat /workspace/.claude/{name})"')
    cmd = render.render(_desc(), host, ["/bin/sh", "-c", script])
    assert (store / name).is_file() and (store / name).stat().st_size == 0
    proc = subprocess.Popen(cmd[cmd.index("bwrap"):], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    from core.sandbox.session_config_dir import write_no_follow
    inode = (store / name).stat().st_ino
    write_no_follow(store / name, b'{"claudeAiOauth": {"accessToken": "ANOTHER-ACCOUNT"}}', mode=0o600)
    assert (store / name).stat().st_ino == inode
    marker.write_text("x")
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert out.strip() == "late="


def test_a_link_at_a_homes_name_is_refused(tree, tmp_path):
    agents = tree[0]
    target = agents / "pa" / "workspace" / ".claude" / "own" / SID
    shutil.rmtree(target)
    os.symlink(tmp_path, target)
    with pytest.raises(render.RenderRefused):
        _run(tree, _desc(), "true")


def test_a_folder_a_mask_hides_is_not_seen():
    """What a session sees at a store's place decides its mask: the agent
    scope's engine folder masked empty for a person's session shows nothing
    (no mask inside that mask, which bwrap could not make), a state folder
    bound onto itself shows the store, and a later mount on a place wins."""
    from core.sandbox.sandbox import Mount, _seen_at, empty_mount_dir
    agent = "/agents/pa"
    table = [Mount(f"{agent}/workspace", "/workspace", False),
             Mount(str(empty_mount_dir()), "/workspace/.claude", False),
             Mount(f"{agent}/users/alice", "/users/alice", True)]
    assert _seen_at(table, f"{agent}/workspace/.claude") is None
    assert _seen_at(table, f"{agent}/workspace/notes") == "/workspace/notes"
    assert _seen_at(table, f"{agent}/users/alice/.claude") == "/users/alice/.claude"
    assert _seen_at(table, f"{agent}/users/bob/.claude") is None
    rebound = [*table, Mount(f"{agent}/workspace/.claude", "/workspace/.claude", True)]
    assert _seen_at(rebound, f"{agent}/workspace/.claude") == "/workspace/.claude"
    elsewhere = [*table, Mount("/somewhere/else", "/users/alice/.claude", True)]
    assert _seen_at(elsewhere, f"{agent}/users/alice/.claude") is None
