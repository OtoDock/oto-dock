"""A session's home in its scope's engine folder (``core.sandbox.session_home``):
the layout, the links to the store's folders, the seed of the account record,
and removals that never follow a link (the plan's security rule 7)."""

import contextlib
import json
import os
import stat

import pytest

from core.sandbox import session_home as sh

SID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
OTHER = "11111111-2222-3333-4444-555555555555"
LINKS = ("projects", "todos", "skills")
SEED = {".claude.json": ("oauthAccount",)}


@pytest.fixture(params=["fd", "path"])
def ops(request, monkeypatch):
    """Both implementations: the descriptor walk (Linux, macOS) and the path
    walk Windows takes, run here with links standing in for junctions."""
    monkeypatch.setattr(sh, "_WINDOWS", request.param == "path")
    return request.param


@pytest.fixture
def store(tmp_path):
    s = tmp_path / "agents" / "a" / "workspace" / ".claude"
    s.mkdir(parents=True)
    return s


def test_ids_and_paths(tmp_path):
    assert sh.valid_id(SID)
    for bad in ("", "short", "../etc/passwd", "a" * 65, "x/y" * 5, "abc def ghi"):
        assert not sh.valid_id(bad)
    home = sh.home_of(tmp_path, SID)
    assert home == tmp_path / "own" / SID
    assert sh.is_home(home) and sh.store_of(home) == tmp_path
    assert sh.relative(SID) == f"own/{SID}"
    assert not sh.is_home(tmp_path / "projects" / SID)
    with pytest.raises(ValueError):
        sh.home_of(tmp_path, "../x")


def test_prepare_makes_the_home_and_its_links(ops, store):
    home = sh.prepare(store, SID, LINKS)
    assert home == store / "own" / SID
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    for name in LINKS:
        assert (store / name).is_dir() and not (store / name).is_symlink()
        assert (home / name).is_symlink()
        assert os.path.realpath(home / name) == os.path.realpath(store / name)
    if ops == "fd":
        assert os.readlink(home / "projects") == "../../projects"
    (home / "projects" / "conv.jsonl").write_text("x")
    assert (store / "projects" / "conv.jsonl").read_text() == "x"


def test_prepare_makes_a_found_own_and_home_the_owners_alone(ops, store):
    """A machine with its own copy writes the home's files before the home
    is opened, and a writer makes a missing folder with the process's default
    mode. The two folders found get the mode one made here has. A folder of
    the store that the home links to keeps the mode its maker gave it."""
    own, home, shared = store / "own", store / "own" / SID, store / "projects"
    home.mkdir(parents=True)
    shared.mkdir()
    for folder in (own, home, shared):
        os.chmod(folder, 0o755)
    (home / "config.toml").write_text("the frame's")
    assert sh.prepare(store, SID, ("projects",)) == home
    assert stat.S_IMODE(os.stat(own).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(shared).st_mode) == 0o755
    assert (home / "config.toml").read_text() == "the frame's"


def test_prepare_is_idempotent_and_repairs_a_wrong_link(ops, store, tmp_path):
    sh.prepare(store, SID, LINKS)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.unlink(store / "own" / SID / "todos")
    os.symlink(elsewhere, store / "own" / SID / "todos")
    sh.prepare(store, SID, LINKS)
    assert os.path.realpath(store / "own" / SID / "todos") == os.path.realpath(store / "todos")
    assert list(elsewhere.iterdir()) == []


def test_a_real_folder_at_a_links_name_is_moved_into_the_store(ops, store):
    home = store / "own" / SID
    (home / "projects").mkdir(parents=True)
    (home / "projects" / "a.jsonl").write_text("mine")
    (home / "projects" / "same.jsonl").write_text("from the home")
    (store / "projects").mkdir()
    (store / "projects" / "same.jsonl").write_text("from the store")
    sh.prepare(store, SID, LINKS)
    assert (home / "projects").is_symlink()
    assert (store / "projects" / "a.jsonl").read_text() == "mine"
    assert (store / "projects" / "same.jsonl").read_text() == "from the store"
    kept = [p.read_text() for p in (store / "projects").iterdir() if p.name.startswith("same.jsonl.")]
    assert kept == ["from the home"]


