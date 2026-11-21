"""A program of the platform's own in a session's sandbox: the general part.

Load-bearing: the program's text and its facts ride the open and the text
runs as sent (the fake machine here runs exactly what the open carries), its
sandbox is the role table and nothing else, its environment holds no token,
its start is counted and never judged, a call that passes its cap and a
program that ended are closed and started again a few times, a lost link is
the session's loss, the manager holds it beside its servers, and the pool's
idle reaper leaves the manager of a session that lives. Nothing here names a
file tool: the program under test echoes.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.layers.direct import mcp as direct_mcp, sandbox_program as sp
from core.remote import tool_relay
from core.sandbox import sandbox_render as render

MACHINE, SESSION = "compute-1", "sess-1"
_ECHO = '''
def _echo(facts, args):
    return {"ok": True, "facts": facts, "args": args}

def _odd(facts, args):
    raise RuntimeError("odd")

def _size(facts, args):
    return {"ok": True, "n": len(args.get("text") or "")}

OPERATIONS = {"echo": _echo, "odd": _odd, "size": _size}
'''
SPEC = sp.ProgramSpec(name="oto:echo", source=_ECHO,
                      facts=lambda description, builder: {"rows": [b.sandbox for b in description.binds]},
                      call_timeout_s=1, max_failures=2)


def _builder():
    description = render.SandboxDescription(
        agent="pa", binds=(render.Bind(render.ROOT_AGENT, "workspace", "/workspace", True),),
        mcp_binds=("custom/memory-mcp",), mcps_root="/opt/mcps",
        extra_binds=(render.Bind(render.ROOT_MCPS, "community/x/data", "/workspace/x", True),),
        cwd="/workspace", proxy_port=8400, forwards=(render.PROXY_FORWARD,),
        exit_port=3129, ssh_agent="sess-1.sock")
    return SimpleNamespace(describe=lambda: description, cfg=SimpleNamespace(username="alice", role="manager"),
                           get_env_overrides=lambda: {"PATH": "/cfg/bin:/usr/bin", "HOME": "/tmp",
                                                      "CLAUDE_CONFIG_DIR": "/workspace/.claude"})


class _Machine:
    """Stands where the connection manager does and plays the compute
    machine: at an open it takes the program's text out of the open's
    environment and runs exactly that, with the facts of the open's argv."""

    def __init__(self):
        self.sent: list[dict] = []
        self.refuse = ""
        self.connected = True
        self.silent = False          # the program answers no call
        self.garbled = False         # the program answers no JSON
        self.answer_text = None      # the text the program answers a call with, in place of its own
        self.hold_close = False      # the machine does not say that a closed program ended
        self.dies = False            # the program ends at a call, before it answers
        self.stillborn = False       # the program ends at its start, before it answers
        self.on_call = None          # called when a call is answered
        self.hold_answers = False    # a call's answer waits until ``release``
        self.held_answers: list = []
        self._run = None
        self.conn = SimpleNamespace(enqueue_send_nowait=self._frame)

    def tunnel_port(self, _machine):
        return 43210

    def get_connection(self, machine):
        return self.conn if self.connected and machine == MACHINE else None

    def builds_own_tools(self, _machine):
        return False

    async def send_command(self, machine, msg, *, timeout=30.0):
        self.sent.append(msg)
        if not self.connected:
            raise RuntimeError("the machine is not connected")
        if self.refuse:
            raise RuntimeError(self.refuse)
        namespace: dict = {}
        text = msg["env"][sp.TEXT_ENV]
        assert text.endswith("\n_serve(OPERATIONS)\n")
        exec(compile(text[:-len("_serve(OPERATIONS)\n")], "<the open's text>", "exec"), namespace)
        facts = json.loads(msg["argv"][-1])
        self._run = lambda line: namespace["_handle"](namespace["OPERATIONS"], facts, json.loads(line))
        return {"status": "ok"}

    def _frame(self, msg):
        self.sent.append(msg)
        if msg.get("type") == "tool_close" and not self.hold_close:
            tool_relay.on_exit(MACHINE, {**msg, "type": "tool_exit"})
        if msg.get("type") != "tool_in":
            return
        request = json.loads(msg["line"])
        if request.get("method") == "initialize" and self.stillborn:
            tool_relay.on_exit(MACHINE, {**msg, "type": "tool_exit"})
            return
        if request.get("method") == "tools/call" and self.dies:
            tool_relay.on_exit(MACHINE, {**msg, "type": "tool_exit"})
            return
        if request.get("method") == "tools/call" and self.silent:
            return
        reply = self._run(msg["line"])
        if reply is None:
            return
        if request.get("method") == "tools/call" and self.garbled:
            reply["result"]["content"][0]["text"] = "this is no answer"
        if request.get("method") == "tools/call" and self.answer_text is not None:
            reply["result"]["content"][0]["text"] = self.answer_text
        if request.get("method") == "tools/call" and self.on_call is not None:
            self.on_call()
        out = {**msg, "type": "tool_out", "line": json.dumps(reply)}
        if request.get("method") == "tools/call" and self.hold_answers:
            self.held_answers.append(out)
            return
        tool_relay.on_out(MACHINE, out)

    def release(self):
        """Every answer that was held goes out now."""
        held, self.held_answers = self.held_answers, []
        for out in held:
            tool_relay.on_out(MACHINE, out)

    def exit_program(self):
        frame = self.of("tool_open")[-1]
        tool_relay.on_exit(MACHINE, {**frame, "type": "tool_exit"})

    def of(self, kind):
        return [m for m in self.sent if m.get("type") == kind]

    def asked(self):
        """The arguments of every call the machine was sent."""
        lines = [json.loads(m["line"]) for m in self.of("tool_in")]
        return [m["params"]["arguments"] for m in lines if m.get("method") == "tools/call"]


@pytest.fixture
def world(monkeypatch):
    machine = _Machine()
    counted: list[tuple] = []

    async def grow(session_id, *, judged, server=""):
        counted.append(("grow", judged, server))
        return SimpleNamespace(admitted=True, user_message="")

    async def shrink(session_id, server=""):
        counted.append(("shrink", server))

    from core import concurrency
    from core.remote import satellite_connection
    monkeypatch.setattr(tool_relay, "_manager", lambda: machine)
    monkeypatch.setattr(satellite_connection, "get_connection_manager", lambda: machine)
    monkeypatch.setattr(concurrency, "grow", grow)
    monkeypatch.setattr(concurrency, "shrink", shrink)
    monkeypatch.setattr(tool_relay, "_CLOSE_WAIT_S", 0.5)
    tool_relay._children.clear()
    yield machine, counted
    tool_relay._children.clear()


def _program(spec=SPEC, machine_id=MACHINE):
    return sp.SandboxProgram(spec, session_id=SESSION, sandbox_builder=_builder(), agent_name="pa",
                             machine_id=machine_id, main_loop=asyncio.get_running_loop())


# -- the spec and the text ----------------------------------------------------

def test_the_text_is_the_loop_the_program_and_the_start_line():
    direct = Path(sp.__file__).parent
    assert sp.program_text(SPEC) == (
        f"{(direct / 'sandbox_program_runtime.py').read_text()}\n{_ECHO}\n_serve(OPERATIONS)\n")


def test_a_spec_must_carry_the_reserved_prefix_and_fit_an_open():
    with pytest.raises(ValueError, match="starts with"):
        sp.ProgramSpec(name="files", source=_ECHO, facts=lambda d, b: {})
    with pytest.raises(ValueError, match="too large"):
        sp.ProgramSpec(name="oto:big", source="# " + "x" * sp.MAX_TEXT_BYTES, facts=lambda d, b: {})
    # No installed tool can take a program's name: a manifest's name has no colon.
    from services.mcp import mcp_manifest_types as manifest_types
    assert not manifest_types._BUILDER_TOOL_RE.fullmatch(f"mcp__{SPEC.name}__echo")


