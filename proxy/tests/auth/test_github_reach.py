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


REVIEW = "pull_request_review_write"


@pytest.mark.parametrize("arguments", [
    {"method": "resolve_thread", "threadId": "PRRT_x"},
    {"method": "unresolve_thread", "threadId": "PRRT_x"},
    {"method": "Resolve_Thread", "threadId": "PRRT_x"},
    {"method": " unresolve_thread "},
    {"method": "RESOLVE_THREAD"},
    {"method": "create", "threadId": "PRRT_x"},
    {"method": "submit_pending", "thread_id": "PRRT_x"},
    {"method": "delete_pending", "ThreadID": 7},
    {"method": "create", "threadId": " "},
    {"method": "resolve_thread", "threadId": ""},
])
def test_a_review_thread_is_not_resolved_under_a_list(arguments):
    """The tool resolves a thread by its id alone and reads no repository
    for it, so the listed repository the call names beside it says nothing."""
    refusal = _judge(SUBSET, _call(REVIEW, owner="octo", repo="hello", pullNumber=3, **arguments)).refusal
    assert refusal == f"{gr._only(SUBSET)} The tool {REVIEW} may not resolve a review thread for it."


def test_a_review_is_written_under_a_list_and_a_thread_resolved_with_nothing_chosen():
    # An engine fills a field it does not use with nothing or with an empty text.
    for arguments in ({"method": "create", "event": "APPROVE"}, {"method": "submit_pending", "body": "ok"},
                      {"method": "delete_pending"}, {"method": "create", "threadId": None},
                      {"method": "create", "threadId": ""}, {"method": "submit_pending", "thread_id": ""}):
        assert _passes(SUBSET, _call(REVIEW, owner="octo", repo="hello", pullNumber=3, **arguments))
    # Another tool with such a key is judged by what it names, as before.
    assert _passes(SUBSET, _call("add_comment_to_pending_review", owner="octo", repo="hello", threadId="x"))
    # No list: nothing changes. Read only refuses it as it refuses every write.
    resolve = _call(REVIEW, owner="any", repo="repo", pullNumber=3, method="resolve_thread", threadId="PRRT_x")
    assert not _judge(gr.reach_of([], False), resolve).refusal
    assert _passes(READ_ONLY, resolve)
    assert _judge(READ_ONLY, resolve, writes=lambda _tool: True).refusal \
        == gr.read_only_sentence(READ_ONLY, f"The tool {REVIEW}")


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


# What gh sends, by a capture of gh 2.103.3 at a stand-in: the working
# directory's remote is acme/here, and each source names acme/there.
HERE = ["https://github.com/acme/here.git"]
THERE = gr.reach_of(["acme/there"], False)
HERE_ONLY = gr.reach_of(["acme/here"], False)
BOTH_LISTED = gr.reach_of(["acme/there", "acme/here"], False)
ADDRESS = "https://github.com/acme/there"


@pytest.mark.parametrize("argv", [
    ["issue", "list", "--search", "repo:acme/there bug"],
    ["issue", "list", "-S", "repo:acme/there"],
    ["pr", "list", "--search", "repo:acme/there"],
    ["pr", "list", "-S", "repo:acme/there is:open"],
])
def test_gh_judges_the_folders_repository_beside_a_search_in_it(argv):
    """gh adds the repository it works in to the search it sends, so a
    repository named in the search does not take its place."""
    assert gr.judge_gh(THERE, argv, HERE) == gr.outside_sentence(THERE, "acme/here")
    assert gr.judge_gh(BOTH_LISTED, argv, HERE) == ""
    assert gr.judge_gh(THERE, argv, [], gh_repo="acme/there") == ""
    assert gr.judge_gh(THERE, [*argv, "-R", "acme/there"], HERE) == ""
    assert gr.judge_gh(THERE, argv, []).endswith("This gh command names none of them.")
    assert gr.judge_gh(HERE_ONLY, argv, []) == gr.outside_sentence(HERE_ONLY, "acme/there")


