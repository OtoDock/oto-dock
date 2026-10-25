"""The one rule an agent's GitHub reach is judged by (``auth/github_reach``):
the repositories a tool call, a search, a resource, a git request or a gh
command names, the searches that widen past ``repo:``, the qualifiers a
search gets, read-only by the tools' own annotations and by gh's verbs, the
installation token's read permissions. The rule alone, no platform."""

from __future__ import annotations

import json

import pytest

from auth import github_reach as gr

SUBSET = gr.reach_of(["OtoDock/otodock-git-test", "octo/hello"], False)
READ_ONLY = gr.reach_of([], True)
BOTH = gr.reach_of(["octo/hello"], True)


def _call(tool: str, /, **arguments) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}}).encode()


def _never(_tool: str):
    return None


def _judge(reach, body: bytes, writes=_never) -> gr.Verdict:
    return gr.judge_mcp(reach, body, writes=writes)


def _passes(reach, body: bytes, writes=_never) -> bool:
    """The body goes on as it was judged: no refusal, the same JSON."""
    v = _judge(reach, body, writes)
    return not v.refusal and v.body is not None and json.loads(v.body) == json.loads(body)


# --- the shape ------------------------------------------------------------------

def test_names_are_compared_without_case_and_unreadable_ones_are_dropped():
    r = gr.reach_of([" OtoDock/Otodock-Git-Test.git ", "not a name", "a/b/c", "x/..", ""], False)
    assert r.repositories == frozenset({"otodock/otodock-git-test"})
    assert r.allows("OTODOCK/otodock-git-test") and not r.allows("otodock/other")
    assert gr.reach_of([], False).allows("anyone/anything")
    assert not gr.reach_of([], False).narrows and SUBSET.narrows and READ_ONLY.narrows


def test_the_stored_column_and_the_descriptor_round_trip():
    assert gr.parse_stored(gr.dump_stored(["a/b", "c/d"])) == ("a/b", "c/d")
    assert gr.parse_stored("") == () and gr.parse_stored("{oops") == () and gr.parse_stored('{"a":1}') == ()
    assert gr.Reach.from_dict(BOTH.to_dict()) == BOTH
    assert gr.Reach.from_dict(None) is None
    assert BOTH.digest() == gr.reach_of(["Octo/Hello"], True).digest() != SUBSET.digest()
    # A service reach's shape has no personal key; a person's own carries it,
    # and who narrowed is no part of what the reach allows.
    assert BOTH.to_dict() == {"repositories": ["octo/hello"], "read_only": True}
    mine = gr.reach_of(["octo/hello"], True, personal=True)
    assert mine.to_dict() == {"repositories": ["octo/hello"], "read_only": True, "personal": True}
    assert gr.Reach.from_dict(mine.to_dict()) == mine and mine.digest() == BOTH.digest()
    assert gr.Reach.from_dict({"repositories": [], "read_only": True, "personal": "yes"}).personal is False


# --- the MCP's tool calls --------------------------------------------------------

def test_a_call_on_a_repository_of_the_reach_goes_through_untouched():
    for args in ({"owner": "octo", "repo": "hello"}, {"owner": "OtoDock", "repo": "otodock-git-test", "path": "x"}):
        assert _passes(SUBSET, _call("get_file_contents", **args))


def test_a_call_naming_another_repository_is_refused_with_its_name():
    v = _judge(SUBSET, _call("issue_write", owner="octo", repo="secret", title="t"))
    assert v.refusal.startswith("This agent may reach only these GitHub repositories: ")
    assert "octo/secret is not one of them." in v.refusal and v.body is None


def test_every_way_an_argument_names_a_repository_is_read():
    cases = [
        {"parent_owner": "octo", "parent_repo": "secret", "owner": "octo", "repo": "hello"},
        {"items": [{"item_owner": "octo", "item_repo": "secret"}], "owner": "octo", "repo": "hello"},
        {"owner": "octo", "repo": "hello", "repository_url": "https://github.com/octo/secret/pull/1"},
        {"owner": "octo", "repo": "hello", "html_url": "https://api.github.com/repos/octo/secret/issues"},
        {"repo": "octo/secret"},
        {"owner": "octo", "repository_name": "secret"},
    ]
    for args in cases:
        assert "is not one of them" in _judge(SUBSET, _call("anything", **args)).refusal, args


