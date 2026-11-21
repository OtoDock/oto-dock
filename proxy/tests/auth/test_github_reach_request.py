"""An agent's GitHub reach at the credential exit
(``auth/github_reach.judge_request``): each request that will carry git's and
gh's token is held to the repositories and to read only by its address, a
GraphQL request to read only alone, and a body is read only where the
address does not say enough.
"""

from __future__ import annotations

import json

import pytest

from auth import github_reach as gr

LISTED = gr.reach_of(["Acme/App", "acme/tools"], False)
READ_ONLY = gr.reach_of([], True)
BOTH = gr.reach_of(["acme/app"], True)
MINE = gr.reach_of(["acme/app"], True, personal=True)
GIT, API, UPLOADS = "github.com", "api.github.com", "uploads.github.com"
JSON = "application/json; charset=utf-8"


def _judge(reach, method, host, target, **more) -> gr.RequestVerdict:
    return gr.judge_request(reach, method, host, target, **more)


def _goes(reach, method, host, target, **more) -> bool:
    verdict = _judge(reach, method, host, target, **more)
    return not verdict.refusal and not verdict.wants_body


def test_nothing_chosen_judges_nothing():
    wide = gr.reach_of([], False)
    for method, host, target in [("POST", GIT, "/x/y.git/git-receive-pack"), ("DELETE", API, "/repos/x/y"),
                                 ("POST", API, "/graphql"), ("GET", API, "/repositories/1/../../x")]:
        assert _judge(wide, method, host, target) == gr.RequestVerdict()


# ---------------------------------------------------------------------------
# git over https
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method, target", [
    ("GET", "/acme/app.git/info/refs?service=git-upload-pack"),
    ("POST", "/acme/app.git/git-upload-pack"),
    ("GET", "/ACME/App/info/refs?service=git-receive-pack"),
    ("POST", "/acme/app.git/git-receive-pack"),
    ("POST", "/acme/tools.git/info/lfs/objects/batch"),
])
def test_gits_requests_for_a_listed_repository_go(method, target):
    assert _goes(LISTED, method, GIT, target)


@pytest.mark.parametrize("target, name", [
    ("/other/repo.git/info/refs?service=git-upload-pack", "other/repo"),
    ("/acme/secret.git/git-upload-pack", "acme/secret"),
    ("/acme/app2/info/refs", "acme/app2"),
])
def test_gits_request_for_another_repository_is_refused_with_its_name(target, name):
    verdict = _judge(LISTED, "GET", GIT, target)
    assert verdict.refusal == gr.outside_sentence(LISTED, name) and verdict.kind == gr.KIND_GIT


def test_a_git_address_that_names_no_repository_is_refused_under_a_list_alone():
    for target in ("/", "/acme", "/login"):
        assert "names none of them" in _judge(LISTED, "GET", GIT, target).refusal
        assert _goes(READ_ONLY, "GET", GIT, target)


@pytest.mark.parametrize("method, target", [
    ("GET", "/acme/app.git/info/refs?service=git-receive-pack"),
    ("GET", "/acme/app.git/info/refs?service=git-upload-pack&service=git-receive-pack"),
    ("GET", "/acme/app.git/info/refs?x=1;service=git-receive-pack"),
    ("GET", "/acme/app.git/info/refs?service=git%2Dreceive-pack"),
    ("GET", "/acme/app.git/info/refs?SERVICE=GIT-RECEIVE-PACK"),
    ("POST", "/acme/app.git/git-receive-pack"),
    ("GET", "/acme/app.git/git-receive-pack"),
])
def test_read_only_refuses_a_push_at_either_of_its_requests(method, target):
    for reach in (READ_ONLY, BOTH):
        verdict = _judge(reach, method, GIT, target)
        assert verdict.refusal == gr.push_refused(reach) and verdict.kind == gr.KIND_GIT
    assert _judge(MINE, method, GIT, target).refusal == gr.push_refused(MINE)


@pytest.mark.parametrize("method, target", [
    ("GET", "/acme/app.git/info/refs?service=git-upload-pack"),
    ("GET", "/acme/app.git/info/refs"),
    ("POST", "/acme/app.git/git-upload-pack"),
    ("GET", "/acme/app.git/info/lfs/locks"),
    ("HEAD", "/acme/app/archive/main.zip"),
])
def test_read_only_lets_a_clone_and_a_fetch_go(method, target):
    assert _goes(READ_ONLY, method, GIT, target) and _goes(BOTH, method, GIT, target)


