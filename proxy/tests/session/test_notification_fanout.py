"""The notification fan-out and the file_updated window
(services/notifications/notification_manager.py): the inbox rows of a
fan-out are one job and the recipients are delivered concurrently under a
bound; the install id is read once per process and shared by the first
callers; a burst of file_updated calls for one agent is one audience read
and one frame per path, merged by the rules a mixed burst needs, with an
agent-level frame past the per-flush cap.
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from services.notifications import notification_manager as nm
from storage.automation import notification_store

pytestmark = pytest.mark.asyncio


@dataclass
class _Conn:
    connection_id: str
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    active: bool = True
    platform: str = "web"
    away: bool = False
    live_queue: object = None
    focus: object = None
    focus_at: float = 0.0
    catalog_feeds: set = field(default_factory=set)


def _frames(c: _Conn) -> list[dict]:
    out = []
    while not c.queue.empty():
        out.append(c.queue.get_nowait())
    return out


@pytest.fixture(autouse=True)
def _clean():
    nm._user_connections.clear()
    nm._file_batches.clear()
    nm._install_id_value = ""
    yield
    nm._user_connections.clear()
    nm._file_batches.clear()
    nm._install_id_value = ""


# --- the fan-out -----------------------------------------------------------

async def test_a_fan_out_writes_its_rows_in_one_job_and_delivers_concurrently(monkeypatch):
    subs = [f"u{i}" for i in range(12)]
    monkeypatch.setattr(nm, "resolve_targets", lambda scope, target: list(subs))
    jobs: list[str] = []
    created: list[str] = []

    def _create_delivery(**kw):
        created.append(kw["user_sub"])
        return {"id": f"d-{kw['user_sub']}", "user_sub": kw["user_sub"], "title": kw["title"],
                "body": kw["body"], "severity": kw["severity"], "scope": kw["scope"],
                "source": kw["source"], "delivered_at": "t", "notification_id": None,
                "agent_slug": None, "chat_id": None, "href": ""}

    monkeypatch.setattr(notification_store, "create_delivery", _create_delivery)

    async def _run_db(fn, *a, **kw):
        jobs.append(getattr(fn, "__name__", "?"))
        return fn(*a, **kw)

    monkeypatch.setattr(nm, "run_db", _run_db)
    in_flight = 0
    peak = 0
    order: list[str] = []

    async def _deliver(user_sub, delivery):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        if user_sub == "u3":
            raise RuntimeError("one recipient fails")
        order.append(user_sub)
        in_flight -= 1

    monkeypatch.setattr(nm, "_deliver_to_user", _deliver)
    deliveries = await nm.fire_notification("t", "b", scope="agent", target="a")
    assert [d["user_sub"] for d in deliveries] == subs
    assert created == subs and jobs.count("_create_rows") == 1
    assert set(order) == set(subs) - {"u3"}
    assert 1 < peak <= nm._FAN_OUT_CONCURRENCY


async def test_the_install_id_is_read_once_and_shared_by_the_first_callers(monkeypatch):
    reads = 0

    def _read():
        nonlocal reads
        reads += 1
        return "install-1"

    async def _run_db(fn, *a, **kw):
        await asyncio.sleep(0.01)
        return fn(*a, **kw)

    monkeypatch.setattr(nm, "_install_id", _read)
    monkeypatch.setattr(nm, "run_db", _run_db)
    assert await asyncio.gather(*(nm._install_id_async() for _ in range(5))) == ["install-1"] * 5
    assert reads == 1
    assert await nm._install_id_async() == "install-1" and reads == 1


# --- the file_updated window -----------------------------------------------

def _wire_audience(monkeypatch, users: dict[str, _Conn]):
    from api.apps import catalog
    from core.remote import file_sync
    from storage import database as task_store
    deltas: list[tuple[list[str], str, str]] = []
    monkeypatch.setattr(catalog, "file_changed",
                        lambda told, agent, rel, **kw: deltas.append((sorted(told), rel, kw.get("source"))))
    monkeypatch.setattr(nm, "resolve_targets", lambda scope, target: list(users))
    monkeypatch.setattr(nm, "acting_role_of", lambda sub, agent: "editor")
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: sub)
    monkeypatch.setattr(file_sync, "should_sync_to_target", lambda rel, username, role: True)
    audience_reads = []

    async def _run_db(fn, *a, **kw):
        audience_reads.append(fn.__name__)
        return fn(*a, **kw)

    monkeypatch.setattr(nm, "run_db", _run_db)
    nm._user_connections.update({u: [c] for u, c in users.items()})
    return deltas, audience_reads


async def test_a_burst_is_one_audience_read_and_one_frame_per_path(monkeypatch):
    c1, c2 = _Conn("c1"), _Conn("c2")
    deltas, reads = _wire_audience(monkeypatch, {"u1": c1, "u2": c2})
    for i in range(5):
        await nm.broadcast_file_updated("agent-x", f"workspace/f{i}.md")
    assert _frames(c1) == []            # nothing before the window closes
    await nm.flush_file_updates()
    assert reads == ["_audience"]
    assert [f["rel_path"] for f in _frames(c1)] == [f"workspace/f{i}.md" for i in range(5)]
    assert [f["rel_path"] for f in _frames(c2)] == [f"workspace/f{i}.md" for i in range(5)]
    assert [d[1] for d in deltas] == [f"workspace/f{i}.md" for i in range(5)]
    assert all(d[0] == ["u1", "u2"] for d in deltas)


async def test_the_window_closes_by_itself(monkeypatch):
    c1 = _Conn("c1")
    _wire_audience(monkeypatch, {"u1": c1})
    monkeypatch.setattr(nm, "_FILE_UPDATED_WINDOW_S", 0.02)
    await nm.broadcast_file_updated("agent-x", "workspace/a.md")
    await asyncio.sleep(0.1)
    assert [f["rel_path"] for f in _frames(c1)] == ["workspace/a.md"]


async def test_merge_rules_disk_wins_pin_ors_and_exclusion_is_the_intersection(monkeypatch):
    saver, peer = _Conn("saver"), _Conn("peer")
    _wire_audience(monkeypatch, {"saver": saver, "peer": peer})
    # A Collabora save by `saver`, then an agent's disk write of the same file.
    await nm.broadcast_file_updated("agent-x", "workspace/doc.docx", source="collabora",
                                    exclude_user_sub="saver")
    await nm.broadcast_file_updated("agent-x", "workspace/doc.docx", source="disk")
    # A pin, then a content write of the pinned file.
    await nm.broadcast_file_updated("agent-x", "workspace/plan.md", pin=True)
    await nm.broadcast_file_updated("agent-x", "workspace/plan.md")
    # Excluded by every call: never told.
    await nm.broadcast_file_updated("agent-x", "workspace/own.md", exclude_user_sub="saver")
    await nm.broadcast_file_updated("agent-x", "workspace/own.md", exclude_user_sub="saver")
    await nm.flush_file_updates()
    by_path = {f["rel_path"]: f for f in _frames(saver)}
    assert by_path["workspace/doc.docx"]["source"] == "disk"      # the saver hears the agent's write
    assert by_path["workspace/plan.md"].get("pin") is True
    assert "workspace/own.md" not in by_path
    peer_paths = {f["rel_path"]: f for f in _frames(peer)}
    assert set(peer_paths) == {"workspace/doc.docx", "workspace/plan.md", "workspace/own.md"}


async def test_a_change_during_a_flush_opens_the_next_window(monkeypatch):
    c1 = _Conn("c1")
    _wire_audience(monkeypatch, {"u1": c1})
    gate = asyncio.Event()
    real_send = nm._send_file_updates

    async def _slow(agent, paths):
        await gate.wait()
        await real_send(agent, paths)

    monkeypatch.setattr(nm, "_send_file_updates", _slow)
    await nm.broadcast_file_updated("agent-x", "workspace/first.md")
    nm._flush_file_batch("agent-x")                # the flush is in flight, parked
    await nm.broadcast_file_updated("agent-x", "workspace/second.md")
    assert "agent-x" in nm._file_batches          # a new window, not the flushed one
    gate.set()
    await nm.flush_file_updates()
    assert [f["rel_path"] for f in _frames(c1)] == ["workspace/first.md", "workspace/second.md"]


async def test_a_burst_past_the_cap_ends_with_one_agent_level_frame(monkeypatch):
    c1 = _Conn("c1")
    deltas, _ = _wire_audience(monkeypatch, {"u1": c1})
    monkeypatch.setattr(nm, "_FILE_UPDATED_MAX_FRAMES", 3)
    for i in range(7):
        await nm.broadcast_file_updated("agent-x", f"workspace/f{i}.md")
    await nm.flush_file_updates()
    frames = _frames(c1)
    assert [f["rel_path"] for f in frames] == ["workspace/f0.md", "workspace/f1.md", "workspace/f2.md", ""]
    assert frames[-1]["file_id"] == "" and frames[-1]["agent_slug"] == "agent-x"
    # The catalog still takes every change once.
    assert len(deltas) == 7


async def test_an_excluded_or_unentitled_user_gets_nothing(monkeypatch):
    from core.remote import file_sync
    c1, c2 = _Conn("c1"), _Conn("c2")
    _wire_audience(monkeypatch, {"u1": c1, "u2": c2})
    monkeypatch.setattr(file_sync, "should_sync_to_target",
                        lambda rel, username, role: username != "u2")
    await nm.broadcast_file_updated("agent-x", "users/u1/workspace/a.md", exclude_user_sub="")
    await nm.flush_file_updates()
    assert [f["rel_path"] for f in _frames(c1)] == ["users/u1/workspace/a.md"]
    assert _frames(c2) == []


async def test_offline_recipients_with_pushes_never_deadlock_the_fan_out(monkeypatch):
    """A recipient slot is held across the push leg; the sends inside take
    a slot of their own, so ten offline recipients with a subscription
    each complete (the same semaphore inside would park every recipient
    slot on a send slot none of them can free)."""
    from services.notifications import push_sender as ps
    subs = [f"u{i}" for i in range(10)]
    monkeypatch.setattr(nm, "resolve_targets", lambda scope, target: list(subs))
    monkeypatch.setattr(notification_store, "create_delivery", lambda **kw: {
        "id": "d", "user_sub": kw["user_sub"], "title": "t", "body": "b", "severity": "info",
        "scope": "agent", "source": "", "delivered_at": "t", "notification_id": None,
        "agent_slug": None, "chat_id": None, "href": ""})
    monkeypatch.setattr(notification_store, "get_push_subscriptions",
                        lambda u: [{"platform": "web", "subscription_data": "{}"}])
    sent: list[str] = []

    async def _web_push(data, payload):
        await asyncio.sleep(0.01)
        sent.append(payload["title"])
        return True

    monkeypatch.setattr(ps, "send_web_push", _web_push)

    async def _run_db(fn, *a, **kw):
        return fn(*a, **kw)

    monkeypatch.setattr(nm, "run_db", _run_db)
    monkeypatch.setattr("storage.pg.run_db", _run_db)

    async def _deliver(user_sub, delivery):          # the offline branch: a push
        await ps.send_to_user(user_sub, {"title": delivery["title"]})

    monkeypatch.setattr(nm, "_deliver_to_user", _deliver)
    deliveries = await asyncio.wait_for(nm.fire_notification("t", "b", scope="agent", target="a"), 5)
    assert len(deliveries) == 10 and len(sent) == 10


async def test_pins_and_saves_keep_their_frame_past_the_cap(monkeypatch):
    c1 = _Conn("c1")
    _wire_audience(monkeypatch, {"u1": c1})
    monkeypatch.setattr(nm, "_FILE_UPDATED_MAX_FRAMES", 3)
    for i in range(6):
        await nm.broadcast_file_updated("agent-x", f"workspace/f{i}.md")
    await nm.broadcast_file_updated("agent-x", "workspace/plan.md", pin=True)
    await nm.broadcast_file_updated("agent-x", "workspace/doc.docx", source="collabora")
    await nm.flush_file_updates()
    frames = [f["rel_path"] for f in _frames(c1)]
    # The pin and the save first, then the writes up to the cap, then the summary.
    assert frames == ["workspace/plan.md", "workspace/doc.docx", "workspace/f0.md", ""]