def test_a_call_naming_no_repository_is_refused_unless_it_reads_who_the_account_is():
    v = _judge(SUBSET, _call("list_notifications"))
    assert v.refusal.endswith("The tool list_notifications names none of them.")
    assert "names none" in _judge(SUBSET, _call("projects_list", owner="octo")).refusal
    for tool in ("get_me", "get_teams", "get_team_members"):
        assert _passes(SUBSET, _call(tool, org="octo"))
    for tool in ("create_repository", "fork_repository", "sub_issue_write"):
        refusal = _judge(SUBSET, _call(tool, owner="octo", repo="hello", name="x")).refusal
        assert refusal.endswith(f"The tool {tool} is not allowed for it."), tool
    # A query on a tool that is no search earns it nothing.
    assert "names none" in _judge(SUBSET, _call("list_gists", query="anything")).refusal


def test_a_search_is_held_to_its_repo_qualifiers():
    assert _passes(SUBSET, _call("search_code", query="foo REPO:octo/hello"))
    for wild in ("foo repo:octo/*", "foo repo:octo/hello,octo/secret", "foo repo:octo"):
        assert "is not one of them" in _judge(SUBSET, _call("search_code", query=wild)).refusal, wild
    assert "octo/secret is not one of them" in _judge(
        SUBSET, _call("search_code", query='foo repo:"octo/secret"')).refusal
    for wide in ("foo org:octo", "foo user:octo", "foo owner:octo", "repo:octo/hello OR is:open",
                 "(repo:octo/hello) OR (is:open)", "foo -org:octo repo:octo/hello", "foo ORG:octo",
                 "secret NOT repo:octo/hello", "secret NOT (repo:octo/hello)"):
        v = _judge(SUBSET, _call("search_issues", query=wide))
        assert "It may not use org:, user:, owner:, OR, or NOT before repo:" in v.refusal, wide
    # A lower-case "or" is a word, not the operator; NOT before a word excludes the word.
    assert _passes(SUBSET, _call("search_code", query="this or that repo:octo/hello"))
    assert _passes(SUBSET, _call("search_code", query="foo NOT bar repo:octo/hello"))


def test_a_search_past_the_query_cap_is_refused_unread():
    # GitHub takes 256 characters; past four times that the judge reads nothing
    # and refuses as widening, so the judge's work never grows with a crafted query.
    long_ok = "foo " * 200 + "repo:octo/hello"
    assert len(long_ok) <= gr._QUERY_MAX
    assert _passes(SUBSET, _call("search_code", query=long_ok))
    too_long = "foo " * 300 + "repo:octo/hello"
    assert len(too_long) > gr._QUERY_MAX
    assert "It may not use org:, user:, owner:, OR, or NOT before repo:" in _judge(
        SUBSET, _call("search_code", query=too_long)).refusal
    assert gr.named_in_query(too_long) == []
    # The blanks after NOT, however many, and the parentheses with them.
    assert gr.widens("secret NOT      ( ( repo:octo/hello")


def test_a_search_naming_no_repository_gets_the_reachs_qualifiers():
    v = _judge(SUBSET, _call("search_code", query="foo -repo:octo/hello"))
    assert not v.refusal and v.body is not None
    sent = json.loads(v.body)
    assert sent["id"] == 7 and sent["params"]["name"] == "search_code"
    assert sent["params"]["arguments"]["query"] == (
        "foo -repo:octo/hello repo:octo/hello repo:otodock/otodock-git-test")
    big = gr.reach_of([f"o/r{i}" for i in range(gr.MAX_ADDED_QUALIFIERS + 1)], False)
    assert _judge(big, _call("search_code", query="foo")).refusal.endswith(
        "Add repo:owner/name to the search.")


def test_a_batch_is_judged_message_by_message_and_refused_whole():
    batch = json.dumps([json.loads(_call("get_me")), json.loads(_call("list_issues", owner="x", repo="y"))])
    assert "x/y is not one of them" in _judge(SUBSET, batch.encode()).refusal
    batch = json.dumps([json.loads(_call("get_me")), json.loads(_call("search_code", query="q"))])
    v = _judge(SUBSET, batch.encode())
    assert isinstance(json.loads(v.body), list) and "repo:octo/hello" in v.body.decode()