@pytest.mark.parametrize("argv", [
    ["label", "create", ADDRESS],
    ["release", "view", f"{ADDRESS}/releases/tag/v1"],
    ["release", "delete-asset", "v1", ADDRESS, "--yes"],
    ["run", "view", f"{ADDRESS}/actions/runs/5"],
    ["workflow", "view", f"{ADDRESS}/blob/main/.github/workflows/ci.yml"],
    ["workflow", "run", ADDRESS],
    ["cache", "delete", ADDRESS],
    ["secret", "delete", ADDRESS],
    ["variable", "get", ADDRESS],
    ["browse", ADDRESS, "--no-browser"],
    ["repo", "read-file", f"{ADDRESS}/blob/main/README.md"],
    ["issue", "transfer", "1", ADDRESS],
])
def test_gh_judges_the_folders_repository_beside_an_address_it_does_not_work_in(argv):
    """An address that is a label's name, a tag, a file or where an issue
    moves to: gh works in the directory's repository all the same."""
    assert gr.judge_gh(THERE, argv, HERE) == gr.outside_sentence(THERE, "acme/here")
    assert gr.judge_gh(BOTH_LISTED, argv, HERE) == ""
    assert gr.judge_gh(THERE, [*argv, "-R", "acme/there"], HERE) == ""
    assert gr.judge_gh(THERE, argv, []).endswith("This gh command names none of them.")
    assert gr.judge_gh(HERE_ONLY, argv, []) == gr.outside_sentence(HERE_ONLY, "acme/there")


@pytest.mark.parametrize("argv", [
    ["issue", "transfer", "1", "acme/there"],
    ["issue", "transfer", "1", "github.com/acme/there"],
    ["issue", "transfer", "https://github.com/acme/here/issues/1", "acme/there"],
    ["label", "clone", "acme/there"],
    ["label", "clone", "acme/there", "--force"],
    ["issue", "develop", "1", "--branch-repo", "acme/there", "--name", "b"],
    ["issue", "develop", "1", f"--branch-repo={ADDRESS}"],
    ["issue", "close", "1", "--duplicate-of", f"{ADDRESS}/issues/2"],
])
def test_gh_judges_a_second_repository_a_command_names(argv):
    """Where an issue moves to, where labels are copied from, where a linked
    branch is made and the issue a closed one duplicates: gh reaches that
    repository beside the one it works in."""
    here = HERE_ONLY
    assert gr.judge_gh(here, argv, HERE) == gr.outside_sentence(here, "acme/there")
    assert gr.judge_gh(here, argv, [], gh_repo="acme/here") == gr.outside_sentence(here, "acme/there")
    assert gr.judge_gh(BOTH_LISTED, argv, HERE) == ""


def test_gh_a_second_repository_without_its_owner_is_refused():
    here = HERE_ONLY
    for argv in (["issue", "transfer", "1", "there"], ["label", "clone", "there"],
                 ["issue", "develop", "1", "--branch-repo", "there"]):
        assert gr.judge_gh(here, argv, HERE) == (
            f"{gr._only(here)} there names no owner. Write it as owner/name."), argv
    assert gr.judge_gh(here, ["issue", "close", "1", "--duplicate-of", "2"], HERE) == ""
    assert gr.judge_gh(here, ["issue", "close", "1", "--duplicate-of=#2"], HERE) == ""


@pytest.mark.parametrize("argv", [
    ["search", "issues", ADDRESS],
    ["search", "code", "x", f"{ADDRESS}/blob/main/a.py"],
    ["search", "prs", f"{ADDRESS}/pull/1", "is:open"],
])
def test_gh_search_is_held_to_its_repo_qualifiers_not_an_address(argv):
    """An address among a search's words is a word to look for, not a
    repository the search is held to."""
    assert gr.judge_gh(THERE, argv, HERE).endswith("This gh command names none of them.")
    assert gr.judge_gh(THERE, [*argv, "repo:acme/there"], HERE) == ""
    assert gr.judge_gh(THERE, [*argv, "-R", "acme/there"], HERE) == ""
    assert gr.judge_gh(THERE, [*argv, "repo:acme/here"], HERE) == gr.outside_sentence(THERE, "acme/here")


