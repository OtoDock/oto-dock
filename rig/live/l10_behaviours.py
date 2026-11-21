"""The live layer: real engines on their cheapest models. A prompt provokes a
behaviour, and the platform's own frames and rows say whether it happened.

What an engine supports comes from the flags it declares (``capabilities``), so
nothing is asked of an engine that never said it could. A behaviour that did not
happen although the turn ended well is ``inconclusive``: a model may decline. A
wrong row or a turn that errors is a failure. With no account on this host, the
layer runs against the scripted server, each prompt carrying its marker: that
proves the code and is reported as ``scripted-only``, which a release refuses.
Each behaviour keeps its raw output for the diff against the last release.
"""
from __future__ import annotations

import base64
import contextlib
import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field

from lib.chat import ChatSession, assistant_text, script
from lib.key import Planted
from lib.scenario import (
    NOT_LOGGED_IN,
    CheckFailed,
    Inconclusive,
    NotSupported,
    check,
    scenario,
)
from run import ENGINES, IDLE_TIMEOUT_S, SCRIPTED_KEY, model_for, rig_home
from scenarios import s10_install

ACCOUNT_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


@dataclass
class Behaviour:
    name: str
    about: str
    prompt: str
    steps: dict[str, list[dict]]    # engine word -> the scripted model's steps
    flags: tuple[str, ...] = ()     # capabilities it needs, dotted for a group's
    mode: str = "dontAsk"
    wanted_frames: tuple[str, ...] = ()
    answer_card: bool = False
    answer_plan: bool = False
    needs_shell: bool = False
    extra: dict = field(default_factory=dict)
    # A model of the engine's own, where the behaviour is that model's (with real
    # accounts only: the scripted row has none of its tools), and the words its
    # answer must hold.
    models: dict[str, str] = field(default_factory=dict)
    said: str = ""


