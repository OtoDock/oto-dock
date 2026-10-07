"""scripts/backup.sh hardening: private perms, atomic writes, symlink-safe apps.

Runs the real script via subprocess with a stub `docker` on PATH: no DB, no
Docker daemon.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_BACKUP = _REPO / "scripts" / "backup.sh"


def _stub_docker(bindir: Path, *, exec_exit: int = 0, apps_exit: int | None = None) -> None:
    """A fake `docker` that answers `ps` with a container id and `exec` with SQL.
    ``apps_exit`` set: the `--apps` program's `docker exec -i` writes a few
    bytes of an archive and exits with it."""
    d = bindir / "docker"
    apps = "" if apps_exit is None else (
        f'        if [ "$1" = -i ]; then cat >/dev/null; printf "\\037\\213partial"; exit {apps_exit}; fi;\n')
    d.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        '  ps) echo fakecid ;;\n'
        f'  exec) shift; # docker exec <cid> <cmd...>\n'
        + apps +
        f'        exit_code={exec_exit};\n'
        '        if [ "$exit_code" = 0 ]; then echo "-- fake dump"; echo "SELECT 1;"; fi;\n'
        '        exit $exit_code ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n"
    )
    d.chmod(0o755)


def _stub_python3(bindir: Path, exit_code: int) -> None:
    """A `python3` that dies part way through the archive (a crash, a missing
    interpreter's 127)."""
    p = bindir / "python3"
    p.write_text("#!/bin/sh\ncat >/dev/null\nprintf '\\037\\213partial'\n"
                 f"exit {exit_code}\n")
    p.chmod(0o755)


def _run(tmp: Path, out_dir: Path, *args, exec_exit: int = 0, agents_dir: Path | None = None,
         apps_exit: int | None = None, python_exit: int | None = None,
         extra_env: dict[str, str] | None = None):
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    _stub_docker(bindir, exec_exit=exec_exit, apps_exit=apps_exit)
    if python_exit is not None:
        _stub_python3(bindir, python_exit)
    env = dict(os.environ)
    env["OTODOCK_BACKUP_RETAIN"] = "0"
    env.update(extra_env or {})
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["OTODOCK_BACKUP_DIR"] = str(out_dir)
    if agents_dir is not None:
        env["OTODOCK_AGENTS_DIR"] = str(agents_dir)
    return subprocess.run(
        ["bash", str(_BACKUP), *args],
        env=env, capture_output=True, text=True, cwd=str(tmp),
    )


def test_dump_is_private_in_private_dir(tmp_path):
    out = tmp_path / "backups"
    r = _run(tmp_path, out, exec_exit=0)
    assert r.returncode == 0, r.stderr
    dumps = list(out.glob("*.sql.gz"))
    assert len(dumps) == 1, r.stdout + r.stderr
    assert stat.S_IMODE(dumps[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(out.stat().st_mode) == 0o700


def test_failed_dump_leaves_no_archive(tmp_path):
    out = tmp_path / "backups"
    r = _run(tmp_path, out, exec_exit=1)
    assert r.returncode != 0
    assert list(out.glob("*.sql.gz")) == []
    assert list(out.glob("*.part")) == []


def test_apps_skips_symlinked_db(tmp_path):
    out = tmp_path / "backups"
    agents = tmp_path / "agents"
    (agents / "victim").mkdir(parents=True)
    (agents / "victim" / "secret").write_text("SECRET")
    a = agents / "agentA" / "app-data" / "app1"
    a.mkdir(parents=True)
    # a symlinked .db must be skipped, not followed to the victim
    (a / "evil.db").symlink_to(agents / "victim" / "secret")
    # a real .db must still be backed up
    import sqlite3
    con = sqlite3.connect(a / "real.db")
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()
    r = _run(tmp_path, out, "--apps", exec_exit=0, agents_dir=agents)
    assert r.returncode == 0, r.stderr
    apps = list(out.glob("otodock-apps-*.tar.gz"))
    assert len(apps) == 1
    import tarfile
    with tarfile.open(apps[0]) as tf:
        names = tf.getnames()
    assert any("real.db" in n for n in names), names
    assert not any("evil.db" in n for n in names), names


def _app_db(path: Path) -> None:
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()


def test_apps_database_error_keeps_the_archive_and_exits_3(tmp_path):
    """A database that cannot be copied is reported and the rest archived:
    the archive is kept and the script exits 3 (an incomplete archive), not
    the 1 of a failed backup."""
    out = tmp_path / "backups"
    agents = tmp_path / "agents"
    _app_db(agents / "agentA" / "app-data" / "app1" / "real.db")
    (agents / "agentA" / "app-data" / "app1" / "bad.db").write_bytes(b"not a database" * 64)
    r = _run(tmp_path, out, "--apps", agents_dir=agents)
    assert r.returncode == 3, r.stderr
    assert "INCOMPLETE" in r.stderr
    apps = list(out.glob("otodock-apps-*.tar.gz"))
    assert len(apps) == 1
    import tarfile
    with tarfile.open(apps[0]) as tf:
        names = tf.getnames()
    assert any("real.db" in n for n in names) and not any("bad.db" in n for n in names), names
    assert list(out.glob("*.part")) == []


def test_apps_database_error_still_prunes_old_archives(tmp_path):
    """A run that exits 3 kept a whole archive: the apps retention prunes by
    it as a clean run does, so one database that keeps failing never stops
    old archives from going."""
    out = tmp_path / "backups"
    out.mkdir(mode=0o700)
    for i, name in enumerate(("otodock-apps-20200101-000000.tar.gz",
                              "otodock-apps-20200102-000000.tar.gz")):
        old = out / name
        old.write_bytes(b"old")
        os.utime(old, (1_000_000 + i, 1_000_000 + i))
    agents = tmp_path / "agents"
    _app_db(agents / "agentA" / "app-data" / "app1" / "real.db")
    (agents / "agentA" / "app-data" / "app1" / "bad.db").write_bytes(b"not a database" * 64)
    r = _run(tmp_path, out, "--apps", agents_dir=agents,
             extra_env={"OTODOCK_BACKUP_RETAIN": "1"})
    assert r.returncode == 3, r.stderr
    apps = sorted(p.name for p in out.glob("otodock-apps-*.tar.gz"))
    assert len(apps) == 1 and not apps[0].startswith("otodock-apps-2020"), apps


# Loaded by the apps program's interpreter at start (PYTHONPATH): the copy of
# a database's member stops half way, as a short read of the temp copy would.
_CUT_MEMBER = """\
import tarfile
_real = tarfile.copyfileobj
def _cut(src, dst, length=None, exception=OSError, bufsize=None):
    _real(src, dst, max(1, (length or 0) // 2), exception, bufsize)
    raise OSError("unexpected end of data")
tarfile.copyfileobj = _cut
"""


def test_apps_failure_while_a_member_is_written_leaves_no_archive(tmp_path):
    """A database whose member fails part way through leaves a cut archive:
    that is not a per-database error, the run exits 1 and keeps nothing."""
    out = tmp_path / "backups"
    agents = tmp_path / "agents"
    _app_db(agents / "agentA" / "app-data" / "app1" / "real.db")
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(_CUT_MEMBER)
    r = _run(tmp_path, out, "--apps", agents_dir=agents, extra_env={"PYTHONPATH": str(site)})
    assert r.returncode == 1, r.stderr
    assert "unexpected end of data" in r.stderr
    assert list(out.glob("otodock-apps-*")) == []


@pytest.mark.parametrize("code", [1, 2, 127])
def test_apps_fatal_failure_on_the_host_leaves_no_archive(tmp_path, code):
    out = tmp_path / "backups"
    agents = tmp_path / "agents"
    _app_db(agents / "agentA" / "app-data" / "app1" / "real.db")
    r = _run(tmp_path, out, "--apps", agents_dir=agents, python_exit=code)
    assert r.returncode == 1, r.stderr
    assert list(out.glob("otodock-apps-*")) == []
    assert len(list(out.glob("*.sql.gz"))) == 1  # the database dump stands


@pytest.mark.parametrize("code", [1, 125, 137])
def test_apps_fatal_failure_in_the_proxy_container_leaves_no_archive(tmp_path, code):
    out = tmp_path / "backups"
    r = _run(tmp_path, out, "--apps", apps_exit=code)
    assert r.returncode == 1, r.stderr
    assert list(out.glob("otodock-apps-*")) == []


def test_apps_in_the_proxy_container_keeps_a_complete_archive(tmp_path):
    out = tmp_path / "backups"
    r = _run(tmp_path, out, "--apps", apps_exit=0)
    assert r.returncode == 0, r.stderr
    assert len(list(out.glob("otodock-apps-*.tar.gz"))) == 1