# -- the open -----------------------------------------------------------------

def test_a_step_that_ended_is_let_go(world):
    async def run():
        program = _program()
        assert (await program.call("echo", {}))["ok"] is True
        assert not program._steps            # its start ended, and the program holds the task no longer
        await asyncio.wait_for(program.close(), 5)
    asyncio.run(run())


def test_the_open_carries_the_text_the_facts_and_a_sandbox_of_the_role_table_alone(world):
    machine, _counted = world

    async def run():
        program = _program()
        answer = await program.call("echo", {"x": 1})
        assert answer == {"ok": True, "facts": {"rows": ["/workspace"]}, "args": {"x": 1}}
        frame = machine.of("tool_open")[0]
        assert frame["server"] == "oto:echo" and frame["session_id"] == SESSION
        # Every field of a program's open is a decision: a field a later lane adds fails here.
        assert set(frame) == {"type", "session_id", "server", "key", "sandbox", "argv", "env", "tunnel_port"}
        assert frame["argv"][1:4] == ["-I", "-c", sp._START] and frame["argv"][0].endswith("python3")
        assert json.loads(frame["argv"][4]) == {"rows": ["/workspace"]}
        assert frame["env"][sp.TEXT_ENV] == sp.program_text(SPEC)
        sandbox = frame["sandbox"]
        assert not sandbox["mcp_binds"] and not sandbox["extra_binds"]
        assert "exit_port" not in sandbox and "ssh_agent" not in sandbox and "exit" not in frame
        assert [b["sandbox"] for b in sandbox["binds"]] == ["/workspace"] and sandbox["forwards"]
        await program.close()
    asyncio.run(run())


def test_a_programs_open_carries_no_exit_door_of_the_session(world, monkeypatch):
    """A session with a credential exit sends its door with each tool's
    open. A program's open sends none."""
    from core.credentials import credential_exit, mcp_broker
    from core.remote import mcp_gateway_push
    machine, _counted = world
    monkeypatch.setattr(credential_exit, "machine_secret", lambda session_id: "a-secret")
    monkeypatch.setattr(credential_exit, "machine_block", lambda session_id, bundles, token: {"door": session_id})
    monkeypatch.setattr(mcp_broker, "bundles_of", lambda session_id: {})
    monkeypatch.setattr(mcp_gateway_push, "token_hash_of", lambda secret: "hash")

    async def run():
        program = _program()
        assert direct_mcp.MCPServerConnection._relay_exit_block(program) == {"door": SESSION}   # a tool's open
        assert (await program.call("echo", {}))["ok"] is True
        assert "exit" not in machine.of("tool_open")[0]
        await program.close()
    asyncio.run(run())


def test_an_ordinary_tools_open_still_carries_the_sessions_exit_door(world, monkeypatch):
    """The other side of the seam: a tool's server is opened with the
    session's own description and its exit door, as before a program
    existed."""
    from core.credentials import credential_exit, mcp_broker
    from core.remote import mcp_gateway_push
    machine, _counted = world
    monkeypatch.setattr(credential_exit, "machine_secret", lambda session_id: "a-secret")
    monkeypatch.setattr(credential_exit, "machine_block", lambda session_id, bundles, token: {"door": session_id})
    monkeypatch.setattr(mcp_broker, "bundles_of", lambda session_id: {})
    monkeypatch.setattr(mcp_gateway_push, "token_hash_of", lambda secret: "hash")

    async def run():
        tool = direct_mcp.MCPServerConnection(
            "some-tool", {"type": "stdio", "command": "python3", "args": ["-c", "pass"]}, session_id=SESSION,
            sandbox_builder=_builder(), agent_name="pa", machine_id=MACHINE, main_loop=asyncio.get_running_loop())
        await tool.start()          # the stand-in machine runs programs only: the open is what is read
        frame = machine.of("tool_open")[0]
        assert frame["server"] == "some-tool" and frame["exit"] == {"door": SESSION}
        sandbox = frame["sandbox"]
        assert sandbox["exit_port"] == 3129 and sandbox["ssh_agent"] == "sess-1.sock"
        assert list(sandbox["mcp_binds"]) == ["custom/memory-mcp"] and sandbox["extra_binds"]
        await tool.close()
    asyncio.run(run())


def test_the_programs_environment_holds_no_token_and_nothing_of_the_session(world):
    machine, _counted = world

    async def run():
        program = _program()
        await program.call("echo", {})
        env = machine.of("tool_open")[0]["env"]
        assert not [k for k in env if k.startswith(("OTO_", "PROXY_")) and k != sp.TEXT_ENV and k != "OTO_HOOKS_DIR"
                    and k != "OTO_HOOK_PY"]
        assert "PROXY_API_KEY" not in env and "OTO_SESSION_ID" not in env and "PROXY_URL" not in env
        await program.close()
    asyncio.run(run())


def test_a_program_keeps_no_tool_hints(world, monkeypatch):
    from services.mcp import mcp_permissions
    kept: list = []
    monkeypatch.setattr(mcp_permissions, "record_tool_hints", lambda *a: kept.append(a))

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["ok"] is True
        await program.close()
    asyncio.run(run())
    assert kept == []


# -- counted, never judged ----------------------------------------------------

def test_its_start_is_counted_and_never_judged(world):
    _machine, counted = world

    async def run():
        program = _program()
        await program.call("echo", {})
        await program.call("echo", {})
        assert counted == [("grow", False, "oto:echo")]
        await program.close()
    asyncio.run(run())


def test_a_start_the_machine_refuses_gives_its_count_back_once(world):
    machine, counted = world
    machine.refuse = "the tool could not start: no"

    async def run():
        program = _program()
        answer = await program.call("echo", {})
        assert answer["ok"] is False and answer["code"] == sp.NOT_STARTED and "did not start" in answer["detail"]
        assert counted == [("grow", False, "oto:echo"), ("shrink", "oto:echo")]
        assert program.failures == 1 and program.pending
        await program._give_back()            # nothing is left to give back
        assert counted == [("grow", False, "oto:echo"), ("shrink", "oto:echo")]
        await program.close()
    asyncio.run(run())


def test_its_count_is_given_back_once(world):
    _machine, counted = world

    async def run():
        program = _program()
        await program.call("echo", {})
        await program._give_back()
        await program._give_back()
        assert counted == [("grow", False, "oto:echo"), ("shrink", "oto:echo")]
        await program.close()
    asyncio.run(run())


# -- the cap and the restart --------------------------------------------------

def test_a_call_past_the_cap_closes_the_program_and_the_next_call_starts_it_again(world):
    machine, counted = world

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["ok"] is True
        machine.silent = True
        began = time.monotonic()
        assert await program.call("echo", {}) == {"ok": False, "code": sp.TIMED_OUT, "seconds": 1}
        assert time.monotonic() - began < 5 and program.failures == 0     # a slow call counts nothing
        assert machine.of("tool_close") and tool_relay.live_count() == 0
        assert counted == [("grow", False, "oto:echo"), ("shrink", "oto:echo")]
        machine.silent = False
        assert (await program.call("echo", {"again": True}))["args"] == {"again": True}
        assert len(machine.of("tool_open")) == 2 and counted[-1] == ("grow", False, "oto:echo")
        await program.close()
    asyncio.run(run())


def test_a_program_that_ended_is_started_again_at_the_next_call(world):
    machine, _counted = world

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["ok"] is True
        machine.exit_program()
        await asyncio.sleep(0.05)
        assert program.dead
        assert (await program.call("echo", {"n": 2}))["args"] == {"n": 2}
        assert len(machine.of("tool_open")) == 2 and program.failures == 0     # the answer cleared the count
        await program.close()
    asyncio.run(run())


