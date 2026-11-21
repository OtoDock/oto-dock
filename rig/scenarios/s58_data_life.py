"""What the platform keeps and what it lets go: an upload's age survives a start of the
install's containers, and a file a session deleted stays deleted on a paired machine."""
from __future__ import annotations

import time
import uuid

from lib.chat import script
from lib.key import Planted, files_key
from lib.scenario import check, scenario
from run import ENGINES

from scenarios.s55_continuity import _healthy
from scenarios.s55_frozen_machine import _on_machine

CLAUDE, CLAUDE_MODEL = ENGINES["claude"]
# How long a file that should stay deleted is watched for its return after the
# machine's merge: a heal of an absence is a push or a pull, seconds after it.
STAYS_GONE_S = 45.0
# The scan of a turn's deletes rests 30 s between two scans of one agent.
RECORD_WAIT_S = 90.0

# The services an update starts again: the one-shot that sets the folders' owner, the
# one that hands the compute machine its tools, the server and the compute machine.
RESTARTED = ("otodock-agents-init", "otodock-tools-copy", "otodock-proxy", "otodock-compute")


def _in_proxy(ctx, rel: str) -> str:
    """A path under the agent's folder, as the server's container sees it."""
    return f"/opt/otodock/agents/{ctx.agent}/{rel}"


def _times(ctx, path: str) -> tuple[int, int]:
    """A file's change time and modification time, in seconds."""
    done = ctx.proxy_exec(f"stat -c '%Z %Y' {path}", timeout=60)
    check(done.returncode == 0, f"{path} was not read: {done.stderr[-200:]}")
    changed, modified = done.stdout.split()
    return int(changed), int(modified)


@scenario("upload-age-survives-up", needs=("sign-in",), needs_rig=True)
def upload_age_survives_up(ctx, broken: bool = False):
    """A photo's upload keeps its age through `docker compose up`: its change time and its
    modification time are the same after the one-shot that sets the folders' owner, the
    server and the compute machine started again, so the working-files window counts from
    the upload. Broken: the file's owner is moved before the start, which the one-shot
    then puts back, as a start that touched every upload would."""
    owner = ctx.owner
    # The folder the working-files window ages: a chat's photos land there.
    rel = f"users/{owner.username}/workspace/uploads/photos/rig-age-{uuid.uuid4().hex[:8]}.jpg"
    files_key(ctx).ok("PUT", f"/v1/api/agents/{ctx.agent}/files/{rel}", {"content": "an old photo"})
    path = _in_proxy(ctx, rel)
    if broken:
        moved = ctx.compose("exec", "-T", "-u", "0", "otodock-proxy", "chown", "0:0", path, timeout=60)
        check(moved.returncode == 0, f"the owner was not moved: {moved.stderr[-200:]}")
    # Seconds are what stat prints: a change in the same second would hide.
    time.sleep(2)
    before = _times(ctx, path)
    time.sleep(2)
    done = ctx.compose("up", "-d", "--force-recreate", *RESTARTED, timeout=900)
    check(done.returncode == 0, f"docker compose up failed: {done.stderr[-300:]}")
    _healthy(ctx)
    from scenarios.s70_compute import _signed_in
    _signed_in(ctx)
    after = _times(ctx, path)
    check(after == before, f"the photo's change and modification times moved from {before} to {after}")
    return f"the photo kept its times {before} through docker compose up"


def _machine_turn(ctx, machine_id: str) -> None:
    """A session on the paired machine: its warm-up merges the machine's files."""
    chat = ctx.owner.chat(ctx.agent, engine=CLAUDE, model=CLAUDE_MODEL, target=machine_id,
                          text="Say hello. " + script({"text": "Hello."}))
    try:
        chat.wait_done()
    finally:
        chat.close()


@scenario("delete-stays-deleted", needs=("machine-pair", "turn-claude", "machine-turn-claude"), needs_rig=True,
          machine=True)
def delete_stays_deleted(ctx, broken: bool = False):
    """A file the paired machine holds, deleted by a session on the platform: the turn's
    end records the delete, and at the machine's next session its copy goes and the file
    does not come back. Broken: the file is removed in the server's container outside any
    session, which records nothing, so the machine's copy heals the absence."""
    owner = ctx.owner
    machine_id = ctx.state["machine_id"]
    # A scenario before this one starts the containers again: the machine reconnects.
    owner.wait_until("the machine online", lambda: owner.ok(
        "GET", f"/v1/admin/remote-machines/{machine_id}").get("status") == "online", timeout=120, every=3)
    planted = Planted(ctx, "delete")
    there = _on_machine(ctx, owner.username, planted.step_path)
    # The machine's own session merges first, so it holds the file and the platform
    # knows it does: a delete is recorded only for such a path.
    _machine_turn(ctx, machine_id)
    owner.wait_until("the machine holds the file", there.is_file, timeout=120, every=2)
    if broken:
        done = ctx.proxy_exec(f"rm -f {_in_proxy(ctx, planted.key_path)}", timeout=60)
        check(done.returncode == 0, f"the file was not removed: {done.stderr[-200:]}")
    else:
        chat = owner.chat(ctx.agent, engine=CLAUDE, model=CLAUDE_MODEL, text="Remove the file. " + script(
            {"tool": "shell", "input": {"command": f"rm -f {planted.step_path}"}}, {"text": "Removed."}))
        try:
            chat.wait_done()
        finally:
            chat.close()
    check(planted.wait(present=False), "the file is still on the platform after its delete")
    if not broken:
        # The turn's end scans once per agent every 30 s: the machine merges after it.
        rel = planted.key_path.replace("'", "")
        owner.wait_until("the delete recorded", lambda: ctx.sql(
            f"SELECT origin FROM file_tombstones WHERE agent_slug = '{ctx.agent}' AND rel_path = '{rel}'") == "session-delete",
            timeout=RECORD_WAIT_S, every=3)
    _machine_turn(ctx, machine_id)
    owner.wait_until("the machine's copy gone", lambda: not there.is_file(), timeout=120, every=2)
    check(not planted.wait(present=True, timeout=STAYS_GONE_S), "the file came back on the platform after the machine's merge")
    check(not there.is_file(), "the machine's copy came back")
    return "deleted on the platform, gone on the machine after its next session, and not back"
