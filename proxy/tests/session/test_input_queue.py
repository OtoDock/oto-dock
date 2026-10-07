"""The chat's input queue (``core/events/input_queue``): one registry per
chat, write-through to ``chat_input_queue``, loaded from the table on the
first touch, a duplicate acked before and after acceptance, the claim that
keeps two turn starters apart, the sequence a return is bounded by, the
fan-out to the chat's audience and the items' authors with the composer
fields on the author's copy only, the return rules for a card, the drops by
connection and by person, and the accept job that turns waiting rows into
user messages in one transaction."""

import asyncio
import json
import uuid

import pytest

from core.events import chat_writer, input_queue
from core.events.common_events import TurnInput
from storage import database as task_store
from ws import wire_events as wire


def _chat(temp_db, agent: str = "agent-a", sub: str = "user-admin") -> str:
    cid = str(uuid.uuid4())
    temp_db.create_chat(cid, sub, agent, "default")
    return cid


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    input_queue._registry.clear()
    from services.scheduler import shared
    monkeypatch.setattr(shared, "_shutting_down", False)
    yield
    input_queue._registry.clear()


@pytest.fixture
def pushed(monkeypatch):
    """Every live push by person; ``reach`` names the subs with a socket."""
    from services.notifications import notification_manager
    out: dict = {"frames": [], "reach": {"user-admin"}}

    def push_live(sub, frame, **_kw):
        out["frames"].append((sub, frame))
        return 1 if sub in out["reach"] else 0
    monkeypatch.setattr(notification_manager, "push_live", push_live)
    return out


@pytest.mark.asyncio
async def test_add_remove_and_snapshot_write_through(temp_db):
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    status, qi, index = await q.add("q1", "user-admin", TurnInput("first"), origin_conn="c1")
    assert (status, index, qi.queue_id, qi.seq) == (input_queue.QUEUED, 0, "q1", 1)
    status, _qi, index = await q.add("q2", "user-admin", TurnInput(
        "second", cli_text="second /tmp/x.png", image_meta=[{"name": "x.png", "path": "u/x.png"}]))
    assert (status, index) == (input_queue.QUEUED, 1)
    await chat_writer.drain(cid, timeout=5)
    rows = task_store.list_chat_input_queue(cid)
    assert [r["queue_id"] for r in rows] == ["q1", "q2"]
    assert rows[1]["cli_text"] == "second /tmp/x.png"
    assert json.loads(rows[1]["event_data"]) == {"images": [{"name": "x.png", "path": "u/x.png"}]}
    assert q.snapshot() == [
        {"queue_id": "q1", "text": "first", "author_sub": "user-admin"},
        {"queue_id": "q2", "text": "second", "author_sub": "user-admin",
         "images": [{"name": "x.png", "path": "u/x.png"}]},
    ]
    assert await q.remove(qi) == 0
    await chat_writer.drain(cid, timeout=5)
    assert [r["queue_id"] for r in task_store.list_chat_input_queue(cid)] == ["q2"]
    assert await q.remove(qi) == -1


@pytest.mark.asyncio
async def test_a_duplicate_is_acked_before_and_after_acceptance(temp_db):
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    await q.add("q1", "user-admin", TurnInput("once"))
    status, qi, index = await q.add("q1", "user-admin", TurnInput("once again"))
    assert status == input_queue.DUPLICATE and qi.item.text == "once" and index == 0
    # Another person's message under the same id is a message of its own.
    status, other, _ = await q.add("q1", "user-other", TurnInput("mine"))
    assert status == input_queue.QUEUED and other.queue_id != "q1"
    taken = q.take("user-admin")
    assert await q.accept_job(taken)
    status, qi, _ = await q.add("q1", "user-admin", TurnInput("once more"))
    assert status == input_queue.ACCEPTED and qi is None


@pytest.mark.asyncio
async def test_the_cap_refuses(temp_db, monkeypatch):
    monkeypatch.setattr(input_queue, "CAP", 2)
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    await q.add("a", "user-admin", TurnInput("a"))
    await q.add("b", "user-admin", TurnInput("b"))
    assert (await q.add("c", "user-admin", TurnInput("c")))[0] == input_queue.FULL