def test_a_project_folder_at_a_links_name_is_merged_where_a_resume_reads_it(ops, store):
    """Claude Code keeps one folder per project in ``projects``: a home's
    real ``projects/-workspace`` merges into the store's, the transcript
    lands beside the older ones, and a file whose name is taken is kept."""
    home = store / "own" / SID
    (home / "projects" / "-workspace" / "sub").mkdir(parents=True)
    (home / "projects" / "-workspace" / f"{SID}.jsonl").write_text("this chat")
    (home / "projects" / "-workspace" / "sub" / "a.txt").write_text("deep")
    (home / "projects" / "-workspace" / "same.jsonl").write_text("from the home")
    (store / "projects" / "-workspace").mkdir(parents=True)
    (store / "projects" / "-workspace" / "older.jsonl").write_text("an older chat")
    (store / "projects" / "-workspace" / "same.jsonl").write_text("from the store")
    sh.prepare(store, SID, LINKS)
    project = store / "projects" / "-workspace"
    assert sorted(p.name for p in (store / "projects").iterdir()) == ["-workspace"]
    assert (project / f"{SID}.jsonl").read_text() == "this chat"
    assert (project / "older.jsonl").read_text() == "an older chat"
    assert (project / "sub" / "a.txt").read_text() == "deep"
    assert (project / "same.jsonl").read_text() == "from the store"
    assert [p.read_text() for p in project.iterdir() if p.name.startswith("same.jsonl.")] == ["from the home"]
    assert (home / "projects").is_symlink()


def test_the_account_record_is_seeded_without_its_sign_in(ops, store):
    record = {"userID": "a" * 64, "oauthAccount": {"emailAddress": "x@example.com"}, "theme": "dark"}
    (store / ".claude.json").write_text(json.dumps(record))
    home = sh.prepare(store, SID, LINKS, SEED)
    seeded = json.loads((home / ".claude.json").read_text())
    assert seeded == {"userID": "a" * 64, "theme": "dark"}
    # The session's own record is never replaced by a later start.
    (home / ".claude.json").write_text('{"mine": true}')
    sh.prepare(store, SID, LINKS, SEED)
    assert json.loads((home / ".claude.json").read_text()) == {"mine": True}


def test_a_seed_with_no_keys_copies_the_bytes_as_they_are(ops, store, tmp_path):
    """Codex's ``installation_id`` is a bare id, not a JSON object: a seed
    that names no keys takes the store's bytes unchanged, once."""
    (store / "installation_id").write_bytes(b"0b6f3c2e-1d2a-4f7e-9a51-3c4d5e6f7a8b")
    home = sh.prepare(store, SID, LINKS, {"installation_id": ()})
    assert (home / "installation_id").read_bytes() == b"0b6f3c2e-1d2a-4f7e-9a51-3c4d5e6f7a8b"
    (home / "installation_id").write_bytes(b"mine")
    sh.prepare(store, SID, LINKS, {"installation_id": ()})
    assert (home / "installation_id").read_bytes() == b"mine"
    secret = tmp_path / "secret"
    secret.write_bytes(b"outside")
    (store / "installation_id").unlink()
    os.symlink(secret, store / "installation_id")
    other = sh.prepare(store, OTHER, LINKS, {"installation_id": ()})
    assert not (other / "installation_id").exists()


@pytest.mark.parametrize("content", [None, "not json", "[1, 2]", "link"])
def test_no_seed_from_a_missing_bad_or_linked_record(ops, store, tmp_path, content):
    if content == "link":
        secret = tmp_path / "secret.json"
        secret.write_text('{"a": 1}')
        os.symlink(secret, store / ".claude.json")
    elif content is not None:
        (store / ".claude.json").write_text(content)
    home = sh.prepare(store, SID, LINKS, SEED)
    assert not (home / ".claude.json").exists()