def test_starts_that_fail_in_a_row_make_the_program_rest_and_then_it_is_tried_again(world, monkeypatch):
    machine, counted = world
    now = [1000.0]
    monkeypatch.setattr(sp, "time", SimpleNamespace(monotonic=lambda: now[0]))
    machine.refuse = "the tool could not start: no"

    async def run():
        program = _program()
        for _ in range(SPEC.max_failures):
            assert (await program.call("echo", {}))["code"] == sp.NOT_STARTED
        opens = len(machine.of("tool_open"))
        assert opens == SPEC.max_failures
        # It rests: the machine is not asked, and the answer says for how long.
        assert await program.call("echo", {}) == {"ok": False, "code": sp.RESTING, "seconds": 60}
        now[0] += 59.5
        assert await program.call("echo", {}) == {"ok": False, "code": sp.RESTING, "seconds": 1}
        assert len(machine.of("tool_open")) == opens
        now[0] += 0.5
        # Tried again after the rest, and the count started anew: one more failure is one failure.
        assert (await program.call("echo", {}))["code"] == sp.NOT_STARTED
        assert len(machine.of("tool_open")) == opens + 1 and program.failures == 1
        machine.refuse = ""
        assert (await program.call("echo", {}))["ok"] is True
        assert len(machine.of("tool_open")) == opens + 2 and program.failures == 0
        await program.close()
    asyncio.run(run())
    assert [c[0] for c in counted] == ["grow", "shrink"] * (SPEC.max_failures + 1) + ["grow"]


def test_a_program_that_ended_at_its_start_leaves_nothing_against_the_next_one(world):
    machine, _counted = world
    machine.stillborn = True

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["code"] == sp.NOT_STARTED
        assert program.failures == 1 and program.pending
        machine.stillborn = False
        assert (await program.call("echo", {"n": 2}))["args"] == {"n": 2}
        # The one that ended left its mark on nothing: the new one runs on after its answer.
        assert program.failures == 0 and not program.pending and program.session is not None
        assert len(machine.of("tool_open")) == 2
        await program.close()
    asyncio.run(run())


def test_a_program_that_ends_at_every_call_rests_too(world, monkeypatch):
    machine, _counted = world
    now = [1000.0]
    monkeypatch.setattr(sp, "time", SimpleNamespace(monotonic=lambda: now[0]))
    machine.dies = True

    async def run():
        program = _program()
        for _ in range(SPEC.max_failures):
            # The machine is there and the program ended: the answer says so, not that nothing can be reached.
            assert await program.call("echo", {}) == {"ok": False, "code": sp.ENDED}
        assert len(machine.of("tool_open")) == SPEC.max_failures
        assert (await program.call("echo", {}))["code"] == sp.RESTING
        now[0] += 60
        machine.dies = False
        assert (await program.call("echo", {}))["ok"] is True
        await program.close()
    asyncio.run(run())


def test_an_answered_call_clears_the_count_of_failures(world):
    machine, _counted = world

    async def run():
        program = _program()
        for n in range(SPEC.max_failures + 2):
            # It ends between two calls, time and again, and answers every call: it never rests.
            assert (await program.call("echo", {"n": n}))["args"] == {"n": n}
            assert program.failures == 0
            machine.exit_program()
            await asyncio.sleep(0.05)
        assert len(machine.of("tool_open")) == SPEC.max_failures + 2
        await program.close()
    asyncio.run(run())


def test_an_answer_that_is_no_answer_is_garbled(world):
    machine, _counted = world
    machine.garbled = True

    async def run():
        program = _program()
        assert await program.call("echo", {}) == {"ok": False, "code": sp.GARBLED}
        machine.garbled = False
        machine.answer_text = "[" * 200_000 + "]" * 200_000      # nested deeper than the parser goes
        assert await program.call("echo", {}) == {"ok": False, "code": sp.GARBLED}
        machine.answer_text = '["a list is no answer"]'
        assert await program.call("echo", {}) == {"ok": False, "code": sp.GARBLED}
        machine.answer_text = None
        assert (await program.call("odd", {}))["code"] == "failed"
        assert (await program.call("nope", {}))["code"] == "unknown_operation"
        await program.close()
    asyncio.run(run())


def test_a_lost_link_is_the_sessions_loss_and_arms_no_restart(world):
    machine, _counted = world
    machine.connected = False

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["code"] == sp.NOT_RUNNING
        assert program.link_lost and not program.pending
        manager = direct_mcp.AgentMCPManager("pa", session_id=SESSION, machine_id=MACHINE)
        manager.programs[SPEC.name] = program
        assert manager.tools_lost()
    asyncio.run(run())


def test_a_session_with_no_machine_is_told_so(world):
    async def run():
        program = _program(machine_id="")
        answer = await program.call("echo", {})
        from core import placement
        assert answer["code"] == sp.NOT_STARTED and answer["detail"] == placement.NO_COMPUTE_SENTENCE
    asyncio.run(run())


def test_a_closed_program_answers_no_call(world):
    machine, _counted = world

    async def run():
        program = _program()
        await program.call("echo", {})
        await program.close()
        # A call after the close touches nothing: nothing is counted, nothing rests, nothing is armed.
        untouched = (0, 0.0, False, 1)
        for ended_before_the_close in (False, True):
            program.dead = program.dead or ended_before_the_close
            assert await program.call("echo", {}) == {"ok": False, "code": sp.NOT_RUNNING}
            assert (program.failures, program._rest_until, program.pending, len(machine.of("tool_open"))) == untouched
    asyncio.run(run())


# -- the manager holds it beside its servers ----------------------------------

def _manager(**kw):
    manager = direct_mcp.AgentMCPManager("pa", session_id=SESSION, machine_id=MACHINE,
                                         sandbox_builder=_builder(), programs=(SPEC,), **kw)
    manager._main_loop = asyncio.get_running_loop()
    return manager


def test_the_manager_makes_its_programs_and_no_tool_list_sees_one(world):
    machine, _counted = world

    async def run():
        manager = _manager()
        await manager._start_impl()
        assert list(manager.programs) == ["oto:echo"] and manager.servers == {}
        assert manager.get_tools() == [] and not manager.has_tool("mcp__oto:echo__echo")
        assert machine.of("tool_open") == []          # it waits for its first call
        assert not manager.programs["oto:echo"]._steps        # and no start is under way: this is no call
        assert (await manager.programs["oto:echo"].call("echo", {}))["ok"] is True
        await manager._close_impl()
        assert manager.programs == {} and machine.of("tool_close")
    asyncio.run(run())


def test_a_calls_session_starts_its_program_with_the_session(world):
    machine, counted = world

    async def run():
        manager = _manager(phone_mode=True)
        await manager._start_impl()
        program = manager.programs["oto:echo"]
        await program.settled()
        assert len(machine.of("tool_open")) == 1 and not program.pending
        assert counted == [("grow", False, "oto:echo")]
        await manager._close_impl()
    asyncio.run(run())


def test_a_program_that_asks_for_it_waits_for_the_first_call_on_a_call_too(world):
    machine, _counted = world
    late = sp.ProgramSpec(name="oto:late", source=_ECHO, facts=lambda d, b: {}, starts_with_call=False)

    async def run():
        manager = direct_mcp.AgentMCPManager("pa", session_id=SESSION, machine_id=MACHINE,
                                             sandbox_builder=_builder(), programs=(late,), phone_mode=True)
        manager._main_loop = asyncio.get_running_loop()
        await manager._start_impl()
        assert not manager.programs["oto:late"]._steps and machine.of("tool_open") == []
        await manager._close_impl()
    asyncio.run(run())