@pytest.mark.asyncio
async def test_accept_writes_one_row_per_message_and_drops_the_queue_rows(temp_db):
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    await q.add("q1", "user-admin", TurnInput("first"))
    await q.add("q2", "user-b", TurnInput("second", files=[{"path": "u/a.pdf", "name": "a.pdf"}]))
    taken = q.take()
    ids = await q.accept_job(taken)
    assert len(ids) == 2 and q.items == []
    assert task_store.list_chat_input_queue(cid) == []
    rows = [m for m in task_store.get_chat_messages(cid) if m["role"] == "user"]
    assert [r["content"] for r in rows] == ["first", "second"]
    assert [r["author_sub"] for r in rows] == ["user-admin", "user-b"]
    assert json.loads(rows[1]["event_data"]) == {"files": [{"path": "u/a.pdf", "name": "a.pdf"}],
                                                "queue_id": "q2"}
    assert {"q1", "q2"} <= set(q.accepted)


@pytest.mark.asyncio
async def test_rows_survive_a_new_registry_in_queue_order(temp_db):
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    await q.add("q1", "user-admin", TurnInput("first", cli_text="first /x"))
    await q.add("q2", "user-admin", TurnInput("second"))
    await chat_writer.drain(cid, timeout=5)
    input_queue._registry.clear()
    fresh = await input_queue.loaded(cid)
    assert fresh is not q
    assert [qi.queue_id for qi in fresh.items] == ["q1", "q2"]
    assert fresh.items[0].item.cli_text == "first /x"
    assert [qi.seq for qi in fresh.items] == [1, 2]


@pytest.mark.asyncio
async def test_the_claim_keeps_two_starters_apart(temp_db, monkeypatch):
    from core.events import stream_pump
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    first = q.try_claim()
    assert first is not None
    assert q.try_claim() is None
    q.release(first)
    live = type("P", (), {"is_done": False})()
    monkeypatch.setitem(stream_pump._active_pumps, cid, live)
    assert q.try_claim() is None
    live.is_done = True
    second = q.try_claim()
    assert second is not None
    assert input_queue.busy(cid) is True
    q.release(second)
    assert input_queue.busy(cid) is False


@pytest.mark.asyncio
async def test_only_the_holder_frees_the_claim(temp_db, monkeypatch):
    """A starter that already released (or never held the chat) and
    releases again late never frees another starter's claim: two turns
    would start on one chat."""
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    stale = q.try_claim()
    q.release(stale)
    held = q.try_claim()
    q.release(stale)
    q.release(None)
    assert q.claimed and q.try_claim() is None
    q.release(held)
    assert not q.claimed


@pytest.mark.asyncio
async def test_a_release_with_messages_left_and_no_pump_arms_their_delivery(temp_db,
                                                                         monkeypatch):
    """The holder's start failed (a limit, a refused heal) while another
    person's message waits: the release sends it on instead of leaving it
    until some later touch."""
    armed = []
    monkeypatch.setattr(input_queue, "arm", armed.append)
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    claim = q.try_claim()
    q.release(claim)
    assert armed == []
    await q.add("q1", "user-b", TurnInput("waiting"))
    claim = q.try_claim()
    q.release(claim)
    assert armed == [cid]


@pytest.mark.asyncio
async def test_a_failed_pump_keeps_the_chat_until_its_return(temp_db, monkeypatch):
    """A pump that failed is done but still registered while its lane
    drains: no starter may take the messages its end is about to return."""
    from core.events import stream_pump
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    failed = type("P", (), {"is_done": True, "_return_upto": 3})()
    monkeypatch.setitem(stream_pump._active_pumps, cid, failed)
    assert q.try_claim() is None
    failed._return_upto = None
    assert q.try_claim() is not None


@pytest.mark.asyncio
async def test_a_deferred_delivery_with_another_author_waiting_does_not_re_arm(temp_db,
                                                                               monkeypatch):
    """The turn could not start (no session to resume, a live terminal):
    the delivery's own release leaves the queue to the next pump end or
    touch instead of arming the same delivery again every 2 s."""
    armed = []
    monkeypatch.setattr(input_queue, "arm", armed.append)
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    await q.add("q1", "user-a", TurnInput("a's"))
    await q.add("q2", "user-b", TurnInput("b's"))
    calls = []

    class _Conn:
        async def _deliver_queued(self, chat_id, taken):
            calls.append([qi.queue_id for qi in taken])
            return input_queue.DELIVERY_DEFERRED, "no session to resume"
    monkeypatch.setattr(input_queue, "_driver_for", lambda sub, items: _Conn())
    assert await input_queue.deliver(cid) is False
    assert calls == [["q1"]] and armed == []
    assert [qi.queue_id for qi in q.items] == ["q1", "q2"] and not q.claimed