def test_other_methods_resources_and_prompts():
    for method in ("initialize", "tools/list", "notifications/initialized", "ping", "resources/list",
                   "resources/templates/list", "prompts/list", "logging/setLevel", "tasks/get"):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}).encode()
        assert _passes(SUBSET, body), method
    for method in ("completion/complete", "sampling/createMessage", "brand/new"):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}).encode()
        assert _judge(READ_ONLY, body).refusal == gr.METHOD_REFUSED, method
    answer = json.dumps({"jsonrpc": "2.0", "id": 9, "result": {}}).encode()
    assert _passes(SUBSET, answer)

    def read(uri: str) -> bytes:
        return json.dumps({"jsonrpc": "2.0", "id": 2, "method": "resources/read",
                           "params": {"uri": uri}}).encode()
    assert _passes(SUBSET, read("repo://octo/hello/contents/README.md"))
    assert "octo/secret is not one" in _judge(SUBSET, read("repo://octo/secret/contents/x")).refusal
    assert "This resource names none" in _judge(SUBSET, read("https://example.com/x")).refusal
    assert _passes(READ_ONLY, read("repo://octo/secret/contents/x"))
    prompt = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "prompts/get",
                         "params": {"name": "p", "arguments": {"owner": "octo", "repo": "secret"}}}).encode()
    assert "octo/secret" in _judge(SUBSET, prompt).refusal


def test_an_unreadable_body_is_refused_and_an_unnarrowed_reach_judges_nothing():
    for bad in (b"{not json", b"\xff\xfe", b'{"a": 1} {"b": 2}', b"42", b"[]", b'[1]',
                b'{"method": "tools/call", "Method": "ping"}', b'{"id": NaN, "method": "ping"}',
                b'{"method": 7}'):
        assert _judge(SUBSET, bad).refusal == gr.UNREADABLE, bad
    assert _judge(gr.Reach(), b"{not json") == gr.Verdict()
    assert _judge(SUBSET, b"") == gr.Verdict()


def test_a_key_a_looser_parser_would_read_is_refused():
    """A server that matches keys without case, or reads a key up to a NUL,
    must find nothing the judge did not read."""
    probes = (
        b'{"jsonrpc":"2.0","id":1,"Method":"tools/call","Params":{"name":'
        b'"get_file_contents","arguments":{"owner":"evil","repo":"y"}}}',
        b'{"method":"tools/call","params":{"name":"get_me","name\\u0000":'
        b'"get_file_contents","arguments":{"owner":"evil","repo":"y"}}}',
        b'{"method":"tools/call","params":{"name":"list_issues","arguments":{"Owner":"evil","repo":"octo/hello"}}}',
        '{"method":"tools/call","params":{"name":"get_me","arguments":{"ownér":"evil"}}}'.encode(),
    )
    for body in probes:
        assert _judge(SUBSET, body).refusal == gr.UNREADABLE, body
    # A key in camel case that is no key the judge reads is the server's own.
    assert _passes(SUBSET, _call("pull_request_read", owner="octo", repo="hello", pullNumber=3))