# A removal asks, in Ask, on every placement. A plain write in the session's own folder
# runs, and outside it a machine without full access refuses. PLANTED is a file the
# owner's key writes before the turn: still there, the step did not run.
PLANTED, NEW_FILE = "{planted}", "{new_file}"
CARD_COMMAND = f"rm -f {PLANTED}"
# What an engine's own terminal draws when it asks before a step.
TERMINAL_ASKS = re.compile(r"would you like to run|do you want to|yes, proceed|allow command|approve", re.IGNORECASE)
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)")
STEP_DESCRIPTION = "Print the word rig"
# What the model sends the person mid-turn (Codex's message tool, the GPT-6.2 rows).
MESSAGE_NOTE = "rig note to the person"
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{8,}")
# What the live layer asks each engine for, as the engines declare it today. A flag
# flipped to false would turn a broken behaviour into "not supported": a change here
# is a decision, made in this table and in the engine's declaration together.
DECLARED = {
    "claude-code-cli": {"supports_subagents": True, "supports_plan_mode": True, "supports_permissions": True,
                        "behaviour.supports_background_commands": True, "behaviour.supports_step_descriptions": True},
    "codex-cli": {"supports_subagents": True, "supports_plan_mode": True, "supports_permissions": True,
                  "behaviour.supports_background_commands": True, "behaviour.supports_step_descriptions": False},
    "direct-llm": {"supports_subagents": False, "supports_plan_mode": False, "supports_permissions": True,
                   "behaviour.supports_background_commands": False, "behaviour.supports_step_descriptions": False},
}
BEHAVIOURS = [
    Behaviour("plain", "A plain turn ends with an answer row.", "Reply with one short sentence.",
              {"claude": [{"text": "Done."}], "codex": [{"text": "Done."}], "direct": [{"text": "Done."}]}),
    Behaviour("subagent", "A sub-agent starts and finishes.",
              "Use your sub-agent tool to start exactly one sub-agent with the description 'Rig probe', whose whole job is to "
              "reply with one short sentence. Wait for it, then reply with one short sentence.",
              {"claude": [{"tool": "Agent", "input": {"description": "Rig probe", "subagent_type": "general-purpose",
                                                      "prompt": "Reply. " + script({"text": "From the sub-agent."})}},
                          {"text": "Done."}],
               "codex": [{"tool": "spawn_agent", "input": {"message": "Reply with one short sentence."}}, {"text": "Done."}]},
              flags=("supports_subagents",), wanted_frames=("task_spawn",)),
    Behaviour("background-command", "A background command starts, and the chat shows it.",
              "Run the shell command `sleep 3; echo rig` as a background command, so that it does not block you, then reply "
              "with one short sentence without waiting for it.",
              {"claude": [{"tool": "Bash", "input": {"command": "sleep 3; echo rig", "run_in_background": True,
                                                     "description": "A background step"}}, {"text": "Started."}],
               "codex": [{"tool": "exec_command", "input": {"cmd": "sleep 8; echo rig", "yield_time_ms": 200}}, {"text": "Started."}]},
              flags=("behaviour.supports_background_commands",), wanted_frames=("bg_command_spawn",)),
    Behaviour("step-description", "A step carries its description.",
              "Run the shell command `echo rig` and give that step a short description, then reply with one short sentence.",
              {"claude": [{"tool": "Bash", "input": {"command": "echo rig", "description": STEP_DESCRIPTION}},
                          {"text": "Done."}]},
              flags=("behaviour.supports_step_descriptions",)),
    Behaviour("card", "In Ask, a step raises a card and an answer ends the wait.",
              f"Run the shell command `{CARD_COMMAND}`, then reply with one short sentence. If you have no shell, "
              f"write the word rig to a new file named {NEW_FILE}.",
              {"claude": [{"tool": "shell", "input": {"command": CARD_COMMAND}}, {"text": "Done."}],
               "codex": [{"tool": "exec_command", "input": {"cmd": CARD_COMMAND, "sandbox_permissions": "require_escalated",
                                                            "justification": "The rig asks for a card."}}, {"text": "Done."}],
               "direct": [{"tool": "Write", "input": {"file_path": NEW_FILE, "content": "rig"}}, {"text": "Done."}]},
              flags=("supports_permissions",), mode="ask", wanted_frames=("permission_prompt",), answer_card=True),
    Behaviour("plan", "Plan mode reaches a plan's review.",
              "Make a one-line plan to create a file named rig-plan.txt, and submit it for review. Do not create the file yet.",
              {"claude": [{"tool": "ExitPlanMode", "input": {"plan": "Create rig-plan.txt."}}, {"text": "Reviewed."}],
               "codex": [{"text": "<proposed_plan>\nCreate rig-plan.txt.\n</proposed_plan>"}]},
              flags=("supports_plan_mode",), mode="plan", wanted_frames=("plan_review",), answer_plan=True),
    Behaviour("message-to-person", "A message the model sends the person runs in Ask with no card, as text.",
              "Use your tool for sending the person a message while you work, once, with exactly the words "
              f"'{MESSAGE_NOTE}'. Then reply with one short sentence.",
              {"codex": [{"tool": "send_message_to_user_async", "input": {"message": MESSAGE_NOTE}}, {"text": "Done."}]},
              mode="ask", models={"codex": "gpt-6.2-sol"}, said=MESSAGE_NOTE),
]
# Terminal and machine take these two, per engine: the rest are the same code path.
NARROW = ("plain", "card")
TERMINAL_ENGINES = ("claude", "codex")
MACHINE_ENGINES = ("claude", "codex")


def declares(ctx, word: str, flag: str) -> bool:
    return declares_on(ctx, ENGINES[word][0], flag)


def declares_on(ctx, engine: str, flag: str) -> bool:
    caps = ctx.state.get("capabilities", {}).get(engine, {})
    node = caps
    for part in flag.split("."):
        node = node.get(part, False) if isinstance(node, dict) else False
    return bool(node)


def real_accounts(ctx) -> dict[str, str]:
    """The keys on this host: outside every repository and export, never logged."""
    ctx.require_rig()
    path = rig_home() / ".otodock-rig" / "accounts"
    if not path.exists():
        return {}
    found = {}
    for line in path.read_text().splitlines():
        key, _, value = line.partition("=")
        if key in ACCOUNT_KEYS and value.strip():
            found[key] = value.strip()
    return found


def model_of(ctx, word: str, item: Behaviour | None = None) -> str:
    """The behaviour's own model with real accounts, else the engine's (``run.model_for``)."""
    if ctx.state.get("real_models") and item is not None and item.models.get(word):
        return item.models[word]
    return model_for(ctx, word)