@pytest.mark.parametrize("method, target", [
    ("POST", "/acme/app.git/info/lfs/locks"),
    ("POST", "/acme/app.git/info/lfs/locks/7/unlock"),
    ("PUT", "/acme/app.git/info/lfs/objects/abc"),
    ("DELETE", "/acme/app"),
    ("PATCH", "/acme/app.git/whatever"),
])
def test_read_only_refuses_every_other_write_to_a_git_host(method, target):
    assert _judge(READ_ONLY, method, GIT, target).refusal == gr.read_only_sentence(READ_ONLY, gr.REQUEST)


def test_the_read_only_marker_path_is_refused_as_a_push():
    assert _judge(READ_ONLY, "GET", GIT, "/otodock-read-only/acme/app.git/info/refs").refusal \
        == gr.push_refused(READ_ONLY)


LFS = "/acme/app.git/info/lfs/objects/batch"
LFS_TYPE = "application/vnd.git-lfs+json"


def test_an_lfs_batch_under_read_only_is_read_and_goes_for_a_download_alone():
    first = _judge(READ_ONLY, "POST", GIT, LFS)
    assert first.wants_body == gr.REQUEST_BODY_MAX and not first.refusal and first.kind == gr.KIND_LFS
    download = json.dumps({"operation": "download", "objects": [{"oid": "a", "size": 1}]}).encode()
    verdict = _judge(READ_ONLY, "POST", GIT, LFS, content_type=LFS_TYPE, body=download)
    assert not verdict.refusal and json.loads(verdict.body) == json.loads(download)
    upload = json.dumps({"operation": "upload", "objects": []}).encode()
    refused = _judge(READ_ONLY, "POST", GIT, LFS, content_type=LFS_TYPE, body=upload)
    assert refused.refusal == gr.read_only_sentence(READ_ONLY, "This upload to GitHub")
    assert refused.kind == gr.KIND_LFS
    # Without read only the batch is not read at all.
    assert _goes(LISTED, "POST", GIT, LFS)


@pytest.mark.parametrize("body, content_type, encoding", [
    (b'{"operation": "download", "operation": "upload"}', LFS_TYPE, ""),
    (b'{"operation": "download", "Operation": "upload"}', LFS_TYPE, ""),
    (b'{"Operation": "download"}', LFS_TYPE, ""),
    (b'[{"operation": "download"}]', LFS_TYPE, ""),
    (b'{"operation": "verify"}', LFS_TYPE, ""),
    (b'{"operation": "download"}', "text/plain", ""),
    (b'{"operation": "download"}', LFS_TYPE, "gzip"),
    (b'{"operation": "download"', LFS_TYPE, ""),
    (b'\xff\xfe', LFS_TYPE, ""),
])
def test_an_lfs_batch_that_cannot_be_read_as_the_server_reads_it_is_refused(body, content_type, encoding):
    verdict = _judge(READ_ONLY, "POST", GIT, LFS, content_type=content_type, content_encoding=encoding, body=body)
    assert verdict.refusal == gr.UNREADABLE


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method, host, target", [
    ("GET", API, "/repos/acme/app"),
    ("GET", API, "/repos/Acme/APP/pulls?state=open&per_page=30"),
    ("POST", API, "/repos/acme/app/pulls"),
    ("GET", API, "/repos/acme/app/actions/runs?per_page=20"),
    ("GET", API, "/repos/acme/app/contents/docs/a%20b.md"),
    ("GET", API, "/repos/acme/app/releases/tags/v1.0"),
    ("POST", UPLOADS, "/repos/acme/app/releases/7/assets?name=big.bin"),
    ("DELETE", API, "/repos/acme/tools/git/refs/heads/x"),
    ("GET", API, "/user"),
    ("HEAD", API, "/"),
])
def test_a_rest_request_for_a_listed_repository_goes(method, host, target):
    assert _goes(LISTED, method, host, target)


@pytest.mark.parametrize("method, host, target, name", [
    ("GET", API, "/repos/other/repo", "other/repo"),
    ("GET", API, "/repos/acme/secret/contents/a", "acme/secret"),
    ("POST", UPLOADS, "/repos/other/repo/releases/7/assets?name=x", "other/repo"),
    ("PUT", API, "/repos/acme/app2/contents/x", "acme/app2"),
])
def test_a_rest_request_for_another_repository_is_refused_with_its_name(method, host, target, name):
    verdict = _judge(LISTED, method, host, target)
    assert verdict.refusal == gr.outside_sentence(LISTED, name) and verdict.kind == gr.KIND_API


