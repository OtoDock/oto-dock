"""scripts/backup.sh: one run directory per backup, private, whole or not at
all; the version from the database, the image or VERSIONS.md; the trees
through the proxy container (running, or a one-off on its image) or on this
host; links kept, SQLite copied by header; the retention over runs and trees.
The platform's hold is asked for again for the length of the copy, and one
run at a time writes into a folder.

The real script runs against the fake docker of tests/deploy/fake_docker.py:
no daemon, no database.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from tests.deploy.fake_docker import write_fake

REPO = Path(__file__).resolve().parents[3]
BACKUP = REPO / "scripts" / "backup.sh"
RESTORE = REPO / "scripts" / "restore.sh"

PG = {"id": "pg-t2", "name": "otodock-otodock-postgres-1",
      "labels": {"com.docker.compose.project": "otodock", "com.docker.compose.service": "otodock-postgres"}}
PG_T1 = {"id": "pg-t1", "name": "otodock-postgres",
         "labels": {"com.docker.compose.project": "oto-dock", "com.docker.compose.service": "postgres"}}
KEYS = json.dumps({"a" * 32: "JWT_SECRET"})
# The one file a run keeps in the output folder beside its run directories.
LOCK = ".backup.lock"


def proxy(world: Path, *, running: bool = True) -> dict:
    """The proxy container: its env file bind mount and its two volumes stand
    in by directories under ``world``."""
    return {
        "id": "proxy-t2", "name": "otodock-otodock-proxy-1", "running": running, "image": "ghcr.io/x/proxy:1.8.0",
        "labels": {"com.docker.compose.project": "otodock", "com.docker.compose.service": "otodock-proxy"},
        "mounts": {
            "/opt/otodock/config.env": {"source": str(world / "env")},
            "/opt/otodock/agents": {"name": "vol-agents", "source": str(world / "agents")},
            "/opt/otodock/mcps/custom/ssh-hosts/keys": {"name": "vol-keys", "source": str(world / "keys")},
            "/opt/otodock/sessions": {"name": "vol-sessions", "source": str(world / "sessions")},
        },
    }


@pytest.fixture
def world(tmp_path):
    """An install's files: the env file, an agents tree with an app database,
    a workspace file and a link, the ssh keys, the sessions tree with a
    connected account's token file."""
    w = tmp_path / "world"
    (w / "agents" / "helper" / "workspace").mkdir(parents=True)
    (w / "agents" / "helper" / "workspace" / "notes.md").write_text("hello")
    db = w / "agents" / "helper" / "app-data" / "app1" / "app.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t(x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()
    os.symlink("/usr/bin/python3", w / "agents" / "helper" / "workspace" / "py")
    (w / "keys").mkdir()
    (w / "keys" / "id_ed25519").write_text("KEY")
    (w / "sessions" / "github-tokens" / "dev-admin").mkdir(parents=True)
    (w / "sessions" / "github-tokens" / "dev-admin" / "walk-a1.json").write_text('{"access_token": "t"}')
    (w / "sessions" / "index.json").write_text("{}")
    (w / "env").write_text("JWT_SECRET=s3cret\nPOSTGRES_PASSWORD=pw\n")
    return w


def _env(tmp_path, containers, out=None, env=None) -> dict:
    bindir = tmp_path / "bin"
    write_fake(bindir)
    world = tmp_path / "world"
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(("POSTGRES_", "OTODOCK_", "PLATFORM_", "PROXY_"))}
    e.update({
        "PATH": f"{bindir}:{e.get('PATH', '/usr/bin:/bin')}",
        "FAKE_CONTAINERS": json.dumps(containers),
        "FAKE_LOG": str(tmp_path / "docker.log"),
        "FAKE_VOLUMES": json.dumps({"vol-agents": str(world / "agents"), "vol-keys": str(world / "keys"),
                                    "vol-sessions": str(world / "sessions")}),
        "FAKE_DB_VERSION": "1.8.0",
        "FAKE_KEYS": KEYS,
        "OTODOCK_BACKUP_DIR": str(out or tmp_path / "backups"),
        "OTODOCK_BACKUP_RETAIN": "0",
        # Bare metal reads the install's ssh keys folder: point it away from this checkout's.
        "OTODOCK_SSH_KEYS_DIR": str(tmp_path / "no-keys"),
    })
    e.update(env or {})
    return e


def run(tmp_path, *args, containers, out=None, env=None):
    return subprocess.run(["bash", str(BACKUP), *args], cwd=tmp_path, env=_env(tmp_path, containers, out, env),
                          capture_output=True, text=True, timeout=120)


def start(tmp_path, *args, containers, out=None, env=None) -> subprocess.Popen:
    """The script as the leader of its own process group: a test acts while
    it runs, and asks afterwards what is left of the group."""
    return subprocess.Popen(["bash", str(BACKUP), *args], cwd=tmp_path, env=_env(tmp_path, containers, out, env),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)


def calls(tmp_path) -> list[list[str]]:
    log = tmp_path / "docker.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def runs(out: Path) -> list[Path]:
    return sorted(p for p in out.glob("otodock-*") if p.is_dir())


def manifest(run_dir: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in (run_dir / "manifest").read_text().splitlines())


def names(archive: Path) -> list[str]:
    with tarfile.open(archive) as tf:
        return tf.getnames()


# ── the run directory ────────────────────────────────────────────────────────

def test_a_run_directory_holds_everything_private(tmp_path, world):
    r = run(tmp_path, containers=[PG, proxy(world)])
    assert r.returncode == 0, r.stderr
    (d,) = runs(tmp_path / "backups")
    assert d.name.startswith("otodock-1.8.0-")
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert sorted(p.name for p in d.iterdir()) == ["agents.tar.gz", "db.sql.gz", "env", "manifest",
                                                   "sessions.tar.gz", "ssh-keys.tar.gz"]
    assert stat.S_IMODE((d / "env").stat().st_mode) == 0o600
    assert (d / "env").read_text() == (world / "env").read_text()
    m = manifest(d)
    assert m["otodock_version"] == "1.8.0" and m["topology"] == "docker" and m["project"] == "otodock"
    assert m["env"] == "1" and m["agents"] == "present" and m["ssh_keys"] == "present" and m["sessions"] == "present"
    assert m["credential_keys"] == KEYS and m["errors"] == "0" and m["complete"] == "1"
    assert "./helper/workspace/notes.md" in names(d / "agents.tar.gz")
    assert "./id_ed25519" in names(d / "ssh-keys.tar.gz")
    assert "./github-tokens/dev-admin/walk-a1.json" in names(d / "sessions.tar.gz")
    assert not list(d.glob("*.part"))


def test_a_proxy_without_a_sessions_volume_skips_the_tree_and_says_so(tmp_path, world):
    p = proxy(world)
    del p["mounts"]["/opt/otodock/sessions"]
    r = run(tmp_path, containers=[PG, p])
    assert r.returncode == 0, r.stderr
    assert "mounts no volume at /opt/otodock/sessions" in r.stderr and "OAuth accounts" in r.stderr
    (d,) = runs(tmp_path / "backups")
    assert not (d / "sessions.tar.gz").exists() and manifest(d)["sessions"] == "skipped"
    assert manifest(d)["agents"] == "present"


def test_the_tree_is_read_inside_the_running_proxy(tmp_path, world):
    r = run(tmp_path, containers=[PG, proxy(world)])
    assert r.returncode == 0, r.stderr
    execs = [c for c in calls(tmp_path) if c[0] == "exec"]
    assert any(c[1:3] == ["-i", "proxy-t2"] and "python3" in c for c in execs)
    assert not any(c[0] == "run" for c in calls(tmp_path))


def test_a_stopped_proxy_is_served_by_a_one_off_container_on_its_image(tmp_path, world):
    r = run(tmp_path, containers=[PG, proxy(world, running=False)])
    assert r.returncode == 0, r.stderr
    one_offs = [c for c in calls(tmp_path) if c[0] == "run"]
    tree = [c for c in one_offs if "vol-agents:/opt/otodock/agents:ro" in c]
    assert tree and "--network" in tree[0] and "none" in tree[0] and "ghcr.io/x/proxy:1.8.0" in tree[0]
    assert not any(c[0] == "exec" and "proxy-t2" in c[:3] for c in calls(tmp_path))
    (d,) = runs(tmp_path / "backups")
    assert "./helper/workspace/notes.md" in names(d / "agents.tar.gz")


def test_the_version_comes_from_the_database_then_the_image_then_unknown(tmp_path, world):
    r = run(tmp_path, containers=[PG, proxy(world)], env={"FAKE_DB_VERSION": "", "FAKE_IMAGE_VERSION": "1.7.1"})
    assert r.returncode == 0, r.stderr
    assert runs(tmp_path / "backups")[0].name.startswith("otodock-1.7.1-")
    r = run(tmp_path, containers=[PG, proxy(world)], out=tmp_path / "b2", env={"FAKE_DB_VERSION": "garbage", "FAKE_IMAGE_VERSION": ""})
    assert r.returncode == 0, r.stderr
    assert runs(tmp_path / "b2")[0].name.startswith("otodock-unknown-")


def test_db_only_writes_the_dump_the_env_and_the_manifest(tmp_path, world):
    r = run(tmp_path, "--db-only", containers=[PG, proxy(world)])
    assert r.returncode == 0, r.stderr
    (d,) = runs(tmp_path / "backups")
    assert sorted(p.name for p in d.iterdir()) == ["db.sql.gz", "env", "manifest"]
    assert manifest(d)["agents"] == "skipped" and manifest(d)["sessions"] == "skipped"


def test_apps_is_still_accepted(tmp_path, world):
    r = run(tmp_path, "--apps", containers=[PG, proxy(world)])
    assert r.returncode == 0, r.stderr


def test_a_full_backup_without_an_env_file_fails_and_db_only_warns(tmp_path, world):
    p = proxy(world)
    del p["mounts"]["/opt/otodock/config.env"]
    r = run(tmp_path, containers=[PG, p])
    assert r.returncode == 1
    assert "no env file found" in r.stderr
    assert runs(tmp_path / "backups") == []
    r = run(tmp_path, "--db-only", containers=[PG, p])
    assert r.returncode == 0, r.stderr
    assert "holds the database alone" in r.stderr
    (d,) = runs(tmp_path / "backups")
    assert manifest(d)["env"] == "0" and not (d / "env").exists()