def test_the_forward_carries_only_what_the_judge_read():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "extra": 1,
                       "params": {"name": "get_me", "arguments": {}, "_meta": {"progressToken": 2},
                                  "other": "x"}}).encode()
    assert json.loads(_judge(SUBSET, body).body) == {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_me", "arguments": {}, "_meta": {"progressToken": 2}}}
    init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "c"}}}
    assert json.loads(_judge(SUBSET, json.dumps({**init, "x": 1}).encode()).body) == init
    answer = {"jsonrpc": "2.0", "id": 9, "result": {}}
    assert json.loads(_judge(SUBSET, json.dumps({**answer, "method2": "tools/call"}).encode()).body) == answer
    for bad in ({"jsonrpc": "2.0", "id": 9}, {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": [1]}):
        assert _judge(SUBSET, json.dumps(bad).encode()).refusal == gr.UNREADABLE, bad


def test_a_dot_segment_or_a_percent_sign_in_an_address_argument_is_refused():
    """``../`` in a ``sha`` or a ``path`` walks out of the repository the call
    names; text a person reads may carry anything."""
    assert _judge(SUBSET, _call("anything", owner="Octo", repo="hello/../secret")).refusal == gr.PATH_REFUSED
    for args in ({"sha": "../../../evil/y/commits/main"}, {"path": "docs/../../../evil/y/x"},
                 {"path": "%2e%2e/x"}, {"ref": "main%2F..%2F"}, {"path": "a\\b"},
                 {"files": [{"path": "../x", "content": "c"}]}):
        assert _judge(SUBSET, _call("get_commit", owner="octo", repo="hello", **args)).refusal == (
            gr.PATH_REFUSED), args
    assert _passes(SUBSET, _call("issue_write", owner="octo", repo="hello", method="create", title="50% done",
                                 body="Run ../configure, then C:\\build"))
    assert _passes(SUBSET, _call("push_files", owner="octo", repo="hello", branch="release/1.9.0", message="m",
                                 files=[{"path": "src/a.py", "content": "import ../x  # 100%"}]))
    # Read only alone names no repository to walk out of.
    assert _passes(READ_ONLY, _call("get_commit", owner="octo", repo="hello", sha="../x"))

    def read(uri: str) -> bytes:
        return json.dumps({"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": uri}}).encode()
    assert _judge(SUBSET, read("repo://octo/hello/contents/../../../evil/y/contents/README.md")).refusal == (
        gr.PATH_REFUSED)
    assert _judge(SUBSET, read("repo://octo/hello/contents/a%2Fb")).refusal == gr.PATH_REFUSED
    prompt = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "prompts/get",
                         "params": {"name": "p", "arguments": {"owner": "octo", "repo": "hello", "ref": "../x"}}})
    assert _judge(SUBSET, prompt.encode()).refusal == gr.PATH_REFUSED


def test_read_only_refuses_a_tool_its_annotations_call_a_write():
    def writes(tool: str):
        return {"issue_write": True, "issue_read": False}.get(tool)
    assert _judge(READ_ONLY, _call("issue_write", owner="a", repo="b"), writes).refusal == (
        "This agent may only read on GitHub. The tool issue_write changes something, so it was refused.")
    assert _passes(READ_ONLY, _call("issue_read", owner="a", repo="b"), writes)
    # Unknown to the platform: GitHub's own read-only mode decides.
    assert _passes(READ_ONLY, _call("brand_new_tool"), writes)
    # Both: the write is refused before the repository is looked at.
    assert "only read" in _judge(BOTH, _call("issue_write", owner="x", repo="y"), writes).refusal


# --- git and gh -------------------------------------------------------------------

def test_git_asks_with_the_repositorys_path():
    assert gr.judge_git(SUBSET, "octo/hello.git") == ""
    assert gr.judge_git(SUBSET, "/OtoDock/otodock-git-test") == ""
    assert "octo/secret is not one of them" in gr.judge_git(SUBSET, "octo/secret.git")
    assert gr.judge_git(SUBSET, "") == (
        "This agent may reach only some GitHub repositories, and this session's credential helper "
        "does not say which one a command needs. Start a new session.")
    assert gr.judge_git(READ_ONLY, "") == ""                       # no list: any path
    assert gr.judge_git(READ_ONLY, f"{gr.PUSH_MARKER}/octo/hello.git") == (
        "This agent may only read on GitHub. git push was refused.")
    assert gr.judge_git(gr.Reach(), "") == ""
    assert gr.judge_git(SUBSET, "octo/hello.git/info/lfs") == ""
    for bad in ("octo/../secret", "octo/hel%6co"):
        assert gr.judge_git(SUBSET, bad) == gr.UNREADABLE, bad