@pytest.mark.asyncio
async def test_an_offboardings_return_leaves_the_card_for_an_unreached_teammate(temp_db, pushed):
    pushed["reach"] = set()
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    await q.add("q1", "user-c", TurnInput("teammate's"))
    await input_queue.return_all(cid, "revoked")
    await chat_writer.drain(cid, timeout=5)
    cards = [json.loads(m["event_data"]) for m in task_store.get_chat_messages(cid)
             if m["role"] == "event"]
    assert [c["reason"] for c in cards] == [wire.UNDELIVERED_STOPPED]


@pytest.mark.asyncio
async def test_take_up_to_a_sequence_leaves_the_later_items(temp_db):
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    await q.add("q1", "user-admin", TurnInput("before"))
    upto = q.seq
    await q.add("q2", "user-admin", TurnInput("after"))
    assert [qi.queue_id for qi in q.take(upto_seq=upto)] == ["q1"]
    assert [qi.queue_id for qi in q.items] == ["q2"]


@pytest.mark.asyncio
async def test_the_fan_out_reaches_the_audience_and_the_authors(temp_db, pushed):
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    _s, qi, idx = await q.add("q1", "user-b", TurnInput("from b"))
    input_queue.announce_queued(qi, idx)
    frames = {sub: f for sub, f in pushed["frames"]}
    assert set(frames) == {"user-admin", "user-b"}
    assert frames["user-admin"] == {"type": wire.QUEUED, "index": 0, "queue_id": "q1",
                                    "text": "from b", "author_sub": "user-b", "chat_id": cid}


@pytest.mark.asyncio
async def test_a_return_restores_the_authors_composer_only(temp_db, pushed):
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    await q.add("q1", "user-admin", TurnInput("mine", files=[{"path": "u/a", "name": "a"}]))
    await input_queue.return_all(cid, "stopped")
    [(sub, frame)] = pushed["frames"]
    assert sub == "user-admin" and frame["returned"] is True
    assert frame["text"] == "mine" and frame["files"] == [{"path": "u/a", "name": "a"}]
    assert frame["reason"] == "stopped" and frame["queue_ids"] == ["q1"]
    await chat_writer.drain(cid, timeout=5)
    assert task_store.list_chat_input_queue(cid) == []
    # The author had a socket: a Stop's return writes no card.
    assert [m for m in task_store.get_chat_messages(cid) if m["role"] == "event"] == []


@pytest.mark.asyncio
async def test_a_returned_copy_carries_the_text_the_others_do_not(temp_db, pushed):
    cid = _chat(temp_db, sub="agent::agent-a")
    q = await input_queue.loaded(cid)
    q.owner_sub, q.agent = "agent::agent-a", "agent-a"
    from services.notifications import notification_manager
    import unittest.mock as um
    with um.patch.object(notification_manager, "agent_audience",
                         return_value=["user-admin", "user-b"]):
        await q.add("q1", "user-b", TurnInput("b's"))
        await input_queue.return_all(cid, "cancel")
    frames = {sub: f for sub, f in pushed["frames"]}
    assert "text" not in frames["user-admin"] and "returned" not in frames["user-admin"]
    assert frames["user-b"]["text"] == "b's" and frames["user-b"]["returned"] is True


@pytest.mark.asyncio
async def test_a_server_decided_return_always_writes_the_card(temp_db, pushed):
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    await q.add("q1", "user-admin", TurnInput("lost turn"))
    await input_queue.return_all(cid, "turn_failed")
    await chat_writer.drain(cid, timeout=5)
    cards = [json.loads(m["event_data"]) for m in task_store.get_chat_messages(cid)
             if m["role"] == "event"]
    assert cards == [{"type": "system", "subtype": "undelivered_input",
                      "reason": wire.UNDELIVERED_TURN_FAILED, "message": "lost turn"}]


@pytest.mark.asyncio
async def test_a_stop_with_no_socket_of_the_author_writes_the_card(temp_db, pushed):
    pushed["reach"] = set()
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    await q.add("q1", "user-admin", TurnInput("typed then gone"))
    await input_queue.return_all(cid, "stopped")
    await chat_writer.drain(cid, timeout=5)
    cards = [json.loads(m["event_data"]) for m in task_store.get_chat_messages(cid)
             if m["role"] == "event"]
    assert [c["reason"] for c in cards] == [wire.UNDELIVERED_STOPPED]


