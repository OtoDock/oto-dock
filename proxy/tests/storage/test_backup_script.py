"""scripts/backup.sh: one run directory per backup, private, whole or not at
all; the version from the database, the image or VERSIONS.md; the trees
through the proxy container (running, or a one-off on its image) or on this
host; links kept, SQLite copied by header; the retention over runs and trees.

The real script runs against the fake docker of tests/deploy/fake_docker.py:
no daemon, no database.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.deploy.fake_docker import write_fake

REPO = Path(__file__).resolve().parents[3]
BACKUP = REPO / "scripts" / "backup.sh"

PG = {"id": "pg-t2", "name": "otodock-otodock-postgres-1",
      "labels": {"com.docker.compose.project": "otodock", "com.docker.compose.service": "otodock-postgres"}}
PG_T1 = {"id": "pg-t1", "name": "otodock-postgres",
         "labels": {"com.docker.compose.project": "oto-dock", "com.docker.compose.service": "postgres"}}
KEYS = json.dumps({"a" * 32: "JWT_SECRET"})


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


def run(tmp_path, *args, containers, out=None, env=None):
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
    return subprocess.run(["bash", str(BACKUP), *args], cwd=tmp_path, env=e,
                          capture_output=True, text=True, timeout=120)


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
    assert not list((tmp_path / "backups").glob("*"))


def test_a_failed_dump_leaves_no_run_directory(tmp_path, world):
    r = run(tmp_path, containers=[PG, proxy(world)], env={"FAKE_PG_DUMP_RC": "1"})
    assert r.returncode == 1, r.stderr
    assert runs(tmp_path / "backups") == []
    assert not list((tmp_path / "backups").glob("*"))


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