def test_no_seed_from_a_pipe_or_a_record_past_the_cap(ops, store, monkeypatch):
    """The store's record is opened never waiting on a pipe and read under
    a cap: neither seeds the home, and an honest record is never cut."""
    os.mkfifo(store / ".claude.json")
    home = sh.prepare(store, SID, LINKS, SEED)
    assert not (home / ".claude.json").exists()
    os.unlink(store / ".claude.json")
    monkeypatch.setattr(sh, "SEED_MAX_BYTES", 16)
    (store / ".claude.json").write_text(json.dumps({"theme": "dark", "x": "y" * 32}))
    home = sh.prepare(store, SID, LINKS, SEED)
    assert not (home / ".claude.json").exists()


@pytest.mark.parametrize("planted", ["own", "home", "shared"])
def test_prepare_refuses_a_link_where_a_folder_belongs(ops, store, tmp_path, planted):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if planted == "own":
        os.symlink(elsewhere, store / "own")
    elif planted == "home":
        (store / "own").mkdir()
        os.symlink(elsewhere, store / "own" / SID)
    else:
        os.symlink(elsewhere, store / "skills")
    with pytest.raises(sh.HomeRefused):
        sh.prepare(store, SID, LINKS)
    assert list(elsewhere.iterdir()) == []


def test_drop_removes_the_home_and_never_what_its_links_reach(ops, store, tmp_path):
    home = sh.prepare(store, SID, LINKS)
    (store / "projects" / "conv.jsonl").write_text("kept")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("kept")
    os.symlink(outside, home / "planted")
    (home / "nested" / "deep").mkdir(parents=True)
    os.symlink(outside, home / "nested" / "deep" / "link")
    (home / ".credentials.json").write_text("token")
    assert sh.drop(store, SID, LINKS)
    assert not home.exists() and not home.is_symlink()
    assert (store / "projects" / "conv.jsonl").read_text() == "kept"
    assert (outside / "keep.txt").read_text() == "kept"
    assert not sh.drop(store, SID, LINKS)