def test_gh_is_held_to_the_repositories_it_names_or_works_in():
    remote = ["https://github.com/octo/hello.git"]
    assert gr.judge_gh(SUBSET, ["pr", "list", "-R", "octo/hello"], []) == ""
    assert gr.judge_gh(SUBSET, ["pr", "list", "--repo=octo/hello"], []) == ""
    assert gr.judge_gh(SUBSET, ["pr", "list", "-R", "github.com/octo/hello"], []) == ""
    assert gr.judge_gh(SUBSET, ["pr", "list", "-R", "https://github.com/octo/hello"], []) == ""
    assert "is not one of them" in gr.judge_gh(SUBSET, ["pr", "list", "-R", "ghe.example/octo/hello"], [])
    # gh prefers another remote than origin: every GitHub remote is held.
    assert "octo/secret is not one" in gr.judge_gh(
        SUBSET, ["pr", "list"], ["https://github.com/octo/hello", "https://github.com/octo/secret"])
    assert gr.judge_gh(SUBSET, ["issue", "list"], remote) == ""
    assert gr.judge_gh(SUBSET, ["issue", "list"], ["git@github.com:octo/hello.git"]) == ""
    assert "octo/secret is not one" in gr.judge_gh(SUBSET, ["pr", "view", "-Rocto/secret", "1"], remote)
    assert "octo/secret is not one" in gr.judge_gh(
        SUBSET, ["pr", "view", "https://github.com/octo/secret/pull/3"], remote)
    assert "octo/secret is not one" in gr.judge_gh(SUBSET, ["repo", "clone", "octo/secret"], [])
    assert gr.judge_gh(SUBSET, ["api", "repos/octo/hello/issues"], []) == ""
    assert "names none" in gr.judge_gh(SUBSET, ["api", "graphql", "-f", "query=x"], [])
    assert "names none" in gr.judge_gh(SUBSET, ["search", "repos", "foo"], remote)
    assert "names none" in gr.judge_gh(SUBSET, ["issue", "list"], ["https://gitlab.com/octo/hello"])
    assert gr.judge_gh(SUBSET, ["auth", "status"], []) == ""
    assert gr.judge_gh(SUBSET, ["api", "user"], []) == ""


def test_gh_reads_its_flags_as_gh_does():
    """A flag's value is never read as the command's argument, and a flag
    gh's help does not list, before a word, stops the command."""
    here = ["https://github.com/octo/hello"]
    assert "octo/secret is not one" in gr.judge_gh(
        SUBSET, ["api", "--template", "repos/octo/hello", "repos/octo/secret"], here)
    assert "octo/secret is not one" in gr.judge_gh(SUBSET, ["repo", "view", "--json", "name", "octo/secret"], here)
    assert gr.judge_gh(SUBSET, ["repo", "view", "--json", "name", "octo/hello"], []) == ""
    assert gr.judge_gh(SUBSET, ["issue", "list", "-L", "5", "-s", "open", "--json", "title"], here) == ""
    assert gr.judge_gh(SUBSET, ["issue", "list", "-L5", "--state=open"], here) == ""
    assert gr.judge_gh(SUBSET, ["pr", "view", "-c", "12"], here) == ""          # -c takes no value here
    assert gr.judge_gh(SUBSET, ["pr", "merge", "-s", "12"], here) == ""         # nor -s here
    assert gr.judge_gh(SUBSET, ["pr", "list", "-s", "closed"], here) == ""      # but -s does here
    assert gr.judge_gh(SUBSET, ["issue", "view", "3", "--brand-new"], here) == ""
    for argv in (["issue", "view", "--brand-new", "3"], ["pr", "-Z", "x", "list"], ["pr", "list", "-Zq"]):
        assert gr.judge_gh(SUBSET, argv, here) == gr.GH_UNREADABLE, argv
        assert gr.judge_gh(READ_ONLY, argv, here) == gr.GH_UNREADABLE, argv
    assert gr.judge_gh(SUBSET, ["issue", "view", "--brand-new=3"], here) == ""
    # What follows -- is git's, not gh's.
    assert gr.judge_gh(SUBSET, ["repo", "clone", "octo/hello", "--", "--depth", "1"], []) == ""


