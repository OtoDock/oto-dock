"""The usage-limit rests survive a proxy restart
(services/engines/subscription_pool, storage/billing/subscription_store):
every set and every clear of the pool's four rest structures also writes its
row of ``subscription_rests`` through the binding writer, and ``load_rests``
fills the structures back at boot, so an account that reached its limit is
not picked again right after a restart.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest

from core.events import turn_ending
from core.execution_layer import Scoped, Window
from services.engines import subscription_pool as pool, subscription_windows as windows
from storage.billing import subscription_store as store
from storage.pg import get_conn

LAYER = "claude-code-cli"
FABLE, SONNET = "claude-fable-5-2", "claude-sonnet-5"


def _forget() -> None:
    pool._throttled_until.clear()
    pool._throttled_hard.clear()
    pool._window_rests.clear()
    pool._scoped_rests.clear()


@pytest.fixture(autouse=True)
def _fresh_pool(temp_db):
    """The binding writer on its own thread, as in production (the suite
    runs it inline, ``temp_db``), and no rest or binding of another test."""
    pool._reopen_binding_writes()
    _forget()
    with pool._session_maps_lock:
        pool._session_subscriptions.clear()
        pool._session_binding_ctx.clear()
        pool._session_scope_keys.clear()
        pool._scope_recent.clear()
    yield
    pool.flush_binding_writes()
    pool._reopen_binding_writes()
    _forget()
    with pool._session_maps_lock:
        pool._session_subscriptions.clear()
        pool._session_binding_ctx.clear()


def _account(owner: str = "") -> str:
    return store.add_subscription(
        layer=LAYER, provider="anthropic", auth_type="api_key", owner_sub=owner,
        use_personal=False, contribute_platform=True, label="t",
        credential_data={"api_key": "k"},
    )["id"]


def _two_accounts() -> tuple[str, str]:
    """Two accounts of the platform pool, sess-a bound to the first and
    sess-b to the second. The second holds more seats, so the selection
    takes the first whenever nothing rests."""
    a, b = _account(), _account("user-admin")
    for _ in range(20):
        store.increment_active_sessions(b)
    pool.bind_session("sess-a", a, layer=LAYER, user_sub="").result(5)
    pool.bind_session("sess-b", b, layer=LAYER, user_sub="").result(5)
    return a, b


def _limit(window: str, in_s: float) -> turn_ending.TurnEnding:
    at = (datetime.now(timezone.utc) + timedelta(seconds=in_s)).isoformat()
    return turn_ending.TurnEnding(reason=turn_ending.LIMIT, resets_at=at, window=window)


def _reading(five: float, *, fable: float | None = None, ago_s: float = -1.0):
    now = datetime.now(timezone.utc)
    w = windows.Windows(specs=windows.window_specs(LAYER),
                        observed_at=now - timedelta(seconds=ago_s), reached="")
    w.windows["five_hour"] = Window(five, now + timedelta(hours=2))
    w.windows["seven_day"] = Window(20.0, now + timedelta(days=3))
    if fable is not None:
        w.scoped.append(Scoped("fable", "Fable", fable, now + timedelta(days=3)))
    return w


def _rows() -> list[dict]:
    pool.flush_binding_writes()
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM subscription_rests ORDER BY account_id, scope").fetchall()]


def _picks(model: str = "") -> str:
    return pool.acquire_subscription(LAYER, None, model=model).subscription_id


def _restart() -> int:
    """The stand-in for a restart: the queued rows land, the four structures
    start empty, the boot's load runs."""
    pool.flush_binding_writes()
    _forget()
    return pool.load_rests()


# --- the pool ---------------------------------------------------------------

def test_an_account_wide_window_rest_is_written_and_loaded():
    a, b = _two_accounts()
    assert _picks() == a
    pool.rest_after_limit("sess-a", _limit("five_hour", 7200), "")
    until, entry = pool._throttled_until[a], pool._window_rests[a]
    assert abs(until - (time.time() + 7200)) < 5 and entry[0] == "five_hour"
    assert _rows() == [{"account_id": a, "scope": "", "until_at": until, "rested_at": entry[1],
                        "window_key": "five_hour", "hard": True}]
    assert pool.account_resting(a) and _picks() == b

    assert _restart() == 1
    assert pool._throttled_until == {a: until}
    assert pool._window_rests == {a: entry}        # the window key, the rested-at instant exact
    assert pool._throttled_hard == {a} and pool._scoped_rests == {}
    assert pool.account_resting(a) and not pool.account_resting(b)
    for model in ("", FABLE, SONNET):
        assert _picks(model) == b


