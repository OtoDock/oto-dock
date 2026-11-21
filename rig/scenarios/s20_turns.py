"""A chat turn on each engine, the queue, a card, plan mode."""
from __future__ import annotations

from lib.chat import assistant_text, script, tool_rows
from lib.key import Planted
from lib.scenario import check, scenario
from run import ENGINES

MARK = "Scripted answer."
IMPLEMENT = "implement_ask"


def _turn_rows_ok(rows: list[dict], *, words: str = MARK) -> None:
    roles = [r.get("role") for r in rows]
    check("user" in roles and "assistant" in roles, f"the rows hold a person's and an answer's: {roles}")
    check(words in assistant_text(rows), f"the answer came from the scripted server: {assistant_text(rows)[:200]!r}")


def _ran_on(ctx, chat_id: str, target: str) -> None:
    """The chat ran where it was asked to: on the machine, or on the host."""
    row = ctx.owner.ok("GET", f"/v1/chats/{chat_id}?limit=1")["chat"]
    where = row.get("execution_target") or ""
    if target:
        check(where == target, f"the chat ran on {where!r}, not on the machine {target}")
    else:
        check(where in ("", "local"), f"the chat ran on {where!r}, not on the host")


def _headless_turn(ctx, word: str, *, target: str = "", text: str = "Say hello.") -> list[dict]:
    engine, model = ENGINES[word]
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, text=text, target=target)
    try:
        chat.wait_done()
        rows = chat.rows()
    finally:
        chat.close()
    _turn_rows_ok(rows)
    _ran_on(ctx, chat.chat_id, target)
    ctx.state.setdefault("chats", {})[f"headless-{word}{'-machine' if target else ''}"] = chat.chat_id
    return rows


def _terminal_turn(ctx, word: str, *, target: str = "") -> list[dict]:
    engine, model = ENGINES[word]
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, text="Say hello from the terminal.",
                          terminal=True, target=target)
    try:
        chat.attach_terminal()
        rows = chat.wait_rows("the terminal's answer row", lambda r: any(x.get("role") == "assistant" for x in r), timeout=150)
    finally:
        chat.close()
    _turn_rows_ok(rows)
    _ran_on(ctx, chat.chat_id, target)
    ctx.state.setdefault("chats", {})[f"terminal-{word}{'-machine' if target else ''}"] = chat.chat_id
    return rows


@scenario("turn-claude", needs=("engines",))
def turn_claude(ctx):
    """A headless turn on Claude Code on the host."""
    _headless_turn(ctx, "claude")
    calls = [c for c in ctx.model_calls() if str(c.get("api", "")).endswith("/messages")]
    check(calls, "the Messages API was called")


@scenario("turn-codex", needs=("engines",))
def turn_codex(ctx):
    """A headless turn on Codex on the host."""
    _headless_turn(ctx, "codex")
    check([c for c in ctx.model_calls() if str(c.get("api", "")).endswith("/responses")], "the Responses API was called")


@scenario("turn-direct", needs=("engines",))
def turn_direct(ctx):
    """A headless turn on Direct LLM on the host."""
    _headless_turn(ctx, "direct")
    check([c for c in ctx.model_calls() if str(c.get("api", "")).endswith("/chat/completions")],
          "the Chat Completions API was called")


@scenario("terminal-claude", needs=("engines",))
def terminal_claude(ctx):
    """A terminal chat on Claude Code on the host."""
    _terminal_turn(ctx, "claude")


@scenario("terminal-codex", needs=("engines",))
def terminal_codex(ctx):
    """A terminal chat on Codex on the host."""
    _terminal_turn(ctx, "codex")


@scenario("queue", needs=("turn-claude",))
def queue(ctx):
    """A message sent during a held turn waits, and runs after it, in order."""
    engine, model = ENGINES["claude"]
    held = "First. " + script({"sleep": 12, "text": "The held turn's answer."})
    # Its own script: a message without one would replay the first's, and the
    # answers would read the same in either order.
    queued = "Second, while the first runs. " + script({"text": "The queued turn's answer."})
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, text=held)
    try:
        chat.send(queued, queue_only=True)
        chat.wait_for("queued", timeout=20)
        chat.wait_done(timeout=120)
        rows = chat.wait_rows("both turns' rows", lambda r: sum(1 for x in r if x.get("role") == "assistant") >= 2, timeout=120)
    finally:
        chat.close()
    users = [r.get("content", "") for r in rows if r.get("role") == "user"]
    check(len(users) == 2 and users[0].startswith("First") and users[1].startswith("Second"), f"the person's rows in order: {users}")
    answers = [r.get("content", "") for r in rows if r.get("role") == "assistant"]
    check(answers[:2] == ["The held turn's answer.", "The queued turn's answer."], f"the answers in order: {answers}")


def _card(ctx, *, approved: bool, flip: bool = False) -> tuple[list[dict], Planted]:
    engine, model = ENGINES["claude"]
    planted = Planted(ctx, "card")
    marker = script({"tool": "shell", "input": {"command": f"rm -f {planted.step_path}"}}, {"text": "The step ran."})
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, text=f"Run the step. {marker}", mode="ask")
    try:
        card = chat.wait_for_card(timeout=120)
        check(card.get("request_id"), f"the card has an id: {card}")
        check(planted.present(), "the step ran before its card was answered")
        chat.answer_card(card, approved=approved != flip)
        chat.wait_done(timeout=120)
        return chat.rows(), planted
    finally:
        chat.close()


@scenario("card", needs=("turn-claude",))
def card(ctx, broken: bool = False):
    """In Ask, a command raises a card: Allow runs it, Deny refuses it. Broken: the
    answer is turned around on its way, as a product that misreads it would."""
    rows, planted = _card(ctx, approved=True, flip=broken)
    check(planted.wait(present=False), "after Allow the step did not run: its file is still there")
    ran = tool_rows(rows, "Bash")
    check(ran and not ran[-1].get("is_error"), f"after Allow the step's row is a refusal: {str(ran)[:300]}")
    rows, planted = _card(ctx, approved=False, flip=broken)
    check(any(r.get("role") == "assistant" for r in rows), "the turn ended after Deny")
    check(planted.present(), "after Deny the step ran anyway: its file is gone")
    refused = tool_rows(rows, "Bash")
    check(not refused or refused[-1].get("is_error", True), f"after Deny the step's row is not a refusal: {str(refused)[:300]}")


@scenario("plan", needs=("turn-claude",))
def plan(ctx):
    """Plan mode reaches a plan's review, and implementing it starts the build turn."""
    engine, model = ENGINES["claude"]
    marker = script({"tool": "ExitPlanMode", "input": {"plan": "Create rig-plan.txt with one line."}}, {"text": "The plan was reviewed."})
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, text=f"Plan it. {marker}", mode="plan")
    try:
        review = chat.wait_for("plan_review", timeout=120)
        check(review.get("request_id"), f"the review has an id: {review}")
        chat.ws.send_json({"type": "plan_review_response", "request_id": review["request_id"], "action": IMPLEMENT,
                           "filename": review.get("filename", "")})
        chat.wait_done(timeout=180)
        rows = chat.rows()
    finally:
        chat.close()
    check("The plan was reviewed." in assistant_text(rows), f"the engine went on after the review: {assistant_text(rows)[:200]!r}")