def test_gh_repo_search_api_and_its_environment():
    here = ["https://github.com/octo/hello"]
    for verb in ("create", "fork", "list"):
        assert f"gh repo {verb} is not allowed" in gr.judge_gh(SUBSET, ["repo", verb, "newrepo"], here)
    for argv in (["repo", "clone", "hello"], ["repo", "view", "secret"]):
        assert "names no owner. Write it as owner/name." in gr.judge_gh(SUBSET, argv, here), argv
    assert "octo/secret is not one" in gr.judge_gh(SUBSET, ["repo", "sync", "--source", "octo/secret"], here)
    assert "octo/secret is not one" in gr.judge_gh(
        SUBSET, ["search", "issues", "--repo", "octo/hello", "repo:octo/secret"], here)
    assert gr.judge_gh(SUBSET, ["search", "issues", "--repo", "octo/hello", "bug"], here) == ""
    for wide in (["search", "code", "--owner", "octo", "x"], ["search", "issues", "x", "org:octo"],
                 ["secret", "list", "--org", "octo"], ["variable", "list", "-o", "octo"],
                 ["issue", "list", "-S", "bug OR repo:octo/secret"]):
        assert "It may not use org:" in gr.judge_gh(SUBSET, wide, here), wide
    assert "octo/secret is not one" in gr.judge_gh(SUBSET, ["issue", "list", "--search", "repo:octo/secret"], here)
    assert gr.judge_gh(SUBSET, ["api", "repos/octo/hello/../../octo/secret"], []) == gr.PATH_REFUSED
    assert gr.judge_gh(SUBSET, ["api", "repos/octo/hello/contents/a%2F..%2Fb"], []) == gr.PATH_REFUSED
    assert gr.judge_gh(SUBSET, ["api", "repos/{owner}/{repo}/issues"], here) == ""
    assert "names none" in gr.judge_gh(SUBSET, ["api", "repos/{owner}/{repo}/issues"], [])
    # GH_REPO and GH_HOST are read as gh reads them.
    assert "octo/secret is not one" in gr.judge_gh(SUBSET, ["issue", "list"], here, gh_repo="octo/secret")
    assert gr.judge_gh(SUBSET, ["issue", "list"], [], gh_repo="octo/hello") == ""
    assert "set to another host, ghe.example" in gr.judge_gh(SUBSET, ["issue", "list"], here, gh_host="ghe.example")
    assert "set to another host" in gr.judge_gh(SUBSET, ["api", "--hostname", "x.ghe.com", "repos/octo/hello"], [])
    assert gr.judge_gh(SUBSET, ["issue", "list"], here, gh_host="github.com") == ""
    # The token is never printed under a reach; without one, gh runs as it is.
    for argv in (["auth", "status", "-t"], ["auth", "status", "--show-token"],
                 ["auth", "token"], ["auth", "git-credential", "get"],
                 ["auth", "token", "--hostname", "github.com"]):
        assert gr.judge_gh(SUBSET, argv, []) == gr.GH_SHOWS_TOKEN
        assert gr.judge_gh(READ_ONLY, argv, []) == gr.GH_SHOWS_TOKEN
        assert gr.judge_gh(gr.Reach(), argv, []) == ""


def test_gh_under_read_only_runs_reading_commands_alone():
    for argv in (["pr", "list"], ["repo", "view"], ["api", "repos/a/b"], ["api", "-X", "GET", "repos/a/b"],
                 ["search", "code", "x"], ["run", "view", "1"], ["auth", "status"]):
        assert gr.judge_gh(READ_ONLY, argv, []) == "", argv
    for argv in (["pr", "create"], ["issue", "comment", "1"], ["repo", "delete", "a/b"],
                 ["api", "-X", "POST", "repos/a/b/issues"], ["api", "--method=PATCH", "repos/a/b"],
                 ["api", "repos/a/b/issues", "-f", "title=x"], ["workflow", "run", "ci"], ["secret", "set", "X"]):
        assert gr.judge_gh(READ_ONLY, argv, []) == (
            "This agent may only read on GitHub. This gh command changes something, so it did not run."), argv


def test_remote_addresses_on_github_alone():
    assert gr.name_of_remote("ssh://git@github.com/Octo/Hello.git") == "octo/hello"
    assert gr.name_of_remote("git@github.com:octo/hello.git") == "octo/hello"
    assert gr.name_of_remote("git@example.com:octo/hello.git") == ""
    assert gr.name_of_url("https://example.com/octo/hello") == ""


# --- GitHub's own narrowing ------------------------------------------------------

