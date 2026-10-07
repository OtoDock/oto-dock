"""Check 1: a hundred sessions streaming at once, each watched by a person's
dashboard, on one loop.

Every session's turn comes from a pipe fed by a separate process, so the
loop pays the pipe reads, the stream-json parsing and the Claude CLI
translation as it does for a real engine; the turn runs through the real
``drive_turns`` and ``turn_end``, the real pump, and the real dashboard
socket (a ``resume_chat`` that attaches to the running pump). Twenty people
hold five sockets each, as five tabs would.
"""

import asyncio
import contextlib
import json
import os
import time

import pytest

pytestmark = [pytest.mark.loadtest, pytest.mark.timeout(900, method="thread")]

SESSIONS = 100
PEOPLE = 20
RATE = 30.0          # deltas per second: token-by-token, the harder stream shape
DELTAS = 600         # 20 s turns
STAGGER_S = 5.0      # starts spread over 5 s: a 15 s plateau with all 100 streaming
CLIENTS = 4          # client processes, 25 sockets each
AGENT = "lt-agent"


def _seed() -> tuple[list[dict], dict]:
    """One agent, 20 people with editor rights on it, five chats each (and a
    warm-up chat): the rows a dashboard resume reads."""
    from auth.password import hash_password
    from auth.providers import create_session_jwt
    from storage import database as task_store
    from storage.agents import agent_store
    from storage.identity import db_users
    from storage.pg import get_conn

    agent_store.create_agent(AGENT, "Load test", created_by="user-admin")
    pw = hash_password("load-test-password")
    people = []
    for p in range(PEOPLE):
        email = f"lt{p}@loadtest.local"
        sub = db_users.create_local_user(email, f"lt{p}", f"Load {p}", "member", pw)
        db_users.set_user_agents(sub, [AGENT], "user-admin", agent_roles={AGENT: "editor"})
        people.append({"sub": sub, "cookie": create_session_jwt(sub, email, f"lt{p}", "member")})
    with get_conn() as conn:
        # A cookie minted in the same second as the password would read as
        # older than it (iat is whole seconds).
        conn.execute("UPDATE users SET password_changed_at = NULL WHERE sub = ANY(%s)",
                     ([p["sub"] for p in people],))
        conn.commit()

    def chat(cid: str, person: dict) -> dict:
        task_store.create_chat(cid, person["sub"], AGENT, execution_path="claude-code-cli")
        task_store.update_chat(cid, title="load test", title_generated=True)
        return {"chat": cid, "session": f"{cid}-session", "sub": person["sub"], "cookie": person["cookie"]}

    sessions = [chat(f"lt-chat-{i}", people[i % PEOPLE]) for i in range(SESSIONS)]
    return sessions, chat("lt-warm", people[0])