def test_bare_metal_reads_the_env_file_and_the_trees_on_this_host(tmp_path, world):
    r = run(tmp_path, containers=[PG_T1],
            env={"OTODOCK_ENV_FILE": str(world / "env"), "OTODOCK_AGENTS_DIR": str(world / "agents"),
                 "OTODOCK_SESSIONS_DIR": str(world / "sessions")})
    assert r.returncode == 0, r.stderr
    (d,) = runs(tmp_path / "backups")
    m = manifest(d)
    assert m["topology"] == "host" and m["env"] == "1" and m["agents"] == "present" and m["ssh_keys"] == "skipped"
    assert m["sessions"] == "present" and "./github-tokens/dev-admin/walk-a1.json" in names(d / "sessions.tar.gz")
    assert not any(c[0] == "run" for c in calls(tmp_path))
    # No sessions directory on this host: skipped with a note, the rest unchanged.
    r = run(tmp_path, containers=[PG_T1], out=tmp_path / "b2",
            env={"OTODOCK_ENV_FILE": str(world / "env"), "OTODOCK_AGENTS_DIR": str(world / "agents"),
                 "OTODOCK_SESSIONS_DIR": str(tmp_path / "no-sessions")})
    assert r.returncode == 0, r.stderr
    assert "no sessions directory at" in r.stderr and "OTODOCK_SESSIONS_DIR" in r.stderr
    assert manifest(runs(tmp_path / "b2")[0])["sessions"] == "skipped"


def test_a_named_project_never_falls_back_to_a_bare_container_name(tmp_path, world):
    r = run(tmp_path, containers=[PG_T1], env={"OTODOCK_COMPOSE_PROJECT": "laneu-a"})
    assert r.returncode == 1
    assert "no running otodock-postgres container found (project laneu-a" in r.stderr


def test_the_postgres_container_can_be_named_outright(tmp_path, world):
    """A bare-metal checkout beside a compose stack on one daemon: its
    Postgres is service ``postgres`` of another project, found by name."""
    host = {"OTODOCK_ENV_FILE": str(world / "env"), "OTODOCK_AGENTS_DIR": str(world / "agents"),
            "OTODOCK_COMPOSE_PROJECT": "oto-dock"}
    r = run(tmp_path, containers=[PG, PG_T1], env={**host, "OTODOCK_POSTGRES_CONTAINER": "otodock-postgres"})
    assert r.returncode == 0, r.stderr
    assert all(c[1] == "pg-t1" for c in calls(tmp_path) if c[0] == "exec")
    assert manifest(runs(tmp_path / "backups")[0])["topology"] == "host"
    r = run(tmp_path, containers=[PG, PG_T1], out=tmp_path / "b2", env={**host, "OTODOCK_POSTGRES_CONTAINER": "nope"})
    assert r.returncode == 1 and "OTODOCK_POSTGRES_CONTAINER=nope is not a container" in r.stderr
    r = run(tmp_path, containers=[PG, {**PG_T1, "running": False}], out=tmp_path / "b3",
            env={**host, "OTODOCK_POSTGRES_CONTAINER": "otodock-postgres"})
    assert r.returncode == 1 and "is not running (exited)" in r.stderr


# ── the tree program ─────────────────────────────────────────────────────────

def test_links_are_kept_databases_copied_by_header_and_wal_files_left_out(tmp_path, world):
    agents = world / "agents" / "helper"
    state = agents / ".codex" / "state_5.sqlite"
    state.parent.mkdir()
    con = sqlite3.connect(state)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t(x)")
    con.execute("INSERT INTO t VALUES (7)")
    con.commit()
    # The connection stays open through the backup: the row sits in the live
    # -wal file, as an engine's state database is while it runs.
    assert (state.parent / "state_5.sqlite-wal").exists()
    os.symlink("../workspace", agents / "link-to-dir")
    os.mkfifo(agents / "pipe")
    (agents / "empty").mkdir()
    r = run(tmp_path, containers=[PG_T1],
            env={"OTODOCK_ENV_FILE": str(world / "env"), "OTODOCK_AGENTS_DIR": str(world / "agents")})
    con.close()
    assert r.returncode == 0, r.stderr
    (d,) = runs(tmp_path / "backups")
    with tarfile.open(d / "agents.tar.gz") as tf:
        members = {m.name: m for m in tf.getmembers()}
        assert "./helper/.codex/state_5.sqlite" in members
        assert "./helper/.codex/state_5.sqlite-wal" not in members
        assert "./helper/.codex/state_5.sqlite-shm" not in members
        copy = tmp_path / "copy.sqlite"
        copy.write_bytes(tf.extractfile(members["./helper/.codex/state_5.sqlite"]).read())
        assert sqlite3.connect(copy).execute("SELECT x FROM t").fetchall() == [(7,)]
        assert members["./helper/workspace/py"].issym()
        assert members["./helper/workspace/py"].linkname == "/usr/bin/python3"
        assert members["./helper/link-to-dir"].issym() and members["./helper/link-to-dir"].linkname == "../workspace"
        assert "./helper/pipe" not in members
        assert members["./helper/empty"].isdir()
        assert members["./helper/app-data/app1/app.db"].isreg()
    assert "skip (not a regular file)" in r.stderr


def test_no_session_token_is_archived(tmp_path, world):
    """A scope's sign-in file and its sessions' homes (own/) hold session
    tokens or a key that every start writes again: a backup holds none, in a
    person's scope, the agent's, or an outside caller's tree, and keeps the
    scope's other files and a repository's own engine folder."""
    sid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    sign_in = {".claude": ".credentials" + ".json", ".codex": "auth" + ".json"}
    helper = world / "agents" / "helper"
    stores = {".claude": helper / "workspace" / ".claude", ".codex": helper / "users" / "alice" / ".codex"}
    callers = [(".codex", helper / "externals" / "phone" / "caller-1" / ".codex"),
               (".claude", helper / "externals" / "phone" / "_ephemeral" / sid / ".claude")]
    for engine, store in callers:
        store.mkdir(parents=True)
        (store / "settings.json").write_text("{}")
        (store / sign_in[engine]).write_text("token")
    for engine, store in stores.items():
        (store / "own" / sid).mkdir(parents=True)
        (store / "own" / sid / "token.json").write_text("token")
        (store / "settings.json").write_text("{}")
        (store / sign_in[engine]).write_text("token")
    # A temporary sign-in file a killed Codex save left (``.auth-XXXXXX``).
    (stores[".codex"] / ".auth-Ab12Cd").write_text("token")
    repo = helper / "workspace" / "repo" / ".claude"
    repo.mkdir(parents=True)
    (repo / sign_in[".claude"]).write_text("a repository's own file")
    r = run(tmp_path, containers=[PG_T1],
            env={"OTODOCK_ENV_FILE": str(world / "env"), "OTODOCK_AGENTS_DIR": str(world / "agents")})
    assert r.returncode == 0, r.stderr
    (d,) = runs(tmp_path / "backups")
    with tarfile.open(d / "agents.tar.gz") as tf:
        names = set(tf.getnames())
    assert not any("/own" in n for n in names)
    for engine, store in stores.items():
        rel = "./" + str(store.relative_to(world / "agents"))
        assert f"{rel}/{sign_in[engine]}" not in names
        assert f"{rel}/settings.json" in names
    for engine, store in callers:
        rel = "./" + str(store.relative_to(world / "agents"))
        assert f"{rel}/{sign_in[engine]}" not in names
        assert f"{rel}/settings.json" in names
    assert not any(n.endswith("/.auth-Ab12Cd") for n in names)
    assert f"./helper/workspace/repo/.claude/{sign_in['.claude']}" in names


def test_a_failed_restores_set_aside_tree_is_not_archived(tmp_path, world):
    aside = world / "agents" / ".pre-restore-20261001-010101" / "helper"
    aside.mkdir(parents=True)
    (aside / "old.md").write_text("set aside")
    (world / "agents" / "helper" / ".pre-restore-not-at-the-root").mkdir()
    r = run(tmp_path, containers=[PG, proxy(world)])
    assert r.returncode == 0, r.stderr
    (d,) = runs(tmp_path / "backups")
    got = names(d / "agents.tar.gz")
    assert not any(".pre-restore-20261001" in n for n in got)
    assert "./helper/.pre-restore-not-at-the-root" in got


def test_a_database_that_will_not_copy_keeps_the_archive_and_exits_3(tmp_path, world):
    bad = world / "agents" / "helper" / "app-data" / "app1" / "bad.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"garbage" * 64)
    r = run(tmp_path, containers=[PG, proxy(world)])
    assert r.returncode == 3, r.stderr
    assert "INCOMPLETE" in r.stderr and "error copying database" in r.stderr
    (d,) = runs(tmp_path / "backups")
    assert "./helper/app-data/app1/app.db" in names(d / "agents.tar.gz")
    assert "./helper/app-data/app1/bad.db" not in names(d / "agents.tar.gz")
    assert manifest(d)["errors"] == "1" and manifest(d)["complete"] == "1"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_an_unreadable_file_is_an_error_not_the_end_of_the_backup(tmp_path, world):
    secret = world / "agents" / "helper" / "workspace" / "locked"
    secret.write_text("x")
    secret.chmod(0)
    try:
        r = run(tmp_path, containers=[PG, proxy(world)])
    finally:
        secret.chmod(0o600)
    assert r.returncode == 3, r.stderr
    (d,) = runs(tmp_path / "backups")
    assert "./helper/workspace/notes.md" in names(d / "agents.tar.gz")
    assert "./helper/workspace/locked" not in names(d / "agents.tar.gz")


HOST = {"OTODOCK_ENV_FILE": "{world}/env", "OTODOCK_AGENTS_DIR": "{world}/agents"}


def bounded(tmp_path, world, seconds: float = 30, env: dict | None = None) -> subprocess.CompletedProcess:
    """A bare-metal run that must end by itself: one that waits past
    ``seconds`` fails the test, and nothing of it outlives the test."""
    host = {k: v.format(world=world) for k, v in HOST.items()}
    p = start(tmp_path, containers=[PG_T1], env={**host, **(env or {})})
    try:
        out, err = p.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        _end(p)
        pytest.fail(f"the backup was still waiting after {seconds} seconds")
    return subprocess.CompletedProcess(p.args, p.returncode, out, err)