def test_the_one_call_moves_the_managers_clock(world):
    async def run():
        seen: list = []

        async def answer(operation, args, then=None):
            seen.append((operation, args))
            return {"ok": True}

        manager = direct_mcp.AgentMCPManager("pa", session_id=SESSION, machine_id=MACHINE)
        manager.programs["oto:echo"] = SimpleNamespace(call=answer)
        manager.last_activity = 0.0
        assert await sp.call(manager, "oto:echo", "echo", {"x": 1}) == {"ok": True}
        assert seen == [("echo", {"x": 1})] and manager.last_activity > 0.0
        assert await sp.call(manager, "oto:other", "echo", {}) == {"ok": False, "code": sp.NOT_RUNNING}
        assert await sp.call(None, "oto:echo", "echo", {}) == {"ok": False, "code": sp.NOT_RUNNING}
    asyncio.run(run())


# -- the pool's idle reaper ---------------------------------------------------

def test_the_reaper_leaves_the_manager_of_a_session_that_lives():
    async def run():
        pool = direct_mcp.MCPPool()
        closed: list[str] = []
        for sid in ("lives", "orphan", "busy"):
            manager = direct_mcp.AgentMCPManager("pa", session_id=sid)

            async def close(sid=sid):
                closed.append(sid)
            manager.close = close
            manager.last_activity = time.monotonic() - (0 if sid == "busy" else 1000)
            pool._managers[sid] = manager
        await pool.reap_idle(timeout=900, keep=frozenset({"lives"}))
        assert closed == ["orphan"] and set(pool._managers) == {"lives", "busy"}
    asyncio.run(run())


def test_the_reaper_tick_names_the_sessions_that_live(monkeypatch):
    from core import concurrency
    from core.layers.direct import session as sess
    from core.session import session_state
    kept: list = []

    async def idle():
        return 900

    async def reap_idle(timeout=0, *, keep=frozenset()):
        kept.append((timeout, keep))

    async def reap(sid, close):
        return None

    live = SimpleNamespace(mcp_manager=None, last_activity=time.monotonic(), lock=asyncio.Lock())
    monkeypatch.setattr(sess, "_direct_sessions", {"s-live": live})
    monkeypatch.setattr(session_state, "cached_idle_timeout", idle)
    monkeypatch.setattr(concurrency, "reap_session", reap)
    monkeypatch.setattr(sess.mcp_pool, "reap_idle", reap_idle)
    asyncio.run(sess._reap_idle_direct_pass())
    assert kept == [(900, frozenset({"s-live"}))]


# -- every field of the description is a decision -------------------------------

_KEPT, _CLEARED, _REFUSED = "kept", "cleared", "refused"
#: What a program's sandbox gets of each field of the session's description.
#: A field a later lane adds fails the test below until it has a line here:
#: a grant is cleared, a mask and a refusal are kept, and a field with no
#: safe answer refuses the program.
_A_PROGRAM_GETS = {
    "agent": _KEPT,             # whose folders the binds name
    "binds": _KEPT,             # the role table: what the session's role gives
    "mcp_binds": _CLEARED,      # no tool folder
    "mcps_root": _KEPT,         # a name only: nothing is bound there with no tool folder
    "extra_binds": _CLEARED,    # no manifest mount, which can sit inside a session folder
    "cwd": _KEPT,
    "proxy_port": _KEPT,        # the render refuses a description with none
    "forwards": _KEPT,
    "allow_hosts": _KEPT,
    "hosted": _KEPT,
    "inbound": _KEPT,
    "egress_deny": _KEPT,       # a refusal stays
    "beneath": _KEPT,
    "tmp_size_mb": _KEPT,
    "exit_port": _CLEARED,      # no credential exit
    "ssh_agent": _CLEARED,      # no SSH socket
    "kind": _KEPT,
    "home_masks": _KEPT,        # a mask stays: every session's home is shown empty
    # A session's own engine home holds its sign-in, so a program never gets
    # it. But the render shows the store's own sign-in file empty only for a
    # description that names a home: with the home taken out, that file would
    # show. Neither answer is safe, so a session with a home gets no program
    # until the render can keep that mask without the home. No Direct session
    # has a home (the test below holds that).
    "home": _REFUSED,
}
_CLEARED_IS = {"mcp_binds": (), "extra_binds": (), "exit_port": 0, "ssh_agent": ""}


def test_no_direct_session_has_a_home_of_its_own():
    """The day the Direct layer declares a session home, a program's sandbox
    is a new decision (``_A_PROGRAM_GETS``, the line of ``home``): until it
    is taken, every program of such a session refuses to start."""
    from core.layers.direct import layer
    assert layer._DIRECT_CAPABILITIES.auth.session_home is None


def test_every_field_of_the_description_is_a_decision_for_a_program():
    fields = {f.name for f in dataclasses.fields(render.SandboxDescription)}
    assert fields == set(_A_PROGRAM_GETS), "decide what a program gets of each field"
    assert {n for n, verdict in _A_PROGRAM_GETS.items() if verdict == _CLEARED} == set(_CLEARED_IS)
    own = "users/alice/.codex/own"
    full = render.SandboxDescription(
        agent="pa", binds=(render.Bind(render.ROOT_AGENT, "workspace", "/workspace", True),),
        mcp_binds=("custom/memory-mcp",), mcps_root="/opt/mcps",
        extra_binds=(render.Bind(render.ROOT_MCPS, "community/x/data", "/workspace/x", True),),
        cwd="/workspace", proxy_port=8400, forwards=(render.PROXY_FORWARD, "5432"),
        allow_hosts=("example.com",), hosted=True, inbound="8080", egress_deny=True, beneath=False,
        tmp_size_mb=64, exit_port=3129, ssh_agent="sess-1.sock", kind=render.KIND_BUILD,
        home_masks=(render.Bind(render.ROOT_EMPTY, "", f"/{own}", False),),
        home=render.Bind(render.ROOT_AGENT, f"{own}/s1", f"/{own}/s1", True))
    plain = render.SandboxDescription(agent="", binds=(), mcp_binds=(), mcps_root="", extra_binds=(), cwd="",
                                      proxy_port=0, forwards=())
    assert [n for n, verdict in _A_PROGRAM_GETS.items() if verdict == _REFUSED] == ["home"]
    with pytest.raises(RuntimeError, match="a home of its own"):
        sp.program_description(SimpleNamespace(describe=lambda: full))
    homeless = dataclasses.replace(full, home=None)
    got = sp.program_description(SimpleNamespace(describe=lambda: homeless))
    for name, verdict in _A_PROGRAM_GETS.items():
        given = getattr(full, name)
        assert given != getattr(plain, name), f"{name}: the description under test must set it"
        if verdict != _REFUSED:
            assert getattr(got, name) == (given if verdict == _KEPT else _CLEARED_IS[name]), name
    assert got.home is None and got.home_masks == full.home_masks


# -- a stopped call -----------------------------------------------------------
# A Stop in a chat cancels the turn's task, and the cancel reaches the call on
# the tool loop. A step of the program's life (a start, a restart) is the
# program's own: a stopped call leaves it to end, and nothing is half done.

def _hold_opens(machine):
    """Every open waits at the machine until the gate is set."""
    gate, real = asyncio.Event(), machine.send_command

    async def held(machine_id, msg, *, timeout=30.0):
        if msg.get("type") == "tool_open" and not gate.is_set():
            machine.sent.append({"type": "held"})
            await gate.wait()
        return await real(machine_id, msg, timeout=timeout)

    machine.send_command = held
    return gate


async def _until(said, seconds=5.0):
    end = time.monotonic() + seconds
    while not said():
        assert time.monotonic() < end, "it did not happen in time"
        await asyncio.sleep(0.01)


async def _stopped(call, until):
    """``call`` begun and stopped as a Stop stops it, once ``until`` says so."""
    task = asyncio.ensure_future(call)
    await _until(until)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _left_running():
    """The owner tasks that still hold a program's streams."""
    return [t for t in asyncio.all_tasks() if "._serve" in repr(t.get_coro()) and not t.done()]