@pytest.mark.parametrize("method, host, target", [
    ("GET", API, "/repositories/123/issues?page=2"),
    ("GET", API, "/user/repos"),
    ("POST", API, "/user/repos"),
    ("GET", API, "/orgs/acme/repos"),
    ("GET", API, "/gists"),
    ("GET", API, "/notifications"),
    ("GET", API, "/repos/acme"),
    ("POST", API, "/user"),
    ("GET", UPLOADS, "/"),
    ("GET", UPLOADS, "/user"),
    ("GET", "gist.github.com", "/abc.git/info/refs"),
    ("GET", "codeload.github.com", "/acme/app/tar.gz/main"),
    ("GET", "evil.example", "/repos/acme/app"),
])
def test_a_request_that_names_no_listed_repository_is_refused_under_a_list(method, host, target):
    assert _judge(LISTED, method, host, target).refusal.endswith("names none of them.")


@pytest.mark.parametrize("target", ["/repos/acme/app/forks", "/repos/acme/app/generate"])
def test_a_new_repository_is_outside_any_list(target):
    assert "A new repository is outside them." in _judge(LISTED, "POST", API, target).refusal
    assert _goes(LISTED, "GET", API, target)
    assert _goes(READ_ONLY, "GET", API, target)


@pytest.mark.parametrize("target", ["/repos/acme/app/fork%73", "/repos/acme/app/Forks", "/repos/acme/app/GENERATE/",
                                    "/repos/acme/app/%67enerate"])
def test_a_new_repository_is_read_as_a_server_that_decodes_and_folds_case_would(target):
    assert "A new repository is outside them." in _judge(LISTED, "POST", API, target).refusal
    assert _goes(LISTED, "GET", API, target)


@pytest.mark.parametrize("target", ["/repos/acme/app/security-advisories/GHSA-abcd-1234-efgh/forks",
                                    "/repos/acme/app/Security-Advisories/GHSA-abcd-1234-efgh/Forks/",
                                    "/repos/acme/app/security%2Dadvisories/GHSA-abcd-1234-efgh/forks"])
def test_an_advisorys_temporary_fork_is_a_new_repository_too(target):
    assert "A new repository is outside them." in _judge(LISTED, "POST", API, target).refusal
    assert _goes(LISTED, "GET", API, target)


def test_a_file_or_a_branch_named_like_a_new_repositorys_address_is_changed_as_any():
    """Only the addresses that make a repository are refused, by their
    place under the repository, never by a word alone."""
    for method, target in (("PUT", "/repos/acme/app/contents/forks"), ("PUT", "/repos/acme/app/contents/docs/generate"),
                           ("PATCH", "/repos/acme/app/git/refs/heads/forks"),
                           ("POST", "/repos/acme/app/security-advisories/GHSA-abcd-1234-efgh/cve"),
                           ("POST", "/repos/acme/app/security-advisories"),
                           ("POST", "/repos/acme/app/security-advisories/reports"),
                           ("POST", "/repos/acme/app/issues/7/forks"),
                           ("DELETE", "/repos/acme/app/branches/forks/protection")):
        assert _goes(LISTED, method, API, target), target


@pytest.mark.parametrize("method, target", [
    ("POST", "/repos/acme/app/issues/7/sub_issues"),
    ("DELETE", "/repos/acme/app/issues/7/sub_issue"),
    ("PATCH", "/repos/acme/app/issues/7/sub_issues/priority"),
    ("PUT", "/repos/Acme/App/issues/7/sub_issues/"),
    ("POST", "/repos/acme/app/Issues/7/Sub_Issues"),
    ("POST", "/repos/acme/app/issues/7/sub%5Fissues"),
    ("POST", "/repos/acme/app/issues/7/sub_issues?x=1"),
    ("POST", "/repos/acme/app/issues/7/dependencies/blocked_by"),
    ("DELETE", "/repos/acme/app/issues/7/dependencies/blocked_by/4321"),
    ("PUT", "/repos/acme/app/issues/7/Dependencies/blocking"),
    ("POST", "/repos/acme/app/issues/7/relates_to"),
    ("DELETE", "/repos/acme/app/issues/7/relates_to/4321"),
    ("POST", "/repos/acme/app/issues/7/Relates%5Fto/"),
])
def test_a_change_of_an_issues_links_to_other_issues_is_refused_under_a_list(method, target):
    """The address names the issue's repository and the request names the
    other issue by an id alone, in its body or as its last segment, so the
    address says too little: sub-issues, the issues that block it, its
    related issues."""
    for host in (API, UPLOADS):
        refusal = _judge(LISTED, method, host, target).refusal
        assert refusal == f"{gr._only(LISTED)} {gr.LINKS_REFUSED}"