def prompt_of(ctx, item: Behaviour, word: str, files: dict[str, str]) -> str:
    text = item.prompt if ctx.state.get("real_models") else f"{item.prompt} {script(*item.steps[word])}"
    for slot, path in files.items():
        text = text.replace(slot, path)
    return text


class CardFiles:
    """What the card's step acts on: a file the key planted for a shell to remove, and
    the name of a new file for an engine that only writes."""

    def __init__(self, ctx, word: str):
        self.planted = Planted(ctx, "live-card")
        self.shell = word != "direct"
        self.new_file = f"workspace/rig-card-new-{uuid.uuid4().hex[:10]}.txt"
        self.new_key_path = f"users/{ctx.owner.username}/{self.new_file}"

    def slots(self) -> dict[str, str]:
        return {PLANTED: self.planted.step_path, NEW_FILE: self.new_file}

    def untouched(self) -> bool:
        if self.shell:
            return self.planted.present()
        status, _ = self.planted.key.call("GET", f"/v1/api/agents/{self.planted.agent}/files/{self.new_key_path}")
        return status == 404

    def ran(self) -> bool:
        """The step happened, after a wait for a machine's sync at the turn's end."""
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if not self.untouched():
                return True
            time.sleep(2)
        return False


def keep_raw(ctx, chat: ChatSession, word: str, placement: str, mode_name: str, name: str) -> None:
    """The frames, the rows and the engine's own transcript, for the diff."""
    folder = ctx.raw_dir / word / placement / mode_name / name
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "frames.jsonl").open("w") as out:
        for frame in chat.frames:
            if frame.get("type") != "pty_output":
                out.write(json.dumps(frame) + "\n")
    with contextlib.suppress(OSError):
        (folder / "rows.json").write_text(json.dumps(chat.rows(), indent=1))
    # By name only, the session's own file: an engine's folder also holds its
    # credentials, and a recent file of another session is not this one's.
    sid = chat.session_id
    if not SESSION_ID.fullmatch(sid or ""):
        return
    find = f"f=$(find {{root}} -name '*{sid}*.jsonl' 2>/dev/null | head -n1); [ -n \"$f\" ] && cat \"$f\""
    if placement == "host":
        text = ctx.proxy_exec(find.format(root="/opt/otodock/agents")).stdout
    else:
        ctx.require_rig()
        text = subprocess.run(["bash", "-c", find.format(root="~/.oto-dock/agents")], capture_output=True, text=True,
                              timeout=60, check=False).stdout
    if text:
        (folder / "transcript.jsonl").write_text(text)


def run_behaviour(ctx, item: Behaviour, word: str, *, terminal: bool = False, target: str = "") -> str:
    ctx.require_rig()
    placement = "machine" if target else "host"
    mode_name = "terminal" if terminal else "headless"
    for flag in item.flags:
        if not declares(ctx, word, flag):
            raise NotSupported(f"{ENGINES[word][0]} does not declare {flag}")
    if not ctx.state.get("real_models") and word not in item.steps:
        raise NotSupported(f"the scripted server has no script for {item.name} on {word}")
    if item.models and word not in item.models:
        raise NotSupported(f"{item.name} is a behaviour of {sorted(item.models)} alone")
    if item.models and not ctx.state.get("real_models"):
        raise NotSupported(f"the scripted model row offers none of {item.models[word]}'s own tools")
    engine = ENGINES[word][0]
    files = CardFiles(ctx, word) if item.answer_card else None
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model_of(ctx, word, item),
                          text=prompt_of(ctx, item, word, files.slots() if files else {}),
                          mode=item.mode, terminal=terminal, target=target)
    try:
        if terminal:
            chat.attach_terminal()
            return _terminal_outcome(ctx, chat, item, word, files)
        return _headless_outcome(ctx, chat, item, word, files)
    finally:
        keep_raw(ctx, chat, word, placement, mode_name, item.name)
        chat.close()


def did_not_happen(ctx, what: str) -> None:
    """A real model may decline: inconclusive. The scripted one did what it was told,
    so the platform lost it: a failure."""
    if ctx.state.get("real_models"):
        raise Inconclusive(f"{what}: the model may not have done it")
    raise CheckFailed(what)


