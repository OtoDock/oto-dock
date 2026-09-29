"""services/checks/classify + changed_set.condition_matches — the words a
condition uses: the kind of a written file, the events a command or a tool
call stands for, globs, and the matching rules (CHECKS.md "Conditions")."""

from services.checks import classify
from services.checks.changed_set import condition_matches


def test_kinds_by_extension_and_name():
    assert classify.classify_path("/users/u/workspace/src/a.py") == "code"
    assert classify.classify_path("C:/Users/dev/repo/app.tsx") == "code"
    assert classify.classify_path("Dockerfile") == "code"
    assert classify.classify_path("notes/plan.md") == "text"
    assert classify.classify_path("out/report.pdf") == "document"
    assert classify.classify_path("data/q3.xlsx") == "spreadsheet"
    assert classify.classify_path("deck.pptx") == "presentation"
    assert classify.classify_path("img/logo.PNG") == "image"
    assert classify.classify_path("clip.mp4") == "video"
    assert classify.classify_path("voice.wav") == "audio"
    assert classify.classify_path("config.yaml") == "data"
    assert classify.classify_path("package.json") == "data"
    assert classify.classify_path("LICENSE") == "other"
    assert classify.classify_path("") == "other"


def test_events_from_commands():
    assert classify.classify_command("cd repo && git commit -m 'x'") == {"commit"}
    assert classify.classify_command("git -C /r push origin main") == {"push"}
    assert classify.classify_command("git push --tags") == {"push", "publish"}
    assert classify.classify_command("npm run build && npm test") == {"build", "test"}
    assert classify.classify_command("pytest -q tests/") == {"test"}
    assert classify.classify_command("ffmpeg -i a.mov out.mp4") == {"render"}
    assert classify.classify_command("npm publish --access public") == {"publish"}
    assert classify.classify_command("git commit-tree abc") == set()
    assert classify.classify_command("ls -la") == set()
    assert classify.classify_command("") == set()


def test_events_from_tools():
    assert classify.classify_tool("mcp__video-tools__render_composition") == {
        "tool:mcp__video-tools__render_composition", "render"}
    assert classify.classify_tool("mcp__file-tools__write_xlsx") == {"tool:mcp__file-tools__write_xlsx"}
    assert classify.classify_tool("Write") == set()


def test_globs_any_depth():
    assert classify.matches_glob("users/u/workspace/src/a.py", "**/*.py")
    assert classify.matches_glob("workspace/src/deep/a.py", "workspace/src/**/*.py")
    assert classify.matches_glob("workspace/src/a.py", "workspace/src/**")
    assert not classify.matches_glob("workspace/docs/a.md", "workspace/src/**")
    assert classify.matches_glob("workspace/a.md", "workspace/*.md")
    assert not classify.matches_glob("workspace/x/a.md", "workspace/*.md")


def _changed(*, written=(), events=(), git_roots=(), project="", commands=()):
    return {
        "paths": [{"path": p, "relative": p, "kind": classify.classify_path(p), "writes": True}
                  for p in written],
        "events": list(events),
        "tools": [{"name": "Bash", "command": c, "paths": []} for c in commands],
        "places": {"project": project, "git_roots": list(git_roots)},
    }