def test_a_call_stopped_while_its_program_starts_leaves_one_start(world):
    machine, counted = world

    async def run():
        gate = _hold_opens(machine)
        program = _program()
        await _stopped(program.call("echo", {}), lambda: machine.of("held"))
        gate.set()
        assert (await asyncio.wait_for(program.call("echo", {}), 5))["ok"] is True
        await program.close()
        assert len(machine.of("tool_open")) == 1 and not _left_running()
    asyncio.run(run())
    assert [c[0] for c in counted] == ["grow"]


def test_a_call_right_after_a_stopped_one_waits_for_the_start_under_way(world):
    machine, counted = world

    async def run():
        gate = _hold_opens(machine)
        program = _program()
        await _stopped(program.call("echo", {}), lambda: machine.of("held"))
        second = asyncio.ensure_future(program.call("echo", {"n": 2}))
        await asyncio.sleep(0.1)
        assert not second.done()                 # it starts nothing beside the start under way
        gate.set()
        assert (await asyncio.wait_for(second, 5))["args"] == {"n": 2}
        assert len(machine.of("tool_open")) == 1 and len(machine.of("held")) == 1
        await program.close()
        assert not _left_running()
    asyncio.run(run())
    assert [c[0] for c in counted] == ["grow"]


def test_a_call_after_a_stopped_ones_start_has_ended_finds_the_program_running(world):
    machine, counted = world

    async def run():
        gate = _hold_opens(machine)
        program = _program()
        await _stopped(program.call("echo", {}), lambda: machine.of("held"))
        gate.set()
        await _until(lambda: program.session is not None)
        await asyncio.sleep(0.05)
        assert (await asyncio.wait_for(program.call("echo", {"n": 3}), 5))["args"] == {"n": 3}
        assert len(machine.of("tool_open")) == 1
        await program.close()
        assert not _left_running()
    asyncio.run(run())
    assert [c[0] for c in counted] == ["grow"]


def test_a_stopped_calls_start_that_fails_gives_its_count_back_once(world):
    machine, counted = world

    async def run():
        gate = _hold_opens(machine)
        program = _program()
        await _stopped(program.call("echo", {}), lambda: machine.of("held"))
        machine.refuse = "the tool could not start: no"
        gate.set()
        await _until(lambda: ("shrink", "oto:echo") in counted)
        machine.refuse = ""
        assert (await asyncio.wait_for(program.call("echo", {}), 5))["ok"] is True
        assert counted == [("grow", False, "oto:echo"), ("shrink", "oto:echo"), ("grow", False, "oto:echo")]
        await program.close()
        assert not _left_running()
    asyncio.run(run())


def test_a_stopped_calls_start_that_meets_a_lost_link_is_the_sessions_loss(world):
    machine, counted = world

    async def run():
        gate = _hold_opens(machine)
        program = _program()
        await _stopped(program.call("echo", {}), lambda: machine.of("held"))
        machine.connected = False
        gate.set()
        await _until(lambda: program.link_lost)
        await program.settled()
        assert not program.pending and program.failures == 1     # nobody waited, and the loss is still marked
        assert await program.call("echo", {}) == {"ok": False, "code": sp.NOT_RUNNING}
        # The machine is not asked again and nothing more is counted: the session is rebuilt.
        assert len(machine.of("tool_open")) == 1 and program.failures == 1
        await program.close()
        assert not _left_running()
    asyncio.run(run())
    assert counted == [("grow", False, "oto:echo"), ("shrink", "oto:echo")]


def test_a_close_waits_for_a_stopped_calls_start_and_leaves_nothing(world):
    machine, _counted = world

    async def run():
        gate = _hold_opens(machine)
        program = _program()
        await _stopped(program.call("echo", {}), lambda: machine.of("held"))
        closing = asyncio.ensure_future(program.close())
        await asyncio.sleep(0.1)
        assert not closing.done()                # the start under way ends first
        gate.set()
        await asyncio.wait_for(closing, 5)
        assert machine.of("tool_close") and tool_relay.live_count() == 0 and not _left_running()
        assert await program.call("echo", {}) == {"ok": False, "code": sp.NOT_RUNNING}
        assert len(machine.of("tool_open")) == 1
    asyncio.run(run())


@pytest.mark.parametrize("way", ["a call past its cap", "a program that ended"])
def test_a_call_stopped_while_its_program_is_closed_and_armed_again_loses_nothing(world, way):
    machine, counted = world

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["ok"] is True
        owner = program._owner_task
        machine.hold_close = True                            # the close takes a while
        if way == "a call past its cap":
            machine.silent = True
        else:
            machine.on_call = lambda: setattr(program, "dead", True)
        await _stopped(program.call("echo", {}), lambda: machine.of("tool_close"))
        machine.silent, machine.hold_close, machine.on_call = False, False, None
        await program.settled()
        # The stop reached the call alone: the program was closed by its own task, to the end.
        assert owner.done() and not owner.cancelled() and program.pending and program.session is None
        assert (await asyncio.wait_for(program.call("echo", {"n": 2}), 5))["args"] == {"n": 2}
        assert len(machine.of("tool_open")) == 2
        assert counted == [("grow", False, "oto:echo"), ("shrink", "oto:echo"), ("grow", False, "oto:echo")]
        await program.close()
        assert not _left_running() and tool_relay.live_count() == 0
    asyncio.run(run())


@pytest.mark.parametrize("way", ["a call past its cap", "a program that ended"])
def test_a_call_that_comes_while_its_program_is_closed_and_armed_again_waits_its_turn(world, way):
    machine, counted = world

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["ok"] is True
        machine.hold_close = True                            # the close takes a while
        if way == "a call past its cap":
            machine.silent = True
        else:
            machine.on_call = lambda: setattr(program, "dead", True)
        await _stopped(program.call("echo", {}), lambda: machine.of("tool_close"))
        machine.silent, machine.on_call = False, None
        # The next call comes while the program is still being closed: it waits for that, then starts a new one.
        answer = await asyncio.wait_for(program.call("echo", {"n": 2}), 5)
        assert answer["args"] == {"n": 2} and len(machine.of("tool_open")) == 2
        assert counted == [("grow", False, "oto:echo"), ("shrink", "oto:echo"), ("grow", False, "oto:echo")]
        machine.hold_close = False
        await program.close()
        assert not _left_running() and tool_relay.live_count() == 0
    asyncio.run(run())


def test_a_close_while_a_program_is_closed_and_armed_again_leaves_nothing(world):
    machine, _counted = world

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["ok"] is True
        machine.silent, machine.hold_close = True, True
        await _stopped(program.call("echo", {}), lambda: machine.of("tool_close"))
        await program.close()
        assert not _left_running() and tool_relay.live_count() == 0 and not program.pending
        assert await program.call("echo", {}) == {"ok": False, "code": sp.NOT_RUNNING}
    asyncio.run(run())


def test_a_close_under_a_call_stops_the_program_and_gives_nothing_back(world):
    machine, counted = world

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["ok"] is True
        machine.silent = True
        call = asyncio.ensure_future(program.call("echo", {"n": 2}))
        await _until(lambda: len(machine.asked()) == 2)
        await program.close()
        # The program had been asked: what it did is not known, and the code says so.
        assert await asyncio.wait_for(call, 5) == {"ok": False, "code": sp.CLOSED}
        await program.settled()
        # The session's own release takes the count: the program gives nothing back at its close.
        assert counted == [("grow", False, "oto:echo")] and not _left_running()
    asyncio.run(run())