def _rollback_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t(x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()


def test_a_pipe_at_a_databases_name_beside_its_side_file_is_never_waited_on(tmp_path, world):
    """The name beside a ``-wal`` file is looked at before anything opens it:
    a pipe there is no database, and its side file is an ordinary file."""
    app = world / "agents" / "helper" / "app-data" / "app1"
    os.mkfifo(app / "cache")
    (app / "cache-wal").write_bytes(b"not a database's journal")
    r = bounded(tmp_path, world)
    assert r.returncode == 0, r.stderr
    (d,) = runs(tmp_path / "backups")
    got = names(d / "agents.tar.gz")
    assert "./helper/app-data/app1/cache-wal" in got and "./helper/app-data/app1/cache" not in got
    assert "./helper/app-data/app1/app.db" in got


@pytest.mark.parametrize("side, header", [
    ("-wal", b"\x37\x7f\x06\x82" + b"\x00" * 28),
    ("-journal", b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + b"\x00" * 20),
    ("-wal", b"an ordinary file"),
])
def test_a_link_at_a_databases_name_is_never_opened(tmp_path, world, side, header):
    """A link beside a side file points at a pipe of this test's own folder:
    nothing opens it. The side file is left out only when its own header is
    SQLite's (what SQLite below 3.32 leaves beside the link it opened)."""
    app = world / "agents" / "helper" / "app-data" / "app1"
    os.mkfifo(tmp_path / "elsewhere")
    os.symlink(tmp_path / "elsewhere", app / "linked.db")
    (app / f"linked.db{side}").write_bytes(header)
    r = bounded(tmp_path, world)
    assert r.returncode == 0, r.stderr
    (d,) = runs(tmp_path / "backups")
    with tarfile.open(d / "agents.tar.gz") as tf:
        members = {m.name: m for m in tf.getmembers()}
    assert members["./helper/app-data/app1/linked.db"].issym()
    assert (f"./helper/app-data/app1/linked.db{side}" in members) is (header == b"an ordinary file")


def test_a_pipe_at_a_rollback_databases_journal_is_an_error_never_a_wait(tmp_path, world):
    """SQLite opens a database's ``-journal`` by name and would wait on a
    pipe there for ever: the database is not copied, the rest is."""
    app = world / "agents" / "helper" / "app-data" / "app1"
    _rollback_db(app / "orders.db")
    os.mkfifo(app / "orders.db-journal")
    r = bounded(tmp_path, world)
    assert r.returncode == 3, r.stderr
    assert "error copying database" in r.stderr and "orders.db-journal is not a regular file" in r.stderr
    (d,) = runs(tmp_path / "backups")
    got = names(d / "agents.tar.gz")
    assert "./helper/app-data/app1/orders.db" not in got and "./helper/app-data/app1/app.db" in got
    assert manifest(d)["errors"] == "1"


# A writer that stops in the middle of a write: with a small cache SQLite moves
# changed pages into the database file before the commit, keeping what they held
# in the -journal, and the process then ends without a word.
_HALF_WRITE = """\
import os, sqlite3, sys
con = sqlite3.connect(sys.argv[1], isolation_level=None)
con.execute("PRAGMA cache_size=5")
con.execute("BEGIN")
con.execute("UPDATE t SET x = x + 1000000, pad = randomblob(300)")
os._exit(0)
"""


def test_a_database_whose_writer_stopped_mid_write_is_copied_whole(tmp_path, world):
    """SQLite cannot open such a database read-only where it is (it must put
    it right from its -journal first): the copy takes the journal with it and
    holds every row as it was before that write, whole, while the database
    and its journal in the tree stay as they were."""
    app = world / "agents" / "helper" / "app-data" / "app1"
    db = app / "orders.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t(x, pad)")
    con.executemany("INSERT INTO t VALUES (?, randomblob(300))", [(i,) for i in range(4000)])
    con.commit()
    con.close()
    subprocess.run([sys.executable, "-c", _HALF_WRITE, str(db)], check=True, timeout=60)
    journal = app / "orders.db-journal"
    assert journal.stat().st_size > 0
    left = (db.read_bytes(), journal.read_bytes())
    r = bounded(tmp_path, world)
    assert r.returncode == 0, r.stderr
    (d,) = runs(tmp_path / "backups")
    with tarfile.open(d / "agents.tar.gz") as tf:
        assert "./helper/app-data/app1/orders.db-journal" not in tf.getnames()
        copy = tmp_path / "orders-copy.db"
        copy.write_bytes(tf.extractfile("./helper/app-data/app1/orders.db").read())
    con = sqlite3.connect(copy)
    assert con.execute("SELECT count(*), sum(x) FROM t").fetchone() == (4000, sum(range(4000)))
    assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    con.close()
    assert (db.read_bytes(), journal.read_bytes()) == left


# Loaded by the tree program's interpreter at start (PYTHONPATH): SQLite's
# read-only open of a live database never returns, or fails in the child.
_STUCK_OPEN = """\
import sqlite3, time
_real = sqlite3.connect
def _connect(database, *a, **k):
    if "mode=ro" in str(database):
        {action}
    return _real(database, *a, **k)
sqlite3.connect = _connect
"""


def _hook(tmp_path, action: str) -> str:
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    (site / "sitecustomize.py").write_text(_STUCK_OPEN.format(action=action))
    return str(site)


def test_a_database_that_does_not_open_in_time_is_an_error_and_the_rest_is_archived(tmp_path, world):
    """SQLite's own open can wait (a name swapped for a pipe after the
    check): the parent ends it at the bound, and the backup goes on."""
    r = bounded(tmp_path, world, env={"PYTHONPATH": _hook(tmp_path, "time.sleep(3600)"),
                                      "OTODOCK_BACKUP_TEST_OPEN_LIMIT_S": "2"})
    assert r.returncode == 3, r.stderr
    assert "error copying database" in r.stderr and "did not open within 2 seconds" in r.stderr
    (d,) = runs(tmp_path / "backups")
    got = names(d / "agents.tar.gz")
    assert "./helper/app-data/app1/app.db" not in got and "./helper/workspace/notes.md" in got


# Loaded by the tree program's interpreter at start (PYTHONPATH): SQLite's
# read-only open of orders.db gets a connection whose copy first puts a pipe
# of this test's own at the database's journal name, after the first read.
_PIPE_AT_THE_COPY = """\
import os, sqlite3, urllib.parse
_real = sqlite3.connect
class _Pipe(sqlite3.Connection):
    def backup(self, *a, **k):
        os.mkfifo(self.where + "-journal")
        return super().backup(*a, **k)
def _connect(database, *a, **k):
    where = urllib.parse.unquote(str(database).split("file://", 1)[-1].split("?", 1)[0])
    if "mode=ro" in str(database) and os.path.basename(where) == "orders.db":
        con = _real(database, *a, factory=_Pipe, **k)
        con.where = where
        return con
    return _real(database, *a, **k)
sqlite3.connect = _connect
"""


def test_the_copy_holds_its_read_and_opens_nothing_after_the_first_read(tmp_path, world):
    """The time limit covers SQLite's open and first read alone: the copy
    then holds that read to its end and opens no file of the tree by name,
    so a pipe put at the journal's name after the first read is never
    opened and the copy ends."""
    _rollback_db(world / "agents" / "helper" / "app-data" / "app1" / "orders.db")
    site = tmp_path / "pipe-site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(_PIPE_AT_THE_COPY)
    r = bounded(tmp_path, world, env={"PYTHONPATH": str(site)})
    assert r.returncode == 0, r.stderr
    (d,) = runs(tmp_path / "backups")
    assert "./helper/app-data/app1/orders.db" in names(d / "agents.tar.gz")
    assert stat.S_ISFIFO(os.lstat(world / "agents" / "helper" / "app-data" / "app1" / "orders.db-journal").st_mode)


def test_a_child_that_fails_leaves_each_member_once_and_nothing_behind(tmp_path, world):
    """The copy runs in a forked child: whatever ends it, the parent's
    archive stream is written by the parent alone, and the run's scratch
    folder goes."""
    (world / "agents" / "helper" / "workspace" / ("x" * 40 + ".md")).write_text("y" * 70_000)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    r = bounded(tmp_path, world, env={"PYTHONPATH": _hook(tmp_path, "raise RuntimeError('refused')"),
                                      "TMPDIR": str(scratch)})
    assert r.returncode == 3, r.stderr
    assert "error copying database" in r.stderr and "refused" in r.stderr
    (d,) = runs(tmp_path / "backups")
    got = names(d / "agents.tar.gz")
    assert len(got) == len(set(got))
    assert subprocess.run(["gzip", "-t", str(d / "agents.tar.gz")]).returncode == 0
    assert list(scratch.iterdir()) == []


# Loaded by the tree program's interpreter at start (PYTHONPATH): the copy of
# a member stops half way, as a short read of the file would.
_CUT_MEMBER = """\
import tarfile
_real = tarfile.copyfileobj
def _cut(src, dst, length=None, exception=OSError, bufsize=None):
    _real(src, dst, max(1, (length or 0) // 2), exception, bufsize)
    raise OSError("unexpected end of data")
tarfile.copyfileobj = _cut
"""


def test_a_cut_stream_leaves_no_run_directory(tmp_path, world):
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(_CUT_MEMBER)
    r = run(tmp_path, containers=[PG_T1],
            env={"OTODOCK_ENV_FILE": str(world / "env"), "OTODOCK_AGENTS_DIR": str(world / "agents"),
                 "PYTHONPATH": str(site)})
    assert r.returncode == 1, r.stderr
    assert "unexpected end of data" in r.stderr
    assert runs(tmp_path / "backups") == []
    assert [p.name for p in (tmp_path / "backups").iterdir()] == [LOCK]


def test_a_failed_dump_leaves_no_run_directory(tmp_path, world):
    r = run(tmp_path, containers=[PG, proxy(world)], env={"FAKE_PG_DUMP_RC": "1"})
    assert r.returncode == 1, r.stderr
    assert runs(tmp_path / "backups") == []
    assert [p.name for p in (tmp_path / "backups").iterdir()] == [LOCK]