@pytest.mark.parametrize(("argv", "gh_repo"), [
    (["pr", "view", f"{ADDRESS}/pull/1"], ""),
    (["pr", "diff", f"{ADDRESS}/pull/1"], ""),
    (["pr", "comment", f"{ADDRESS}/pull/1", "--body", "hi"], ""),
    (["pr", "merge", f"{ADDRESS}/pull/1", "--squash"], ""),
    (["pr", "checkout", f"{ADDRESS}/pull/1"], ""),
    (["issue", "view", f"{ADDRESS}/issues/1"], ""),
    (["issue", "close", f"{ADDRESS}/issues/1"], ""),
    (["issue", "edit", "1", f"{ADDRESS}/issues/2", "--title", "x"], ""),
    (["issue", "edit", f"{ADDRESS}/issues/2", "1", "--title", "x"], ""),
    (["issue", "develop", f"{ADDRESS}/issues/1", "--name", "b"], ""),
    (["issue", "transfer", f"{ADDRESS}/issues/1", f"{ADDRESS}"], ""),
    (["pr", "list", "-R", "acme/there"], ""),
    (["pr", "status", "--repo=acme/there"], ""),
    (["issue", "create", "-R", "acme/there", "--title", "T", "--body", "B"], ""),
    (["pr", "list"], "acme/there"),
    (["issue", "list", "--search", "repo:acme/there"], "acme/there"),
    (["repo", "view", "acme/there"], ""),
    (["repo", "edit", ADDRESS, "--description", "x"], ""),
    (["repo", "sync", "--source", "acme/there"], ""),
    (["repo", "read-file", "README.md", "-R", "acme/there"], ""),
    (["label", "clone", "acme/there", "-R", "acme/there"], ""),
    (["release", "view", "v1", "-R", "acme/there"], ""),
    (["browse", "-R", "acme/there", "--no-browser"], ""),
    (["api", "repos/{owner}/{repo}/issues"], "acme/there"),
    (["api", "repos/acme/there/issues"], ""),
    (["search", "issues", "repo:acme/there", "bug"], ""),
    (["search", "prs", "--repo", "acme/there", "x"], ""),
    (["search", "issues", ADDRESS, "repo:acme/there"], ""),
    (["extension", "install", "https://github.com/acme/there"], ""),
])
def test_gh_a_source_gh_works_in_still_replaces_the_folders_repository(argv, gh_repo):
    """The capture's rows that stay: gh works in the repository these name,
    in place of the directory's, so the directory's is not judged."""
    assert gr.judge_gh(THERE, argv, HERE, gh_repo=gh_repo) == ""


@pytest.mark.parametrize("argv", [
    ["issue", "list", "--search", "repo:acme/there"],
    ["release", "view", f"{ADDRESS}/releases/tag/v1"],
    ["search", "issues", ADDRESS],
    ["label", "clone", "there"],
    ["issue", "transfer", "1", ADDRESS],
    ["issue", "develop", "1", "--branch-repo", "acme/there"],
])
def test_gh_an_agent_with_nothing_chosen_meets_no_change(argv):
    """No repository chosen: nothing above is judged, and Read only alone
    judges the verb as before."""
    for remotes in ([], HERE):
        assert gr.judge_gh(gr.Reach(), argv, remotes) == ""
        reading = argv[0] in ("search", "release") or argv[1] == "list"
        assert gr.judge_gh(READ_ONLY, argv, remotes) == ("" if reading else gr.read_only_sentence(
            READ_ONLY, "This gh command", "it did not run"))


