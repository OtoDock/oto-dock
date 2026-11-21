"""services/apps/app_db: an app's database copies. The data folder is the
app's own server's to write, so the snapshot never opens a live name: it
links the live files, as they are, into a private folder and copies from
there; the restore replaces a link at the database's name instead of
writing through it. Every link and pipe here sits in the test's own folder.
"""

import os
import sqlite3
import time

import pytest

import config
from services.apps import app_db, app_render, releases

ROW = {"agent": "dbagent", "username": "", "id": "app-1", "slug": "board"}


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    d = releases.app_data_dir(ROW)
    d.mkdir(parents=True)
    return d


def _db(path, rows=(1,), *, wal=False):
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=wal")
    con.execute("CREATE TABLE t (x)")
    con.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    con.commit()
    con.close()


def _rows(path):
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT x FROM t ORDER BY x")]
    finally:
        con.close()


def _dst():
    return releases.app_release_dir(ROW) / "1" / releases.DB_BEFORE_NAME


def test_a_link_at_a_live_name_refuses_the_copy(data, tmp_path):
    from services.apps.app_deploy import DeployError
    other = tmp_path / "agents" / "other" / "secret.db"
    other.parent.mkdir(parents=True)
    _db(other, rows=(99,))
    (data / "app.db").symlink_to(other)
    with pytest.raises(DeployError) as e:
        app_db.snapshot_db(ROW, _dst())
    assert "not a plain file" in str(e.value)
    assert not _dst().exists()
    (data / "app.db").unlink()
    _db(data / "app.db")
    for side in ("app.db-wal", "app.db-shm", "app.db-journal"):
        (data / side).symlink_to(other)
        with pytest.raises(DeployError):
            app_db.snapshot_db(ROW, _dst())
        (data / side).unlink()
    assert app_db.snapshot_db(ROW, _dst()) and _rows(_dst()) == [1]
    # Nothing is left beside the releases.
    assert not [p for p in releases.app_release_dir(ROW).iterdir()
                if p.name.startswith(releases.DB_SNAPSHOT_PREFIX)]


def test_a_pipe_at_a_live_name_is_refused_without_waiting(data):
    from services.apps.app_deploy import DeployError
    _db(data / "app.db")
    os.mkfifo(data / "app.db-wal")
    with pytest.raises(DeployError):
        app_db.snapshot_db(ROW, _dst())


def test_a_link_on_the_way_to_the_data_folder_refuses_the_copy(data, tmp_path):
    from services.apps.app_deploy import DeployError
    elsewhere = tmp_path / "agents" / "other" / "data"
    elsewhere.mkdir(parents=True)
    _db(elsewhere / "app.db", rows=(99,))
    os.rmdir(data)
    data.symlink_to(elsewhere)
    with pytest.raises(DeployError):
        app_db.snapshot_db(ROW, _dst())


def test_the_copy_is_the_database_that_was_there_when_a_name_is_swapped_at_the_open(data, tmp_path, monkeypatch):
    # The app's server swaps its database for a link at the moment SQLite
    # opens: what is copied is still the app's own database.
    other = tmp_path / "agents" / "other" / "secret.db"
    other.parent.mkdir(parents=True)
    _db(other, rows=(99,))
    _db(data / "app.db", rows=(1, 2))
    real = sqlite3.connect
    swapped = []

    def _connect(*a, **kw):
        if not swapped:
            swapped.append(True)
            os.replace(data / "app.db", data / "moved.db")
            (data / "app.db").symlink_to(other)
        return real(*a, **kw)

    monkeypatch.setattr(sqlite3, "connect", _connect)
    scratch = releases.app_release_dir(ROW) / "scratch-data-x"
    scratch.mkdir(parents=True)
    assert app_render._seed(ROW, scratch)
    monkeypatch.setattr(sqlite3, "connect", real)
    assert swapped and _rows(scratch / "app.db") == [1, 2]


def test_a_link_where_the_copy_lands_is_replaced_never_written_through(data, tmp_path):
    victim = tmp_path / "agents" / "other" / "victim.bin"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"keep")
    _db(data / "app.db", rows=(3,))
    scratch = releases.app_release_dir(ROW) / "scratch-data-x"
    scratch.mkdir(parents=True)
    for name in ("app.db", "app.db.tmp"):
        (scratch / name).symlink_to(victim)
    assert app_render._seed(ROW, scratch)
    assert victim.read_bytes() == b"keep"
    assert not (scratch / "app.db").is_symlink() and _rows(scratch / "app.db") == [3]


def test_a_wal_database_is_copied_with_what_its_wal_holds(data):
    _db(data / "app.db", rows=(1,), wal=True)
    con = sqlite3.connect(data / "app.db")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("INSERT INTO t VALUES (2)")
    con.commit()
    try:
        assert (data / "app.db-wal").stat().st_size > 0
        assert app_db.snapshot_db(ROW, _dst())
    finally:
        con.close()
    assert _rows(_dst()) == [1, 2]