def _headless_outcome(ctx, chat: ChatSession, item: Behaviour, word: str, files: CardFiles | None) -> str:
    seen: set[str] = set()
    finished = False
    while not finished:
        frame = chat.next_frame(timeout=240)
        if frame is None:
            break
        kind = frame.get("type")
        seen.add(kind)
        if kind == "permission_prompt" and files is not None:
            check(files.untouched(), "the step ran before its card was answered")
            chat.answer_card(frame, approved=True)
        if kind == "plan_review" and item.answer_plan:
            chat.ws.send_json({"type": "plan_review_response", "request_id": frame.get("request_id"),
                               "action": "reject", "filename": frame.get("filename", "")})
        if kind in ("error", "warmup_failed", "limit_reached"):
            raise CheckFailed(f"the turn failed: {json.dumps(frame)[:300]}")
        finished = kind == "done"
    check(finished, f"no end of turn; frames seen: {sorted(seen)}")
    rows = chat.rows()
    check(any(r.get("role") == "assistant" for r in rows) or item.answer_plan, f"no answer row: {[r.get('role') for r in rows]}")
    missing = [w for w in item.wanted_frames if w not in seen]
    if missing:
        did_not_happen(ctx, f"the turn ended well, with no {missing}")
    if item.said:
        check("permission_prompt" not in seen, f"{item.name} raised a card")
        if item.said not in assistant_text(rows):
            did_not_happen(ctx, f"the chat shows no text {item.said!r}")
    if files is not None and not files.ran():
        did_not_happen(ctx, "after Allow the step did not run: its file is untouched")
    if item.name == "step-description":
        want = "" if ctx.state.get("real_models") else STEP_DESCRIPTION
        described = [f for f in chat.frames if f.get("type") in ("tool_info", "tool_start")
                     and str((f.get("tool_input") or {}).get("description") or f.get("summary") or "")
                     and want in json.dumps(f)]
        if not described:
            did_not_happen(ctx, "no step with a description")
    return ""


def _screen(chat: ChatSession) -> str:
    """The text the terminal drew so far, without its escape codes."""
    raw = b"".join(base64.b64decode(f.get("data") or "") for f in chat.frames if f.get("type") == "pty_output")
    return ANSI.sub("", raw.decode("utf-8", errors="replace"))


def _terminal_outcome(ctx, chat: ChatSession, item: Behaviour, word: str, files: CardFiles | None) -> str:
    asked: list[str] = []

    def answered(rows):
        done = any(r.get("role") == "assistant" for r in rows)
        if files is None:
            return done
        if not asked:
            frames = [f for f in chat.frames if f.get("type") == "pty_permission"]
            if frames or TERMINAL_ASKS.search(_screen(chat)[-3000:]):
                # The card itself: the step waits on it, so its file is untouched here.
                check(files.untouched(), "the step ran before its card was answered")
                if frames:
                    chat.ws.send_json({"type": "permission_response", "request_id": frames[0].get("request_id"),
                                       "approved": True})
                    asked.append("the card")
                else:
                    chat.ws.send_json({"type": "pty_input", "chat_id": chat.chat_id,
                                       "data": base64.b64encode(b"y").decode()})
                    asked.append("the terminal's own question")
            elif not files.untouched():
                raise CheckFailed("the step ran in the terminal with no card")
        return done
    rows = chat.wait_rows("the terminal's answer", answered, timeout=180)
    check(rows and rows[0].get("role") == "user", f"the person's row: {[r.get('role') for r in rows]}")
    said = assistant_text(rows)
    check(NOT_LOGGED_IN not in said, f"the engine is not signed in: {said[:200]!r}")
    if files is not None:
        if not asked:
            did_not_happen(ctx, "the terminal asked nothing before the step")
        if not files.ran():
            did_not_happen(ctx, f"after {asked[0]} was answered the step did not run")
    if not ctx.state.get("real_models"):
        expected = item.steps[word][-1].get("text", "")
        check(expected in said, f"the terminal's answer is the script's end: {said[:200]!r}")
    return f"answered {asked[0]}" if asked else ""