def test_an_installation_token_asks_for_its_permissions_at_read():
    granted = {"contents": "write", "issues": "write", "metadata": "read", "workflows": "write",
               "administration": "admin", "odd": "none", "profile": "write",
               "organization_copilot_seat_management": "write"}
    assert gr.read_only_permissions(granted) == {
        "contents": "read", "issues": "read", "metadata": "read", "administration": "read"}
    assert gr.read_only_permissions(None) == {}
    assert gr.repository_names(BOTH, "Octo") == ["hello"]
    for account in ("otodock", ""):
        with pytest.raises(ValueError):       # never cut to a name of this account
            gr.repository_names(SUBSET, account)


# --- who narrowed ---------------------------------------------------------------

MINE = gr.reach_of(["octo/c", "octo/a", "octo/b"], False, personal=True)
MINE_READ_ONLY = gr.reach_of([], True, personal=True)


def test_a_persons_own_narrowing_is_worded_as_theirs():
    assert _judge(MINE, _call("get_file_contents", owner="evil", repo="x")).refusal == (
        "You limited this agent to octo/a, octo/b and octo/c in your GitHub connection. "
        "evil/x is not one of them.")
    def writes(tool: str):
        return tool == "issue_write"
    assert _judge(MINE_READ_ONLY, _call("issue_write", owner="a", repo="b"), writes).refusal == (
        "You made this agent read only in your GitHub connection. The tool issue_write changes "
        "something, so it was refused.")
    assert gr.judge_git(MINE_READ_ONLY, f"{gr.PUSH_MARKER}/octo/a.git") == (
        "You made this agent read only in your GitHub connection. git push was refused.")
    assert gr.judge_git(MINE, "") == (
        "You limited this agent to some repositories in your GitHub connection, and this session's "
        "credential helper does not say which one a command needs. Start a new session.")
    assert gr.judge_git(MINE, "octo/z.git").startswith("You limited this agent to octo/a, octo/b and octo/c")
    assert gr.judge_gh(MINE, ["issue", "list", "-R", "evil/x"], []).endswith("evil/x is not one of them.")
    assert gr.judge_gh(MINE_READ_ONLY, ["issue", "create", "-R", "octo/a"], []) == (
        "You made this agent read only in your GitHub connection. This gh command changes something, "
        "so it did not run.")
    assert gr.judge_gh(MINE, ["repo", "create", "x"], []) == (
        "You limited this agent to octo/a, octo/b and octo/c in your GitHub connection. "
        "The command gh repo create is not allowed for it.")


def test_the_verdicts_never_depend_on_who_narrowed():
    service = gr.reach_of(sorted(MINE.repositories), False)
    for body in (_call("get_file_contents", owner="octo", repo="a"), _call("search_code", query="x"),
                 _call("search_code", query="x org:evil"), _call("get_me")):
        a, b = _judge(service, body), _judge(MINE, body)
        assert bool(a.refusal) == bool(b.refusal) and a.body == b.body
    for path in ("octo/a.git", "evil/x.git", ""):
        assert bool(gr.judge_git(service, path)) == bool(gr.judge_git(MINE, path))


def test_a_list_is_worded_as_a_sentence_lists_it():
    def own(*names):
        return gr.outside_sentence(gr.reach_of(names, False, personal=True), "z/z")
    assert own("a/a").startswith("You limited this agent to a/a in")
    assert own("a/a", "b/b").startswith("You limited this agent to a/a and b/b in")
    assert own(*(f"o/r{i}" for i in range(7))).startswith(
        "You limited this agent to o/r0, o/r1, o/r2, o/r3, o/r4 and 2 more in")


def test_a_user_scope_session_is_told_who_narrowed_it():
    both = gr.reach_of(["octo/a", "octo/b"], True, personal=True)
    assert gr.instructions_line(both).splitlines()[2:] == [
        "Your user limited this agent to octo/a and octo/b in their GitHub connection. "
        "Any other repository is refused, in the GitHub tool and in git and gh.",
        "Your user made this agent read only in their GitHub connection. Every change (a commit, "
        "a push, an issue, a comment, a merge) is refused.",
    ]
    assert gr.instructions_line(BOTH).splitlines()[2:] == [
        "This agent may reach only these GitHub repositories: octo/hello. "
        "Any other repository is refused, in the GitHub tool and in git and gh.",
        "This agent may only read on GitHub. Every change (a commit, a push, an issue, a comment, "
        "a merge) is refused.",
    ]
