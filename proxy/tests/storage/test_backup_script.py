"""scripts/backup.sh hardening: private perms, atomic writes, symlink-safe apps.

Runs the real script via subprocess with a stub `docker` on PATH: no DB, no
Docker daemon.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_BACKUP = _REPO / "scripts" / "backup.sh"


def _stub_docker(bindir: Path, *, exec_exit: int = 0) -> None:
    """A fake `docker` that answers `ps` with a container id and `exec` with SQL."""
    d = bindir / "docker"
    d.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        '  ps) echo fakecid ;;\n'
        f'  exec) shift; # docker exec <cid> <cmd...>\n'
        f'        exit_code={exec_exit};\n'
        '        if [ "$exit_code" = 0 ]; then echo "-- fake dump"; echo "SELECT 1;"; fi;\n'
        '        exit $exit_code ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n"
    )
    d.chmod(0o755)


def _run(tmp: Path, out_dir: Path, *args, exec_exit: int = 0, agents_dir: Path | None = None):
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    _stub_docker(bindir, exec_exit=exec_exit)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["OTODOCK_BACKUP_DIR"] = str(out_dir)
    if agents_dir is not None:
        env["OTODOCK_AGENTS_DIR"] = str(agents_dir)
    # a permissive inherited umask, to prove the script sets its own
    env["OTODOCK_BACKUP_RETAIN"] = "0"
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