def _register() -> None:
    for item in BEHAVIOURS:
        for word in item.models or ENGINES:
            _add(item, word, placement="", terminal=False)
    for name in NARROW:
        item = next(b for b in BEHAVIOURS if b.name == name)
        for word in TERMINAL_ENGINES:
            _add(item, word, placement="", terminal=True)
        for word in MACHINE_ENGINES:
            _add(item, word, placement="machine", terminal=False)
            _add(item, word, placement="machine", terminal=True)




def _add(item: Behaviour, word: str, *, placement: str, terminal: bool) -> None:
    parts = ["live", item.name, word]
    if terminal:
        parts.append("terminal")
    if placement:
        parts.append(placement)
    needs = ("live-accounts", "live-machine") if placement else ("live-accounts",)

    def run(ctx, item=item, word=word, placement=placement, terminal=terminal):
        target = ctx.state["machine_id"] if placement else ""
        return run_behaviour(ctx, item, word, terminal=terminal, target=target)
    scenario("-".join(parts), layer="live", needs=needs, needs_rig=True, retries=1,
             about=item.about)(run)


def _remove_scripted(owner, layer: str, sub_id: str) -> None:
    """The scripted account goes once no session holds it: the platform refuses
    the removal (409) while a chat of the scripted layer is still open, and
    closes an idle one after ``IDLE_TIMEOUT_S``."""
    path = f"/v1/admin/execution-layers/{layer}/subscriptions/{sub_id}"

    def gone() -> bool:
        status, data = owner.call("DELETE", path)
        if status == 409:
            return False
        check(status < 300 or status == 404, f"DELETE {path} answered {status}: {str(data)[:200]}")
        return True
    owner.wait_until(f"the scripted account of {layer} holds no session", gone, timeout=5 * IDLE_TIMEOUT_S, every=10)


@scenario("live-accounts", layer="live", needs_rig=True)
def live_accounts(ctx):
    """The accounts on this host connect through the admin route, or the layer stays on the scripted server."""
    owner = ctx.owner
    layers = owner.ok("GET", "/v1/admin/execution-layers")["layers"]
    keys = real_accounts(ctx)
    if keys:
        rows = {layer: owner.ok("GET", f"/v1/admin/execution-layers/{layer}/subscriptions") for layer, _ in ENGINES.values()}
        for layer, subs in rows.items():
            for sub in (subs.get("subscriptions", subs) if isinstance(subs, dict) else subs):
                if sub.get("label") == "Scripted":
                    _remove_scripted(owner, layer, sub["id"])
        pairs = [("claude-code-cli", "anthropic", "ANTHROPIC_API_KEY"), ("codex-cli", "openai", "OPENAI_API_KEY"),
                 ("direct-llm", "anthropic", "ANTHROPIC_API_KEY"), ("direct-llm", "openai", "OPENAI_API_KEY")]
        served: dict[str, list[str]] = {}
        for layer, provider, name in pairs:
            if name in keys:
                owner.ok("POST", f"/v1/admin/execution-layers/{layer}/subscriptions",
                         {"provider": provider, "auth_type": "api_key", "api_key": keys[name], "label": "Rig live"})
                served.setdefault(layer, []).append(provider)
        ctx.state["live_providers"] = served
        layers = owner.ok("GET", "/v1/admin/execution-layers")["layers"]
    # Said once the accounts stand: a row after a failure here stays scripted.
    ctx.state["real_models"] = bool(keys)
    ctx.state["capabilities"] = {row["name"]: row["capabilities"] for row in layers}
    moved = [f"{engine} {flag}" for engine, flags in DECLARED.items() for flag, want in flags.items()
             if declares_on(ctx, engine, flag) != want]
    check(not moved, f"the engines declare otherwise than the live layer expects: {moved}")
    if not keys:
        check(ctx.state["capabilities"].get("codex-cli"), "the engines answer")
        return f"no accounts on this host: the scripted server answers (key {SCRIPTED_KEY[:12]}...)"
    return f"accounts connected: {sorted(keys)}"


@scenario("live-machine", layer="live", needs=("live-accounts",), needs_rig=True)
def live_machine(ctx):
    """The paired machine answers, paired now when the scripted layer did not."""
    if ctx.state.get("machine_id"):
        return "paired by the scripted layer"
    from scenarios.s50_machine import pair_linux
    return pair_linux(ctx)


_ = s10_install  # the layer starts from an install the scripted layer's scenarios set up
_register()
