"""A stop and the work that is open: a restart in mid-turn continues it, a drain waits for it."""
from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass

from lib.chat import ChatSession, TurnFailed, assistant_text, script, tool_rows
from lib.key import Planted, files_key
from lib.person import Http, Person
from lib.scenario import check, scenario
from run import ENGINES

from scenarios.s30_automation import CLAUDE, CLAUDE_MODEL, SECOND_AGENT, wait_run_ended

WAKE = "schedule_wake"
CONTINUED = "continue"
RESULT = "delegate_result"
# How long a form made wrong on purpose is waited for: it never comes.
BROKEN_WAIT_S = 60.0
CONTINUE_WAIT_S = 240.0
# How long a second result of one lane would take to show: past the turn of a
# scenario's own worker.
SECOND_RESULT_S = 30.0


def _data(row: dict) -> dict:
    data = row.get("event_data")
    return json.loads(data) if isinstance(data, str) and data else (data or {})


def _continued(rows: list[dict]) -> list[dict]:
    """The rows that say the platform continued the chat by itself."""
    return [r for r in rows if r.get("event_type") == WAKE and _data(r).get("source") == CONTINUED]


def _cut_cards(rows: list[dict]) -> list[dict]:
    """The cards that tell a person their turn was cut and nothing goes on."""
    return [r for r in rows if r.get("event_type") == "system" and _data(r).get("subtype") == "turn_ended"]


def _held_call(ctx, tag: str, *, step: int = 0) -> None:
    """A turn is running only once its call has reached the model server."""
    ctx.owner.wait_until(f"the call of {tag} at the model server",
                         lambda: any(c.get("tag") == tag and c.get("step") == step for c in ctx.model_calls()),
                         timeout=120, every=1)


def _stop_and_start(ctx, downtime=None, *, update: bool = False) -> None:
    """The server stopped and started as an operator's compose does it, with the
    sockets gone. ``downtime`` runs while it is down. ``update``: the compute service
    too, in the order an update rolls them after its one drain (scripts/upgrade.sh),
    which ends the engines' turns there; alone, the server's restart leaves them
    running on the compute machine."""
    services = ("otodock-proxy", "otodock-compute") if update else ("otodock-proxy",)
    if downtime is None:
        done = ctx.compose("restart", *services, timeout=240)
        check(done.returncode == 0, f"the restart failed: {done.stderr[-300:]}")
    else:
        done = ctx.compose("stop", *services, timeout=240)
        check(done.returncode == 0, f"the stop failed: {done.stderr[-300:]}")
        downtime()
        done = ctx.compose("start", *services, timeout=240)
        check(done.returncode == 0, f"the start failed: {done.stderr[-300:]}")
    _healthy(ctx)
    if update:
        from scenarios.s70_compute import _signed_in
        _signed_in(ctx)


def _healthy(ctx) -> None:
    """The server answers again, and the sign-in of the process before it is forgotten."""
    http = Http(ctx.base)

    def answers():
        try:
            status, body = http.call("GET", "/health", timeout=5)
        except OSError:
            return None
        return status == 200 and body.get("status") == "ok"
    Person(ctx.base, "", "").wait_until("the proxy back", answers, timeout=240, every=5)
    ctx.people.pop("owner", None)


# A turn's own answer after its held call: seen only where the restart did not cut it.
KEPT_ANSWER = "Never seen."
# A call that is in flight when the stop comes: its effect at once, then a wait no
# engine answers before the cut.
HOLD = 'python3 -c "import time; time.sleep(50)"'


def _cut_in_flight(ctx, word: str, *, answer: str) -> tuple[str, str, str]:
    """A chat of this engine with a call cut in flight: (its id, its tag, the path of
    the file the call appends to). Direct LLM's tools answer at once, so its call has
    answered and the cut lands in the model's next call."""
    engine, model = ENGINES[word]
    tag = f"restart-{word}-{uuid.uuid4().hex[:8]}"
    ran = f"workspace/rig-ran-{tag}.txt"
    if word == "direct":
        steps = [{"tool": "Write", "input": {"file_path": ran, "content": "x"}},
                 {"sleep": 30, "text": "Never seen."}]
        after = [{"text": answer}]
        held_at = 1
    else:
        steps = [{"tool": "shell", "input": {"command": f"printf x >> {ran}; {HOLD}"}}, {"text": KEPT_ANSWER}]
        # What the words ask: check what the call did, then go on.
        after = [{"tool": "shell", "input": {"command": f"cat {ran}"}}, {"text": answer}]
        held_at = 0
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model,
                          text="Hold. " + script(*steps, tag=tag, after_cut=after))
    _held_call(ctx, tag, step=held_at)
    key_path = f"users/{ctx.owner.username}/{ran}"
    key = files_key(ctx)
    ctx.owner.wait_until(f"the {word} call's effect", lambda: key.call(
        "GET", f"/v1/api/agents/{ctx.agent}/files/{key_path}")[0] == 200, timeout=60, every=1)
    chat.close()
    return chat.chat_id, tag, key_path