@pytest.mark.asyncio
async def test_drops_by_connection_and_by_person_in_one_chat(temp_db, pushed):
    a, b = _chat(temp_db), _chat(temp_db)
    qa, qb = await input_queue.loaded(a), await input_queue.loaded(b)
    await qa.add("a1", "user-admin", TurnInput("phone"), origin_conn="phone")
    await qa.add("a2", "user-admin", TurnInput("laptop"), origin_conn="laptop")
    await qb.add("b1", "user-admin", TurnInput("phone b"), origin_conn="phone")
    await qb.add("b2", "user-admin", TurnInput("laptop b"), origin_conn="laptop")
    await chat_writer.drain(a, timeout=5)
    await chat_writer.drain(b, timeout=5)
    await input_queue.drop_where(conn_id="phone")
    assert [qi.queue_id for qi in qa.items] == ["a2"]
    assert [qi.queue_id for qi in qb.items] == ["b2"]
    await input_queue.drop_where(author_sub="user-admin", chat_id=a)
    assert qa.items == [] and [qi.queue_id for qi in qb.items] == ["b2"]
    assert {r["queue_id"] for r in task_store.list_chat_input_queue(b)} == {"b2"}
    assert all(f["reason"] == "revoked" and "text" not in f for _s, f in pushed["frames"])


@pytest.mark.asyncio
async def test_a_failed_end_returns_up_to_its_sequence_and_delivers_the_rest(temp_db, pushed,
                                                                            monkeypatch):
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    await q.add("q1", "user-admin", TurnInput("queued before the error"))
    upto = q.seq
    await q.add("q2", "user-admin", TurnInput("sent after the error"))
    delivered: list[str] = []

    async def deliver(chat_id):
        delivered.extend(qi.queue_id for qi in input_queue.get(chat_id).items)
        return True
    monkeypatch.setattr(input_queue, "deliver", deliver)
    input_queue.on_pump_end(cid, return_upto=upto, viewers=False)
    await asyncio.gather(*list(input_queue._tasks))
    assert delivered == ["q2"]
    cleared = [f for _s, f in pushed["frames"] if f["type"] == wire.QUEUE_CLEARED]
    assert [f["queue_ids"] for f in cleared] == [["q1"]]


@pytest.mark.asyncio
async def test_at_shutdown_the_queue_neither_returns_nor_delivers(temp_db, pushed, monkeypatch):
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    await q.add("q1", "user-admin", TurnInput("across the restart"))
    from services.scheduler import shared
    monkeypatch.setattr(shared, "_shutting_down", True)
    input_queue.on_pump_end(cid, return_upto=q.seq, viewers=False)
    assert not input_queue._tasks and [qi.queue_id for qi in q.items] == ["q1"]


@pytest.mark.asyncio
async def test_rows_restored_at_load_arm_the_delivery(temp_db, monkeypatch):
    cid = _chat(temp_db)
    q = await input_queue.loaded(cid)
    await q.add("q1", "user-admin", TurnInput("from before the restart"))
    await chat_writer.drain(cid, timeout=5)
    input_queue._registry.clear()
    armed: list[str] = []
    monkeypatch.setattr(input_queue, "arm", armed.append)
    await input_queue.loaded(cid)
    assert armed == [cid]
    await input_queue.loaded(cid)
    assert armed == [cid]


def test_the_chats_with_waiting_input_on_a_machine(temp_db):
    def _chat(cid, *, target="", sid=None, queued=True):
        task_store.create_chat(cid, "user-1", "pa")
        task_store.update_chat(cid, execution_target=target, session_id=sid)
        if queued:
            task_store.enqueue_chat_input(cid, "q-" + cid, "user-1", text="t", cli_text="t",
                                          event_data="", images="", origin_conn="")
    _chat("w-pinned", target="m-1")
    _chat("w-other", target="m-2")
    _chat("w-by-session", sid="s-on-m1")
    _chat("w-empty", target="m-1", queued=False)
    assert task_store.list_chats_with_waiting_input("m-1", ["s-on-m1"]) == [
        "w-by-session", "w-pinned"]
    assert task_store.list_chats_with_waiting_input("m-1", []) == ["w-pinned"]