def test_sub_issues_are_read_under_a_list_and_changed_with_nothing_chosen():
    for target in ("/repos/acme/app/issues/7/sub_issues", "/repos/acme/app/issues/7/parent",
                   "/repos/acme/app/issues/7/sub_issues?per_page=50",
                   "/repos/acme/app/issues/7/dependencies/blocked_by", "/repos/acme/app/issues/7/dependencies/blocking",
                   "/repos/acme/app/issues/7/relates_to"):
        assert _goes(LISTED, "GET", API, target) and _goes(BOTH, "GET", API, target)
    # The other changes of an issue go as before.
    for method, target in (("POST", "/repos/acme/app/issues"), ("PATCH", "/repos/acme/app/issues/7"),
                           ("POST", "/repos/acme/app/issues/7/comments"), ("POST", "/repos/acme/app/issues/7/labels"),
                           ("POST", "/repos/acme/app/pulls/7/sub_issues"), ("POST", "/repos/acme/app/pulls/7/dependencies"),
                           ("PUT", "/repos/acme/app/contents/relates_to"),
                           ("POST", "/repos/acme/app/issues/7/issue-field-values"),
                           ("PUT", "/repos/acme/app/issues/7/lock")):
        assert _goes(LISTED, method, API, target)
    # No list: nothing changes. Read only refuses the change as every other.
    wide = gr.reach_of([], False)
    for target in ("/repos/any/repo/issues/7/sub_issues", "/repos/any/repo/issues/7/dependencies/blocked_by",
                   "/repos/any/repo/issues/7/relates_to", "/repos/any/repo/security-advisories/GHSA-x/forks"):
        assert _judge(wide, "POST", API, target) == gr.RequestVerdict()
    assert _judge(READ_ONLY, "POST", API, "/repos/any/repo/issues/7/sub_issues").refusal \
        == gr.read_only_sentence(READ_ONLY, gr.REQUEST)


@pytest.mark.parametrize("target", [
    "/repos/acme/app/../../other/repo",
    "/repos/acme/app/%2e%2e/%2e%2e/other/repo",
    "/repos/acme%2fapp/x",
    "/repos/acme/app\\x",
    "/repos//acme/app",
    "/repos/acme/app/contents/a%00b",
    "repos/acme/app",
])
def test_a_path_that_could_lead_elsewhere_is_refused(target):
    for host in (API, GIT, UPLOADS):
        assert _judge(LISTED, "GET", host, target).refusal == gr.PATH_REFUSED
        assert _judge(READ_ONLY, "GET", host, target).refusal == gr.PATH_REFUSED


@pytest.mark.parametrize("reach, method, host, target", [
    (READ_ONLY, "POST", GIT, "/acme/app.git/git-receive-pack#/git-upload-pack"),
    (READ_ONLY, "POST", GIT, "/acme/app.git/info/lfs/objects/batch#/git-upload-pack"),
    (LISTED, "POST", API, "/repos/acme/app/forks#/issues"),
    (LISTED, "GET", API, "/search/issues?q=bug#+repo:acme/app"),
    (LISTED, "GET", API, "/repos/acme/app/issues#"),
    (LISTED, "GET", API, "/repos/ac\tme/app/issues"),
    (LISTED, "GET", API, "/repos/acme/app\r/issues"),
    (LISTED, "GET", API, "/repos/acme/app/issues?per_page=5\n"),
    (BOTH, "GET", UPLOADS, "/repos/acme/app/releases/1/assets?name=a#b"),
])
def test_a_target_a_url_parser_would_cut_or_change_is_refused(reach, method, host, target):
    """The text judged is the address that is read and sent: a number sign
    ends an address for a URL parser, and a tab, a carriage return and a
    line feed are dropped by one."""
    verdict = _judge(reach, method, host, target)
    assert verdict.refusal == gr.PATH_REFUSED
    assert verdict.kind == (gr.KIND_GIT if host == GIT else gr.KIND_API)
    # An escaped number sign is text, in a path and in a search.
    assert _goes(LISTED, "GET", API, "/repos/acme/app/contents/a%23b")
    assert _goes(LISTED, "GET", API, "/search/issues?q=%23123+repo:acme/app")


@pytest.mark.parametrize("target, refusal", [
    ("/acme/app.git/git-receive-pack?/git-upload-pack?x", gr.push_refused(READ_ONLY)),
    ("/acme/app.git/info/lfs/locks?/git-upload-pack?x", gr.read_only_sentence(READ_ONLY, gr.REQUEST)),
])
def test_the_path_ends_at_the_first_question_mark_under_read_only(target, refusal):
    assert _judge(READ_ONLY, "POST", GIT, target).refusal == refusal