def _gone_on(ctx, word: str, chat_id: str, answer: str, wait_s: float) -> list[dict]:
    """The chat's rows once it has gone on by itself and answered."""
    def rows_when_done():
        rows = ctx.owner.rows(chat_id)
        return rows if _continued(rows) and answer in assistant_text(rows) else None
    return ctx.owner.wait_until(f"the {word} chat to go on by itself", rows_when_done, timeout=wait_s, every=3)


def _says(ctx, what: str, chat_id: str, words: str) -> None:
    ctx.owner.wait_until(what, lambda: words in assistant_text(ctx.owner.rows(chat_id)), timeout=180, every=2)


# The engines whose turn runs on the compute machine, which a restart of the server
# alone does not end.
ON_COMPUTE = ("claude", "codex")


def _kept(ctx, word: str, chat_id: str, tag: str) -> list[dict]:
    """A turn the compute machine kept through the server's restart: its own answer
    once, nothing continued, no card, the model never told of a cut."""
    def answered():
        rows = ctx.owner.rows(chat_id)
        return rows if KEPT_ANSWER in assistant_text(rows) else None
    ctx.owner.wait_until(f"the {word} chat's own answer", answered, timeout=CONTINUE_WAIT_S, every=3)
    time.sleep(SECOND_RESULT_S)
    rows = ctx.owner.rows(chat_id)
    check(not _continued(rows), f"{word}: the platform continued a turn the compute machine held")
    check(not _cut_cards(rows), f"{word}: a card for a turn the compute machine held: {_cut_cards(rows)}")
    check(assistant_text(rows).count(KEPT_ANSWER) == 1, f"{word}: the answer once: {assistant_text(rows)[:200]!r}")
    check(not any(c.get("told_of_the_cut") for c in ctx.model_calls() if c.get("tag") == tag),
          f"{word}: the model was told of a cut that never came")
    return rows


@scenario("restart-mid-turn", needs=("turn-claude", "turn-codex", "turn-direct"), needs_rig=True)
def restart_mid_turn(ctx, broken: bool = False):
    """A restart of the server with a call in flight: Direct LLM's chat goes on with
    nobody there, said in the chat, its model told the call may have finished, and the
    platform runs nothing again. Claude Code and Codex run on the compute machine, which
    keeps their turn: it ends with its own answer, once. Broken: the record of the cut
    turns is gone."""
    answer = "The answer after the cut."
    key = files_key(ctx)
    cut = {word: _cut_in_flight(ctx, word, answer=answer) for word in ENGINES}
    chats = [chat_id for chat_id, _, _ in cut.values()]
    forget = (lambda: ctx.sql("DELETE FROM chat_open_turns WHERE chat_id IN ("
                              + ",".join(f"'{c}'" for c in chats) + ")")) if broken else None
    _stop_and_start(ctx, downtime=forget)
    wait_s = BROKEN_WAIT_S if broken else CONTINUE_WAIT_S
    for word, (chat_id, tag, key_path) in cut.items():
        if word in ON_COMPUTE:
            _kept(ctx, word, chat_id, tag)
            check(key.read_text(ctx.agent, key_path) == "x", f"{word}: the call's effect is there once")
            _answers_after(ctx, word, chat_id)
            continue
        rows = _gone_on(ctx, word, chat_id, answer, wait_s)
        check(len(_continued(rows)) == 1, f"{word}: one continue, said once: {len(_continued(rows))}")
        check(not _cut_cards(rows), f"{word}: no card asks the person to send again: {_cut_cards(rows)}")
        check(sum(1 for r in rows if r.get("role") == "user") == 1, f"{word}: no person wrote")
        check(assistant_text(rows).count(answer) == 1, f"{word}: the answer once: {assistant_text(rows)[:200]!r}")
        check("Never seen." not in assistant_text(rows), f"{word}: the cut turn's own answer never came")
        calls = [c for c in ctx.model_calls() if c.get("tag") == tag]
        check(any(c.get("told_of_the_cut") for c in calls), f"{word}: the model was told of the cut")
        # The engine runs a cut call again only if the model asks: this one did not.
        done = key.read_text(ctx.agent, key_path)
        check(done == "x", f"{word}: the cut call's effect is there once: {done!r}")
        if word == "direct":
            # It rebuilds from rows, so the words carry the calls: nothing else says.
            rider = next((c.get("rider") for c in calls if c.get("rider")), "")
            check("Write (answered)" in rider and "This list may be short" in rider,
                  f"direct: the words name its call: {rider!r}")
        _answers_after(ctx, word, chat_id)
    return (f"direct went on after the server's restart, {' and '.join(ON_COMPUTE)} kept their turn on the "
            "compute machine, each call's effect there once")