async def _round(tmp_path, host: str, port: int, sessions: list[dict], *, deltas: int,
                 stagger: float, clients: int, name: str, measure: bool) -> dict:
    """One round of turns: a pump and a producer per session, the feeder and
    the client processes, until every viewer has its ``done``."""
    from core.events import stream_pump
    from core.events.common_events import DONE, ERROR, PRODUCER_DONE, CommonEvent
    from core.layers.cli.helpers import ClaudeStreamChunk
    from core.layers.cli.layer import cli_chunk_to_events
    from core.layers.cli.translator import ClaudeCLIEventTranslator
    from core.session import session_events
    from core.session.session_state import _chat_streaming_state, get_permission_queue
    from services.engines import subscription_pool
    from tests.loadtest import _harness as h

    loop = asyncio.get_running_loop()
    marks = {s["chat"]: {} for s in sessions}
    pumps, transports, write_fds, helpers = [], [], [], []

    def run_turn_for(reader: asyncio.StreamReader, s: dict):
        translator = ClaudeCLIEventTranslator(s["session"])
        mark = marks[s["chat"]]

        async def run_turn(_message: str):
            while True:
                line = await reader.readline()
                if not line:
                    return
                data = json.loads(line)
                for chunk in translator.feed(data):
                    if chunk.event_type == "rate_limit":
                        continue
                    for event in cli_chunk_to_events(chunk):
                        yield event
                if data.get("type") == "result":
                    mark["t_end"] = data["t_end"]
                    mark["result_read"] = time.monotonic()
                    # The CLI read loop ends a turn with an is_done chunk
                    # after the result; drive_turns holds its DONE for the
                    # turn end.
                    for event in cli_chunk_to_events(ClaudeStreamChunk(
                            is_done=True, session_id=translator.actual_session_id)):
                        yield event
                    return

        return run_turn

    async def produce(eq: asyncio.Queue, s: dict, run_turn) -> None:
        # The dashboard producer's shape: the layer's turn loop, then
        # PRODUCER_DONE in a finally.
        mark = marks[s["chat"]]
        try:
            async for event in session_events.drive_turns(s["session"], "claude-code-cli", "go", run_turn):
                if event.type == DONE:
                    # Released by drive_turns only after turn_end judged it.
                    mark["judged"] = time.monotonic()
                await eq.put(event)
        except Exception as exc:
            await eq.put(CommonEvent(type=ERROR, data={"message": str(exc)}))
        finally:
            mark["producer_done"] = time.monotonic()
            await eq.put(CommonEvent(type=PRODUCER_DONE, data={}))

    try:
        for s in sessions:
            r, w = os.pipe()
            write_fds.append(w)
            reader = asyncio.StreamReader(limit=200 * 1024 * 1024)
            transport, _ = await loop.connect_read_pipe(
                lambda reader=reader: asyncio.StreamReaderProtocol(reader), os.fdopen(r, "rb", 0))
            transports.append(transport)
            subscription_pool._session_subscriptions[s["session"]] = "lt-subscription"
            eq: asyncio.Queue = asyncio.Queue()
            producer = loop.create_task(produce(eq, s, run_turn_for(reader, s)))
            pump = stream_pump.ChatStreamPump(
                chat_id=s["chat"], session_id=s["session"], producer=producer, event_queue=eq,
                perm_queue=get_permission_queue(s["session"]), scope="user",
                chat_owner=s["sub"], chat_agent=AGENT)
            forward, mark = pump._forward, marks[s["chat"]]

            async def stamped(item, forward=forward, mark=mark):
                if item.get("pump_type") == "all_done":
                    mark["all_done"] = time.monotonic()
                await forward(item)

            pump._forward = stamped
            stream_pump._active_pumps[s["chat"]] = pump
            pump.start()
            pumps.append(pump)

        feeder = await h.spawn(h.FEEDER, str(RATE), str(deltas), str(stagger),
                               ",".join(map(str, write_fds)), pass_fds=write_fds)
        helpers.append(feeder)
        for w in write_fds:
            os.close(w)
        write_fds.clear()
        await h.read_line(feeder, 60)

        spec = {"url": f"ws://{host}:{port}/ws/dashboard", "origin": f"http://{host}:{port}",
                "timeout": deltas / RATE + stagger + 120}
        groups = [sessions[i::clients] for i in range(clients)]
        viewers = []
        for g, group in enumerate(groups):
            path = tmp_path / f"{name}-stream-{g}.json"
            path.write_text(json.dumps({**spec, "conns": [{"chat": s["chat"], "cookie": s["cookie"]}
                                                          for s in group]}))
            viewers.append(await h.spawn(h.CLIENT, "stream", str(path)))
            helpers.append(viewers[-1])
        for v in viewers:
            await h.read_line(v, 120)
        async with asyncio.timeout(60):
            while not all(p._ws_queues for p in pumps):
                await asyncio.sleep(0.05)

        async with h.Window() if measure else contextlib.nullcontext() as window:
            t_go = time.monotonic()
            await h.tell(feeder, "go")
            results = [await h.read_line(v, spec["timeout"] + 30) for v in viewers]
            fed = await h.read_line(feeder, 60)
        return {"t_go": t_go, "results": results, "fed": fed, "marks": marks, "window": window}
    finally:
        for proc in helpers:
            await h.end(proc)
        for w in write_fds:
            os.close(w)
        for t in transports:
            t.close()
        for pump in pumps:
            pump.producer.cancel()
            if pump._task is not None and not pump._task.done():
                pump._task.cancel()
            stream_pump._active_pumps.pop(pump.chat_id, None)
            _chat_streaming_state.pop(pump.chat_id, None)
        for s in sessions:
            subscription_pool._session_subscriptions.pop(s["session"], None)