def test_the_path_ends_at_the_first_question_mark_under_a_list():
    assert "A new repository is outside them." in _judge(LISTED, "POST", API, "/repos/acme/app/forks?/x?y").refusal
    assert _judge(LISTED, "GET", API, "/repos/other/repo?/repos/acme/app?x").refusal \
        == gr.outside_sentence(LISTED, "other/repo")


def test_read_only_refuses_every_rest_write_and_lets_every_read_go():
    for method in ("POST", "PUT", "PATCH", "DELETE", "post"):
        for host, target in ((API, "/repos/acme/app/issues"), (UPLOADS, "/repos/acme/app/releases/1/assets"),
                             (API, "/user/repos"), ("gist.github.com", "/x")):
            assert _judge(READ_ONLY, method, host, target).refusal == gr.read_only_sentence(READ_ONLY, gr.REQUEST)
    for target in ("/repos/any/repo/issues", "/user/repos", "/search/issues?q=x", "/repositories/5"):
        assert _goes(READ_ONLY, "GET", API, target)
    # Both: a read of the listed repository alone.
    assert _goes(BOTH, "GET", API, "/repos/acme/app/issues")
    assert _judge(BOTH, "POST", API, "/repos/acme/app/issues").refusal == gr.read_only_sentence(BOTH, gr.REQUEST)
    assert _judge(BOTH, "GET", API, "/repos/acme/tools").refusal == gr.outside_sentence(BOTH, "acme/tools")


def test_a_listed_repositorys_next_page_is_named_again_by_the_answers_link():
    verdict = _judge(LISTED, "GET", API, "/repos/Acme/App/issues?per_page=100")
    assert verdict.link == (r"https://api\.github\.com/repositories/\d+/", "https://api.github.com/repos/acme/app/")
    import re
    assert re.sub(verdict.link[0], verdict.link[1],
                  '<https://api.github.com/repositories/99/issues?per_page=100&page=2>; rel="next"') \
        == '<https://api.github.com/repos/acme/app/issues?per_page=100&page=2>; rel="next"'
    # No rewrite where there is no list, and none toward the uploads host.
    assert _judge(READ_ONLY, "GET", API, "/repos/acme/app/issues").link is None
    assert _judge(LISTED, "POST", UPLOADS, "/repos/acme/app/releases/1/assets").link is None


def test_a_rest_search_is_held_to_its_repo_qualifiers():
    assert _goes(LISTED, "GET", API, "/search/issues?q=bug+repo%3Aacme%2Fapp&per_page=5")
    assert _goes(LISTED, "GET", API, "/search/code?q=repo:acme/app+repo:ACME/tools+x")
    assert _judge(LISTED, "GET", API, "/search/issues?q=bug+repo%3Aother%2Frepo").refusal \
        == gr.outside_sentence(LISTED, "other/repo")
    assert "Add repo:owner/name to the search." in _judge(LISTED, "GET", API, "/search/issues?q=bug").refusal
    assert "It may not use org:" in _judge(LISTED, "GET", API, "/search/issues?q=org%3Aacme+bug").refusal
    assert "It may not use org:" in _judge(LISTED, "GET", API,
                                           "/search/issues?q=repo:acme/app+OR+repo:other/repo").refusal
    for target in ("/search/issues", "/search/issues?q=repo:acme/app&q=repo:other/repo"):
        assert _judge(LISTED, "GET", API, target).refusal == f"{gr._only(LISTED)} {gr.SEARCH_ADDRESS}"
    assert _judge(LISTED, "GET", API, "/search").refusal.endswith("names none of them.")


@pytest.mark.parametrize("query", [
    "q=repo:acme/app+bug",
    "q=repo%3Aacme%2Fapp&sort=updated&order=desc&per_page=100&page=2",
    "page=3&q=bug+repo:acme/tools",
    "q=repo:acme/app+a%3Bb",
])
def test_a_search_with_the_keys_the_rule_knows_goes(query):
    assert _goes(LISTED, "GET", API, f"/search/issues?{query}")