def test_calls_of_one_program_run_one_at_a_time(world):
    machine, _counted = world

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["ok"] is True
        machine.silent = True
        first = asyncio.ensure_future(program.call("echo", {"n": 1}))
        await _until(lambda: len(machine.asked()) == 2)
        second = asyncio.ensure_future(program.call("echo", {"n": 2}))
        await asyncio.sleep(0.2)
        assert machine.asked() == [{}, {"n": 1}]             # the second waits its turn
        machine.silent = False
        assert (await first)["code"] == sp.TIMED_OUT
        assert (await asyncio.wait_for(second, 5))["args"] == {"n": 2}
        await program.close()
    asyncio.run(run())


def test_a_call_stopped_before_its_turn_asks_the_program_nothing(world):
    machine, _counted = world

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["ok"] is True
        machine.silent = True
        first = asyncio.ensure_future(program.call("echo", {"n": 1}))
        await _until(lambda: len(machine.asked()) == 2)
        waiting = asyncio.ensure_future(program.call("echo", {"n": 2}))
        await asyncio.sleep(0.05)
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting
        machine.silent = False
        assert (await first)["code"] == sp.TIMED_OUT
        assert (await program.call("echo", {"n": 3}))["args"] == {"n": 3}
        assert machine.asked() == [{}, {"n": 1}, {"n": 3}]   # what was stopped was never asked
        await program.close()
    asyncio.run(run())


def test_a_step_that_fails_is_told_and_the_next_call_still_works(world, monkeypatch, caplog):
    from core import concurrency
    _machine, _counted = world
    grow = concurrency.grow

    async def no_room(session_id, *, judged, server=""):
        raise RuntimeError("the count could not be asked")

    async def run():
        program = _program()
        monkeypatch.setattr(concurrency, "grow", no_room)
        with pytest.raises(RuntimeError, match="could not be asked"):
            await program.call("echo", {})
        monkeypatch.setattr(concurrency, "grow", grow)
        assert (await program.call("echo", {}))["ok"] is True
        await program.close()
        assert not program._steps
    with caplog.at_level("ERROR", logger="mcp-manager"):
        asyncio.run(run())
    assert any("a step of its life failed" in r.getMessage() for r in caplog.records)


def _no_give_back(monkeypatch, counted):
    """Every give back of the count fails from now on."""
    from core import concurrency

    async def shrink(session_id, server=""):
        counted.append(("shrink failed", server))
        raise RuntimeError("the count could not be asked")

    monkeypatch.setattr(concurrency, "shrink", shrink)


def test_a_restart_that_raises_still_arms_the_next_start(world, monkeypatch, caplog):
    machine, counted = world

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["ok"] is True
        _no_give_back(monkeypatch, counted)
        # A call passes its cap, and the machine never says that the closed program ended.
        machine.silent, machine.hold_close = True, True
        # The restart's failure is the step's own: the call still answers what is known of it.
        assert await program.call("echo", {}) == {"ok": False, "code": sp.TIMED_OUT, "seconds": 1}
        machine.silent, machine.hold_close = False, False
        assert program.pending and program.failures == 1       # armed, and the restart that raised is counted
        assert (await asyncio.wait_for(program.call("echo", {"n": 2}), 5))["args"] == {"n": 2}
        assert len(machine.of("tool_open")) == 2
        await program.close()
        assert not _left_running() and tool_relay.live_count() == 0
    with caplog.at_level("ERROR", logger="mcp-manager"):
        asyncio.run(run())
    assert any("a step of its life failed" in r.getMessage() for r in caplog.records)     # and it is told


def test_a_restart_that_raises_every_time_comes_to_rest(world, monkeypatch):
    machine, counted = world

    async def run():
        program = _program()
        _no_give_back(monkeypatch, counted)
        machine.silent, machine.hold_close = True, True
        for _ in range(SPEC.max_failures):
            assert (await program.call("echo", {}))["code"] == sp.TIMED_OUT
        # It rests like a program that cannot start: the machine is not asked at every call.
        assert (await program.call("echo", {}))["code"] == sp.RESTING
        assert len(machine.of("tool_open")) == SPEC.max_failures
        machine.hold_close = False
        await program.close()
        assert not _left_running()
    asyncio.run(run())


def test_a_stop_that_raises_still_gives_the_count_back_and_arms(world, monkeypatch):
    machine, counted = world

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["ok"] is True
        stop = direct_mcp.MCPServerConnection._stop_owner

        async def stop_and_raise(self):
            await stop(self)
            raise RuntimeError("the stop failed")

        monkeypatch.setattr(direct_mcp.MCPServerConnection, "_stop_owner", stop_and_raise)
        machine.silent = True
        assert (await program.call("echo", {}))["code"] == sp.TIMED_OUT
        monkeypatch.setattr(direct_mcp.MCPServerConnection, "_stop_owner", stop)
        machine.silent = False
        assert counted == [("grow", False, "oto:echo"), ("shrink", "oto:echo")] and program.pending
        assert (await asyncio.wait_for(program.call("echo", {"n": 2}), 5))["args"] == {"n": 2}
        await program.close()
    asyncio.run(run())


def test_an_owner_task_that_ended_as_cancelled_is_taken_as_ended(world):
    machine, counted = world

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["ok"] is True
        owner = program._owner_task
        owner.cancel()                    # as the client's own task group can leave it
        await asyncio.wait([owner])
        assert owner.cancelled()
        program.dead = True
        # Nobody stopped this call: it is answered, by a new program.
        assert (await asyncio.wait_for(program.call("echo", {"n": 2}), 5))["args"] == {"n": 2}
        assert len(machine.of("tool_open")) == 2
        assert counted == [("grow", False, "oto:echo"), ("shrink", "oto:echo"), ("grow", False, "oto:echo")]
        await program.close()
        assert not _left_running()
    asyncio.run(run())


def test_a_stop_that_is_itself_cancelled_stays_cancelled(world):
    machine, _counted = world

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["ok"] is True
        machine.hold_close = True                 # the stop waits for the machine's word
        stopping = asyncio.ensure_future(program._stop_owner())
        await _until(lambda: machine.of("tool_close"))
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        machine.hold_close = False
        await program.close()
    asyncio.run(run())


def test_a_call_past_its_cap_never_ends_the_program_for_the_session(world):
    machine, _counted = world
    machine.silent = True

    async def run():
        program = _program()
        for _ in range(5):
            assert (await program.call("echo", {}))["code"] == sp.TIMED_OUT
        machine.silent = False
        assert (await program.call("echo", {"n": 6}))["args"] == {"n": 6}
        assert len(machine.of("tool_open")) == 6
        await program.close()
    asyncio.run(run())



# -- a call and what the platform does with its answer, as one piece -------------
#
# A call that changes something takes a ``then``: what the platform does with
# the answer. Once the program was asked, a stop ends only the caller's wait,
# and the piece runs to its end with the call's turn kept. A call stopped
# before that asks the program nothing.

def _then(seen):
    async def then(answer):
        seen.append(answer)
    return then


async def _pieces_done(program, seconds=5.0):
    await _until(lambda: not program._pieces, seconds)


def test_a_call_with_a_then_answers_and_runs_it_once(world):
    machine, _counted = world

    async def run():
        program, seen = _program(), []
        answer = await program.call("echo", {"n": 1}, _then(seen))
        assert answer["ok"] is True and answer["args"] == {"n": 1}
        assert seen == [answer] and not program._pieces
        await program.close()
    asyncio.run(run())


def test_a_call_stopped_while_the_program_works_still_runs_its_then(world):
    machine, _counted = world

    async def run():
        program, seen = _program(), []
        assert (await program.call("echo", {}))["ok"] is True
        machine.hold_answers = True
        await _stopped(program.call("echo", {"n": 2}, _then(seen)), lambda: machine.held_answers)
        assert seen == []                       # the program has not answered yet
        machine.hold_answers = False
        machine.release()
        await _pieces_done(program)
        assert [a["args"] for a in seen] == [{"n": 2}]      # once, with the program's own answer
        await program.close()
    asyncio.run(run())


