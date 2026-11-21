"""The bare-metal compute unit's walls: the data folders it takes from the
server's own PLATFORM_DATA_DIR, and every folder and file the server keeps
beside its code walled in both unit texts, bound for it, or read for a
written reason. The unit texts come from ``test_ops_scripts``' harness:
dev-setup.sh's own install_service, run with fakes only."""
from __future__ import annotations

import ast

import pytest

from tests.deploy.test_ops_scripts import (
    REPO,
    _dev_setup_unit,
    _service_section,
    _walled_beside,
)


def _checkout(tmp_path, name: str, config: str):
    """A checkout folder of this test's own holding only a config.env."""
    checkout = tmp_path / name
    checkout.mkdir()
    (checkout / "config.env").write_text(config)
    return checkout


def test_the_compute_unit_takes_its_data_folders_from_the_servers_own_setting(tmp_path):
    """A data folder moved with PLATFORM_DATA_DIR in config.env (its last line
    for it, quotes dropped), the file the proxy's unit reads: the unit binds
    and walls it there, never at the checkout's old folders. The same value in
    the shell too is fine, another one or one in the shell alone stops the
    script with one sentence, and so does a path the unit cannot take, before
    any unit is written."""
    checkout = _checkout(tmp_path, "checkout", 'PLATFORM_DATA_DIR=/srv/old\n  PLATFORM_DATA_DIR="/srv/data"\nJWT_SECRET=x\n')
    (tmp_path / "first").mkdir()
    svc = _service_section(_dev_setup_unit(tmp_path / "first", "otodock-compute", root=str(checkout),
                                           env_extra={"PLATFORM_DATA_DIR": "/srv/data/"}, data="/srv/data"))
    assert svc["BindPaths"][0] == "/srv/data/agents"
    assert '"OTODOCK_COMPUTE_AGENTS_DIR=/srv/data/agents"' in svc["Environment"]
    hidden = " ".join(svc["InaccessiblePaths"]).split()
    for path in ("/srv/data/sessions", f"{checkout}/config.env", "-/srv/data/recover-bin", "-/srv/data/upload-staging",
                 "-/srv/data/preview-snapshots", *_walled_beside(str(checkout), "/srv/data")):
        assert path in hidden, path
    assert not any(h.lstrip("-") in (f"{checkout}/sessions", f"{checkout}/recover-bin") for h in hidden)

    for case, root, shell in (("alone", "/srv/otodock", "/srv/data"), ("another", str(checkout), "/srv/elsewhere")):
        (tmp_path / case).mkdir()
        with pytest.raises(AssertionError, match="die: PLATFORM_DATA_DIR is set in this shell and not in config.env. "
                                                 "The units read config.env: add the line there."):
            _dev_setup_unit(tmp_path / case, "otodock-compute", root=root, env_extra={"PLATFORM_DATA_DIR": shell})
        assert "otodock-compute.service" not in (tmp_path / case / "unit-calls.log").read_text()

    for n, bad in enumerate(("relative/data", "/srv/../etc", "/", "/srv/my data", "/srv/tab\tdata")):
        bad_checkout = _checkout(tmp_path, f"bad-checkout-{n}", f"PLATFORM_DATA_DIR={bad}\n")
        (tmp_path / f"bad-{n}").mkdir()
        with pytest.raises(AssertionError, match="die: PLATFORM_DATA_DIR \\("):
            _dev_setup_unit(tmp_path / f"bad-{n}", "otodock-compute", root=str(bad_checkout))
        assert "otodock-compute.service" not in (tmp_path / f"bad-{n}" / "unit-calls.log").read_text()