@pytest.mark.parametrize("query", [
    "q=repo:acme/app&repository_id=42",
    "repository_id=42&q=bug",
    "q=repo:acme/app&sort=updated&sort=created",
    "q=repo:acme/app&page=1&page=2",
    "q=repo:acme/app;q=repo:other/repo",
    "q=repo:acme/app;q=bug",
    "q=repo:acme/app+bug;per_page=5",
    "q=repo:acme/app&x=1;sort=updated",
    "q=repo:acme/app&Q=repo:other/repo",
    "q=repo:acme/app&per_page",
    "q=repo:acme/app&",
    "q=repo:acme/app&unknown=1",
    "sort=updated",
    "per_page=5&page=2",
])
def test_a_search_with_another_key_a_key_twice_or_a_second_separator_is_refused(query):
    """A key the rule does not read could name a repository another way
    (a label search names it by a number), and a server could split the
    same text at a semicolon too."""
    assert _judge(LISTED, "GET", API, f"/search/labels?{query}").refusal == f"{gr._only(LISTED)} {gr.SEARCH_ADDRESS}"
    # Without a list a search is not judged at all.
    assert _goes(READ_ONLY, "GET", API, f"/search/labels?{query}")


# The sentences by their names, read at the call: the module loads on a tree without them.
FIRST, OWN_WORD, CONTROL = "SEARCH_FIRST", "SEARCH_OWN_WORD", "SEARCH_CONTROL"
OUTSIDE, WIDENS = "outside", "widens"


@pytest.mark.parametrize("asked, told", [
    # The one repo: stands where a reading could take it as plain words:
    # inside a phrase, after a quotation mark with no partner, after a blank
    # that is no plain space, inside parentheses, before a blank that is none.
    ('"crash on start repo:acme/app"', FIRST),
    ('5" screen repo:acme/app', FIRST),
    ('"crash on start repo:acme/app today"', FIRST),
    ('bug "unfinished phrase repo:acme/app', FIRST),
    ('bug\trepo:acme/app', FIRST),
    ('bug\nrepo:acme/app', FIRST),
    ('bug\u00a0repo:acme/app', FIRST),
    ('bug\u2003repo:acme/app', FIRST),
    ('bug\u3000repo:acme/app', FIRST),
    ('repo:acme/app\u00a0bug', FIRST),
    ('repo:acme/app\tbug', FIRST),
    ('(repo:acme/app) label:bug', FIRST),
    ('bug (repo:acme/app', FIRST),
    ('"a \\" repo:acme/app "b"', FIRST),
    ('a" "b repo:acme/app c" "d', FIRST),
    ('x "a b"c repo:acme/app', FIRST),
    ('x"a b" repo:acme/app', FIRST),
    ('bug repo:acme/app)', FIRST),
    ('-repo:other/repo "x repo:acme/app"', FIRST),
    # A repo: that a reading could take as a qualifier the count missed.
    ('bug "repo:acme/app', OWN_WORD),
    ('repo:acme/app,repo:other/repo', OWN_WORD),
    ('repo:acme/app bug\u200brepo:other/repo', OWN_WORD),
    ('repo:acme/app x=repo:other/repo', OWN_WORD),
    ('repo:acme/app my-repo:other/repo', OWN_WORD),
    ('repo:acme/app ghcr.io/other/repo:1.2', OWN_WORD),
    ('repo:acme/app \u00e9repo:other/repo', OWN_WORD),
    ('repo:acme/app repo: other/repo', OWN_WORD),
    ('repo: acme/app', OWN_WORD),
    ('repo:acme/app which repo:', OWN_WORD),
    ('repo:acme/app repo:"other/repo', OWN_WORD),
    # A control character that is no blank of pasted text.
    ('bug\x00 repo:acme/app', CONTROL),
    ('bug\x0brepo:acme/app', CONTROL),
    ('repo:acme/app bug\x1f', CONTROL),
    ('repo:acme/app \x7f', CONTROL),
])
def test_a_search_that_could_be_read_two_ways_is_told_what_to_change(asked, told):
    """The rule counts a ``repo:`` with a value at the start, after a blank
    or after a parenthesis. The address cannot be given the list's own
    qualifiers, so the search goes only when it is held to the list in
    every reading, and its refusal says what to write."""
    from urllib.parse import quote
    assert _judge(LISTED, "GET", API, f"/search/issues?q={quote(asked)}").refusal \
        == f"{gr._only(LISTED)} {getattr(gr, told)}"
    # Without a list a search is not judged at all.
    assert _goes(READ_ONLY, "GET", API, f"/search/issues?q={quote(asked)}")