def test_gh_the_folders_repository_alone_where_no_other_is_named():
    here = HERE_ONLY
    for argv in (["issue", "list", "--search", "bug"], ["issue", "close", "1", "--duplicate-of", "2"],
                 ["label", "list", "--search", "repo:acme/there"],
                 ["pr", "create", "--head", "acme2:feature", "--title", "T", "--body", "B"],
                 ["repo", "view"], ["repo", "read-file", "README.md"], ["api", "repos/{owner}/{repo}/issues"]):
        assert gr.judge_gh(here, argv, HERE) == "", argv
        assert gr.judge_gh(THERE, argv, HERE) == gr.outside_sentence(THERE, "acme/here"), argv


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


# ---------------------------------------------------------------------------
# One rule for a REST address: gh api asks the request's rule
# ---------------------------------------------------------------------------

def test_gh_api_asks_the_rule_that_judges_the_request_it_becomes(monkeypatch):
    """The command check and the exit hold one rule for a REST address under
    a list (``judge_request``): what the rule refuses for the request, the
    command check refuses for the command."""
    reach = gr.reach_of(["acme/app"], False)
    asked: list[tuple] = []
    real = gr.judge_request

    def recorded(reach_, method, host, target, **more):
        asked.append((method, host, target, reach_.read_only))
        return real(reach_, method, host, target, **more)

    monkeypatch.setattr(gr, "judge_request", recorded)
    assert gr.judge_gh(reach, ["api", "repos/acme/app/issues"], []) == ""
    assert gr.judge_gh(reach, ["api", "-X", "POST", "/repos/Acme/App/issues/", "-f", "title=x"], []) == ""
    assert gr.judge_gh(reach, ["api", "user"], []) == ""
    assert gr.judge_gh(reach, ["api", "repos/{owner}/{repo}/pulls"], ["https://github.com/acme/app.git"]) == ""
    assert asked == [("GET", "api.github.com", "/repos/acme/app/issues", False),
                     ("POST", "api.github.com", "/repos/Acme/App/issues", False),
                     ("GET", "api.github.com", "/user", False),
                     ("GET", "api.github.com", "/repos/acme/app/pulls", False)]
    # The same sentence as the exit's, for the same address.
    outside = gr.judge_gh(reach, ["api", "repos/other/repo/issues"], [])
    assert outside == gr.judge_request(reach, "GET", "api.github.com", "/repos/other/repo/issues").refusal
    assert outside == gr.outside_sentence(reach, "other/repo")