def test_condition_rules():
    code = _changed(written=["workspace/src/a.py"], events=["commit"], git_roots=["workspace"])
    nothing = _changed()
    # No condition: any write or event; a planning turn matches nothing.
    assert condition_matches({}, code)
    assert not condition_matches({}, nothing)
    assert condition_matches({"always": True}, nothing)
    # Kinds and events narrow; both must hold when both are named.
    assert condition_matches({"kinds": ["code"]}, code)
    assert not condition_matches({"kinds": ["image"]}, code)
    assert condition_matches({"kinds": ["any"]}, code)
    assert condition_matches({"events": ["commit", "push"]}, code)
    assert not condition_matches({"events": ["push"]}, code)
    assert not condition_matches({"kinds": ["code"], "events": ["push"]}, code)
    # Places.
    assert condition_matches({"places": ["git"]}, code)
    assert not condition_matches({"places": ["git"]}, _changed(written=["workspace/a.py"]))
    proj = _changed(written=["workspace/projects/p1/notes.md"], project="workspace/projects/p1")
    assert condition_matches({"places": ["project"]}, proj)
    assert not condition_matches({"places": ["project"]}, code)
    # Globs and command patterns.
    assert condition_matches({"globs": ["workspace/src/**"]}, code)
    assert not condition_matches({"globs": ["workspace/docs/**"]}, code)
    cmds = _changed(commands=["make deploy-staging"])
    assert condition_matches({"commands": [r"make\s+deploy"]}, cmds)
    assert not condition_matches({"commands": [r"make\s+deploy"]}, code)
    # A read-only turn with an event still matches an events condition.
    assert condition_matches({"events": ["test"]}, _changed(events=["test"]))


def test_a_local_write_inside_a_checkout_is_in_a_git_place(tmp_path, monkeypatch):
    # A local session names its files by the sandbox-virtual form; the
    # checkout walk runs over the platform's copy of the tree.
    import asyncio
    import config
    from auth.path_policy import SecurityContext
    from core.session import session_events as se
    from services.checks import changed_set as cs
    root = tmp_path / "agents"
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", root.resolve())
    repo = root / "git-agent" / "users" / "alice" / "workspace" / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / "x.py").write_text("print(1)\n")
    sec = SecurityContext(role="manager", username="alice", agent="git-agent", is_admin_agent=False)
    target = cs.Target(session_id="s", kind="chats", agent="git-agent", chat={"id": "c"},
                       username="alice", security=sec, placement=sec.placement)
    recs = [se.ToolRecord(tool_name="Write", paths=("/users/alice/workspace/repo/x.py",)),
            se.ToolRecord(tool_name="Write", paths=("/users/alice/workspace/notes.md",))]
    out = asyncio.run(cs.build(target, recs, check_name="c", round_no=1, request="", result=""))
    assert out["places"]["git_roots"] == ["users/alice/workspace/repo"]
    assert cs.condition_matches({"places": ["git"]}, out)


def test_a_persons_patterns_never_backtrack():
    # condition.commands, globs and a schema section's patterns run through
    # RE2: the patterns that took seconds to minutes under Python's engine
    # answer at once, and with the same meaning.
    import asyncio
    import time
    from services.checks import documents
    from services.checks.kinds import schema_kind
    evil = "a" * 2000 + "!"
    changed = _changed(written=["workspace/" + "a" * 60 + "b.py"], commands=[evil])
    started = time.monotonic()
    assert not condition_matches({"commands": [r"^(a+)+$"]}, changed)
    assert not condition_matches({"globs": ["**a**a**a**a**a**a**a**a**c"]}, changed)
    assert condition_matches({"commands": [r"^a+!$"]}, changed)
    doc = documents.validate_check_doc({"name": "shape", "schema": {
        "type": "object", "properties": {"id": {"type": "string", "pattern": r"^(a|aa)+$"}},
        "patternProperties": {r"^(x+x+)+y$": {"type": "integer"}}, "additionalProperties": False}})
    check = documents.CheckDoc(agent="a", owner="", name="shape", doc=doc, folder=None, doc_sha256="")
    answer = '```json\n{"id": "%s", "%s": 1}\n```' % ("a" * 3000 + "!", "x" * 3000)
    v = asyncio.run(schema_kind.run(check, None, {"result": answer}, round_no=1))
    assert time.monotonic() - started < 2.0
    assert v.status == "fail" and len(v.findings) == 2
    ok = asyncio.run(schema_kind.run(check, None, {"result": '```json\n{"id": "aaa", "xxy": 2}\n```'},
                                     round_no=1))
    assert ok.status == "pass"
    extra = asyncio.run(schema_kind.run(check, None, {"result": '```json\n{"zz": 1}\n```'}, round_no=1))
    assert extra.status == "fail" and "regexes" in extra.findings[0]["text"]
