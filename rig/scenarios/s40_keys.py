"""What a person's key drives: a headless turn, a task, files and notifications."""
from __future__ import annotations

import time

from lib.key import Key, mint
from lib.scenario import check, scenario
from run import ENGINES

CLAUDE, CLAUDE_MODEL = ENGINES["claude"]


def _key(ctx) -> Key:
    if "key" not in ctx.state:
        ctx.state["key"] = mint(ctx.owner, name=f"rig-key-{int(time.time())}", scopes=["chat", "tasks", "files", "notifications"])
    return Key(ctx.base, ctx.state["key"])


@scenario("key-mint", needs=("wizard",))
def key_mint(ctx):
    """Minting past triggers asks for the password again, and the key names its owner."""
    status, body = ctx.owner.call("POST", "/v1/user-api-keys", {
        "name": "rig-no-password", "permissions": ["chat"], "expires_in_days": 7})
    check(status == 428, f"a mint with no password answered {status}: {str(body)[:200]}")
    me = _key(ctx).ok("GET", "/v1/api/me")
    check(me, "the key's own view answered")


@scenario("key-turn", needs=("key-mint", "turn-claude"))
def key_turn(ctx):
    """A message sent with a key runs a turn and the answer comes back; the row is the person's."""
    key = _key(ctx)
    made = key.ok("POST", "/v1/api/chats", {"agent": ctx.agent, "mode": "dontAsk"})
    chat_id = (made.get("chat") or made).get("id")
    answer = key.send_and_wait(chat_id, "Say hello.")
    check(answer["state"] == "done", f"the turn ended {answer['state']}: {answer.get('detail')}")
    check(answer["answer"], "the answer came back")
    read = key.ok("GET", f"/v1/api/chats/{chat_id}")
    people = [m for m in read["messages"] if m["role"] == "user"]
    check(people and people[0]["sent_with_key"], f"the row names the key: {people[:1]}")
    check(people[0]["author_sub"] == ctx.owner.sub, "the row's author is the person")


@scenario("key-verb", needs=("key-mint", "turn-claude"))
def key_verb(ctx):
    """The otodock command, as downloaded, sends and waits."""
    ctx.require_rig()
    done = _key(ctx).verb("chat", "send", "--agent", ctx.agent, "--wait", "Say hello from the command.")
    check(done.returncode == 0, f"otodock chat send exited {done.returncode}: {done.stderr[-300:]}")
    check(done.stdout.strip(), "it printed the answer")


@scenario("key-task-files-notify", needs=("key-mint", "turn-claude"))
def key_task_files_notify(ctx):
    """A run by key, a file up and down, a notice to oneself."""
    key = _key(ctx)
    started = key.ok("POST", "/v1/api/tasks", {"name": "Rig key task", "agent": ctx.agent, "prompt": "Say hello.",
                                               "notification_mode": "none", "model": CLAUDE_MODEL, "layer": CLAUDE})
    run_id = started["run_id"]
    check(run_id, "the task started")

    def ended():
        run = key.ok("GET", f"/v1/api/runs/{run_id}")
        return run if run.get("status") in ("completed", "failed", "timeout", "cancelled") else None
    run = ctx.owner.wait_until("the key's run", ended, timeout=180)
    check(run["status"] == "completed", f"the run ended {run['status']}")
    path = f"users/{ctx.owner.username}/workspace/rig-file.txt"
    key.ok("PUT", f"/v1/api/agents/{ctx.agent}/files/{path}", {"content": "written by a key"})
    check("written by a key" in key.read_text(ctx.agent, path), "the file reads back")
    sent = key.ok("POST", "/v1/api/notifications", {"title": "Rig notice", "body": "From a key."})
    check(sent.get("id"), "the notice was made")
    box = key.ok("GET", "/v1/api/notifications")
    rows = box.get("notifications", box) if isinstance(box, dict) else box
    check([n for n in rows if n.get("title") == "Rig notice"], "the notice is in the person's list")