def _answers_after(ctx, word: str, chat_id: str) -> None:
    engine, model = ENGINES[word]
    after = ctx.owner.chat(ctx.agent, engine=engine, model=model, chat_id=chat_id,
                           text="Say hello. " + script({"text": "Back."}))
    try:
        # By its rows: a message that lands as the continue's turn ends goes out
        # through the queue, and a page attaches on its own when it reads that.
        _says(ctx, f"{word}: the chat answers a person after the restart", chat_id, "Back.")
    finally:
        after.close()


@dataclass
class OpenWork:
    """What a stop finds open: a turn that runs, a turn on a card, and the words each
    is known by."""
    running: ChatSession
    waiting: ChatSession
    planted: Planted
    whole: str = "The whole answer."
    later: str = "The message sent during the wait."


def open_work(ctx) -> OpenWork:
    """A turn running on the model server, and a second chat on its card."""
    tag = f"drain-{uuid.uuid4().hex[:8]}"
    planted = Planted(ctx, "drain")
    owner = ctx.owner
    running = owner.chat(ctx.agent, engine=CLAUDE, model=CLAUDE_MODEL,
                         text="Hold. " + script({"sleep": 25, "text": OpenWork.whole}, tag=tag))
    _held_call(ctx, tag)
    # The step twice: a resumed engine answers the call the stop cut by itself,
    # which is one result on the script's count, and a model then asks again.
    step = {"tool": "shell", "input": {"command": f"rm -f {planted.step_path}"}}
    marker = script(step, step, {"text": "The step ran."})
    waiting = owner.chat(ctx.agent, engine=CLAUDE, model=CLAUDE_MODEL, text=f"Run the step. {marker}", mode="ask")
    waiting.wait_for_card(timeout=120)
    return OpenWork(running, waiting, planted)


def message_during_the_wait(work: OpenWork) -> None:
    """Sent while the platform holds new work: it waits, and says why."""
    work.running.send("While it waits. " + script({"text": work.later}), queue_only=True)
    chip = work.running.wait_for("queued", timeout=30)
    check(chip.get("waiting") == "updating", f"the message waits for the update: {chip}")


def after_the_stop(ctx, work: OpenWork) -> None:
    """The new server: the turn finished whole and is not continued, its message went
    out, and the card is asked again and not answered by the stop."""
    owner = ctx.owner
    asked = ChatSession(owner, agent=ctx.agent)
    asked.ws = owner.socket()
    asked.chat_id = work.waiting.chat_id
    try:
        asked.ws.send_json({"type": "resume_chat", "chat_id": work.waiting.chat_id})
        card = asked.wait_for_card(timeout=CONTINUE_WAIT_S)
        owner.wait_until("the message sent during the wait to go out",
                         lambda: work.later in assistant_text(owner.rows(work.running.chat_id)), timeout=120, every=3)
        rows = owner.rows(work.running.chat_id)
        check(work.whole in assistant_text(rows) and not _continued(rows),
              f"the finished turn is whole and not continued: {[r.get('event_type') for r in rows]}")
        rows = asked.rows()
        check(len(_continued(rows)) == 1 and not _cut_cards(rows),
              f"the card's turn goes on by itself: {[r.get('event_type') for r in rows]}")
        check(not any(t.get("is_error") for t in tool_rows(rows, "Bash")),
              f"the stop answered the card: {str(tool_rows(rows, 'Bash'))[:300]}")
        check(work.planted.present(), "the step ran before its second card was answered")
        asked.answer_card(card, approved=True)
        check(work.planted.wait(present=False), "after Allow the step did not run")
    finally:
        asked.close()