def test_a_call_with_a_then_stopped_before_its_turn_asks_nothing_and_runs_no_then(world):
    machine, _counted = world

    async def run():
        program, seen = _program(), []
        assert (await program.call("echo", {}))["ok"] is True
        machine.hold_answers = True
        first = asyncio.ensure_future(program.call("echo", {"n": 1}))
        await _until(lambda: machine.held_answers)
        await _stopped(program.call("echo", {"n": 2}, _then(seen)), lambda: True)
        machine.hold_answers = False
        machine.release()
        assert (await first)["args"] == {"n": 1}
        assert (await program.call("echo", {"n": 3}))["args"] == {"n": 3}
        assert machine.asked() == [{}, {"n": 1}, {"n": 3}] and seen == []
        await program.close()
    asyncio.run(run())


def test_a_call_with_a_then_stopped_while_its_program_starts_asks_nothing_and_runs_no_then(world):
    machine, _counted = world

    async def run():
        gate = _hold_opens(machine)
        program, seen = _program(), []
        await _stopped(program.call("echo", {"n": 1}, _then(seen)), lambda: machine.of("held"))
        gate.set()
        await program.settled()
        assert (await program.call("echo", {"n": 2}))["args"] == {"n": 2}
        assert machine.asked() == [{"n": 2}] and seen == []
        await program.close()
    asyncio.run(run())


def test_the_next_call_is_asked_only_after_a_stopped_calls_then_has_ended(world):
    machine, _counted = world

    async def run():
        program, order = _program(), []
        go = asyncio.Event()

        async def slow(answer):
            order.append("then begins")
            await go.wait()
            order.append("then ends")

        assert (await program.call("echo", {}))["ok"] is True
        await _stopped(program.call("echo", {"n": 1}, slow), lambda: "then begins" in order)
        machine.on_call = lambda: order.append("the next call is asked")
        second = asyncio.ensure_future(program.call("echo", {"n": 2}))
        await asyncio.sleep(0.2)
        assert order == ["then begins"]         # the turn is kept until the record is whole
        go.set()
        assert (await asyncio.wait_for(second, 5))["args"] == {"n": 2}
        assert order == ["then begins", "then ends", "the next call is asked"]
        await program.close()
    asyncio.run(run())


def test_a_then_that_raises_is_told_and_the_call_still_answers(world, caplog):
    async def run():
        program = _program()

        async def broken(answer):
            raise RuntimeError("the record could not be written")

        answer = await program.call("echo", {"n": 1}, broken)
        assert answer["ok"] is True
        assert (await program.call("echo", {"n": 2}))["ok"] is True       # the turn was given back
        await program.close()
    with caplog.at_level("ERROR", logger="mcp-manager"):
        asyncio.run(run())
    assert any("what follows its answer failed" in r.getMessage() for r in caplog.records)


def test_a_then_runs_only_for_a_call_that_asked_the_program(world):
    machine, _counted = world

    async def run():
        program, seen = _program(), []
        machine.refuse = "no room"
        answer = await program.call("echo", {}, _then(seen))
        assert answer["code"] == sp.NOT_STARTED and seen == [] and not sp.asked(answer)
        await program.close()
    asyncio.run(run())


def test_a_close_waits_for_a_piece_in_flight(world):
    machine, _counted = world

    async def run():
        program, seen = _program(), []
        assert (await program.call("echo", {}))["ok"] is True
        machine.hold_answers = True
        await _stopped(program.call("echo", {"n": 2}, _then(seen)), lambda: machine.held_answers)
        waiting = asyncio.ensure_future(program.call("echo", {"n": 3}, _then(seen)))
        closing = asyncio.ensure_future(program.close())
        await asyncio.sleep(0.1)
        assert not closing.done() and seen == []        # the close waits for the write that was asked
        machine.hold_answers = False
        machine.release()
        await asyncio.wait_for(closing, 5)
        assert [a["args"] for a in seen] == [{"n": 2}] and not program._pieces and not _left_running()
        # Nothing new was asked while the close waited: the call behind the piece found the session closed.
        assert await asyncio.wait_for(waiting, 5) == {"ok": False, "code": sp.NOT_RUNNING}
        assert machine.asked() == [{}, {"n": 2}]
    asyncio.run(run())


def test_a_close_that_is_cut_while_it_waits_for_a_piece_still_stops_the_program(world):
    """Whoever asked for the close may be stopped while the close waits for
    a piece. The program is told to stop all the same: the session has left
    the platform's books by then, so nothing else would close it again."""
    machine, _counted = world

    async def run():
        program, seen = _program(), []
        assert (await program.call("echo", {}))["ok"] is True
        machine.silent = True                                # the piece's answer does not come
        await _stopped(program.call("echo", {"n": 2}, _then(seen)), lambda: len(machine.asked()) == 2)
        closing = asyncio.ensure_future(program.close())
        await asyncio.sleep(0.1)
        assert not closing.done() and not machine.of("tool_close")      # the close waits for the piece
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 5)
        # The cut close still told the program to stop, and its owner has ended.
        assert machine.of("tool_close") and not _left_running() and tool_relay.live_count() == 0
        await _pieces_done(program)
        assert seen == [{"ok": False, "code": sp.CLOSED}] and not program._steps
        assert await program.call("echo", {}) == {"ok": False, "code": sp.NOT_RUNNING}
    asyncio.run(run())


def test_a_close_under_a_piece_whose_program_hangs_ends_it_as_closed(world, monkeypatch):
    machine, _counted = world
    monkeypatch.setattr(sp, "PIECE_CLOSE_WAIT_S", 0.2)

    async def run():
        program, seen = _program(), []
        assert (await program.call("echo", {}))["ok"] is True
        machine.silent = True
        await _stopped(program.call("echo", {"n": 2}, _then(seen)), lambda: len(machine.asked()) == 2)
        waiting = asyncio.ensure_future(program.call("echo", {"n": 3}, _then(seen)))
        await asyncio.wait_for(program.close(), 5)
        await _pieces_done(program)
        # What the program did is not known, and nothing new was asked after the close.
        assert seen == [{"ok": False, "code": sp.CLOSED}] and sp.asked(seen[0])
        assert await asyncio.wait_for(waiting, 5) == {"ok": False, "code": sp.NOT_RUNNING}
        assert machine.asked() == [{}, {"n": 2}] and not _left_running()
    asyncio.run(run())


def test_an_answer_in_hand_is_used_when_the_session_closed_meanwhile(world):
    machine, _counted = world

    async def run():
        program, seen = _program(), []
        assert (await program.call("echo", {}))["ok"] is True
        machine.on_call = lambda: setattr(program, "closed", True)
        answer = await program.call("echo", {"n": 2}, _then(seen))
        assert answer["ok"] is True and answer["args"] == {"n": 2} and seen == [answer]
        machine.on_call = None
        await program.close()
    asyncio.run(run())


def test_each_answer_of_its_own_says_whether_the_program_was_asked(world, monkeypatch):
    machine, _counted = world

    async def run():
        # Asked nothing: the machine refused the start, the program rests, the session is closed.
        program = _program()
        machine.refuse = "no room"
        for _ in range(SPEC.max_failures):
            assert not sp.asked(await program.call("echo", {}))                # not started
        resting = await program.call("echo", {})
        assert resting["code"] == sp.RESTING and not sp.asked(resting)
        await program.close()
        closed = await program.call("echo", {})
        assert closed["code"] == sp.NOT_RUNNING and not sp.asked(closed)
        machine.refuse = ""
        # Asked, with no answer of its own: past the cap, ended, no answer, closed under the call.
        program = _program()
        machine.silent = True
        timed_out = await program.call("echo", {})
        assert timed_out["code"] == sp.TIMED_OUT and sp.asked(timed_out)
        machine.silent, machine.dies = False, True
        ended = await program.call("echo", {})
        assert ended["code"] == sp.ENDED and sp.asked(ended)
        machine.dies, machine.garbled = False, True
        garbled = await program.call("echo", {})
        assert garbled["code"] == sp.GARBLED and sp.asked(garbled)
        machine.garbled = False
        await program.close()
        # An answer of the program's own is never "not known".
        assert not sp.asked({"ok": True}) and not sp.asked({"ok": False, "code": "missing"})
        assert not sp.asked({"ok": True, "code": sp.ENDED})
        assert sp.asked({"ok": False, "code": sp.CLOSED}) and not sp.asked({"ok": False, "code": sp.UNSENT})
    asyncio.run(run())


