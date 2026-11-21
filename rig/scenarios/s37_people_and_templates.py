"""A member with no engine still reaches the agent and its app and is told why a chat
cannot start; a template's task installs with the mode and the time it declares."""
from __future__ import annotations

import base64
import io
import json
import tarfile

from lib.chat import ChatSession, TurnFailed, script, tool_rows
from lib.scenario import CheckFailed, check, scenario
from run import ENGINES

CLAUDE, CLAUDE_MODEL = ENGINES["claude"]
# What a person with no engine of their own and none lent is told when a chat would start.
NO_ENGINE = "This runs on an AI engine you have not connected yet."
SHARED_APP = "rig-shared-app"
TEMPLATE = "rig-template"
# The template's task: a mode and a time no default gives.
TASK_MODE, TASK_TIMEOUT = "autoReview", 1200


@scenario("member-without-an-engine", needs=("sign-in", "app"), needs_rig=True)
def member_without_an_engine(ctx, broken: bool = False):
    """A member who has no engine and may borrow none opens the agent and its shared app
    with no gate in the way; a new chat's pre-warm and its first message are refused with
    one sentence that says why. Broken: the member may borrow the install's engine, so the
    chat starts."""
    owner, sub = ctx.owner, ctx.state["member_sub"]
    from scenarios.s71_compute_apps import _deploy_server_app
    # The install's first agent is Personal only, which offers no shared app: the row
    # makes it Personal and shared for its window, as an admin does on its page.
    info = owner.ok("GET", f"/v1/agents/{ctx.agent}/info")
    mode = {"collaborative": bool(info.get("collaborative")), "default_scope": info.get("default_scope") or "user"}
    try:
        if not mode["collaborative"]:
            owner.ok("PATCH", f"/v1/agents/{ctx.agent}", {"collaborative": True, "default_scope": mode["default_scope"]})
        app_id = _deploy_server_app(ctx, slug=SHARED_APP, server=None, visibility="agent")
        owner.ok("POST", f"/v1/admin/users/{sub}/agents/{ctx.agent}", {"role": "contributor"})
        owner.ok("PUT", f"/v1/admin/users/{sub}/platform-auth", {"allowed": broken})
        member = ctx.person("member").sign_in()
        status, info = member.call("GET", f"/v1/agents/{ctx.agent}/info")
        check(status == 200, f"the agent's page answered {status}: {str(info)[:200]}")
        status, page, _ = member.raw("GET", f"/v1/apps/{app_id}/html")
        check(status == 200 and b"<p>Rig</p>" in page, f"the shared app answered {status}: {page[:160]!r}")
        token = member.ok("POST", f"/v1/apps/{app_id}/viewer-token")
        check(token.get("token"), f"the app's viewer token: {str(token)[:200]}")
        warm = ChatSession(member, agent=ctx.agent)
        try:
            warm.ws = member.socket()
            warm.ws.send_json({"type": "pre_warmup", "agent": ctx.agent, "permission_mode": "dontAsk", "model": CLAUDE_MODEL,
                               "execution_path": CLAUDE, "theme": "dark"})
            try:
                refused = warm.wait_for("pre_warmup_refused", timeout=60)
            except TimeoutError:
                raise CheckFailed("no refusal within 60 s: the pre-warm was refused, or skipped because the box had no "
                                  "room for two (the proxy's log says 'skipped (no room for two)')") from None
        finally:
            warm.close()
        check(refused.get("error") == NO_ENGINE, f"the pre-warm's refusal: {refused}")
        chat = ChatSession(member, agent=ctx.agent)
        said = {}
        try:
            chat.open(engine=CLAUDE, model=CLAUDE_MODEL, text="Hello. " + script({"text": "Hi."}), timeout=60)
        except TurnFailed as failed:
            said = failed.frame
        finally:
            chat.close()
        check(said.get("type") == "warmup_failed" and said.get("error") == NO_ENGINE, f"the first message's answer: {said}")
    finally:
        owner.ok("PUT", f"/v1/admin/users/{sub}/platform-auth", {"allowed": True})
        if not mode["collaborative"]:
            owner.ok("PATCH", f"/v1/agents/{ctx.agent}", mode)
    return f"the agent and its app opened; the chat said: {NO_ENGINE}"