@pytest.mark.parametrize("asked, told", [
    ('repo:acme/app "see repo:other/repo"', OUTSIDE),
    ('repo:acme/app bug\u00a0repo:other/repo', OUTSIDE),
    ('repo:acme/app (repo:other/repo)', OUTSIDE),
    ('repo:acme/app x,org:acme', WIDENS),
    ('repo:acme/app "x"user:bob', WIDENS),
    ('repo:acme/app a/owner:acme', WIDENS),
    ('repo:acme/app x-enterprise:acme', WIDENS),
])
def test_a_repository_or_a_scope_that_any_reading_could_take_is_judged(asked, told):
    """Every ``repo:`` a reading could take as a qualifier names a listed
    repository, wherever it stands, and a word that scopes a search another
    way is refused wherever it could start a word."""
    from urllib.parse import quote
    sentence = {OUTSIDE: gr.outside_sentence(LISTED, "other/repo"),
                WIDENS: gr._WIDENING_SENTENCE.format(only=gr._only(LISTED))}[told]
    assert _judge(LISTED, "GET", API, f"/search/issues?q={quote(asked)}").refusal == sentence


@pytest.mark.parametrize("asked", [
    'bug repo:acme/app',
    '"crash on start" repo:acme/app',
    'repo:"acme/app" "a b" is:open',
    'label:"good first issue" repo:acme/app',
    '-repo:other/repo repo:acme/tools crash',
    'REPO:Acme/App \u03c3\u03c6\u03ac\u03bb\u03bc\u03b1',
    'path:src\\main repo:acme/app',
    # A word that only holds repo: is one word in every reading.
    'monorepo: tooling repo:acme/app',
    '"the monorepo: notes" repo:acme/app',
    'monorepo:build repo:acme/app',
    'image_repo:latest repo:acme/app norepo:other/repo',
    'repo:acme/app superuser:admin Xorg:display',
    # One quotation mark alone, after the qualifier.
    'repo:acme/app 5" screen',
    'repo:acme/app "say \\"hi\\""',
    'repo:acme/app "unfinished phrase',
    # A blank of pasted text, where it touches no qualifier.
    'repo:acme/app fix\u3000bug',
    'repo:acme/app first line\nsecond\tline',
    'repo:acme/app a\u00a0b',
    # A second repo: where a reading may take it as plain words, and it is listed.
    'repo:acme/app "see repo:acme/tools"',
    'repo:acme/tools bug\u00a0repo:acme/app',
])
def test_a_search_held_to_the_list_in_every_reading_goes(asked):
    from urllib.parse import quote
    assert _goes(LISTED, "GET", API, f"/search/issues?q={quote(asked)}")


# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

def _graphql(document: str, **more) -> bytes:
    return json.dumps({"query": document, **more}).encode()


def test_graphql_under_a_list_is_not_held_to_the_list():
    assert _judge(LISTED, "POST", API, "/graphql") == gr.RequestVerdict()


@pytest.mark.parametrize("host", [UPLOADS, "gist.github.com", "codeload.github.com"])
def test_graphqls_address_is_the_api_hosts_alone(host):
    assert _judge(LISTED, "POST", host, "/graphql").refusal.endswith("names none of them.")
    assert _judge(READ_ONLY, "POST", host, "/graphql").refusal == gr.read_only_sentence(READ_ONLY, gr.REQUEST)
    assert not _judge(READ_ONLY, "POST", host, "/graphql").wants_body


@pytest.mark.parametrize("method, target", [
    ("GET", "/graphql"), ("GET", "/graphql?query=mutation{x}"), ("POST", "/graphql?query=x"),
    ("PUT", "/graphql"), ("DELETE", "/graphql"),
])
def test_only_a_plain_post_reaches_graphql_under_any_reach(method, target):
    for reach in (LISTED, READ_ONLY, BOTH):
        assert _judge(reach, method, API, target).refusal == gr.METHOD_REFUSED


def test_graphql_under_read_only_is_read_and_a_query_goes_rebuilt():
    first = _judge(READ_ONLY, "POST", API, "/graphql")
    assert first.wants_body == gr.REQUEST_BODY_MAX and not first.refusal
    body = _graphql("query($o:String!){repository(owner:$o,name:\"app\"){id}}", variables={"o": "acme"},
                    operationName=None)
    verdict = _judge(READ_ONLY, "POST", API, "/graphql", content_type=JSON, body=body)
    assert not verdict.refusal and json.loads(verdict.body) == json.loads(body)