def test_a_tree_that_does_not_fit_falls_back_to_the_database_alone(tmp_path, world):
    """The nightly line still leaves the database behind (exit 3), and the
    trees retention is about to prune go first, so tomorrow's tree may fit."""
    out = tmp_path / "backups"
    out.mkdir(mode=0o700)
    old = _old_run(out, "otodock-1.7.9-20260101-000000", 1)
    r = run(tmp_path, containers=[PG, proxy(world)],
            env={"FAKE_DU_K": str(1 << 40), "OTODOCK_BACKUP_RETAIN_TREES": "1"})
    assert r.returncode == 3, r.stderr
    assert "not enough free space" in r.stderr and "--db-only" in r.stderr and "INCOMPLETE" in r.stderr
    new = [d for d in runs(out) if d.name.startswith("otodock-1.8.0-")][0]
    assert sorted(p.name for p in new.iterdir()) == ["db.sql.gz", "env", "manifest"]
    m = manifest(new)
    assert m["agents"] == "skipped" and m["ssh_keys"] == "skipped" and m["sessions"] == "skipped" and m["complete"] == "1"
    assert not (old / "agents.tar.gz").exists() and manifest(old)["agents"] == "pruned"
    assert r.stderr.index("not enough free space") < r.stderr.index("INCOMPLETE")


def test_a_backup_folder_inside_the_agents_tree_is_refused(tmp_path, world):
    r = run(tmp_path, containers=[PG_T1], out=world / "agents" / "backups",
            env={"OTODOCK_ENV_FILE": str(world / "env"), "OTODOCK_AGENTS_DIR": str(world / "agents")})
    assert r.returncode == 1
    assert "inside the agents directory" in r.stderr


# ── retention ────────────────────────────────────────────────────────────────

def _old_run(out: Path, name: str, age: int, *, with_manifest: bool = True, tree: bool = True,
             extra: dict | None = None) -> Path:
    d = out / name
    d.mkdir(parents=True)
    (d / "db.sql.gz").write_bytes(b"x")
    if tree:
        (d / "agents.tar.gz").write_bytes(b"x")
        (d / "sessions.tar.gz").write_bytes(b"x")
    if with_manifest:
        (d / "manifest").write_text("otodock_version=1.7.9\nagents=present\nsessions=present\ncomplete=1\n")
    for name, text in (extra or {}).items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)
    t = 1_700_000_000 + age
    os.utime(d, (t, t))
    return d


@pytest.mark.parametrize("awk", ["mawk", "gawk", "busybox"])
def test_retention_orders_runs_by_their_time_under_every_awk(tmp_path, world, awk):
    """mawk (Debian's and Ubuntu's default awk) has no interval expressions:
    a ``{8}`` in the timestamp match never matched, every run sorted as "0"
    and the order fell back to the path name, so ``otodock-unknown-…`` counted
    as the newest run and ``1.9.0`` sorted above ``1.10.0``: after a 1.9 to
    1.10 upgrade retention pruned the newest runs first."""
    binary = shutil.which(awk)
    if binary is None:
        pytest.skip(f"{awk} is not installed")
    shim = tmp_path / "bin" / "awk"
    shim.parent.mkdir()
    shim.write_text(f'#!/bin/sh\nexec {binary} {"awk " if awk == "busybox" else ""}"$@"\n')
    shim.chmod(0o755)
    out = tmp_path / "backups"
    out.mkdir(mode=0o700)
    oldest = _old_run(out, "otodock-unknown-20260101-000000", 1)
    middle = _old_run(out, "otodock-1.9.0-20260301-000000", 2)
    newest = _old_run(out, "otodock-1.10.0-20260401-000000", 3)
    r = run(tmp_path, containers=[PG, proxy(world)], env={"OTODOCK_BACKUP_RETAIN": "2"})
    assert r.returncode == 0, r.stderr
    assert newest.exists() and not middle.exists() and not oldest.exists()
    assert len([d for d in runs(out) if d.name.startswith("otodock-1.8.0-")]) == 1


def test_retention_touches_only_the_runs_this_script_wrote(tmp_path, world):
    """``OTODOCK_BACKUP_DIR=~/otodock`` has sibling checkouts named
    ``otodock-*``: a directory is a candidate only when it is named like a
    run, and pruning removes the files the script writes, never a folder
    holding anything else."""
    out = tmp_path / "backups"
    out.mkdir(mode=0o700)
    checkout = out / "otodock-commercial"
    (checkout / "src").mkdir(parents=True)
    (checkout / "src" / "important.txt").write_text("keep")
    snapshot = out / "otodock-public-snapshot"
    snapshot.mkdir()
    (snapshot / "manifest").write_text("not ours")
    (snapshot / "db.sql.gz").write_bytes(b"not ours either")
    stale_name = out / "otodock-1.7.9-20250101-000000"
    for d in (checkout, snapshot):
        os.utime(d, (1_600_000_000, 1_600_000_000))
    annotated = _old_run(out, "otodock-1.7.9-20260101-000000", 1, extra={"notes.txt": "the operator's note"})
    plain = _old_run(out, "otodock-1.7.9-20260102-000000", 2)
    stale = _old_run(out, stale_name.name, 0, with_manifest=False, extra={"db.sql.gz.part": "half"})
    r = run(tmp_path, containers=[PG, proxy(world)], env={"OTODOCK_BACKUP_RETAIN": "1"})
    assert r.returncode == 0, r.stderr
    assert (checkout / "src" / "important.txt").read_text() == "keep"
    assert (snapshot / "manifest").exists() and (snapshot / "db.sql.gz").exists()
    assert not plain.exists() and not stale.exists()
    # The annotated run lost the files the script wrote; the note and its folder stay.
    assert annotated.exists() and (annotated / "notes.txt").exists()
    assert not (annotated / "db.sql.gz").exists() and not (annotated / "manifest").exists()
    assert "holds files this script did not write" in r.stderr
    assert len([d for d in runs(out) if d.name.startswith("otodock-1.8.0-")]) == 1


def test_retention_keeps_runs_and_the_trees_of_the_newest_and_leaves_old_files_alone(tmp_path, world):
    out = tmp_path / "backups"
    out.mkdir(mode=0o700)
    olds = [_old_run(out, f"otodock-1.7.9-2026010{i}-000000", i) for i in range(1, 6)]
    stale = _old_run(out, "otodock-1.7.9-20250101-000000-2", 0, with_manifest=False)
    fresh = out / "otodock-1.7.9-fresh-no-manifest"
    fresh.mkdir()
    flat = out / "otodock-otodock-20260101-000000.sql.gz"
    flat.write_bytes(b"old flat dump")
    r = run(tmp_path, containers=[PG, proxy(world)],
            env={"OTODOCK_BACKUP_RETAIN": "4", "OTODOCK_BACKUP_RETAIN_TREES": "2"})
    assert r.returncode == 0, r.stderr
    kept = runs(out)
    assert len(kept) == 4 + 1  # four runs with manifests, plus the fresh manifest-less one
    assert not olds[0].exists() and not olds[1].exists()
    assert not stale.exists() and fresh.exists()
    assert flat.exists()
    new = [d for d in kept if d.name.startswith("otodock-1.8.0-")][0]
    assert (new / "agents.tar.gz").exists() and manifest(new)["agents"] == "present"
    assert (olds[4] / "agents.tar.gz").exists() and manifest(olds[4])["agents"] == "present"
    for d in (olds[2], olds[3]):
        assert not (d / "agents.tar.gz").exists() and manifest(d)["agents"] == "pruned"
        assert not (d / "sessions.tar.gz").exists() and manifest(d)["sessions"] == "pruned"
        assert (d / "db.sql.gz").exists()
    # A second run: the pruning touched the older directories, which must not
    # make them count as the newest (the order is the time in the name).
    r = run(tmp_path, containers=[PG, proxy(world)], out=out,
            env={"OTODOCK_BACKUP_RETAIN": "4", "OTODOCK_BACKUP_RETAIN_TREES": "2"})
    assert r.returncode == 0, r.stderr
    with_trees = [d for d in runs(out) if (d / "agents.tar.gz").exists()]
    assert len(with_trees) == 2 and all(d.name.startswith("otodock-1.8.0-") for d in with_trees)
    assert not olds[2].exists()  # the fifth-newest complete run went


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes everywhere")
def test_a_wal_database_in_a_read_only_folder_is_copied_through_a_scratch_copy(tmp_path, world):
    """A stopped proxy's volume is mounted read-only; a WAL database closed
    cleanly has no -shm file and cannot be opened read-only there."""
    folder = world / "agents" / "helper" / ".codex"
    folder.mkdir()
    state = folder / "state_5.sqlite"
    con = sqlite3.connect(state)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t(x)")
    con.execute("INSERT INTO t VALUES (9)")
    con.commit()
    con.close()
    assert not (folder / "state_5.sqlite-shm").exists()
    folder.chmod(0o555)
    try:
        r = run(tmp_path, containers=[PG_T1],
                env={"OTODOCK_ENV_FILE": str(world / "env"), "OTODOCK_AGENTS_DIR": str(world / "agents")})
    finally:
        folder.chmod(0o755)
    assert r.returncode == 0, r.stderr
    (d,) = runs(tmp_path / "backups")
    with tarfile.open(d / "agents.tar.gz") as tf:
        copy = tmp_path / "copy.sqlite"
        copy.write_bytes(tf.extractfile("./helper/.codex/state_5.sqlite").read())
    assert sqlite3.connect(copy).execute("SELECT x FROM t").fetchall() == [(9,)]
    assert not (folder / "state_5.sqlite-shm").exists()


# ── the compute machine is never read live (S-B4) ─────────────────────────────

COMPUTE = {"id": "compute-t2", "name": "otodock-compute-1",
           "labels": {"com.docker.compose.project": "otodock",
                      "com.docker.compose.service": "otodock-compute"}}
SIGNED_IN = {"FAKE_ADMIN_COMPUTE": "The compute machine is signed in.", "FAKE_ADMIN_DRAIN": "",
             "FAKE_ADMIN_RESUME": ""}


def _order(tmp_path) -> list[str]:
    out = []
    for c in calls(tmp_path):
        if c[0] in ("stop", "start") and "compute-t2" in c:
            out.append(c[0])
        elif c[0] == "exec" and any(a.endswith("admin_cli.py") for a in c):
            out.append(c[c.index(next(a for a in c if a.endswith("admin_cli.py"))) + 1])
        elif c[0] == "exec" and "pg_dump" in c:
            out.append("dump")
    return out


