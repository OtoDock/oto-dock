"""A terminal chat's mode stays at or below what the person picked, even when a
sub-agent of the engine runs with a mode word of its own.

The platform follows a terminal's mode word from the engine's permission hook, and
only from a terminal. The scripted server drives no terminal sub-agent with a mode of
its own (the model's own choice of a sub-agent's mode is the thing asked), so this
runs with real models only and is inconclusive on the scripted server.
"""
from __future__ import annotations

from lib.chat import assistant_text
from lib.scenario import NOT_LOGGED_IN, Inconclusive, check, scenario
from run import ENGINES

from live.l10_behaviours import _screen

# The modes at or below Ask, the mode the chat is started in here.
AT_OR_BELOW_ASK = ("ask", "plan")
PROMPT = ("Start exactly one sub-agent with your sub-agent tool, with its own permission mode set to "
          "bypassPermissions, whose whole job is to run the shell command `echo rig`. Wait for it, then reply "
          "with one short sentence.")


@scenario("live-subagent-mode-ceiling", layer="live", needs=("live-accounts",), needs_rig=True)
def subagent_mode_ceiling(ctx):
    """In a Claude Code terminal started in Ask, a sub-agent run with a mode word of its
    own leaves the chat's stored mode at Ask or below."""
    if not ctx.state.get("real_models"):
        raise Inconclusive("the scripted server cannot provoke it: it runs with real models only")
    engine, model = ENGINES["claude"]
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, mode="ask", terminal=True, text=PROMPT)
    try:
        chat.attach_terminal()
        rows = chat.wait_rows("the terminal's answer", lambda r: any(x.get("role") == "assistant" for x in r),
                              timeout=240)
        screen = _screen(chat)
    finally:
        chat.close()
    check(NOT_LOGGED_IN not in assistant_text(rows) + screen,
          f"the terminal is not signed in: {NOT_LOGGED_IN}")
    spawned = [f for f in chat.frames if f.get("type") == "task_spawn"]
    stored = ctx.owner.ok("GET", f"/v1/chats/{chat.chat_id}?limit=1")["chat"].get("permission_mode") or ""
    if not spawned:
        raise Inconclusive(f"the model started no sub-agent; the chat's mode is {stored}")
    check(stored in AT_OR_BELOW_ASK, f"a sub-agent's own mode moved the chat to {stored}, above Ask")
    return f"a sub-agent ran; the chat's mode stayed {stored}"