@pytest.mark.parametrize("document", [
    "mutation{createIssue(input:{repositoryId:\"R_1\",title:\"x\"}){issue{id}}}",
    "mutation M($i:CreatePullRequestInput!){createPullRequest(input:$i){pullRequest{id}}}",
    "query Q{viewer{login}} mutation M{x}",
    "subscription{x}",
    "#c\nmutation{x}",
    "{search(query:\"mutation testing\",type:ISSUE,first:1){issueCount}}",
])
def test_a_graphql_document_that_may_change_something_is_refused_under_read_only(document):
    verdict = _judge(READ_ONLY, "POST", API, "/graphql", content_type=JSON, body=_graphql(document))
    assert verdict.refusal == gr.read_only_sentence(READ_ONLY, "This GraphQL request")
    assert verdict.kind == gr.KIND_API


def test_a_field_that_only_resembles_the_word_is_not_a_change():
    document = "{viewer{login} repository(owner:\"a\",name:\"b\"){viewerSubscription clientMutationId}}"
    assert not _judge(READ_ONLY, "POST", API, "/graphql", content_type=JSON, body=_graphql(document)).refusal


@pytest.mark.parametrize("body, content_type, encoding", [
    (b'{"query": "{a}", "query": "mutation{b}"}', JSON, ""),
    (b'{"query": "{a}", "Query": "mutation{b}"}', JSON, ""),
    (b'{"query": "{a}", "extensions": {"persistedQuery": {}}}', JSON, ""),
    (b'[{"query": "{a}"}]', JSON, ""),
    (b'{"variables": {}}', JSON, ""),
    (b'{"query": ""}', JSON, ""),
    (b'{"query": 5}', JSON, ""),
    (b'{"query": "{a}"}', "application/x-www-form-urlencoded", ""),
    (b'{"query": "{a}"}', "application/graphql", ""),
    (b'{"query": "{a}"}', "", ""),
    (b'{"query": "{a}"}', JSON, "gzip"),
    (b'{"query": "{a}"', JSON, ""),
    (b'query=mutation{x}', JSON, ""),
])
def test_a_graphql_body_that_cannot_be_read_as_the_server_reads_it_is_refused(body, content_type, encoding):
    verdict = _judge(READ_ONLY, "POST", API, "/graphql", content_type=content_type, content_encoding=encoding,
                     body=body)
    assert verdict.refusal == gr.UNREADABLE


@pytest.mark.parametrize("content_type", [
    "application/json; charset=latin-1",
    "application/json; charset=utf-16",
    "application/json;charset=UTF-8;charset=iso-8859-1",
    'application/json; charset="utf-7"',
    "application/json; charset=",
    "application/json, application/json",
    "application/json, text/plain",
    "application/json; a=b, application/json",
    "text/plain, application/json; charset=utf-8",
])
def test_a_body_whose_type_names_another_charset_or_two_types_is_refused(content_type):
    """The rule reads a body as UTF-8 and as one type. What it did not read
    as the server will, it refuses."""
    body = _graphql("{viewer{login}}")
    assert _judge(READ_ONLY, "POST", API, "/graphql", content_type=content_type, body=body).refusal == gr.UNREADABLE
    download = json.dumps({"operation": "download", "objects": []}).encode()
    lfs = content_type.replace("application/json", LFS_TYPE)
    assert _judge(READ_ONLY, "POST", GIT, LFS, content_type=lfs, body=download).refusal == gr.UNREADABLE


@pytest.mark.parametrize("content_type", [
    "application/json", "application/json; charset=utf-8", "Application/JSON;Charset=UTF-8",
    'application/json; charset="utf-8"', "application/json; profile=x",
])
def test_a_body_of_one_type_read_as_utf_8_is_judged(content_type):
    body = _graphql("{viewer{login}}")
    assert not _judge(READ_ONLY, "POST", API, "/graphql", content_type=content_type, body=body).refusal
    refused = _judge(READ_ONLY, "POST", API, "/graphql", content_type=content_type, body=_graphql("mutation{x}"))
    assert refused.refusal == gr.read_only_sentence(READ_ONLY, "This GraphQL request")


def test_the_word_is_found_behind_a_json_escape():
    body = b'{"query": "mu\\u0074ation{x}"}'
    assert _judge(READ_ONLY, "POST", API, "/graphql", content_type=JSON, body=body).refusal \
        == gr.read_only_sentence(READ_ONLY, "This GraphQL request")


def test_a_persons_own_narrowing_is_worded_as_theirs_at_the_exit_too():
    assert _judge(MINE, "GET", API, "/repos/other/repo").refusal \
        == "You limited this agent to acme/app in your GitHub connection. other/repo is not one of them."
    assert _judge(MINE, "POST", API, "/repos/acme/app/issues").refusal.startswith(
        "You made this agent read only in your GitHub connection.")