def test_a_model_scoped_rest_is_written_and_loaded():
    a, b = _two_accounts()
    pool.rest_after_limit("sess-a", _limit("scoped:fable", 86400), "")
    held = pool._scoped_rests[(a, "fable")]
    assert _rows() == [{"account_id": a, "scope": "fable", "until_at": held[0],
                        "rested_at": held[1], "window_key": "", "hard": False}]
    assert not pool.account_resting(a)
    assert _picks(FABLE) == b and _picks(SONNET) == a

    assert _restart() == 1
    assert pool._scoped_rests == {(a, "fable"): held}
    assert pool._throttled_until == {} and pool._throttled_hard == set() and pool._window_rests == {}
    assert not pool.account_resting(a)
    assert _picks(FABLE) == b and _picks(SONNET) == a and _picks() == a


def test_a_cooldown_is_written_and_loaded_with_its_hard_flag():
    a, b = _two_accounts()
    pool.mark_subscription_throttled("sess-a")                                       # a real limit
    pool.mark_subscription_throttled("sess-b", cooldown_s=pool._OVERLOAD_COOLDOWN_S)  # a nudge
    until_a, until_b = pool._throttled_until[a], pool._throttled_until[b]
    rows = {r["account_id"]: r for r in _rows()}
    assert (rows[a]["until_at"], rows[a]["window_key"], rows[a]["hard"]) == (until_a, "", True)
    assert (rows[b]["until_at"], rows[b]["window_key"], rows[b]["hard"]) == (until_b, "", False)

    assert _restart() == 2
    assert pool._throttled_until == {a: until_a, b: until_b}
    assert pool._throttled_hard == {a} and pool._window_rests == {}
    assert pool.account_resting(a) and pool.account_resting(b)
    assert _picks() == b            # every account rests: the one that frees first


def test_the_row_follows_what_the_structures_hold():
    a, _b = _two_accounts()
    pool.rest_after_limit("sess-a", _limit("five_hour", 7200), "")
    row = _rows()[0]
    # A shorter rest never shortens the held one, nor its row.
    pool.mark_subscription_throttled("sess-a")
    pool.rest_after_limit("sess-a", None, "API Error: 529 Overloaded")
    assert _rows() == [row]
    # A longer rest on no window drops the key: no reading ends it.
    pool.mark_subscription_throttled("sess-a", cooldown_s=3 * 3600)
    row = _rows()[0]
    assert (row["until_at"], row["window_key"], row["hard"]) == (pool._throttled_until[a], "", True)
    assert _restart() == 1 and a not in pool._window_rests and pool._throttled_hard == {a}


def test_a_limit_inside_a_longer_soft_rest_marks_its_row_hard():
    a, _b = _two_accounts()
    assert pool._rest_account(a, time.time() + 60)      # the token endpoint's cooldown
    until = pool._throttled_until[a]
    assert [(r["until_at"], r["hard"]) for r in _rows()] == [(until, False)]
    pool.rest_after_limit("sess-a", _limit("five_hour", 20), "")
    assert pool._throttled_until[a] == until and a not in pool._window_rests
    assert [(r["until_at"], r["window_key"], r["hard"]) for r in _rows()] == [(until, "", True)]
    assert _restart() == 1 and pool._throttled_hard == {a}


def test_a_reading_with_headroom_deletes_the_rows_it_ends():
    a, _b = _two_accounts()
    pool.rest_after_limit("sess-a", _limit("five_hour", 7200), "")
    pool.rest_after_limit("sess-a", _limit("scoped:fable", 86400), "")
    assert [r["scope"] for r in _rows()] == ["", "fable"]
    assert _restart() == 2
    # A reading taken before the rests began ends neither, after a restart too.
    pool.clear_rests_with_headroom(a, _reading(20.0, fable=40.0, ago_s=60.0))
    assert pool.account_resting(a) and len(_rows()) == 2
    pool.clear_rests_with_headroom(a, _reading(20.0))
    assert not pool.account_resting(a)
    assert [r["scope"] for r in _rows()] == ["fable"]
    pool.clear_rests_with_headroom(a, _reading(20.0, fable=40.0))
    assert _rows() == []
    assert _restart() == 0 and not pool.account_resting(a) and _picks(FABLE) == a


def test_a_rest_a_reader_finds_ended_deletes_its_row():
    a, _b = _two_accounts()
    pool.rest_after_limit("sess-a", _limit("five_hour", 7200), "")
    pool.rest_after_limit("sess-a", _limit("scoped:fable", 86400), "")
    assert len(_rows()) == 2
    # Both instants pass.
    pool._throttled_until[a] = time.time() - 1
    pool._scoped_rests[(a, "fable")] = (time.time() - 1, pool._scoped_rests[(a, "fable")][1])
    assert not pool.account_resting(a)
    assert [r["scope"] for r in _rows()] == ["fable"]
    assert _picks(FABLE) == a           # the selection meets the scope's expiry
    assert _rows() == []
    assert pool._throttled_hard == set() and pool._window_rests == {} and pool._scoped_rests == {}