@scenario("drained-stop", needs=("card",), needs_rig=True)
def drained_stop(ctx, broken: bool = False):
    """A drain waits for the running turn, holds what is new and answers no card; what
    waited goes out after the stop, which rolls the server and the compute service as
    an update does. Broken: the drain does not wait."""
    work = open_work(ctx)
    owner = ctx.owner
    stopped = False
    drained: list = []
    try:
        status = ctx.admin("status")
        check(status.returncode == 0 and status.stdout.startswith("Busy"), f"status during the turn: {status.stdout!r}")
        check("waits for a person" in status.stdout, f"status names the card: {status.stdout!r}")
        bound = "0" if broken else "120"
        drain = threading.Thread(target=lambda: drained.append(ctx.admin("drain", "--bound", bound, "--away", "300", timeout=180)))
        drain.start()
        owner.wait_until("the platform holding", lambda: "holding" in ctx.admin("status").stdout, timeout=30, every=2)
        message_during_the_wait(work)
        fresh = ChatSession(owner, agent=ctx.agent)
        try:
            fresh.open(engine=CLAUDE, model=CLAUDE_MODEL, text="A new chat. " + script({"text": "Never."}), timeout=30)
            check(False, "a new chat started during the drain")
        except TurnFailed as refused:
            check("updating" in str(refused.frame.get("message", "")), f"the refusal says why: {refused.frame}")
        finally:
            fresh.close()
        drain.join(timeout=180)
        check(drained and drained[0].returncode == 0, f"the drain ended: {drained and drained[0].stderr[-300:]}")
        rows = work.running.rows()
        check(work.whole in assistant_text(rows), f"the stop cut the running turn: {assistant_text(rows)[:200]!r}")
        check(work.later not in assistant_text(rows), "the message went out before the stop")
        check(work.planted.present(), "the card's step ran: nobody answered it")
        work.running.close()
        work.waiting.close()
        _stop_and_start(ctx, update=True)
        stopped = True
    finally:
        work.running.close()
        work.waiting.close()
        if not stopped:
            ctx.admin("resume")
    after_the_stop(ctx, work)
    status = ctx.admin("status").stdout
    check("holding" not in status, f"the new server holds no work: {status!r}")
    return "the turn finished, the message went out, the card was asked again"


@scenario("restart-on-machine", needs=("machine-turn-claude",), needs_rig=True, machine=True)
def restart_on_machine(ctx, broken: bool = False):
    """A restart in mid-turn with the turn on a paired machine: the machine keeps it, its
    one answer lands, and nothing continues it beside the machine. Broken: the machine's
    engine ends in the downtime. Its report still lists the session and the adoption closes
    it, so the chat gets the card and no answer lands. This form fails at that wait, and it
    does not reach the check that nothing continued the turn: unit tests pin that one."""
    answer, tag = "The machine's answer.", f"machine-{uuid.uuid4().hex[:8]}"
    chat = ctx.owner.chat(ctx.agent, engine=CLAUDE, model=CLAUDE_MODEL, target=ctx.state["machine_id"],
                          text="Hold. " + script({"sleep": 20, "text": answer}, tag=tag))
    _held_call(ctx, tag)
    chat.close()
    sid = chat.session_id

    def end_the_engine():
        ctx.require_rig()
        subprocess.run(["pkill", "-f", sid], check=False, timeout=30)

    _stop_and_start(ctx, downtime=end_the_engine if broken else None)

    def answered():
        rows = ctx.owner.rows(chat.chat_id)
        return rows if answer in assistant_text(rows) else None
    ctx.owner.wait_until("the machine's answer", answered, timeout=CONTINUE_WAIT_S, every=3)
    time.sleep(SECOND_RESULT_S)
    rows = ctx.owner.rows(chat.chat_id)
    check(not _continued(rows), f"the platform continued a turn its machine held: {_continued(rows)}")
    check(not _cut_cards(rows), f"a card for a turn its machine held: {_cut_cards(rows)}")
    check(assistant_text(rows).count(answer) == 1, f"the answer once: {assistant_text(rows)[:200]!r}")
    made = [c for c in ctx.model_calls() if c.get("tag") == tag]
    check(len(made) == 1, f"one model call for the turn: {len(made)}")
    return "the machine kept the turn"


def _delegate(ctx, worker_prompt: str) -> ChatSession:
    prompt = "Delegate this. " + script(
        {"tool": "delegate", "input": {"agent": SECOND_AGENT, "name": "Rig worker", "surface": "task", "prompt": worker_prompt}},
        {"text": "Delegated."})
    chat = ctx.owner.chat(ctx.agent, engine=CLAUDE, model=CLAUDE_MODEL, text=prompt)
    chat.wait_done()
    chat.close()
    return chat