def test_no_database_and_one_over_the_cap_copy_nothing(data):
    assert app_db.snapshot_db(ROW, _dst()) is False
    _db(data / "app.db", rows=range(200))
    assert app_db.snapshot_db(ROW, _dst(), max_size=1024) is False
    assert not _dst().exists()


def test_a_killed_copys_folder_is_removed_with_the_old_copies(data):
    leftover = releases.app_release_dir(ROW) / f"{releases.DB_SNAPSHOT_PREFIX}dead"
    leftover.mkdir(parents=True)
    _db(data / "app.db")
    os.link(data / "app.db", leftover / "app.db")
    releases.drop_db_copies(ROW)
    assert not leftover.exists() and (data / "app.db").is_file()


def test_the_restore_replaces_a_link_and_drops_every_stale_side(data, tmp_path):
    _db(data / "app.db", rows=(5,))
    assert app_db.snapshot_db(ROW, _dst())
    victim = tmp_path / "agents" / "other" / "victim.bin"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"keep")
    (data / "app.db").unlink()
    (data / "app.db").symlink_to(victim)
    (data / "app.db-journal").write_bytes(b"stale")
    app_db.restore_db(_dst(), data / "app.db")
    assert victim.read_bytes() == b"keep"
    # Looked at before SQLite opens the database (an open drops a stale
    # journal itself).
    assert not (data / "app.db-journal").exists()
    assert not (data / "app.db").is_symlink() and _rows(data / "app.db") == [5]


def test_a_writer_that_starts_during_the_copy_makes_it_copy_again(data, monkeypatch):
    # A database with no WAL: a writer that starts while it is copied makes
    # one, and the copy is taken again with it; one that never settles stops
    # the deploy with its own sentence.
    from services.apps.app_deploy import DeployError
    _db(data / "app.db", rows=(1,))
    real = sqlite3.connect
    opened = []

    def _connect(*a, **kw):
        con = real(*a, **kw)
        if "mode=ro" in str(a[0]):
            opened.append(a[0])
            if len(opened) == 1:
                (data / "app.db-wal").write_bytes(b"")
        return con

    monkeypatch.setattr(sqlite3, "connect", _connect)
    assert app_db.snapshot_db(ROW, _dst())
    assert len(opened) == 2 and _rows(_dst()) == [1]
    (data / "app.db-wal").unlink()
    flips = iter(range(100))
    monkeypatch.setattr(app_db, "_writer_sides", lambda fd: {"app.db-wal": (0, next(flips))})
    monkeypatch.setattr(app_db.time, "sleep", lambda s: None)
    with pytest.raises(DeployError) as e:
        app_db.snapshot_db(ROW, _dst())
    assert "kept changing" in str(e.value)


def _mid_write_pair(data, tmp_path):
    """A rollback-journal database left in the middle of a write: pages of an
    open transaction already in ``app.db``, its hot journal beside it."""
    import shutil
    work = tmp_path / "work"
    work.mkdir()
    con = sqlite3.connect(work / "app.db", isolation_level=None)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA cache_size=10")
    con.execute("CREATE TABLE t (x, pad)")
    con.execute("BEGIN")
    con.executemany("INSERT INTO t VALUES (?, ?)", [(i, "p" * 200) for i in range(2000)])
    con.execute("COMMIT")
    con.execute("BEGIN")
    con.execute("UPDATE t SET pad = ?", ("q" * 300,))
    shutil.copy(work / "app.db", data / "app.db")
    shutil.copy(work / "app.db-journal", data / "app.db-journal")
    con.execute("ROLLBACK")
    con.close()


def test_a_database_in_the_middle_of_a_write_is_never_copied_torn(data, tmp_path):
    from services.apps.app_deploy import DeployError
    _mid_write_pair(data, tmp_path)
    with pytest.raises(DeployError) as e:
        app_db.snapshot_db(ROW, _dst())
    assert str(e.value) == app_db.MID_WRITE
    assert not _dst().exists()


def test_a_commit_between_the_link_and_the_open_is_copied_again(data, monkeypatch):
    # A rollback-journal writer whose transaction is open when the live names
    # are linked and commits before SQLite opens the private names: the copy
    # runs again and holds the committed rows, never "in the middle of a write".
    con = sqlite3.connect(data / "app.db")
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    w = sqlite3.connect(data / "app.db", isolation_level=None)
    w.execute("PRAGMA cache_size=5")
    w.execute("BEGIN IMMEDIATE")
    w.executemany("INSERT INTO t VALUES (?)", [("y" * 3000,)] * 40)
    assert (data / "app.db-journal").exists()
    real = app_db._link_live

    def _link_then_commit(data_fd, priv_fd):
        linked = real(data_fd, priv_fd)
        if w.in_transaction:
            w.execute("COMMIT")
        return linked

    monkeypatch.setattr(app_db, "_link_live", _link_then_commit)
    try:
        assert app_db.snapshot_db(ROW, _dst())
    finally:
        w.close()
    assert len(_rows(_dst())) == 40