def test_the_load_drops_the_ended_rows_and_those_of_deleted_accounts():
    a, b = _two_accounts()
    now = time.time()
    store.save_rest(a, "", now - 1, now - 900, "", True)                 # ended
    store.save_rest(a, "fable", now + 3600, now - 5, "", False)
    store.save_rest(b, "", now + 3600, now - 5, "seven_day", True)       # its account goes
    store.save_rest(b, "fable", now + 3600, now - 5, "", False)
    store.delete_subscription(b)
    assert pool.load_rests() == 1
    assert pool._scoped_rests == {(a, "fable"): (now + 3600, now - 5)}
    assert pool._throttled_until == {} and pool._throttled_hard == set() and pool._window_rests == {}
    assert [(r["account_id"], r["scope"]) for r in _rows()] == [(a, "fable")]


def test_a_failed_write_is_logged_and_the_rest_stands(monkeypatch, caplog):
    a, b = _two_accounts()
    monkeypatch.setattr(store, "save_rest",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("db down")))
    with caplog.at_level("ERROR"):
        pool.mark_subscription_throttled("sess-a")
        pool.flush_binding_writes()
    assert any("saving a rest failed" in r.getMessage() for r in caplog.records)
    assert pool.account_resting(a) and _picks() == b
    assert _rows() == []


def test_the_boot_loads_the_rests_and_goes_on_when_the_load_fails(monkeypatch, caplog):
    import startup
    a, _b = _two_accounts()
    pool.mark_subscription_throttled("sess-a")
    pool.flush_binding_writes()
    _forget()
    with caplog.at_level("INFO"):
        startup._boot_subscriptions()
    assert pool.account_resting(a) and pool._throttled_hard == {a}
    assert "Loaded 1 usage-limit rest(s)" in caplog.text

    _forget()
    monkeypatch.setattr(pool, "load_rests",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    with caplog.at_level("INFO"):
        startup._boot_subscriptions()
    assert "loading the usage-limit rests failed (non-fatal)" in caplog.text
    assert "Synced builtin models" in caplog.text.split("usage-limit rests failed")[1]
    assert not pool.account_resting(a)


# --- the store --------------------------------------------------------------

def test_save_rest_upserts_one_row_per_account_and_scope():
    store.save_rest("acc-1", "", 1762430000.1234567, 1762429100.7654321, "five_hour", True)
    store.save_rest("acc-1", "fable", 1762516400.25, 1762430000.5, "", False)
    store.save_rest("acc-1", "", 1762433600.000001, 1762430001.9999999, "", False)
    assert _rows() == [
        {"account_id": "acc-1", "scope": "", "until_at": 1762433600.000001,
         "rested_at": 1762430001.9999999, "window_key": "", "hard": False},
        {"account_id": "acc-1", "scope": "fable", "until_at": 1762516400.25,
         "rested_at": 1762430000.5, "window_key": "", "hard": False},
    ]


def test_clear_rest_deletes_one_scope_or_every_row_of_the_account():
    for account in ("acc-1", "acc-2"):
        for scope in ("", "fable", "sonnet"):
            store.save_rest(account, scope, 2.0, 1.0, "", False)
    store.clear_rest("acc-1", "fable")
    store.clear_rest("acc-1", "")
    assert [(r["account_id"], r["scope"]) for r in _rows()] == [
        ("acc-1", "sonnet"), ("acc-2", ""), ("acc-2", "fable"), ("acc-2", "sonnet")]
    store.clear_rest("acc-2")
    assert [(r["account_id"], r["scope"]) for r in _rows()] == [("acc-1", "sonnet")]
    store.clear_rest("acc-1", "never-rested")       # no row: nothing to delete
    assert len(_rows()) == 1


def test_load_rests_returns_the_running_rows_of_existing_accounts():
    a = _account()
    store.save_rest(a, "", 1000.5, 900.25, "seven_day", True)
    store.save_rest(a, "fable", 1000.0, 990.0, "", False)       # ends at the instant read
    store.save_rest(a, "sonnet", 999.0, 990.0, "", False)       # ended
    store.save_rest("no-such-account", "", 5000.0, 900.0, "", True)
    assert store.load_rests(1000.0) == [
        {"account_id": a, "scope": "", "until_at": 1000.5, "rested_at": 900.25,
         "window_key": "seven_day", "hard": True}]
    assert [(r["account_id"], r["scope"]) for r in _rows()] == [(a, "")]
    assert store.load_rests(1000.5) == [] and _rows() == []