def test_a_hundred_sessions_stream_to_their_viewers(tmp_path):
    from tests.loadtest import _harness as h

    sessions, warm = _seed()

    async def main():
        async with h.production_loop(tmp_path):
            async with h.serve_app() as (host, port, _router):
                warmed = await _round(tmp_path, host, port, [warm], deltas=20, stagger=0.0,
                                      clients=1, name="warm", measure=False)
                run = await _round(tmp_path, host, port, sessions, deltas=DELTAS, stagger=STAGGER_S,
                                   clients=CLIENTS, name="run", measure=True)
                return warmed, run

    warmed, run = h.run_loop(main, 600)
    warm_chat = warmed["results"][0]["chats"]["lt-warm"]
    assert warm_chat["received"] == 20 and warm_chat["done_rx"] is not None, warm_chat
    assert "judged" in warmed["marks"]["lt-warm"], warmed["marks"]
    window, marks = run["window"], run["marks"]
    chats: dict = {}
    for r in run["results"]:
        chats.update(r["chats"])
    plateau = (run["t_go"] + STAGGER_S, run["t_go"] + (DELTAS - 1) / RATE)

    ends = {}
    for cid, got in chats.items():
        m = marks[cid]
        if got["done_rx"] is None or "t_end" not in m:
            continue
        if "judged" not in m or "all_done" not in m:
            continue
        ends[cid] = {"done": got["done_rx"] - m["t_end"],
                     "read": m["result_read"] - m["t_end"],
                     "turn_end": m["judged"] - m["result_read"],
                     "drain": m["all_done"] - m["judged"],
                     "viewer": got["done_rx"] - m["all_done"]}

    def spread(key):
        return h.dist([e[key] for e in ends.values()])

    frames = [c["frames"] for c in chats.values()]
    h.record("streaming", sessions=SESSIONS, people=PEOPLE, rate=RATE, deltas=DELTAS, stagger_s=STAGGER_S,
             plateau_loop=window.ticker.stats(*plateau),
             done_after_end=spread("done"),
             done_split={k: spread(k) for k in ("read", "turn_end", "drain", "viewer")},
             delta_latency=[r["latency"] for r in run["results"]],
             frames_per_viewer_per_s=round(sum(frames) / len(frames) / (DELTAS / RATE), 1) if frames else 0,
             frame_types=[r["frame_types"] for r in run["results"]],
             clients=[r["client"] for r in run["results"]], feeder=run["fed"],
             warm_up_done_ms=h.ms(warm_chat["done_rx"] - warmed["marks"]["lt-warm"]["t_end"]),
             **window.summary())

    window.ticker.assert_covered()
    assert len(chats) == SESSIONS, len(chats)
    broken = {cid: c for cid, c in chats.items()
              if c["received"] != DELTAS or not c["in_order"] or c["histories"] != 1 or c["done_rx"] is None}
    assert not broken, dict(list(broken.items())[:5])
    for r in run["results"]:
        assert r["client"]["lag_max_ms"] < h.HELPER_LAG_MAX_S * 1000, (
            "a client fell behind: this run measured the client, not the proxy", r["client"])
    assert run["fed"]["rate_kept_min"] >= 0.95, run["fed"]
    plateau_stats = window.ticker.stats(*plateau)
    assert plateau_stats["p99_ms"] < h.STREAM_P99_S * 1000, plateau_stats
    assert window.ticker.stats()["max_ms"] < h.STREAM_MAX_S * 1000, window.ticker.stats()
    assert len(ends) == SESSIONS, "a turn ended without its turn_end or all_done"
    assert max(e["viewer"] for e in ends.values()) < h.DONE_AFTER_ROWS_S, spread("viewer")
    assert max(e["done"] for e in ends.values()) < h.DONE_AFTER_END_S, spread("done")