def test_the_backup_holds_new_work_until_the_compute_service_signed_in_again(tmp_path, world):
    """The server waits for open work and holds new work, the compute service
    stops, the copy runs, and only then: the compute service starts, its
    sign-in is waited for, and the server takes new work again. Work is never
    handed back in between, when nothing could run it."""
    r = run(tmp_path, containers=[PG, proxy(world), COMPUTE], env=SIGNED_IN)
    assert r.returncode == 0, r.stderr
    assert _order(tmp_path) == ["drain", "stop", "dump", "start", "compute", "resume"]
    assert ["stop", "-t", "120", "compute-t2"] in calls(tmp_path)
    waited = next(c for c in calls(tmp_path) if c[0] == "exec" and "compute" in c)
    assert waited[-2:] == ["--wait", "120"]
    assert "stopping the compute service for the backup" in r.stderr
    (d,) = runs(tmp_path / "backups")
    assert manifest(d)["complete"] == "1"


def test_a_compute_machine_that_does_not_stop_fails_the_run_and_work_comes_back(tmp_path, world):
    r = run(tmp_path, containers=[PG, proxy(world), COMPUTE], env={**SIGNED_IN, "FAKE_STOP_RC": "1"})
    assert r.returncode == 1
    assert "the compute service did not stop: a backup never reads a live compute machine" in r.stderr
    assert _order(tmp_path) == ["drain", "stop", "start", "compute", "resume"]
    assert not runs(tmp_path / "backups")


def test_the_compute_machine_starts_and_work_comes_back_when_the_run_fails_after_its_stop(tmp_path, world):
    r = run(tmp_path, containers=[PG, proxy(world), COMPUTE], env={**SIGNED_IN, "FAKE_PG_DUMP_RC": "1"})
    assert r.returncode != 0
    assert _order(tmp_path)[-3:] == ["start", "compute", "resume"]


def test_an_exit_during_the_drain_starts_the_compute_machine_and_takes_work_again(tmp_path, world):
    """A Ctrl+C or a cron timeout while the backup waits for open work: the
    compute container, which a manual stop would leave stopped for good, is
    started, and the platform takes new work again (never held until the
    drain's own timeout)."""
    r = run(tmp_path, containers=[PG, proxy(world), COMPUTE], env={**SIGNED_IN, "FAKE_ADMIN_DRAIN_SIGNAL": "TERM"})
    assert r.returncode != 0
    assert _order(tmp_path) == ["drain", "start", "compute", "resume"]
    assert not runs(tmp_path / "backups")


def test_a_compute_service_that_does_not_sign_in_again_is_said_and_work_comes_back(tmp_path, world):
    """The compute service started and did not sign in within the wait: the
    backup is complete, the script says what is refused meanwhile and where
    to look, and the platform takes work again all the same. Held work must
    not wait on a service that may never start."""
    env = {**SIGNED_IN, "FAKE_ADMIN_COMPUTE": "The compute machine is not signed in. Chats, tasks and calls "
           "cannot run until it is.", "FAKE_ADMIN_COMPUTE_RC": "1"}
    r = run(tmp_path, containers=[PG, proxy(world), COMPUTE], env=env)
    assert r.returncode == 0, r.stderr
    assert _order(tmp_path) == ["drain", "stop", "dump", "start", "compute", "resume"]
    assert ("the compute service did not sign in within 120 seconds: chats, tasks and calls are refused "
            "until it does. To see why, run: docker compose logs otodock-compute") in r.stderr


def test_a_compute_service_that_does_not_start_again_is_said_and_work_comes_back(tmp_path, world):
    r = run(tmp_path, containers=[PG, proxy(world), COMPUTE], env={**SIGNED_IN, "FAKE_START_RC": "1"})
    assert "the compute service did not start again: docker compose start otodock-compute" in r.stderr
    assert _order(tmp_path)[-2:] == ["start", "resume"]


@pytest.mark.parametrize("why, env, args", [
    ("db-only", SIGNED_IN, ("--db-only",)),
])
def test_a_backup_leaves_a_compute_machine_alone_when_it_holds_nothing_the_backup_reads(tmp_path, world, why, env,
                                                                                      args):
    r = run(tmp_path, *args, containers=[PG, proxy(world), COMPUTE], env=env)
    assert r.returncode == 0, r.stderr
    assert not any(c[0] in ("stop", "start") for c in calls(tmp_path)), why
    assert "drain" not in _order(tmp_path) and "dump" in _order(tmp_path)


def test_a_stopped_compute_container_is_not_started_by_a_backup(tmp_path, world):
    r = run(tmp_path, containers=[PG, proxy(world), {**COMPUTE, "running": False}], env=SIGNED_IN)
    assert r.returncode == 0, r.stderr
    assert _order(tmp_path) == ["dump"]


def test_an_install_without_a_compute_service_backs_up_as_before(tmp_path, world):
    r = run(tmp_path, containers=[PG, proxy(world)])
    assert r.returncode == 0, r.stderr
    assert _order(tmp_path) == ["dump"]


# ── the hold lasts as long as the copy ───────────────────────────────────────

# The script's stand-in for the five minutes between two asks.
RENEW = {"OTODOCK_BACKUP_TEST_HOLD_RENEW_S": "0.2"}


def _hold_the_copy(tmp_path) -> Path:
    """The dump's gzip waits for the file this returns, 20 seconds at most:
    the copy lasts until the test lets it go."""
    gate = tmp_path / "copy-may-end"
    shim = tmp_path / "bin" / "gzip"
    shim.parent.mkdir(exist_ok=True)
    shim.write_text(f'#!/bin/sh\nn=0\nwhile [ ! -e "{gate}" ] && [ "$n" -lt 200 ]; do sleep 0.1; n=$((n + 1)); done\n'
                    f'exec {shutil.which("gzip")} "$@"\n')
    shim.chmod(0o755)
    return gate


def _slow_sign_in(tmp_path, world, seconds: float) -> dict:
    """The proxy container with an admin command whose ``compute --wait``
    takes ``seconds``, as the sign-in of a compute service that boots does.
    The fake docker runs it when FAKE_ADMIN_COMPUTE is not set."""
    home = tmp_path / "admin"
    (home / "venv" / "bin").mkdir(parents=True)
    python = home / "venv" / "bin" / "python"
    python.write_text(f'#!/bin/sh\n[ "$2" != compute ] || sleep {seconds}\n')
    python.chmod(0o755)
    container = proxy(world)
    container["mounts"]["/opt/otodock/proxy"] = {"source": str(home)}
    return container


def _asked(tmp_path, word: str) -> int:
    """How often the fake docker's record names ``word``. Counted in its
    text: a script that still runs may be half way through a line."""
    log = tmp_path / "docker.log"
    return log.read_text().count(f'"{word}"') if log.exists() else 0