def test_gh_api_refuses_what_the_requests_rule_refuses():
    reach = gr.reach_of(["acme/app"], False)
    remote = ["https://github.com/acme/app.git"]
    # A new repository is outside any list, by the address as by gh repo fork.
    for argv in (["api", "-X", "POST", "repos/acme/app/forks"], ["api", "repos/acme/app/generate", "-f", "name=x"],
                 ["api", "--method", "POST", "repos/{owner}/{repo}/forks"]):
        assert "A new repository is outside them." in gr.judge_gh(reach, argv, remote)
    assert gr.judge_gh(reach, ["api", "repos/acme/app/forks"], remote) == ""
    # Who the account is, for a read alone.
    for argv in (["api", "-X", "PATCH", "user", "-f", "bio=x"], ["api", "user", "-f", "bio=x"],
                 ["api", "-X", "DELETE", "user"]):
        assert gr.judge_gh(reach, argv, remote).endswith("names none of them.")
    assert gr.judge_gh(reach, ["api", "-X", "HEAD", "user"], remote) == ""
    # A change of an issue's sub-issues, by the address as by the tool.
    for argv in (["api", "-X", "POST", "repos/acme/app/issues/7/sub_issues", "-F", "sub_issue_id=9"],
                 ["api", "repos/{owner}/{repo}/issues/7/sub_issue", "-X", "DELETE"],
                 ["api", "--method", "PATCH", "repos/acme/app/issues/7/sub_issues/priority"],
                 ["api", "repos/acme/app/issues/7/sub_issues", "-f", "sub_issue_id=9"]):
        assert gr.judge_gh(reach, argv, remote) == f"{gr._only(reach)} {gr.LINKS_REFUSED}"
    assert gr.judge_gh(reach, ["api", "repos/acme/app/issues/7/sub_issues"], remote) == ""
    # The issues that block one, and its related issues, by the address too.
    for argv in (["api", "-X", "POST", "repos/acme/app/issues/7/dependencies/blocked_by", "-F", "issue_id=9"],
                 ["api", "repos/{owner}/{repo}/issues/7/dependencies/blocked_by/9", "-X", "DELETE"],
                 ["api", "repos/acme/app/issues/7/relates_to", "-F", "issue_id=9"],
                 ["api", "--method", "DELETE", "repos/{owner}/{repo}/issues/7/relates_to/9"]):
        assert gr.judge_gh(reach, argv, remote) == f"{gr._only(reach)} {gr.LINKS_REFUSED}"
    for argv in (["api", "repos/acme/app/issues/7/dependencies/blocked_by"],
                 ["api", "repos/{owner}/{repo}/issues/7/relates_to"]):
        assert gr.judge_gh(reach, argv, remote) == ""
    # An advisory's temporary fork is a new repository.
    for argv in (["api", "-X", "POST", "repos/acme/app/security-advisories/GHSA-abcd-1234-efgh/forks"],
                 ["api", "repos/{owner}/{repo}/security-advisories/GHSA-abcd-1234-efgh/forks", "-f", "x=1"]):
        assert "A new repository is outside them." in gr.judge_gh(reach, argv, remote)
    assert gr.judge_gh(reach, ["api", "repos/acme/app/security-advisories/GHSA-abcd-1234-efgh/forks"], remote) == ""
    assert gr.judge_gh(reach, ["api", "-X", "POST", "repos/acme/app/issues/7/labels"], remote) == ""
    # An empty segment.
    assert gr.judge_gh(reach, ["api", "repos/acme/app//issues"], remote) == gr.PATH_REFUSED
    assert gr.judge_gh(reach, ["api", "repos/{owner}/{repo}//issues"], remote) == gr.PATH_REFUSED
    # Under read only the command check's own rule still comes first.
    both = gr.reach_of(["acme/app"], True)
    assert gr.judge_gh(both, ["api", "-X", "POST", "repos/acme/app/forks"], remote) \
        == gr.read_only_sentence(both, "This gh command", "it did not run")
    assert gr.judge_gh(both, ["api", "repos/acme/app/forks"], remote) == ""


@pytest.mark.parametrize("argv", [
    ["api", "/"],
    ["api", ""],
    ["api", "search/issues?q=repo:acme/app+bug"],
    ["api", "-X", "GET", "search/issues", "-f", "q=repo:acme/app bug"],
    ["api", "repos/acme/app/contents/a%20b.md"],
    ["api", "https://api.github.com/repos/acme/app/issues"],
    ["api", "https://uploads.github.com/repos/acme/app/releases/7/assets?name=x"],
    ["api", "repositories/1/issues"],
    ["api", "graphql", "-f", "query={viewer{login}}"],
    ["api", "user/repos"],
    ["api", "repos/acme"],
    ["api", "repos/acme/app#x/issues"],
    ["api", "repos/ac\tme/app/issues"],
    ["api", "repos/acme/ap\rp/issues"],
    ["api", "repos/acme\n/app/issues"],
    ["api", "repos/{owner}/{repo}/issues#x"],
])
def test_gh_api_stays_stricter_than_the_exit_where_it_was(argv):
    """The command check is the only check on a paired macOS or Windows
    computer, so it takes nothing it refused before: a percent
    sign, the root, a search, a whole address and every address that is not
    under ``repos/`` stay refused, also where the exit's rule would let the
    request go."""
    reach = gr.reach_of(["acme/app"], False)
    assert gr.judge_gh(reach, argv, ["https://github.com/acme/app.git"]) != ""