def test_drop_of_a_home_replaced_by_a_link_removes_only_the_link(ops, store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("kept")
    (store / "own").mkdir()
    os.symlink(outside, store / "own" / SID)
    assert sh.drop(store, SID, LINKS)
    assert not (store / "own" / SID).is_symlink()
    assert (outside / "keep.txt").read_text() == "kept"


@pytest.mark.parametrize("replaced", ["own", "store"])
def test_drop_and_sweep_never_enter_a_replaced_own_or_store(ops, store, tmp_path, replaced):
    victim = tmp_path / "victim"
    (victim / "own" / SID).mkdir(parents=True)
    keep = victim / "own" / SID / "keep.txt"
    keep.write_text("kept")
    if replaced == "own":
        os.symlink(victim / "own", store / "own")
        target = store
    else:
        target = tmp_path / "alias"
        os.symlink(victim, target)
    for call in (lambda: sh.drop(target, SID, LINKS), lambda: sh.sweep(target, set(), LINKS)):
        with contextlib.suppress(sh.HomeRefused):
            assert not call()
    assert keep.read_text() == "kept"


def test_sweep_keeps_live_homes_and_names_that_are_not_ids(ops, store):
    sh.prepare(store, SID, LINKS)
    sh.prepare(store, OTHER, LINKS)
    (store / "own" / "notes").mkdir()
    assert sh.sweep(store, {SID}, LINKS) == [OTHER]
    assert (store / "own" / SID).is_dir()
    assert (store / "own" / "notes").is_dir()
    assert sh.sweep(store, set(), LINKS) == [SID]
    assert sh.sweep(store / "missing", set(), LINKS) == []


def _deep(top, depth: int) -> None:
    """A chain of ``depth`` folders under ``top``, made by descriptor (the
    standard library's own ``makedirs`` recurses)."""
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(depth):
            os.mkdir("d", dir_fd=fd)
            nxt = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        with open(os.open("leaf.txt", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd), "w") as leaf:
            leaf.write("x")
    finally:
        os.close(fd)


def test_a_very_deep_tree_in_a_home_is_removed_and_the_sweep_goes_on(ops, store):
    """A session can make any tree in its home: a chain deeper than the
    interpreter's recursion limit is removed, its sign-in first, and a sweep
    removes the sibling after it."""
    home = sh.prepare(store, SID, LINKS)
    (home / ".credentials.json").write_text("token")
    _deep(home, 1200)
    (store / "projects" / "conv.jsonl").write_text("kept")
    assert sh.drop(store, SID, LINKS)
    assert not home.exists()
    sh.prepare(store, SID, LINKS)
    _deep(home, 1200)
    sh.prepare(store, OTHER, LINKS)
    assert sorted(sh.sweep(store, set(), LINKS)) == sorted([SID, OTHER])
    assert list((store / "own").iterdir()) == []
    assert (store / "projects" / "conv.jsonl").read_text() == "kept"


def test_a_sweep_goes_on_past_a_home_it_cannot_remove(ops, store, monkeypatch):
    sh.prepare(store, SID, LINKS)
    sh.prepare(store, OTHER, LINKS)
    walk = sh._PathOps if ops == "path" else sh._FdOps
    name = "_drop_at" if ops == "path" else "_drop_in"
    real = getattr(walk, name)

    def _fails(self, where, *args):
        if str(where).endswith(SID) or (args and args[0] == SID):
            raise PermissionError("held open")
        return real(self, where, *args)
    monkeypatch.setattr(walk, name, _fails)
    assert sh.sweep(store, set(), LINKS) == [OTHER]
    assert (store / "own" / SID).is_dir()


def test_a_merge_deeper_than_its_bound_refuses_the_start(ops, store):
    """Both sides hold the same chain of folders far deeper than any engine
    writes: the merge stops and the start fails closed."""
    home = store / "own" / SID
    (home / "projects").mkdir(parents=True)
    (store / "projects").mkdir()
    _deep(home / "projects", 70)
    _deep(store / "projects", 70)
    with pytest.raises(sh.HomeRefused):
        sh.prepare(store, SID, LINKS)


def test_one_builder_makes_a_home_for_every_engine(store):
    """``session_config_dir.make_session_home``: the engine's links and seed,
    and the start's stamp on the folder (the daily pass keeps a home changed
    in the last hour). Claude's start goes through it."""
    import time
    from types import SimpleNamespace

    from core.engine_records import SessionHomeSpec
    from core.layers.cli.layer import CLIExecutionLayer
    from core.sandbox import session_config_dir

    spec = SessionHomeSpec(links=("projects",), own_files=(), seed=((".claude.json", ("oauthAccount",)),))
    (store / ".claude.json").write_text('{"oauthAccount": {}, "theme": "dark"}')
    home = session_config_dir.make_session_home(store, SID, spec)
    assert home == store / "own" / SID and (home / "projects").is_symlink()
    assert json.loads((home / ".claude.json").read_text()) == {"theme": "dark"}
    then = time.time() - 7200
    os.utime(home, (then, then))
    session_config_dir.make_session_home(store, SID, spec)
    assert time.time() - home.stat().st_mtime < 60
    made = []
    real = session_config_dir.make_session_home
    session_config_dir.make_session_home = lambda *a: made.append(a) or real(*a)
    try:
        layer = CLIExecutionLayer.__new__(CLIExecutionLayer)
        layer._make_home(OTHER, SimpleNamespace(sandbox_host_claude_dir=str(store)))
    finally:
        session_config_dir.make_session_home = real
    assert [a[1] for a in made] == [OTHER]