# What the server keeps beside its code or in its data folder, each with what
# the compute unit (which binds the whole checkout read-only) meets there: a
# wall, its own bind, or the reason it may read it. A new folder or file the
# server joins onto PLATFORM_DATA_DIR, PLATFORM_ROOT or BASE_DIR fails the
# guard below until it is placed here.
_BESIDE_THE_CODE = {
    ("PLATFORM_DATA_DIR", "agents"): "bound: the sessions' own folders",
    ("PLATFORM_DATA_DIR", "sessions"): "walled",
    ("PLATFORM_DATA_DIR", "recover-bin"): "walled",
    ("PLATFORM_DATA_DIR", "preview-snapshots"): "walled",
    ("PLATFORM_DATA_DIR", "upload-staging"): "walled",
    ("PLATFORM_DATA_DIR", "credential-exit"): "walled",
    ("PLATFORM_DATA_DIR", "host-media-cache"): "walled",
    ("PLATFORM_DATA_DIR", "media-cache"): "read: copies of media from the agents folder, which the unit holds",
    ("PLATFORM_DATA_DIR", "mcp-tool-hints.json"): "read: the tools' approval labels, no secret",
    ("PLATFORM_ROOT", "VERSIONS.md"): "read: code",
    ("PLATFORM_ROOT", "mcps"): "read: the bundled tools; the community tools bound, the SSH keys walled",
    ("PLATFORM_ROOT", "scripts"): "read: code",
    ("BASE_DIR", "hooks"): "read: code",
    ("BASE_DIR", "assets"): "read: code",
    ("BASE_DIR", "scripts"): "read: code; the quota helper walled",
    ("BASE_DIR", "sessions"): "walled",
    ("BASE_DIR", "proxy.log"): "read: the server's log, with tokens redacted. A wall on a file that rotation "
                               "renames does not hold: it moves to a walled folder (ROADMAP)",
}


def _joins_beside_the_code() -> set[tuple[str, str]]:
    """Every (base, first part) the proxy's code joins onto one of the three
    folders with ``/``: a literal, or a module's own string constant."""
    found: set[tuple[str, str]] = set()
    for path in (REPO / "proxy").rglob("*.py"):
        rel = path.relative_to(REPO / "proxy").parts
        if rel[0] in ("tests", "venv", "docs") or "node_modules" in rel:
            continue
        tree = ast.parse(path.read_text(), str(path))
        consts = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
                  and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
                  for t in n.targets if isinstance(t, ast.Name)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
                continue
            left = node.left
            if isinstance(left, ast.Call) and left.args:
                left = left.args[0]
            base = left.attr if isinstance(left, ast.Attribute) else getattr(left, "id", None)
            if base not in ("PLATFORM_DATA_DIR", "PLATFORM_ROOT", "BASE_DIR"):
                continue
            right = node.right
            name = right.value if isinstance(right, ast.Constant) else consts.get(getattr(right, "id", ""))
            assert isinstance(name, str), f"{path}:{node.lineno}: a join this guard cannot read"
            found.add((base, name.split("/")[0]))
    return found


def test_the_compute_unit_walls_or_reasons_every_folder_the_server_keeps_beside_its_code(tmp_path):
    """The compute unit binds the whole checkout read-only. Every folder and
    file the server keeps there or in its data folder is walled in both unit
    texts, bound for it, or read for a reason written above."""
    assert _joins_beside_the_code() == set(_BESIDE_THE_CODE), (
        "each new folder or file the server keeps beside its code goes into _BESIDE_THE_CODE: walled in both "
        "unit texts, bound, or read for a written reason")
    template = " ".join(_service_section((REPO / "proxy" / "compute.service").read_text())["InaccessiblePaths"]).split()
    written = " ".join(_service_section(_dev_setup_unit(tmp_path, "otodock-compute"))["InaccessiblePaths"]).split()
    for (base, name), why in _BESIDE_THE_CODE.items():
        if why != "walled":
            continue
        rel = f"proxy/{name}" if base == "BASE_DIR" else name
        for hidden, root in ((template, "/opt/otodock"), (written, "/srv/otodock")):
            assert f"{root}/{rel}" in hidden or f"-{root}/{rel}" in hidden, (root, rel)