def _until(done, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while not done():
        if time.monotonic() >= end:
            return False
        time.sleep(0.05)
    return True


def _group_is_empty(p: subprocess.Popen) -> bool:
    """Whether no process is left in the group the script led."""
    try:
        os.killpg(p.pid, 0)
    except ProcessLookupError:
        return True
    return False


def _end(p: subprocess.Popen) -> None:
    """Nothing of a run outlives its test."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(p.pid, signal.SIGKILL)
    if p.returncode is None:
        p.communicate(timeout=30)


def test_a_copy_that_outlasts_the_hold_asks_the_drain_again_until_the_compute_service_is_back(tmp_path, world):
    """The platform ends a hold nobody asks for again. A copy can last longer
    than that, so the script asks again and again while it copies. It stops
    asking before the compute service starts: the sign-in here takes five
    times the pause between two asks, and nobody asks during it. Nothing of
    the asking is left once the script has ended."""
    gate = _hold_the_copy(tmp_path)
    p = start(tmp_path, containers=[PG, _slow_sign_in(tmp_path, world, 1), COMPUTE],
              env={"FAKE_ADMIN_DRAIN": "", "FAKE_ADMIN_RESUME": "", **RENEW})
    try:
        assert _until(lambda: _asked(tmp_path, "drain") >= 3), "nobody asks the drain again during the copy"
        gate.touch()
        _, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        assert _until(lambda: _group_is_empty(p), 5), "a process of the script outlived it"
        order = _order(tmp_path)
        time.sleep(1)
        assert _order(tmp_path) == order, "the drain is asked again after the script has ended"
    finally:
        _end(p)
    assert order[0] == "drain" and order[-3:] == ["start", "compute", "resume"]
    assert "drain" not in order[order.index("start"):]
    assert sorted(set(order)) == ["compute", "drain", "dump", "resume", "start", "stop"]
    assert order.count("stop") == 1 and order.count("dump") == 1
    (d,) = runs(tmp_path / "backups")
    assert manifest(d)["complete"] == "1"


def test_a_backup_that_ends_before_the_next_ask_does_not_wait_for_it(tmp_path, world):
    """Five minutes lie between two asks. A backup shorter than that ends at
    once and leaves no process waiting for the next one: such a process
    would keep the script's output open, and the lock of the next run."""
    p = start(tmp_path, containers=[PG, proxy(world), COMPUTE], env=SIGNED_IN)
    try:
        _, err = p.communicate(timeout=30)
        assert p.returncode == 0, err
        assert _until(lambda: _group_is_empty(p), 5), "a process of the script outlived it"
    finally:
        _end(p)
    assert _order(tmp_path) == ["drain", "stop", "dump", "start", "compute", "resume"]
    again = run(tmp_path, containers=[PG, proxy(world), COMPUTE], env=SIGNED_IN)
    assert again.returncode == 0, again.stderr


def test_a_backup_stopped_during_its_copy_leaves_nobody_asking(tmp_path, world):
    """A timeout ends the script during its copy: the asking stops, the
    compute service starts and signs in, the platform takes work again, and
    the drain is asked no more."""
    gate = _hold_the_copy(tmp_path)
    p = start(tmp_path, containers=[PG, _slow_sign_in(tmp_path, world, 1), COMPUTE],
              env={"FAKE_ADMIN_DRAIN": "", "FAKE_ADMIN_RESUME": "", **RENEW})
    try:
        assert _until(lambda: _asked(tmp_path, "drain") >= 2), "nobody asks the drain again during the copy"
        p.terminate()
        gate.touch()
        p.communicate(timeout=60)
        assert p.returncode != 0
        assert _until(lambda: _group_is_empty(p)), "a process of the script outlived it"
        order = _order(tmp_path)
        time.sleep(1)
        assert _order(tmp_path) == order, "the drain is asked again after the script has ended"
    finally:
        _end(p)
    assert order[-3:] == ["start", "compute", "resume"]
    assert "drain" not in order[order.index("start"):]
    assert not runs(tmp_path / "backups")


def test_a_killed_backup_stops_asking_so_its_hold_can_lapse(tmp_path, world):
    """A kill runs no trap: the compute service stays stopped and nobody
    calls the hold off. The asking ends with the script all the same, so the
    platform's own lapse ends the hold. A loop that went on asking would
    hold new work for good."""
    gate = _hold_the_copy(tmp_path)
    p = start(tmp_path, containers=[PG, proxy(world), COMPUTE], env={**SIGNED_IN, **RENEW})
    try:
        assert _until(lambda: _asked(tmp_path, "drain") >= 2), "nobody asks the drain again during the copy"
        p.kill()
        gate.touch()
        p.communicate(timeout=60)
        assert _until(lambda: _group_is_empty(p)), "the asking outlived the killed script"
        order = _order(tmp_path)
        time.sleep(1)
        assert _order(tmp_path) == order, "the drain is asked again after the script is gone"
    finally:
        _end(p)
    assert "start" not in order and "resume" not in order


def test_what_a_killed_backup_leaves_does_not_keep_the_next_one_out(tmp_path, world):
    """What is left of a killed run waits up to five minutes for its next
    ask. It holds neither the output of the run nor its lock: whoever
    started the run is not kept waiting, and a backup started by hand right
    after is not told that another one is running."""
    gate = _hold_the_copy(tmp_path)
    p = start(tmp_path, containers=[PG, proxy(world), COMPUTE], env=SIGNED_IN)
    try:
        assert _until(lambda: _asked(tmp_path, "pg_dump") >= 1), "the run never reached its copy"
        p.kill()
        gate.touch()
        p.communicate(timeout=20)
        again = run(tmp_path, containers=[PG, proxy(world), COMPUTE], env=SIGNED_IN)
    finally:
        _end(p)
    assert again.returncode == 0, again.stderr


# ── one backup at a time ─────────────────────────────────────────────────────

REFUSED = "backup.sh: error: Another backup is running. Wait for it to finish, then run this one again.\n"


def test_a_second_backup_beside_the_first_is_refused_and_touches_nothing(tmp_path, world):
    """Two cron lines that overlap: the first run ends, starts the compute
    service and takes work again while the second still copies, which then
    reads a live compute machine. The second run is refused in one sentence
    before it drains, stops or copies anything."""
    gate = _hold_the_copy(tmp_path)
    first = start(tmp_path, containers=[PG, proxy(world), COMPUTE], env=SIGNED_IN)
    try:
        assert _until(lambda: _asked(tmp_path, "pg_dump") >= 1), "the first run never reached its copy"
        second = run(tmp_path, containers=[PG, proxy(world), COMPUTE], env=SIGNED_IN)
        gate.touch()
        _, err = first.communicate(timeout=60)
    finally:
        _end(first)
    assert (second.returncode, second.stdout, second.stderr) == (1, "", REFUSED)
    assert first.returncode == 0, err
    assert _order(tmp_path) == ["drain", "stop", "dump", "start", "compute", "resume"]
    (d,) = runs(tmp_path / "backups")
    assert manifest(d)["complete"] == "1"
    # The lock ends with the run that took it.
    third = run(tmp_path, containers=[PG, proxy(world), COMPUTE], env=SIGNED_IN)
    assert third.returncode == 0, third.stderr
    assert len(runs(tmp_path / "backups")) == 2


def test_a_database_only_run_takes_no_lock_and_waits_for_none(tmp_path, world):
    """It reads no tree and stops nothing, so it may run beside a full
    backup. A cron line for it at the same minute never keeps the full run
    out, and is never kept out by it."""
    gate = _hold_the_copy(tmp_path)
    first = start(tmp_path, containers=[PG, proxy(world), COMPUTE], env=SIGNED_IN)
    try:
        assert _until(lambda: _asked(tmp_path, "pg_dump") >= 1), "the first run never reached its copy"
        beside = run(tmp_path, "--db-only", containers=[PG, proxy(world), COMPUTE], env=SIGNED_IN)
        gate.touch()
        _, err = first.communicate(timeout=60)
    finally:
        _end(first)
    assert beside.returncode == 0 and "Another backup" not in beside.stderr, beside.stderr
    assert first.returncode == 0, err
    # A database-only run that is at work keeps no full run out either.
    gate.unlink()
    slow = start(tmp_path, "--db-only", containers=[PG, proxy(world), COMPUTE], env=SIGNED_IN)
    try:
        assert _until(lambda: _asked(tmp_path, "pg_dump") >= 3), "the database-only run never reached its dump"
        full = run(tmp_path, containers=[PG, proxy(world), COMPUTE], env=SIGNED_IN)
    finally:
        gate.touch()
        slow.communicate(timeout=60)
        _end(slow)
    assert full.returncode in (0, 3) and "Another backup" not in full.stderr, full.stderr


def test_a_host_whose_flock_cannot_lock_backs_up_as_before(tmp_path, world):
    """The lock is a help, never a condition: a host with no flock, or with
    one that does not know the script's options, backs up and reads nothing
    about it."""
    fake = tmp_path / "bin" / "flock"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\necho "flock: unrecognized option" >&2\nexit 64\n')
    fake.chmod(0o755)
    r = run(tmp_path, containers=[PG, proxy(world), COMPUTE], env=SIGNED_IN)
    assert r.returncode == 0, r.stderr
    assert "flock" not in r.stderr and "Another backup" not in r.stderr
    assert _order(tmp_path) == ["drain", "stop", "dump", "start", "compute", "resume"]


@pytest.mark.parametrize("folder, warned", [("backups", False), ("backups/nightly", False), ("kept", True),
                                            ("scripts/backups", True)])
def test_a_backup_folder_the_compute_unit_can_read_is_named(tmp_path, world, folder, warned):
    """On bare metal the compute unit reads the whole install folder but its
    backups/ folder: a backup kept anywhere else inside it is said."""
    install = tmp_path / "install"
    (install / "scripts").mkdir(parents=True)
    shutil.copy2(BACKUP, install / "scripts" / "backup.sh")
    env = _env(tmp_path, [PG_T1], out=install / folder,
               env={"OTODOCK_ENV_FILE": str(world / "env"), "OTODOCK_AGENTS_DIR": str(world / "agents")})
    r = subprocess.run(["bash", str(install / "scripts" / "backup.sh")], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert ("where the platform's own sessions can read it" in r.stderr) is warned


# ── the walk by descriptor ───────────────────────────────────────────────────

def _restore_check(archive: Path, into: Path) -> subprocess.CompletedProcess:
    """restore.sh's own check of a tree archive (its archive filter and its
    check program, taken from the script's text), as it runs it on this host."""
    text = RESTORE.read_text()

    def heredoc(opener: str) -> str:
        start = text.index(opener) + len(opener)
        return text[start:text.index("\nPY\n", start)]

    program = heredoc("FILTER_PY=$(cat <<'PY'\n") + "\n" + heredoc('VALIDATE_PY="$FILTER_PY"$(cat <<\'PY\'\n')
    into.mkdir(exist_ok=True)
    return subprocess.run([sys.executable, "-c", program, str(into)], input=archive.read_bytes(),
                          capture_output=True, timeout=120)


def _deep_tree(top: Path) -> None:
    """Under ``top``: 17 folders of 242-byte names, each inside the one before,
    made from its parent's descriptor (the deepest path passes the system's
    limit), with a small file, a file of a 200-byte name and two files whose
    paths inside the tree are exactly 3,840 and 3,841 bytes in the 15th
    folder, and a file in the last."""
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for i in range(17):
            name = f"{i:02d}" + "d" * 240
            os.mkdir(name, dir_fd=fd)
            inner = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = inner
            for leaf_name in ({14: ("small.txt", "l" * 200, "a" * 188, "b" * 189), 16: ("inside.txt",)}.get(i, ())):
                w = os.open(leaf_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=fd)
                os.write(w, b"x")
                os.close(w)
    finally:
        os.close(fd)


def test_a_path_too_long_to_restore_is_skipped_and_named_and_the_backup_restores(tmp_path, world):
    """restore.sh writes a member back by its whole path, which the system
    caps: an entry whose path inside the tree passes 3,840 bytes is skipped
    with one line (a folder is not walked), the rest is archived, and the
    archive passes restore.sh's own check."""
    _deep_tree(world / "agents" / "helper")
    r = bounded(tmp_path, world)
    assert r.returncode == 0, r.stderr
    said = [line for line in r.stderr.splitlines() if line.startswith("skip (path too long to restore): ")]
    assert sorted(line.rsplit("/", 1)[1][:3] for line in said) == ["15d", "bbb", "lll"], r.stderr[-800:]
    (d,) = runs(tmp_path / "backups")
    got = names(d / "agents.tar.gz")
    assert max(len(n.encode()) for n in got) <= 2 + 3840
    assert any(n.endswith("/14" + "d" * 240 + "/small.txt") for n in got)
    at_the_bound = [n for n in got if n.endswith("/" + "a" * 188)]
    assert len(at_the_bound) == 1 and len(at_the_bound[0].encode()) == 2 + 3840
    assert not any("/15" + "d" * 240 in n or n.endswith("inside.txt") for n in got)
    assert "./helper/workspace/notes.md" in got and "./helper/app-data/app1/app.db" in got
    check = _restore_check(d / "agents.tar.gz", tmp_path / "restore-into")
    assert check.returncode == 0, check.stderr.decode(errors="replace")[-600:]
    assert f"{len(got)} member(s) checked" in check.stderr.decode()


# Loaded by the tree program's interpreter at start (PYTHONPATH): the first
# time the program reaches for the entry named NAME (the walk's open of it, or
# a by-path read of it; with AT "connect", only SQLite's open of it; with AT
# "lstat", only tarfile's look at it), the entry is first swapped for what SWAP
# says: a link to a folder or a file of this test's own temporary folder, a
# pipe, a socket, a regular file, or nothing (the entry is gone).
_SWAP = """\
import builtins, os, sqlite3
NAME, SWAP, TARGET, AT = {name!r}, {swap!r}, {target!r}, {at!r}
done = []
def swap(path):
    if done or os.path.basename(str(path)) != NAME:
        return
    done.append(1)
    os.rename(path, str(path) + ".moved")
    if SWAP == "pipe":
        os.mkfifo(path)
    elif SWAP == "file":
        with _bopen(path, "wb") as fh:
            fh.write(b"OUTSD")
    elif SWAP == "socket":
        import socket
        s = socket.socket(socket.AF_UNIX)
        s.bind(path)
        s.close()
    elif SWAP == "gone":
        pass
    else:
        os.symlink(TARGET, path)
def at(name, dir_fd):
    return name if dir_fd is None else os.path.join(os.readlink(f"/proc/self/fd/{{dir_fd}}"), name)
_open, _scandir, _bopen, _connect, _lstat = os.open, os.scandir, builtins.open, sqlite3.connect, os.lstat
def os_open(path, flags, mode=0o777, *, dir_fd=None):
    if isinstance(path, str) and dir_fd is not None and AT == "any":
        swap(at(path, dir_fd))
    return _open(path, flags, mode, dir_fd=dir_fd)
def scandir(path="."):
    if isinstance(path, str) and AT == "any":
        swap(path)
    return _scandir(path)
def bopen(file, *a, **k):
    if isinstance(file, str) and AT == "any":
        swap(file)
    return _bopen(file, *a, **k)
def connect(database, *a, **k):
    if "mode=ro" in str(database):
        swap(str(database).split("file://", 1)[-1].split("?", 1)[0])
    return _connect(database, *a, **k)
def lstat(path, *a, **k):
    if isinstance(path, str) and AT == "lstat":
        swap(path)
    return _lstat(path, *a, **k)
os.open, os.scandir, builtins.open, sqlite3.connect, os.lstat = os_open, scandir, bopen, connect, lstat
"""


def _swap_hook(tmp_path, name: str, swap: str, target: Path | None = None, at: str = "any") -> str:
    site = tmp_path / "swap-site"
    site.mkdir(exist_ok=True)
    (site / "sitecustomize.py").write_text(_SWAP.format(name=name, swap=swap, target=str(target or ""), at=at))
    return str(site)


def test_a_folder_swapped_for_a_link_during_the_walk_is_never_walked_through(tmp_path, world):
    """Between the listing and the descent, a folder becomes a link to a
    folder of this test's own: nothing there is read or archived."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "outside.txt").write_text("not the tree's")
    r = bounded(tmp_path, world, env={"PYTHONPATH": _swap_hook(tmp_path, "workspace", "link", elsewhere)})
    assert r.returncode == 0, r.stderr
    (d,) = runs(tmp_path / "backups")
    got = names(d / "agents.tar.gz")
    assert not any("outside" in n for n in got)
    assert "./helper/app-data/app1/app.db" in got
    assert "skip (changed while being read)" in r.stderr


@pytest.mark.parametrize("swap, name", [("link", "notes.md"), ("pipe", "notes.md"), ("pipe", "empty.txt")])
def test_a_file_swapped_after_the_listing_is_never_followed_or_waited_on(tmp_path, world, swap, name):
    """Between the listing and the open, a file becomes a link to a file of
    this test's own (of the same size, so no size check catches it), or a
    pipe (an empty file too: a pipe has no size): it is skipped, nothing is
    read through it, and the archive holds no pipe."""
    (tmp_path / "outside.txt").write_text("OUTSD")
    assert (world / "agents" / "helper" / "workspace" / "notes.md").stat().st_size == 5
    (world / "agents" / "helper" / "workspace" / "empty.txt").write_text("")
    r = bounded(tmp_path, world, env={"PYTHONPATH": _swap_hook(tmp_path, name, swap, tmp_path / "outside.txt")})
    assert r.returncode == 0, r.stderr
    (d,) = runs(tmp_path / "backups")
    with tarfile.open(d / "agents.tar.gz") as tf:
        members = {m.name: m for m in tf.getmembers()}
        assert f"./helper/workspace/{name}" not in members
        assert not any(m.isfifo() for m in members.values())
        assert not any(m.isreg() and b"OUTSD" in tf.extractfile(m).read() for m in members.values())
    assert "skip (" in r.stderr


def test_a_link_that_became_a_file_when_it_is_read_is_skipped_and_the_archive_restores(tmp_path, world):
    """A link is archived only while it is still a link: one swapped for a
    regular file between the listing and tarfile's look at it is skipped
    with a line, the archive holds no member there and passes restore.sh's
    check (without the rule, a member with no data behind it breaks it)."""
    r = bounded(tmp_path, world, env={"PYTHONPATH": _swap_hook(tmp_path, "py", "file", at="lstat")})
    assert r.returncode == 0, r.stderr
    assert "skip (changed while being read): " in r.stderr and r.stderr.count("/helper/workspace/py\n") == 1
    (d,) = runs(tmp_path / "backups")
    assert subprocess.run(["gzip", "-t", str(d / "agents.tar.gz")]).returncode == 0
    got = names(d / "agents.tar.gz")
    assert "./helper/workspace/py" not in got and "./helper/workspace/notes.md" in got
    check = _restore_check(d / "agents.tar.gz", tmp_path / "restore-into")
    assert check.returncode == 0, check.stderr.decode(errors="replace")[-600:]


def test_a_link_that_became_a_socket_when_it_is_read_is_skipped_never_the_end(tmp_path, world):
    """tarfile has no member for a socket and answers nothing for it: a link
    swapped for one between the listing and tarfile's look at it is skipped
    with a line, and the run goes on."""
    r = bounded(tmp_path, world, env={"PYTHONPATH": _swap_hook(tmp_path, "py", "socket", at="lstat")})
    assert r.returncode == 0, r.stderr
    assert "skip (changed while being read): " in r.stderr and r.stderr.count("/helper/workspace/py\n") == 1
    (d,) = runs(tmp_path / "backups")
    got = names(d / "agents.tar.gz")
    assert "./helper/workspace/py" not in got and "./helper/workspace/notes.md" in got


def test_a_folder_that_vanished_before_the_walk_entered_it_is_counted(tmp_path, world):
    """A folder gone between the listing and the walk's open of it is
    counted as skipped, as a file that vanished is, with no line."""
    r = bounded(tmp_path, world, env={"PYTHONPATH": _swap_hook(tmp_path, "workspace", "gone")})
    assert r.returncode == 0, r.stderr
    assert any(line.endswith(" copied, 1 skipped, 0 error(s)") for line in r.stderr.splitlines()), r.stderr
    (d,) = runs(tmp_path / "backups")
    got = names(d / "agents.tar.gz")
    assert not any(n.startswith("./helper/workspace") for n in got) and "./helper/app-data/app1/app.db" in got


@pytest.mark.parametrize("target", ["outside", "beside"])
def test_a_database_name_swapped_for_a_link_at_sqlites_open_is_not_archived(tmp_path, world, target):
    """SQLite opens by name and follows a link: when the database it opened
    is not the file the walk checked (another database of this test's own,
    or a regular file beside it at a side file's name), nothing it read
    through the link is kept, and the database is skipped as changed."""
    app = world / "agents" / "helper" / "app-data" / "app1"
    other = tmp_path / "other.db" if target == "outside" else app / "app.db-journal"
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE t(x)")
    con.execute("INSERT INTO t VALUES ('not the tree''s')")
    con.commit()
    con.close()
    r = bounded(tmp_path, world, env={"PYTHONPATH": _swap_hook(tmp_path, "app.db", "link", other, at="connect")})
    assert r.returncode == 0, r.stderr
    (d,) = runs(tmp_path / "backups")
    with tarfile.open(d / "agents.tar.gz") as tf:
        member = tf.getmember("./helper/app-data/app1/app.db") if "./helper/app-data/app1/app.db" in tf.getnames() else None
        assert member is None or member.issym()
    assert "skip (changed while being read)" in r.stderr and "app.db" in r.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
@pytest.mark.parametrize("mode", [0, 0o400])
def test_a_folder_that_cannot_be_read_is_an_error_named_and_counted(tmp_path, world, mode):
    """A folder the account cannot open (mode 0) or can list but not enter
    (0o400) is left out with what is under it, and the run says so: one line
    naming the folder, exit 3, the manifest's errors=1 (which makes a restore
    of that run warn), the rest archived."""
    locked = world / "agents" / "helper" / "locked"
    (locked / "inner").mkdir(parents=True)
    (locked / "inner" / "f.txt").write_text("x")
    locked.chmod(mode)
    try:
        r = bounded(tmp_path, world)
    finally:
        locked.chmod(0o755)
    assert r.returncode == 3, r.stderr
    assert f"error reading {locked}: the folder cannot be read" in r.stderr
    assert "INCOMPLETE" in r.stderr
    (d,) = runs(tmp_path / "backups")
    got = names(d / "agents.tar.gz")
    assert not any("locked" in n for n in got) and "./helper/workspace/notes.md" in got
    assert manifest(d)["errors"] == "1" and manifest(d)["complete"] == "1"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_a_top_folder_that_cannot_be_entered_never_ends_well(tmp_path, world):
    """An agents folder the account can list but not enter: inside the
    server's container the run names it and exits 3 with no member of it
    (an empty archive is never a whole one); on this host the script refuses
    before it writes anything, with the sentence that names the folder."""
    top = world / "agents"
    top.chmod(0o400)
    try:
        docker = run(tmp_path, containers=[PG, proxy(world)])
        host = run(tmp_path, containers=[PG_T1], out=tmp_path / "host-backups",
                   env={"OTODOCK_ENV_FILE": str(world / "env"), "OTODOCK_AGENTS_DIR": str(top)})
    finally:
        top.chmod(0o755)
    assert docker.returncode == 3, docker.stderr
    assert f"error reading {top}/: the folder cannot be read" in docker.stderr
    (d,) = runs(tmp_path / "backups")
    assert names(d / "agents.tar.gz") == [] and manifest(d)["errors"] == "1"
    assert host.returncode == 1 and f"agents directory not readable at {top}" in host.stderr
    assert runs(tmp_path / "host-backups") == []


def test_a_folder_past_the_descriptors_left_is_an_error_never_the_end(tmp_path, world):
    """The walk holds one descriptor per level: a tree deeper than the host
    allows loses that part of the tree, counted as an error (exit 3)."""
    deep = world / "agents" / "helper" / "deep"
    for _ in range(60):
        deep = deep / "d"
    deep.mkdir(parents=True)
    (deep / "bottom.txt").write_text("x")
    site = tmp_path / "fd-site"
    site.mkdir()
    (site / "sitecustomize.py").write_text("import resource\nresource.setrlimit(resource.RLIMIT_NOFILE, (40, 40))\n")
    r = bounded(tmp_path, world, env={"PYTHONPATH": str(site)})
    assert r.returncode == 3, r.stderr
    assert "error reading" in r.stderr
    (d,) = runs(tmp_path / "backups")
    got = names(d / "agents.tar.gz")
    assert "./helper/workspace/notes.md" in got and not any(n.endswith("bottom.txt") for n in got)
    assert manifest(d)["errors"] == "1" and manifest(d)["complete"] == "1"


def _expected_members(top: Path) -> dict[str, tuple]:
    """What a backup of ``top`` holds, read from the tree by a walk of its
    own: every folder, link and regular file by kind, mode, link text and
    bytes (a database by its rows); pipes, a database's side files, a failed
    restore's folder at the root, the engines' sign-in files and own/ out."""
    want: dict[str, tuple] = {}
    for here, dirs, files in os.walk(top):
        rel_here = os.path.relpath(here, top)
        if rel_here == ".":
            dirs[:] = [d for d in dirs if not d.startswith(".pre-restore-")]
        parts = rel_here.split(os.sep)
        engine = (len(parts) == 3 and parts[1] == "workspace" and parts[2] in (".claude", ".codex")) or \
                 (len(parts) == 4 and parts[1] == "users" and parts[3] in (".claude", ".codex"))
        for name in sorted(dirs) + sorted(files):
            path = Path(here) / name
            rel = "./" + str(path.relative_to(top))
            if engine and name in ("own", ".credentials.json", "auth.json"):
                if name in dirs:
                    dirs.remove(name)
                continue
            st = os.lstat(path)
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                if name in dirs:
                    dirs.remove(name)
                want[rel] = ("link", os.readlink(path))
            elif stat.S_ISDIR(st.st_mode):
                want[rel] = ("dir", mode)
            elif stat.S_ISREG(st.st_mode):
                data = path.read_bytes()
                base = next((rel[: -len(s)] for s in ("-wal", "-shm", "-journal") if rel.endswith(s)), None)
                if base and base in want and want[base][0] == "db":
                    continue
                if data.startswith(b"SQLite format 3\x00"):
                    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                    want[rel] = ("db", mode, con.execute("SELECT * FROM t ORDER BY 1").fetchall())
                    con.close()
                else:
                    want[rel] = ("file", mode, data)
    return want


def test_an_honest_tree_backs_up_to_the_same_members(tmp_path, world):
    """Every kind of entry an install holds, links of every shape, a hard
    link, a live WAL database, a rollback one, an empty folder, a side file
    beside a link and beside a file: the archive holds what the tree holds,
    as before this program read by descriptor."""
    helper = world / "agents" / "helper"
    (helper / "empty").mkdir()
    (helper / "venv" / "bin").mkdir(parents=True)
    os.symlink("/usr/bin/python3", helper / "venv" / "bin" / "python")
    os.symlink("../workspace", helper / "link-to-dir")
    os.symlink("missing-target", helper / "dangling")
    (helper / "workspace" / "odd name #?%.txt").write_text("odd")
    (helper / "workspace" / "exec.sh").write_text("#!/bin/sh\n")
    (helper / "workspace" / "exec.sh").chmod(0o750)
    os.link(helper / "workspace" / "notes.md", helper / "workspace" / "notes-again.md")
    (helper / "workspace" / "log.db-wal").write_text("an ordinary file named like a side file")
    os.symlink("elsewhere.db", helper / "workspace" / "linked.db")
    (helper / "workspace" / "linked.db-journal").write_bytes(b"an ordinary file")
    state = helper / ".codex" / "state_5.sqlite"
    state.parent.mkdir()
    live = sqlite3.connect(state)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE t(x)")
    live.execute("INSERT INTO t VALUES (7)")
    live.commit()
    _rollback_db(helper / "app-data" / "app1" / "orders.db")
    os.mkfifo(helper / "pipe")
    want = _expected_members(world / "agents")
    r = bounded(tmp_path, world)
    live.close()
    assert r.returncode == 0, r.stderr
    (d,) = runs(tmp_path / "backups")
    got: dict[str, tuple] = {}
    with tarfile.open(d / "agents.tar.gz") as tf:
        for m in tf.getmembers():
            if m.issym():
                got[m.name] = ("link", m.linkname)
            elif m.isdir():
                got[m.name] = ("dir", m.mode)
            elif m.islnk():
                first = m.linkname if m.linkname.startswith("./") else f"./{m.linkname}"
                got[m.name] = got.get(first, ("hardlink", m.linkname))
            else:
                data = tf.extractfile(m).read()
                if data.startswith(b"SQLite format 3\x00"):
                    copy = tmp_path / "copy.sqlite"
                    copy.write_bytes(data)
                    con = sqlite3.connect(copy)
                    got[m.name] = ("db", m.mode, con.execute("SELECT * FROM t ORDER BY 1").fetchall())
                    con.close()
                    copy.unlink()
                else:
                    got[m.name] = ("file", m.mode, data)
    assert got == want


def _tree_program() -> str:
    text = BACKUP.read_text()
    start = text.index("TREE_PY=$(cat <<'PY'\n") + len("TREE_PY=$(cat <<'PY'\n")
    return text[start:text.index("\nPY\n)", start)]


# Every name the tree program calls. The program opens a file only through
# leaf() and a folder only from its parent's handle: a new call joins this set
# once it holds that rule.
_TREE_CALLS = frozenset({
    "Changed", "Path", "Path(path).as_uri", "add_regular", "add_sqlite", "allowed.add", "copy.close",
    "copy_database", "copy_out", "database_child", "engine_folder", "enter", "fail", "float", "folders.append",
    "head", "head(name, folder).startswith", "info.issym", "int", "iter", "leaf", "len", "live.backup",
    "live.close", "live.execute", "live.execute('SELECT count(*) FROM sqlite_master').fetchone", "memoryview",
    "name.endswith", "name.startswith", "next", "open_now", "open_now().items", "os.WEXITSTATUS", "os.WIFEXITED",
    "os._exit", "os.close", "os.dup2", "os.environ.get", "os.fchdir", "os.fdopen", "os.fork", "os.fsencode",
    "os.fstat", "os.kill", "os.listdir", "os.open", "os.path.abspath", "os.path.join", "os.pipe", "os.pread",
    "os.read", "os.set_blocking", "os.stat", "os.waitpid", "os.write", "out.addfile", "out.close",
    "out.gettarinfo", "print", "ready.poll", "ready.register", "rel.split", "resource.getrlimit",
    "resource.setrlimit", "said.decode", "said.decode(errors='replace').lstrip",
    "said.decode(errors='replace').lstrip('o').strip", "select.poll", "set", "shutil.rmtree", "side_left_out",
    "skip", "sorted", "sqlite3.connect", "stack.append", "stack.pop", "stat.S_ISDIR", "stat.S_ISLNK",
    "stat.S_ISREG", "str", "str(e).encode", "str(e).lower", "struct.pack", "sys.exit", "tarfile.open",
    "tempfile.mkdtemp", "walk",
})
_LEAF_CALLS = {"leaf(name, folder)", "leaf(name + suffix, folder)", "leaf(os.path.join(work, 'copy.db'))"}


def _calls(tree):
    """Each call of the program, with the name of the function it sits in
    ("" at the top level)."""
    inside = {id(node): fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
              for node in ast.walk(fn) if isinstance(node, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield inside.get(id(node), ""), node


def test_every_open_of_the_tree_program_goes_through_its_openers():
    """The guard of the tree program: no builtin open, no walk or read by
    path, every os.open in an opener with its flags, SQLite's open only in
    its child, which never closes the walk's descriptor of the database,
    every import at the top, and nothing a session's folder could put on the
    import path."""
    source = _tree_program()
    tree = ast.parse(source)
    assert ast.unparse(tree.body[0]) == "import sys"
    assert ast.unparse(tree.body[1]) == "sys.path[:] = [p for p in sys.path if p not in ('', '.')]"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert node in tree.body, f"an import inside a function: line {node.lineno}"
            assert not {a.name for a in node.names} & {"multiprocessing", "subprocess", "glob"}
    flags = {t.id: {n.attr for n in ast.walk(a.value) if isinstance(n, ast.Attribute)}
             for a in tree.body if isinstance(a, ast.Assign) for t in a.targets if isinstance(t, ast.Name)}
    assert flags["LEAF"] == {"O_RDONLY", "O_NOFOLLOW", "O_NONBLOCK", "O_NOCTTY", "O_CLOEXEC"}
    assert flags["FOLDER"] == {"O_RDONLY", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"}
    opens = {"leaf": {"LEAF"}, "walk": {"FOLDER", "os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC"},
             "copy_out": {"os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC"},
             "copy_database": {"os.O_WRONLY"}}
    seen = {"os.fork": set(), "sqlite3.connect": set(), "os.fchdir": set()}
    called = {ast.unparse(call.func) for _where, call in _calls(tree)}
    assert called == _TREE_CALLS, (
        f"new calls {sorted(called - _TREE_CALLS)}, gone {sorted(_TREE_CALLS - called)}: the tree program opens a "
        "file only through leaf() and a folder only from its parent's handle; a new call joins _TREE_CALLS once "
        "it holds that rule")
    leaf_calls = {ast.unparse(call) for _where, call in _calls(tree) if ast.unparse(call.func) == "leaf"}
    assert leaf_calls == _LEAF_CALLS, f"leaf() is handed a name in a held folder, never a path of the tree: {leaf_calls}"
    for where_, call in _calls(tree):
        f = ast.unparse(call.func)
        kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
        if f in seen:
            seen[f].add(where_)
        if f == "os.open":
            assert where_ in opens, where_
            assert ast.unparse(call.args[1]) in opens[where_], (where_, ast.unparse(call.args[1]))
        if f == "os.stat":
            assert kw.get("dir_fd") not in (None, "None") and kw.get("follow_symlinks") == "False", (
                f"{where_} calls os.stat by path: the tree program looks at a name only in a held folder")
        if f == "os.listdir":
            arg = ast.unparse(call.args[0])
            assert arg in ("folder", "'/proc/self/fd'"), (where_, arg)
        if where_ in ("database_child", "copy_database", "open_now", "copy_out") and f in ("os.close", "os.fdopen"):
            assert not call.args or ast.unparse(call.args[0]) != "fd", (
                f"{where_} calls {f}(fd): the child never closes the database's descriptor: closing it drops "
                "SQLite's locks")
        if f == "out.gettarinfo":
            assert "fileobj" in kw or where_ == "enter", where_
    assert seen == {"os.fork": {"copy_database"}, "sqlite3.connect": {"database_child"}, "os.fchdir": {"enter"}}
    assert "os._exit(code)" in source and "os.dup2(null, 1)" in source