def _the_one_result(ctx, chat_id: str, *, wait_s: float) -> dict:
    """The lane's result in its caller's chat: one, also after the time a second would take."""
    def results():
        return [r for r in ctx.owner.rows(chat_id) if r.get("event_type") == RESULT]
    ctx.owner.wait_until("the worker's result in the chat", results, timeout=wait_s, every=3)
    time.sleep(SECOND_RESULT_S)
    found = results()
    check(len(found) == 1, f"the caller got one result: {[_data(r).get('status') for r in found]}")
    return _data(found[0])


# The product made wrong for the broken forms below: a reopened lane also reports at
# once, and the engine's own wake turn is not read to its end.
_REOPEN_LINE = '''    logger.info("Recovery: run %s reopened on chat %s", run_id, chat_id[:16])
    return True
'''
_REPORTS_TWICE = '''    logger.info("Recovery: run %s reopened on chat %s", run_id, chat_id[:16])
    await _report(run, run_status.FAILED, "", None)
    return True
'''


# The broken form: the first frame of the wake turn says the run's review
# happened, and nothing holds the run, which closes with that turn still open.
_WAKE_READ = "        if self._settling:\n            self._wake_open = True\n"
_WAKE_NOT_HELD = ("        if self._settling:\n            from core.events import wake_capture\n"
                  "            wake_capture.note_captured(self.session_id)\n")


@scenario("cut-lane-reports-once", needs=("delegation",), needs_rig=True)
def cut_lane_reports_once(ctx, broken: bool = False):
    """A lane cut in mid-turn (an update rolls the server and the compute service)
    finishes under its own run, and its caller gets one result. Broken: this server's
    reopen also reports at once, so a second result can show."""
    report, tag = "The worker's report.", f"lane-{uuid.uuid4().hex[:8]}"
    chat = _delegate(ctx, "Hold. " + script({"sleep": 20, "text": report}, tag=tag))
    _held_call(ctx, tag)
    if broken:
        ctx.break_product("services/scheduler/run_recovery.py", _REOPEN_LINE, _REPORTS_TWICE)
    try:
        _stop_and_start(ctx, update=True)
        data = _the_one_result(ctx, chat.chat_id, wait_s=CONTINUE_WAIT_S)
    finally:
        if broken:
            ctx.mend_product()
            _healthy(ctx)
    check(data.get("status") == "completed", f"the worker ended {data.get('status')}")
    check(report in (data.get("output_text") or ""), f"the result is the report: {data.get('output_text')!r}")
    runs = ctx.owner.ok("GET", f"/v1/tasks/runs?task_id={data['task_id']}&include_delegates=true")
    listed = runs.get("runs", runs) if isinstance(runs, dict) else runs
    check(len(listed) == 1, f"one run, the one that was cut: {[r.get('status') for r in listed]}")
    wait_run_ended(ctx.owner, listed[0].get("run_id") or listed[0]["id"])
    worker = ctx.owner.rows(data["worker_chat_id"])
    check(len(_continued(worker)) == 1, f"the worker's chat says it went on: {[r.get('event_type') for r in worker]}")
    return f"run {listed[0].get('run_id') or listed[0]['id']}"


@scenario("worker-background-report", needs=("delegation",), needs_rig=True)
def worker_background_report(ctx, broken: bool = False):
    """A worker ends its turn on a background command: its caller gets the report of the
    engine's own turn after it, once, with no second turn asked for: with the hold alone
    gone the platform's review nudge carries the report on the person's account. The wake
    turn stays silent past the read's slices, or an instant answer would hide the read.
    Claude Code alone runs commands that way. Broken: the run closes with that turn still open."""
    first, report = "Started. I report when it ends.", "The final report."
    if broken:
        ctx.break_product("core/layers/cli/settle.py", _WAKE_READ, _WAKE_NOT_HELD)
        _stop_and_start(ctx)
    try:
        chat = _delegate(ctx, "Run it. " + script(
            {"tool": "Bash", "input": {"command": "sleep 15; echo the build ended", "description": "The build",
                                       "run_in_background": True}},
            {"text": first, "woken": report, "woken_sleep": 25}))
        data = _the_one_result(ctx, chat.chat_id, wait_s=CONTINUE_WAIT_S)
    finally:
        if broken:
            ctx.mend_product()
            _healthy(ctx)
    check(data.get("status") == "completed", f"the worker ended {data.get('status')}")
    check(report in (data.get("output_text") or ""), f"the result is the final report: {data.get('output_text')!r}")
    nudges = [r for r in ctx.owner.rows(data["worker_chat_id"]) if r.get("event_type") == "bg_command_nudge"]
    check(not nudges, f"a second turn was asked for: {nudges}")
    return "the report of the turn after the command"