def test_a_call_that_does_not_encode_as_text_is_not_sent(world):
    machine, _counted = world

    async def run():
        program, seen = _program(), []
        for bad in ({"text": "a\ud800b"}, {"deep": ["fine", {"k": "\udfff"}]}, {"\ud800": 1}):
            answer = await program.call("echo", bad, _then(seen))
            assert answer == {"ok": False, "code": sp.UNSENT} and not sp.asked(answer)
        assert machine.asked() == [] and machine.of("tool_open") == [] and seen == []
        assert (await program.call("echo", {"text": "καλά 😀"}))["args"] == {"text": "καλά 😀"}
        await program.close()
    asyncio.run(run())


def test_a_megabyte_of_the_worst_text_goes_through_the_relay_and_too_much_is_unsent(world):
    machine, _counted = world
    megabyte = 1024 * 1024

    async def run():
        program, seen = _program(dataclasses.replace(SPEC, call_timeout_s=30)), []
        # Control characters are escaped twice on the way: seven bytes each, under the relay's frame.
        answer = await program.call("size", {"text": "\x01" * megabyte}, _then(seen))
        assert answer == {"ok": True, "n": megabyte}
        too_much = await program.call("size", {"text": "\x01" * (2 * megabyte)}, _then(seen))
        assert too_much == {"ok": False, "code": sp.UNSENT} and not sp.asked(too_much)
        assert seen == [answer, too_much]       # the relay was asked to send it: the piece ran, the program got nothing
        assert len(machine.asked()) == 1
        assert (await program.call("size", {"text": "x"}))["n"] == 1         # the program lives on
        # An answer that holds the relay's own words is still the program's answer.
        echoed = await program.call("echo", {"text": tool_relay.TOO_LARGE})
        assert echoed["ok"] is True and echoed["args"] == {"text": tool_relay.TOO_LARGE}
        await program.close()
    asyncio.run(run())


def test_a_stop_from_the_platforms_loop_never_splits_a_piece(world):
    """The caller on one loop, the program on the real tool loop: a stop
    crosses between them and lands a pass later. Whenever the program was
    asked, its ``then`` ran. Whenever it was not, no ``then`` ran."""
    machine, _counted = world

    async def run():
        program = sp.SandboxProgram(dataclasses.replace(SPEC, call_timeout_s=30), session_id=SESSION,
                                    sandbox_builder=_builder(), agent_name="pa", machine_id=MACHINE,
                                    main_loop=asyncio.get_running_loop())
        manager = direct_mcp.AgentMCPManager("pa", session_id=SESSION, machine_id=MACHINE)
        manager.programs[SPEC.name] = program
        assert (await sp.call(manager, SPEC.name, "echo", {"n": -1}))["ok"] is True
        recorded: list = []
        platform = asyncio.get_running_loop()
        elsewhere: list = []

        async def then(answer):
            recorded.append(answer["args"]["n"])
            if asyncio.get_running_loop() is not platform:
                elsewhere.append(answer["args"]["n"])

        stopped = 0
        for n in range(150):
            call = asyncio.ensure_future(sp.call(manager, SPEC.name, "echo", {"n": n}, then=then))
            await asyncio.sleep((n % 7) * 0.0003)
            call.cancel()
            try:
                await call
            except asyncio.CancelledError:
                stopped += 1
            await _until(lambda: not program._pieces)
            await asyncio.sleep(0.002)
        asked = [a["n"] for a in machine.asked() if a["n"] >= 0]
        assert sorted(recorded) == sorted(asked)        # asked and recorded are the same calls
        assert elsewhere == []                          # what follows an answer runs on the platform's own loop
        assert stopped > 10                             # the stops were real
        await direct_mcp._run_on_mcp_loop(program.close())
    asyncio.run(run())


# -- a restart that raises under a call that asked the program ------------------
#
# The restart after a slow call, and after a program that ended, is the next
# call's business. When it raises, this call still answers what is known of
# it, and a piece still hands that to its ``then``.

def test_a_piece_that_times_out_while_its_restart_raises_hands_its_then_what_is_known(world, monkeypatch):
    machine, counted = world

    async def run():
        program, seen = _program(), []
        assert (await program.call("echo", {}))["ok"] is True
        _no_give_back(monkeypatch, counted)
        machine.silent, machine.hold_close = True, True
        answer = await asyncio.wait_for(program.call("echo", {"n": 2}, _then(seen)), 10)
        assert answer == {"ok": False, "code": sp.TIMED_OUT, "seconds": 1} and sp.asked(answer)
        assert seen == [answer] and not program._pieces
        machine.silent, machine.hold_close = False, False
        # The turn was given back, and the next start was armed.
        assert (await asyncio.wait_for(program.call("echo", {"n": 3}), 5))["args"] == {"n": 3}
        await program.close()
    asyncio.run(run())


@pytest.mark.parametrize("way", ["a call past its cap", "a program that ended"])
def test_a_call_stopped_while_its_restart_runs_ends_as_stopped(world, way):
    """The restart's own failure never becomes the call's. A stop of the
    call still does: it ends the wait for the restart, and the restart
    runs to its end."""
    machine, _counted = world

    async def run():
        program = _program()
        assert (await program.call("echo", {}))["ok"] is True
        machine.hold_close = True                            # the restart takes a while
        if way == "a call past its cap":
            machine.silent = True
        else:
            machine.on_call = lambda: setattr(program, "dead", True)
        call = asyncio.ensure_future(program.call("echo", {}))
        await _until(lambda: machine.of("tool_close"))
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        machine.silent, machine.hold_close, machine.on_call = False, False, None
        await program.settled()
        assert (await asyncio.wait_for(program.call("echo", {"n": 2}), 5))["args"] == {"n": 2}
        await program.close()
    asyncio.run(run())


def test_an_answer_in_hand_is_kept_when_the_restart_after_it_raises(world, monkeypatch):
    machine, counted = world

    async def run():
        program, seen = _program(), []
        assert (await program.call("echo", {}))["ok"] is True
        _no_give_back(monkeypatch, counted)
        # The program answers and is found ended right after: its restart then raises.
        machine.on_call = lambda: setattr(program, "dead", True)
        answer = await asyncio.wait_for(program.call("echo", {"n": 2}, _then(seen)), 10)
        assert answer["ok"] is True and answer["args"] == {"n": 2} and seen == [answer]
        machine.on_call = None
        plain = await asyncio.wait_for(program.call("echo", {"n": 3}), 5)        # a call with no ``then`` too
        assert plain["args"] == {"n": 3}
        await program.close()
    asyncio.run(run())


def test_a_program_that_ended_unanswered_reads_as_ended_when_its_restart_raises(world, monkeypatch):
    machine, counted = world

    async def run():
        program, seen = _program(), []
        assert (await program.call("echo", {}))["ok"] is True
        _no_give_back(monkeypatch, counted)
        machine.dies = True
        answer = await asyncio.wait_for(program.call("echo", {"n": 2}, _then(seen)), 10)
        assert answer == {"ok": False, "code": sp.ENDED} and seen == [answer]
        machine.dies = False
        await program.close()
    asyncio.run(run())