def _template_bundle(*, with_pins: bool) -> str:
    """The rig's own agent template, as a base64 tar.gz the scripted turn unpacks."""
    task = {"slug": "rig-task", "description": "Rig template task", "scope": "agent", "prompt": "Say hello.",
            "schedule": {"type": "cron", "cron": "0 3 1 1 *"}, "default_state": "paused"}
    if with_pins:
        task.update({"permission_mode": TASK_MODE, "timeout_seconds": TASK_TIMEOUT})
    files = {
        "agent.json": json.dumps({"slug": TEMPLATE, "display_name": "Rig Template", "version": "1.0.0",
                                  "description": "The rig's template, made and removed by one scenario."}),
        "agent.md": "You are the rig's template agent. Answer in one short sentence.\n",
        "mcps.json": json.dumps({"required": []}),
        "README.md": "# Rig Template\n\nAn agent the rig installs from its own folder.\n",
        "tasks.json": json.dumps({"tasks": [task]}),
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


def _remove_template_agent(ctx) -> None:
    """The agent an earlier run made from the template, removed as its page removes it."""
    status, body = ctx.owner.call("DELETE", f"/v1/agents/{TEMPLATE}", {"confirm_slug": TEMPLATE})
    check(status in (200, 404), f"removing {TEMPLATE} answered {status}: {str(body)[:200]}")


@scenario("template-task-pins", needs=("turn-claude",), needs_rig=True)
def template_task_pins(ctx, broken: bool = False):
    """An agent made from a template folder by the agent-creation tool gets the template's
    task with the permission mode and the time limit the task declares. Broken: the
    template declares neither, so the task holds the defaults."""
    owner = ctx.owner
    folder = f"/users/{owner.username}/workspace/{TEMPLATE}"
    enabled = [m["name"] for m in owner.ok("GET", f"/v1/agents/{ctx.agent}/mcps")["mcps"]
               if m.get("enabled") and m.get("category") != "skill"]
    owner.ok("PUT", f"/v1/agents/{ctx.agent}/mcps", {"mcps": sorted({*enabled, "agent-creator-mcp"})})
    unpack = f"rm -rf {folder} && mkdir -p {folder} && echo {_template_bundle(with_pins=not broken)} | base64 -d | tar xz -C {folder}"
    _remove_template_agent(ctx)
    try:
        chat = owner.chat(ctx.agent, engine=CLAUDE, model=CLAUDE_MODEL, text="Make the agent. " + script(
            {"tool": "shell", "input": {"command": unpack}},
            {"tool": "create_agent", "input": {"path": folder}}, {"text": "Made."}))
        try:
            chat.wait_done(timeout=300)
            made = tool_rows(chat.rows(), "create_agent")
        finally:
            chat.close()
        check(made and not made[-1].get("is_error"), f"create_agent answered: {str(made)[-400:]}")
        listed = owner.ok("GET", f"/v1/tasks?agent={TEMPLATE}")
        tasks = listed.get("tasks", listed) if isinstance(listed, dict) else listed
        check(len(tasks) == 1, f"the template's task: {str(tasks)[:300]}")
        task = tasks[0]
        check(task.get("permission_mode") == TASK_MODE and task.get("timeout_seconds") == TASK_TIMEOUT,
              f"the task holds the mode {task.get('permission_mode')!r} and {task.get('timeout_seconds')!r} s")
    finally:
        _remove_template_agent(ctx)
        owner.ok("PUT", f"/v1/agents/{ctx.agent}/mcps", {"mcps": enabled})
    return f"the template's task holds {TASK_MODE} and {TASK_TIMEOUT} s"