def test_a_private_folder_a_killed_copy_left_is_swept_by_the_next_copy(data):
    _db(data / "app.db")
    left = releases.app_release_dir(ROW) / f"{releases.DB_SNAPSHOT_PREFIX}killed"
    left.mkdir(parents=True)
    (left / "app.db").write_text("x")
    os.utime(left, (time.time() - 7200, time.time() - 7200))
    assert app_db.snapshot_db(ROW, _dst())
    assert not left.exists()


def test_a_refused_link_tells_the_admin_why(data, monkeypatch, caplog):
    import errno

    from services.apps.app_deploy import DeployError
    _db(data / "app.db")

    def _no_link(*a, **kw):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(app_db.os, "link", _no_link)
    with caplog.at_level("WARNING", logger="otodock.apps"), pytest.raises(DeployError):
        app_db.snapshot_db(ROW, _dst())
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "Operation not permitted" in said and "owned by the platform's own user" in said


def test_two_copies_of_one_database_never_overlap(data, monkeypatch):
    import threading
    import time as _time
    _db(data / "app.db")
    real = app_db._copy_once
    active: list[int] = []
    overlap: list[bool] = []
    guard = threading.Lock()

    def _slow(*a, **kw):
        with guard:
            active.append(1)
            if len(active) > 1:
                overlap.append(True)
        _time.sleep(0.2)
        try:
            return real(*a, **kw)
        finally:
            with guard:
                active.pop()

    monkeypatch.setattr(app_db, "_copy_once", _slow)
    threads = [threading.Thread(target=app_db.snapshot_db,
                                args=(ROW, releases.app_release_dir(ROW) / str(i) / "copy.sqlite"))
               for i in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert overlap == []
    assert all((releases.app_release_dir(ROW) / str(i) / "copy.sqlite").is_file() for i in range(2))


def test_dropping_the_copies_waits_for_a_running_copy(data, monkeypatch):
    import threading
    _db(data / "app.db", rows=(7,))
    linked, go = threading.Event(), threading.Event()
    real = app_db._link_live

    def _link_then_wait(data_fd, priv_fd):
        out = real(data_fd, priv_fd)
        linked.set()
        go.wait(5)
        return out

    monkeypatch.setattr(app_db, "_link_live", _link_then_wait)
    result: list[bool] = []
    copy = threading.Thread(target=lambda: result.append(app_db.snapshot_db(ROW, _dst())))
    copy.start()
    assert linked.wait(5)
    drop = threading.Thread(target=releases.drop_db_copies, args=(ROW,))
    drop.start()
    drop.join(0.5)
    assert drop.is_alive()
    assert [p for p in releases.app_release_dir(ROW).iterdir() if p.name.startswith(releases.DB_SNAPSHOT_PREFIX)]
    go.set()
    copy.join(5)
    drop.join(5)
    assert result == [True]


# An agent's folder may itself be a link the install's operator made
# (core/agent_folder.py): the copy and the restore reach the database through
# that one link and through none below it.


@pytest.fixture
def linked_data(tmp_path, monkeypatch):
    """``data`` of an agent whose folder is a link to a folder outside the
    stand-in agents folder."""
    agents = tmp_path / "agents"
    real = tmp_path / "disk2" / ROW["agent"]
    real.mkdir(parents=True)
    agents.mkdir()
    (agents / ROW["agent"]).symlink_to(real)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    d = releases.app_data_dir(ROW)
    d.mkdir(parents=True)
    return d


def test_a_linked_agents_database_is_copied_and_restored(linked_data, tmp_path):
    _db(linked_data / "app.db", rows=(1, 2), wal=True)
    assert app_db.snapshot_db(ROW, _dst()) and _rows(_dst()) == [1, 2]
    assert (tmp_path / "disk2" / ROW["agent"] / _dst().relative_to(config.AGENTS_DIR / ROW["agent"])).is_file()
    con = sqlite3.connect(linked_data / "app.db")
    con.execute("INSERT INTO t VALUES (3)")
    con.commit()
    con.close()
    app_db.restore_db(_dst(), linked_data / "app.db")
    assert _rows(linked_data / "app.db") == [1, 2]


def test_a_link_below_a_linked_agents_folder_still_refuses_the_copy(linked_data, tmp_path):
    from services.apps.app_deploy import DeployError
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _db(elsewhere / "app.db", rows=(99,))
    os.rmdir(linked_data)
    linked_data.symlink_to(elsewhere)
    with pytest.raises(DeployError):
        app_db.snapshot_db(ROW, _dst())
    assert not _dst().exists()
