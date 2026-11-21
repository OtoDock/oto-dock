"""An agent's GitHub reach: the repositories a service binding, or a person
in their own GitHub connection, narrows the agent to and whether it may
write, and the one rule the places that hand GitHub out share. The MCP's tool calls (``core/credentials/mcp_gateway``),
git and gh (``/v1/hooks/cli-credential``) and an app step judge with these
functions; an installation binding of an admin's own app is narrowed at
GitHub as well (``read_only_permissions``). Each request that will carry
git's and gh's token through the credential exit is judged too
(``judge_request``, asked by the exit through the same route).

A reach names repositories as ``owner/name``, compared without case (GitHub
names are case-insensitive). An empty set is every repository the bound
account or installation reaches.

Stdlib, `auth.request_path` and gh's flag table (`auth.gh_flags`) only. The
sentences are the ones a person and an agent read; they say who narrowed
(``Reach.personal``), the verdicts never depend on it.
"""

from __future__ import annotations

import hashlib
import json
import re
import string
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from auth import gh_flags
from auth.request_path import has_traversal

#: The most repositories one binding may name.
MAX_REPOSITORIES = 100
#: A search that names no repository gets the reach's ``repo:`` qualifiers
#: added while the reach has at most this many; past it, the search must
#: name one (GitHub caps a query's length).
MAX_ADDED_QUALIFIERS = 10

#: A push under read-only goes to this first path segment instead of the
#: owner (git's env-config ``url.<base>/<marker>/.pushInsteadOf``), so the
#: credential request names it and the broker refuses it with a sentence.
PUSH_MARKER = "otodock-read-only"

_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_API_HOST = "api.github.com"
_OWNER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38})$")
_NAME_RE = re.compile(r"^[a-z0-9._-]{1,100}$")

# The tools that name no repository and read only who the account is.
_IDENTITY_TOOLS = frozenset({"get_me", "get_teams", "get_team_members"})
# The tools a subset refuses whatever they name: a new repository is outside
# any reach, and a sub-issue is named by a global id that can be any
# repository's issue.
_REFUSED_TOOLS = frozenset({"create_repository", "fork_repository", "sub_issue_write"})
# The same for a review thread: the tool resolves one by its global id and
# reads no repository for it, whatever the call names beside it.
_THREAD_TOOL = "pull_request_review_write"
_THREAD_METHODS = frozenset({"resolve_thread", "unresolve_thread"})
_THREAD_ID = "threadid"
# What an engine writes into a field it does not use.
_NOT_SET = (None, "")
# The searches: their ``query`` is held to its ``repo:`` qualifiers, or
# given the reach's. A ``query`` on any other tool earns it nothing.
_SEARCH_TOOLS = frozenset({
    "search_code", "search_issues", "search_pull_requests", "search_repositories", "search_commits",
})
# Argument pairs that name a repository, wherever they sit in a call.
_PAIRS = (("owner", "repo"), ("parent_owner", "parent_repo"), ("item_owner", "item_repo"),
          ("owner", "repository_name"))
# Arguments that may hold a whole ``owner/name``.
_FULL_NAME_KEYS = frozenset({"repo", "repository", "full_name"})
_QUERY_KEYS = ("query", "q")
# The arguments that are text a person reads (a body, a file's content, a
# search), never part of an address the server builds: a dot segment or a
# percent sign in any other string argument is refused under a list.
_FREE_TEXT_KEYS = frozenset({
    "body", "title", "content", "message", "commit_message", "commit_title", "description",
    "query", "q", "value", "labels", "field_name", "field_option_name",
})
# An argument whose key ends so may hold a GitHub address.
_URL_KEY_SUFFIX = "url"
_GIT_SUFFIX = ".git"
_MAX_DEPTH = 6

_REPO_QUALIFIER = re.compile(r"""(?i)(?:^|(?<=[\s(]))(-?)repo:("?)([^\s")]+)\2""")
_WIDENING_QUALIFIER = re.compile(r"(?i)(?:^|(?<=[\s(]))-?(?:org|user|owner|enterprise):")
_OR_OPERATOR = re.compile(r"(?:^|(?<=[\s(]))OR(?=[\s)]|$)")
# The blanks after NOT are read once, possessively, and the parentheses with
# them: two adjacent blank runs would make the search quadratic in a long
# query, and the query is the agent's own argument.
_NOT_REPO = re.compile(r"(?i)(?:^|(?<=[\s(]))NOT\s++[(\s]*-?repo:")
# GitHub refuses a search query over 256 characters; a longer one is judged
# widening without a read, so a crafted query cannot make the judge work
# in proportion to its length.
_QUERY_MAX = 1024

_METHOD_CALL = "tools/call"
_METHOD_READ = "resources/read"
_METHOD_PROMPT = "prompts/get"
# The methods that name nothing to judge (a client's answer to the server
# carries no method at all); every other method is refused under a reach
# (``completion/complete`` lists another repository's files).
_PLAIN_METHODS = frozenset({
    "initialize", "ping", "tools/list", "resources/list", "resources/templates/list",
    "prompts/list", "logging/setLevel",
})
_PLAIN_PREFIXES = ("notifications/", "tasks/")
_RESOURCE_SCHEME = "repo"
# What a judged message is forwarded with: its envelope, and in the
# ``params`` of a judged method only these (a key the judge did not read
# never reaches the server).
_RESULT_KEY = "result"
_ERROR_KEY = "error"
_ENVELOPE_KEYS = ("jsonrpc", "id", "method", "params")
_ANSWER_KEYS = ("jsonrpc", "id", _RESULT_KEY, _ERROR_KEY)
_JUDGED_PARAMS = ("name", "arguments", "uri", "_meta", "task")
# The keys the judge reads: a key that is one of them in another case is
# refused (a server that matches keys without case would read it).
_KNOWN_KEYS = frozenset(
    {*_ENVELOPE_KEYS, *_ANSWER_KEYS, *_JUDGED_PARAMS, *(k for pair in _PAIRS for k in pair),
     *_FULL_NAME_KEYS, *_QUERY_KEYS})
_FIRST_PRINTABLE = 0x20
_LAST_PRINTABLE = 0x7E


def normalize(full_name: str) -> str:
    """``owner/name`` in lower case, a trailing ``.git`` dropped; ``""`` when
    it is not a repository's name."""
    text = (full_name or "").strip().strip("/").lower()
    if text.endswith(_GIT_SUFFIX):
        text = text[: -len(_GIT_SUFFIX)]
    owner, sep, name = text.partition("/")
    if not sep or has_traversal(text) or not _OWNER_RE.match(owner) or not _NAME_RE.match(name):
        return ""
    return f"{owner}/{name}"


@dataclass(frozen=True)
class Reach:
    """What a binding lets an agent do on GitHub. ``personal``: the person
    narrowed their own connection (a user-scope session), so the sentences
    say "You"; a service binding's reach is a manager's."""

    repositories: frozenset[str] = field(default_factory=frozenset)
    read_only: bool = False
    personal: bool = False

    @property
    def narrows(self) -> bool:
        return bool(self.repositories) or self.read_only

    def allows(self, full_name: str) -> bool:
        if not self.repositories:
            return True
        return normalize(full_name) in self.repositories

    def _judged(self) -> dict[str, Any]:
        return {"repositories": sorted(self.repositories), "read_only": self.read_only}

    def to_dict(self) -> dict[str, Any]:
        """The shape a credential and a session descriptor carry; a
        service reach's has no ``personal`` key."""
        return {**self._judged(), **({"personal": True} if self.personal else {})}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Reach | None:
        if not isinstance(data, Mapping):
            return None
        names = data.get("repositories")
        return reach_of(names if isinstance(names, list) else [], bool(data.get("read_only")),
                        personal=data.get("personal") is True)

    def digest(self) -> str:
        """A short stable name of what this reach allows (an installation's
        narrowed token file is named by it; who narrowed is no part of it)."""
        blob = json.dumps(self._judged(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


def reach_of(repositories: Iterable[str], read_only: bool, *, personal: bool = False) -> Reach:
    """A reach from stored or typed names (an unreadable name is dropped)."""
    names = frozenset(n for n in (normalize(str(r)) for r in repositories or ()) if n)
    return Reach(repositories=names, read_only=bool(read_only), personal=personal)


def parse_stored(text: str | None) -> tuple[str, ...]:
    """A binding's stored ``repositories`` column (a JSON list), in order."""
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return ()
    return tuple(str(v) for v in value if isinstance(v, str)) if isinstance(value, list) else ()


def dump_stored(names: Iterable[str]) -> str:
    return json.dumps(list(names))


# ---------------------------------------------------------------------------
# The sentences
# ---------------------------------------------------------------------------

_SHOWN_NAMES = 5


def _names(reach: Reach) -> str:
    names = sorted(reach.repositories)
    if len(names) <= _SHOWN_NAMES:
        return ", ".join(names)
    return ", ".join(names[:_SHOWN_NAMES]) + f" and {len(names) - _SHOWN_NAMES} more"


def _listed(reach: Reach) -> str:
    """The names as a sentence lists them: "a, b and c", "a, …, e and 2 more"."""
    names = sorted(reach.repositories)
    if len(names) > _SHOWN_NAMES:
        return ", ".join(names[:_SHOWN_NAMES]) + f" and {len(names) - _SHOWN_NAMES} more"
    return "".join(names) if len(names) <= 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


def _only(reach: Reach) -> str:
    if reach.personal:
        return f"You limited this agent to {_listed(reach)} in your GitHub connection."
    return f"This agent may reach only these GitHub repositories: {_names(reach)}."


def _reads_only(reach: Reach) -> str:
    if reach.personal:
        return "You made this agent read only in your GitHub connection."
    return "This agent may only read on GitHub."


def outside_sentence(reach: Reach, name: str) -> str:
    return f"{_only(reach)} {name} is not one of them."


def read_only_sentence(reach: Reach, what: str, outcome: str = "it was refused") -> str:
    return f"{_reads_only(reach)} {what} changes something, so {outcome}."


def push_refused(reach: Reach) -> str:
    return f"{_reads_only(reach)} git push was refused."


def helper_too_old(reach: Reach) -> str:
    """A request with no repository path under a list (a helper older than
    the reach)."""
    who = ("You limited this agent to some repositories in your GitHub connection"
           if reach.personal else "This agent may reach only some GitHub repositories")
    return (f"{who}, and this session's credential helper does not say which one a command "
            "needs. Start a new session.")


_EVERY_CHANGE = "Every change (a commit, a push, an issue, a comment, a merge) is refused."


def instructions_line(reach: Reach) -> str:
    """What a session of a narrowed agent is told once, beside the GitHub
    tool's own context (a user-scope session reads who narrowed it)."""
    parts = ["## Your GitHub reach", ""]
    if reach.repositories:
        only = (f"Your user limited this agent to {_listed(reach)} in their GitHub connection."
                if reach.personal else _only(reach))
        parts.append(f"{only} Any other repository is refused, in the GitHub tool and in git and gh.")
    if reach.read_only:
        reads = ("Your user made this agent read only in their GitHub connection."
                 if reach.personal else _reads_only(reach))
        parts.append(f"{reads} {_EVERY_CHANGE}")
    return "\n".join(parts)


_WIDENING_SENTENCE = (
    "{only} A search names them with repo:owner/name. It may not use org:, user:, owner:, "
    "OR, or NOT before repo:, and gh may not use --owner, --org or --user."
)
UNREADABLE = "This request to GitHub could not be read, so it was refused."
#: What a search at the exit is told to change (``_judge_search``), after
#: the sentence that names the list.
SEARCH_ADDRESS = "A search's address holds q, and may hold sort, order, per_page and page, each once and with a value."
SEARCH_CONTROL = "Take the control characters out of the search."
SEARCH_OWN_WORD = "Write each repo:owner/name as a word of its own, with a plain space before it."
SEARCH_FIRST = "Write repo:owner/name as a word of its own before any quotation mark, with a plain space after it."
LINKS_REFUSED = "A change of an issue's sub-issues, dependencies or related issues is not allowed for it."
METHOD_REFUSED = "This agent's GitHub reach does not allow this request."
PATH_REFUSED = "A path in this request could lead outside the repository, so it was refused."


# ---------------------------------------------------------------------------
# Names in a request
# ---------------------------------------------------------------------------

def name_of_url(url: str) -> str:
    """The repository a GitHub web, API or git address names, ``""`` for
    any other address."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    segments = [unquote(s) for s in (parts.path or "").split("/") if s]
    if host in _GITHUB_HOSTS and len(segments) >= 2:
        return normalize(f"{segments[0]}/{segments[1]}")
    if host == _API_HOST and len(segments) >= 3 and segments[0] == "repos":
        return normalize(f"{segments[1]}/{segments[2]}")
    return ""


def name_of_remote(remote: str) -> str:
    """A git remote's repository on GitHub (https, ``ssh://`` or the scp
    form ``git@github.com:owner/name.git``), ``""`` for any other host."""
    text = (remote or "").strip()
    m = re.match(r"^[\w.-]+@([\w.-]+):(.+)$", text)
    if m and not urlsplit(text).netloc:
        return normalize(m.group(2)) if m.group(1).lower() in _GITHUB_HOSTS else ""
    return name_of_url(text)


def _raw_pair(owner: Any, repo: Any) -> str:
    """``owner/repo`` as named, lower case (kept even when it is no valid
    name: such a name is in no reach, so the call is refused)."""
    return f"{str(owner).strip().lower()}/{str(repo).strip().lower()}"


def named_in_arguments(args: Any, depth: int = 0) -> list[str]:
    """Every repository a tool's arguments name: the owner and repository
    pairs at any level, a whole ``owner/name``, a GitHub address in an
    argument whose key ends in ``url``."""
    out: list[str] = []
    if depth > _MAX_DEPTH:
        return out
    if isinstance(args, list):
        for item in args:
            out += named_in_arguments(item, depth + 1)
        return out
    if not isinstance(args, Mapping):
        return out
    for owner_key, repo_key in _PAIRS:
        owner, repo = args.get(owner_key), args.get(repo_key)
        if isinstance(owner, str) and owner and isinstance(repo, str) and repo:
            out.append(normalize(f"{owner}/{repo}") or _raw_pair(owner, repo))
    for key, value in args.items():
        if isinstance(value, (Mapping, list)):
            out += named_in_arguments(value, depth + 1)
            continue
        if not isinstance(value, str) or not value:
            continue
        lowered = str(key).lower()
        if lowered in _FULL_NAME_KEYS and "/" in value and not args.get("owner"):
            out.append(normalize(value) or value.strip().lower())
        elif lowered.endswith(_URL_KEY_SUFFIX):
            name = name_of_url(value)
            if name:
                out.append(name)
    return out


def steps_out(args: Any, key: str = "", depth: int = 0) -> bool:
    """Whether a string argument the server may build an address from
    (anything but text a person reads) carries a dot segment, a percent
    sign or a backslash: ``../`` in a ``sha`` or a ``path`` walks out of the
    repository the call names. Nesting past the judge's depth counts too."""
    if depth > _MAX_DEPTH:
        return True
    if isinstance(args, Mapping):
        return any(steps_out(v, str(k).lower(), depth + 1) for k, v in args.items())
    if isinstance(args, list):
        return any(steps_out(v, key, depth + 1) for v in args)
    if isinstance(args, str) and key not in _FREE_TEXT_KEYS:
        return has_traversal(args) or "%" in args
    return False


def named_in_query(query: str) -> list[str]:
    """The repositories a search's positive ``repo:`` qualifiers name (none
    for a query past ``_QUERY_MAX``, which ``widens`` refuses)."""
    if len(query or "") > _QUERY_MAX:
        return []
    return [normalize(m.group(3)) or m.group(3).lower()
            for m in _REPO_QUALIFIER.finditer(query or "") if not m.group(1)]


def widens(query: str) -> bool:
    """A search that can reach past its ``repo:`` qualifiers: an ``org:``,
    ``user:``, ``owner:`` or ``enterprise:`` qualifier, the ``OR``
    operator, or ``NOT`` before a ``repo:`` (every repository but that
    one, where GitHub reads ``NOT`` so)."""
    text = query or ""
    if len(text) > _QUERY_MAX:
        return True
    return bool(_WIDENING_QUALIFIER.search(text) or _OR_OPERATOR.search(text) or _NOT_REPO.search(text))


# ---------------------------------------------------------------------------
# The MCP's tool calls
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    """A judged request: the refusal a client reads, else the body to
    forward (``None`` = the body as it came)."""

    refusal: str = ""
    body: bytes | None = None


def _names_a_thread(args: dict) -> bool:
    """Whether a review call acts on a thread: by its method, or by a
    thread's id among its arguments under any spelling of the key."""
    method = args.get("method")
    if isinstance(method, str) and method.strip().lower() in _THREAD_METHODS:
        return True
    return any(str(k).replace("_", "").lower() == _THREAD_ID and v not in _NOT_SET for k, v in args.items())


def _judge_call(
    reach: Reach, name: str, args: dict, writes: Callable[[str], bool | None],
) -> tuple[str, dict | None]:
    if reach.read_only and writes(name) is True:
        return read_only_sentence(reach, f"The tool {name}"), None
    if not reach.repositories or name in _IDENTITY_TOOLS:
        return "", None
    if name in _REFUSED_TOOLS:
        return f"{_only(reach)} The tool {name} is not allowed for it.", None
    if name == _THREAD_TOOL and _names_a_thread(args):
        return f"{_only(reach)} The tool {name} may not resolve a review thread for it.", None
    if steps_out(args):
        return PATH_REFUSED, None
    named = named_in_arguments(args)
    query_key = next((k for k in _QUERY_KEYS if isinstance(args.get(k), str)), "") if name in _SEARCH_TOOLS else ""
    query = str(args.get(query_key) or "") if query_key else ""
    if query_key:
        if widens(query):
            return _WIDENING_SENTENCE.format(only=_only(reach)), None
        named += named_in_query(query)
    for n in named:
        if n not in reach.repositories:
            return outside_sentence(reach, n), None
    if named:
        return "", None
    if query_key:
        if len(reach.repositories) > MAX_ADDED_QUALIFIERS:
            return f"{_only(reach)} Add repo:owner/name to the search.", None
        added = " ".join(f"repo:{n}" for n in sorted(reach.repositories))
        return "", {**args, query_key: f"{query} {added}".strip()}
    return f"{_only(reach)} The tool {name} names none of them.", None


def _judge_message(
    reach: Reach, msg: Any, writes: Callable[[str], bool | None],
) -> tuple[str, Any]:
    """One JSON-RPC message: its refusal, or the message to forward, rebuilt
    from the keys the judge read."""
    if not isinstance(msg, dict):
        return UNREADABLE, msg
    method = msg.get("method")
    if method is None:
        # A client's answer to the server: an id with a result or an error.
        if _RESULT_KEY not in msg and _ERROR_KEY not in msg:
            return UNREADABLE, msg
        return "", {k: msg[k] for k in _ANSWER_KEYS if k in msg}
    envelope = {k: msg[k] for k in _ENVELOPE_KEYS if k in msg}
    raw_params = msg.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    if not isinstance(method, str):
        return UNREADABLE, msg
    if method in _PLAIN_METHODS or method.startswith(_PLAIN_PREFIXES):
        return "", envelope
    if raw_params is not None and not isinstance(raw_params, dict):
        return UNREADABLE, msg
    judged = {k: params[k] for k in _JUDGED_PARAMS if k in params}
    if raw_params is not None:
        envelope["params"] = judged
    if method == _METHOD_CALL:
        raw_args = params.get("arguments")
        args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
        refusal, new_args = _judge_call(reach, str(params.get("name") or ""), args, writes)
        if refusal or new_args is None:
            return refusal, envelope
        return "", {**envelope, "params": {**judged, "arguments": new_args}}
    if method == _METHOD_READ:
        if not reach.repositories:
            return "", envelope
        uri = str(params.get("uri") or "")
        if has_traversal(uri) or "%" in uri:
            return PATH_REFUSED, envelope
        parts = urlsplit(uri) if uri else None
        name = ""
        if parts is not None and parts.scheme == _RESOURCE_SCHEME:
            segments = [s for s in (parts.netloc + parts.path).split("/") if s]
            name = normalize("/".join(segments[:2])) if len(segments) >= 2 else ""
        if not name:
            return f"{_only(reach)} This resource names none of them.", envelope
        if name not in reach.repositories:
            return outside_sentence(reach, name), envelope
        return "", envelope
    if method == _METHOD_PROMPT:
        if reach.repositories:
            if steps_out(params.get("arguments")):
                return PATH_REFUSED, envelope
            for n in named_in_arguments(params.get("arguments")):
                if n not in reach.repositories:
                    return outside_sentence(reach, n), envelope
        return "", envelope
    return METHOD_REFUSED, envelope


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object the judge reads as any server would: no key twice, not
    even in another case; no key that is one the judge reads in another
    case; no control or non-ASCII character in a key (a server that matches
    keys loosely, or cuts one at a NUL, would read another one than the
    judge did)."""
    seen: set[str] = set()
    for key, _value in pairs:
        if any(not _FIRST_PRINTABLE <= ord(c) <= _LAST_PRINTABLE for c in key):
            raise ValueError("a key holds a control or non-ASCII character")
        folded = key.lower()
        if folded in seen:
            raise ValueError("a key is given twice")
        if folded in _KNOWN_KEYS and key != folded:
            raise ValueError("a key the judge reads is given in another case")
        seen.add(folded)
    return dict(pairs)


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def judge_mcp(reach: Reach, body: bytes, *, writes: Callable[[str], bool | None]) -> Verdict:
    """Judge one POST body of the GitHub MCP (an object or a batch) under
    ``reach``. ``writes(tool)`` says whether the server's own annotations
    mark the tool as a write (``None`` = unknown: GitHub's read-only mode,
    which the gateway turns on with the reach, then decides). A batch with
    one refused message is refused whole. A search naming no repository
    gets the reach's ``repo:`` qualifiers."""
    if not reach.narrows or not body:
        return Verdict()
    try:
        doc = json.loads(body.decode("utf-8"), object_pairs_hook=_strict_object,
                         parse_constant=_no_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return Verdict(refusal=UNREADABLE)
    if not isinstance(doc, (dict, list)) or (isinstance(doc, list) and not doc):
        return Verdict(refusal=UNREADABLE)
    messages = doc if isinstance(doc, list) else [doc]
    out: list[Any] = []
    for msg in messages:
        refusal, new = _judge_message(reach, msg, writes)
        if refusal:
            return Verdict(refusal=refusal)
        out.append(new)
    # What goes on is what was judged, never the client's own bytes.
    return Verdict(body=json.dumps(out if isinstance(doc, list) else out[0]).encode())


# ---------------------------------------------------------------------------
# git and gh
# ---------------------------------------------------------------------------

def judge_git(reach: Reach, path: str) -> str:
    """git's credential request for ``https://github.com/<path>``: the
    refusal, ``""`` to answer. A push under read-only asks for the marker
    path; under a repository list a request with no path (a helper older
    than the reach) is refused."""
    if not reach.narrows:
        return ""
    text = (path or "").strip()
    segments = [s for s in text.split("/") if s]
    if segments and segments[0].lower() == PUSH_MARKER:
        return push_refused(reach)
    if has_traversal(text) or "%" in text:
        return UNREADABLE
    if not reach.repositories:
        return ""
    if len(segments) < 2:
        return helper_too_old(reach)
    name = normalize(f"{segments[0]}/{segments[1]}") or f"{segments[0]}/{segments[1]}".lower()
    if name not in reach.repositories:
        return outside_sentence(reach, name)
    return ""


# The gh commands that only read, by group (``*`` = every verb of it).
_GH_READS: dict[str, frozenset[str]] = {
    "repo": frozenset({"view", "list", "clone", "read-file", "read-dir"}),
    "pr": frozenset({"view", "list", "diff", "checks", "status", "checkout"}),
    "issue": frozenset({"view", "list", "status"}),
    "release": frozenset({"view", "list", "download"}),
    "run": frozenset({"view", "list", "watch", "download"}),
    "workflow": frozenset({"view", "list"}),
    "label": frozenset({"list"}),
    "gist": frozenset({"view", "list"}),
    "cache": frozenset({"list"}),
    "ruleset": frozenset({"view", "list", "check"}),
    "auth": frozenset({"status"}),
    "search": frozenset({"*"}),
    "status": frozenset({"*"}),
    "browse": frozenset({"*"}),
    "api": frozenset({"*"}),
}
# The commands that take no verb: their words are their arguments.
_GH_NO_VERB = frozenset({"api", "status", "browse"})
# The commands that work on a repository even when none is named (gh then
# takes GH_REPO, else the working directory's remote).
_GH_IN_A_REPOSITORY = frozenset({
    "pr", "issue", "release", "run", "workflow", "label", "cache", "ruleset", "secret",
    "variable", "browse", "repo",
})
_GH_REPO_FLAGS = frozenset({"-R", "--repo"})
_GH_API = "api"
_GH_REPO = "repo"
_GH_AUTH = "auth"
_GH_SEARCH = "search"
_GH_GET = "GET"
_GH_POST = "POST"
_GH_WRITE_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})
_GH_METHOD_FLAGS = frozenset({"-X", "--method"})
_GH_HOST_FLAGS = frozenset({"--hostname"})
_GH_SHOW_TOKEN = frozenset({"-t", "--show-token"})
# ``gh auth`` verbs that print the token or hand it to git.
_GH_TOKEN_VERBS = frozenset({"token", "git-credential"})
_GH_USER_ENDPOINT = "user"
_GH_PLACEHOLDERS = ("{owner}", "{repo}")
# ``gh repo`` verbs whose first argument is the repository they work on,
# and the ones that make a new repository or list an account's.
_GH_REPO_TARGET = frozenset({"view", "clone", "edit", "delete", "archive", "unarchive", "sync", "set-default"})
_GH_REPO_NOT_ALLOWED = frozenset({"create", "fork", "list"})
_GH_REPO_SOURCE = "--source"
# ``gh pr list`` and ``gh issue list`` take a search, held to its ``repo:``
# qualifiers as a search is. gh adds the repository it works in to that
# search, so the qualifiers' repositories are judged beside it.
_GH_QUERY_FLAGS = frozenset({"--search", "-S"})
_GH_QUERY_GROUPS = frozenset({"pr", "issue"})
# The groups that take an issue or a pull request by its address and then
# work in the address's repository. In every other group an address among
# the words is a name, a tag or a file, and gh works where it would without.
_GH_BY_ADDRESS = frozenset({"pr", "issue"})
# A repository a command names beside the one it works in: by the place of
# its argument after the verb (where an issue moves to, where labels are
# copied from) or by a flag (where a linked branch is made). An issue a flag
# takes by its address names its repository beside the one worked in too.
_GH_SECOND_ARGUMENT = {("issue", "transfer"): 1, ("label", "clone"): 0}
_GH_SECOND_FLAGS = {"issue": frozenset({"--branch-repo"})}
_GH_ISSUE_FLAGS = {"issue": frozenset({"--duplicate-of"})}
# Flags that reach past a repository: an owner's search, an organization's
# or a user's secrets, variables and rulesets.
_GH_WIDENING_FLAGS: dict[str, frozenset[str]] = {
    "search": frozenset({"--owner"}),
    "secret": frozenset({"--org", "-o", "--user", "-u"}),
    "variable": frozenset({"--org", "-o"}),
    "ruleset": frozenset({"--org", "-o"}),
}
_GH_END_OF_FLAGS = "--"

GH_UNREADABLE = (
    "OtoDock does not know a flag of this gh command, so it did not run. "
    "Write the flag and its value as --name=value."
)
GH_SHOWS_TOKEN = "This gh command would print the token, so it did not run."


@dataclass
class _GhCommand:
    """A gh command as gh reads it: its words (the group, the verb, the
    arguments) and its flags with their values."""

    words: list[str] = field(default_factory=list)
    flags: list[tuple[str, str | None]] = field(default_factory=list)
    unreadable: bool = False

    def values(self, names: frozenset[str]) -> list[str]:
        return [v for f, v in self.flags if f in names and v is not None]

    def has(self, names: frozenset[str]) -> bool:
        return any(f in names for f, _v in self.flags)


def _gh_parse(argv: list[str]) -> _GhCommand:
    """gh's own reading of its arguments (``auth/gh_flags``): a flag that
    takes a value takes the next word or what follows ``=``, short flags
    bundle, ``--`` ends the flags (what follows is git's). A flag gh's help
    does not list, with a word after it, makes the command unreadable: the
    word could be its value or an argument."""
    cmd = _GhCommand()
    i = 0
    while i < len(argv):
        word = argv[i]
        if word == _GH_END_OF_FLAGS:
            break
        group = cmd.words[0] if cmd.words else ""
        verb = cmd.words[1] if len(cmd.words) > 1 and group not in _GH_NO_VERB else ""
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if word.startswith("--") and len(word) > 2:
            name, eq, value = word.partition("=")
            arity = gh_flags.takes_value(group, verb, name)
            if eq:
                cmd.flags.append((name, value))
            elif arity and nxt is not None:
                cmd.flags.append((name, nxt))
                i += 1
            else:
                cmd.unreadable |= arity is None and nxt is not None and not nxt.startswith("-")
                cmd.flags.append((name, None))
            i += 1
            continue
        if word.startswith("-") and len(word) > 1:
            j = 1
            while j < len(word):
                name, rest = "-" + word[j], word[j + 1:]
                arity = gh_flags.takes_value(group, verb, name)
                if arity:
                    if rest:
                        cmd.flags.append((name, rest[1:] if rest.startswith("=") else rest))
                    elif nxt is not None:
                        cmd.flags.append((name, nxt))
                        i += 1
                    else:
                        cmd.flags.append((name, None))
                    break
                cmd.unreadable |= arity is None and bool(
                    rest or (nxt is not None and not nxt.startswith("-")))
                cmd.flags.append((name, None))
                j += 1
            i += 1
            continue
        cmd.words.append(word)
        i += 1
    return cmd


def _gh_method(cmd: _GhCommand) -> str:
    given = cmd.values(_GH_METHOD_FLAGS)
    if given:
        return given[-1].upper()
    return _GH_POST if cmd.has(_GH_WRITE_FLAGS) else _GH_GET


def _gh_repo_flag(value: str) -> str:
    """A repository as gh takes it: ``owner/name``, ``host/owner/name`` or
    a URL (a name on another host is no GitHub repository: kept as typed,
    so no reach holds it)."""
    text = value.strip()
    by_url = name_of_url(text) if urlsplit(text).netloc else ""
    if by_url:
        return by_url
    parts = [p for p in text.split("/") if p]
    if len(parts) == 3 and parts[0].lower() in _GITHUB_HOSTS:
        parts = parts[1:]
    return normalize("/".join(parts)) or text.lower()


def _gh_owner_missing(reach: Reach, word: str) -> str:
    """The refusal for a repository named without its owner, ``""`` for
    ``owner/name`` or an address (which holds a slash too)."""
    if "/" not in word:
        return f"{_only(reach)} {word} names no owner. Write it as owner/name."
    return ""


def _gh_query_refusal(reach: Reach, query: str, named: list[str]) -> str:
    if widens(query):
        return _WIDENING_SENTENCE.format(only=_only(reach))
    named += named_in_query(query)
    return ""


def _gh_named(
    reach: Reach, cmd: _GhCommand, group: str, verb: str, gh_repo: str,
) -> tuple[str, list[str], list[str]]:
    """The repositories a gh command's flags, words and ``GH_REPO`` name:
    the ones gh works in, in place of the working directory's, and the ones
    it reaches beside it. A second repository without its owner is refused."""
    works_in = [_gh_repo_flag(r) for r in cmd.values(_GH_REPO_FLAGS)]
    if gh_repo.strip():
        works_in.append(_gh_repo_flag(gh_repo))
    beside: list[str] = []
    second = _GH_SECOND_ARGUMENT.get((group, verb))
    for place, word in enumerate(cmd.words[1:], start=0 if group in _GH_NO_VERB else -1):
        name = name_of_url(word)
        if place == second:
            refusal = _gh_owner_missing(reach, word)
            if refusal:
                return refusal, [], []
            beside.append(_gh_repo_flag(word))
        elif name and group in _GH_BY_ADDRESS:
            works_in.append(name)
        elif name and (group in _GH_IN_A_REPOSITORY or group == _GH_SEARCH):
            beside.append(name)
        elif name:
            works_in.append(name)
    for value in cmd.values(_GH_SECOND_FLAGS.get(group, frozenset())):
        refusal = _gh_owner_missing(reach, value)
        if refusal:
            return refusal, [], []
        beside.append(_gh_repo_flag(value))
    beside += [n for n in map(name_of_url, cmd.values(_GH_ISSUE_FLAGS.get(group, frozenset()))) if n]
    return "", works_in, beside


def judge_gh(
    reach: Reach, argv: list[str], remotes: list[str], *, gh_repo: str = "", gh_host: str = "",
) -> str:
    """A ``gh`` command (its arguments after ``gh``) under ``reach``: the
    refusal, ``""`` to run it. ``remotes`` are the working directory's git
    remote addresses and ``gh_repo``/``gh_host`` the command's ``GH_REPO``
    and ``GH_HOST``, which gh falls back on. Judged: the repository gh works
    in (what the command names in its place, else every GitHub remote) and
    every repository the command names beside it."""
    if not reach.narrows:
        return ""
    cmd = _gh_parse(list(argv or []))
    if cmd.unreadable:
        return GH_UNREADABLE
    group = cmd.words[0] if cmd.words else ""
    verb = cmd.words[1] if len(cmd.words) > 1 and group not in _GH_NO_VERB else ""
    args = cmd.words[1:] if group in _GH_NO_VERB else cmd.words[2:]
    endpoint = args[0] if group == _GH_API and args else ""
    path = endpoint.split("?", 1)[0].strip("/")
    if group == _GH_AUTH and (verb in _GH_TOKEN_VERBS or cmd.has(_GH_SHOW_TOKEN)):
        return GH_SHOWS_TOKEN
    if reach.read_only:
        reads = _GH_READS.get(group, frozenset())
        reading = "*" in reads or verb in reads
        if not reading or (group == _GH_API and _gh_method(cmd) != _GH_GET):
            return read_only_sentence(reach, "This gh command", "it did not run")
    identity = group == _GH_AUTH and verb == "status"
    if not reach.repositories or identity:
        return ""
    if group == _GH_API and path == _GH_USER_ENDPOINT:
        # Who the account is: the address is judged as the exit judges the
        # request it becomes.
        return _gh_api_refusal(reach, cmd, path)
    for host in [gh_host, *cmd.values(_GH_HOST_FLAGS)]:
        if host.strip() and host.strip().lower() not in _GITHUB_HOSTS:
            return f"{_only(reach)} This gh command is set to another host, {host.strip()}."
    if cmd.has(_GH_WIDENING_FLAGS.get(group, frozenset())):
        return _WIDENING_SENTENCE.format(only=_only(reach))
    refusal, works_in, beside = _gh_named(reach, cmd, group, verb, gh_repo)
    if refusal:
        return refusal
    if group in _GH_QUERY_GROUPS:
        for query in cmd.values(_GH_QUERY_FLAGS):
            refusal = _gh_query_refusal(reach, query, beside)
            if refusal:
                return refusal
    if group == _GH_REPO:
        if verb in _GH_REPO_NOT_ALLOWED:
            return f"{_only(reach)} The command gh repo {verb} is not allowed for it."
        if verb in _GH_REPO_TARGET and args:
            refusal = _gh_owner_missing(reach, args[0])
            if refusal:
                return refusal
            works_in.append(_gh_repo_flag(args[0]))
        works_in += [_gh_repo_flag(v) for v in cmd.values(frozenset({_GH_REPO_SOURCE}))]
    if group == _GH_SEARCH:
        refusal = _gh_query_refusal(reach, " ".join(args), works_in)
        if refusal:
            return refusal
    in_a_repository = group in _GH_IN_A_REPOSITORY
    tail: str | None = None
    if group == _GH_API:
        # What the command check refuses on its own, stricter than the
        # request's rule: any percent sign, and every address that is not
        # ``repos/<owner>/<name>/…`` (the root, a search, a whole address).
        if has_traversal(path) or "%" in path:
            return PATH_REFUSED
        segments = path.split("/")
        if len(segments) < 3 or segments[0] != _REPOS:
            return f"{_only(reach)} This gh api call names none of them."
        if tuple(segments[1:3]) == _GH_PLACEHOLDERS:
            in_a_repository = True              # gh fills them from GH_REPO or the remote
            tail = "/".join(segments[3:])
        else:
            # The address names its repository: the request's rule owns
            # whether it may go.
            refusal = _gh_api_refusal(reach, cmd, path)
            if refusal:
                return refusal
            works_in.append(name_of_url(f"https://{_API_HOST}/{path}") or f"{segments[1]}/{segments[2]}".lower())
    if not works_in and in_a_repository:
        works_in = [n for n in (name_of_remote(r) for r in remotes or []) if n]
    for n in works_in + beside:
        if n not in reach.repositories:
            return outside_sentence(reach, n)
    if not works_in:
        return f"{_only(reach)} This gh command names none of them."
    if tail is not None:
        # gh writes the repository into the address: each one it may take is
        # judged as the address it becomes.
        for n in works_in:
            refusal = _gh_api_refusal(reach, cmd, f"{_REPOS}/{n}/{tail}".rstrip("/"))
            if refusal:
                return refusal
    return ""


def _gh_api_refusal(reach: Reach, cmd: _GhCommand, path: str) -> str:
    """A ``gh api`` address under a repository list, judged by the rule that
    judges the request it becomes (``judge_request``): one rule for a REST
    address, at the command and at the exit. Read only is the command
    check's own above: under it only a GET reaches here."""
    return judge_request(reach, _gh_method(cmd), _API_HOST, f"/{path}").refusal


# ---------------------------------------------------------------------------
# A request at the credential exit
# ---------------------------------------------------------------------------

#: The most of a request's body the rule reads (a GraphQL document, an LFS
#: batch): a larger one is refused. A machine's exit sends the body to the
#: platform inside its ask, which must fit the request body the middleware
#: lets a command-line token send (64 KB), base64 and all.
REQUEST_BODY_MAX = 32 * 1024
#: How a refusal reaches its caller: plain text git prints as ``remote:``
#: lines, a JSON message gh prints, the same under git-lfs's own type.
KIND_GIT, KIND_API, KIND_LFS = "git", "api", "lfs"
_UPLOADS_HOST = "uploads.github.com"
_REQUEST_READS = frozenset({"GET", "HEAD"})
_POST = "POST"
_RECEIVE_PACK = "git-receive-pack"
_UPLOAD_PACK = "git-upload-pack"
_INFO_REFS = ["info", "refs"]
_LFS = ["info", "lfs"]
_LFS_BATCH = ["info", "lfs", "objects", "batch"]
_LFS_DOWNLOAD = "download"
_LFS_UPLOAD = "upload"
_JSON_TYPE = "application/json"
_CHARSET = "charset"
_UTF_8 = "utf-8"
_LFS_TYPE = "application/vnd.git-lfs+json"
_GRAPHQL = ["graphql"]
_GRAPHQL_KEYS = frozenset({"query", "variables", "operationName"})
# A document that holds either word may change something: GraphQL's
# keywords have case, and only an operation of these kinds writes.
_GRAPHQL_WRITES = ("mutation", "subscription")
# The addresses under a repository that make a new one: a fork, a
# repository from a template, an advisory's temporary private fork.
_NEW_REPOSITORY = frozenset({"forks", "generate"})
_ADVISORIES = "security-advisories"
_FORKS = "forks"
# The addresses under an issue that change its links to other issues (its
# sub-issues, the issues that block it, its related issues): the request
# names the other issue by an id alone, in its body or as its last segment,
# and that id can be any repository's.
_ISSUES = "issues"
_LINKED_BY_ID = frozenset({"sub_issues", "sub_issue", "dependencies", "relates_to"})
# What a URL parser ends an address at (a number sign) or drops from it (a
# tab, a carriage return, a line feed): with one of them the text judged
# here is not the address that is read or sent.
_RESHAPED = frozenset("#\t\r\n")
_REPOS = "repos"
_SEARCH = "search"
_SEARCH_QUERY = "q"
_SEARCH_KEYS = frozenset({_SEARCH_QUERY, "sort", "order", "per_page", "page"})
# A word that scopes a search, wherever it stands in the text.
_SCOPE_WORD = re.compile(r"(?i)(repo|org|user|owner|enterprise):")
_REPO_KEY = "repo"
# After one of these a scope word is the inside of another word in every
# reading (``monorepo:build``): no search grammar splits a word there.
_INSIDE_A_WORD = frozenset(string.ascii_letters + string.digits + "_")
_QUOTE = '"'
_ESCAPE = "\\"
_CONTROL = "Cc"
# The control characters pasted text holds. They are blanks, never words.
_PASTED_BLANKS = frozenset("\t\r\n")
# What stands before a phrase's opening quotation mark, and after a word.
_PHRASE_START = ("", " ", ":")
_WORD_END = ("", " ")
_IDENTITY_PATHS: tuple[list[str], ...] = ([], [_GH_USER_ENDPOINT])
# GitHub names a repository's next page by its number: the answer's ``Link``
# header is rewritten to the name, so the next request is judged as any.
_LINK_BY_NUMBER = r"https://api\.github\.com/repositories/\d+/"
REQUEST = "This request to GitHub"


@dataclass(frozen=True)
class RequestVerdict:
    """A judged request: the refusal its caller reads (``kind`` says in
    which form), else ``wants_body`` (the body must be read, up to that
    many bytes, and the request judged again with it), else it goes: with
    ``body`` in the place of the one read, and with the answer's ``Link``
    header rewritten (``link``: a pattern and what replaces each match)."""

    refusal: str = ""
    kind: str = KIND_API
    wants_body: int = 0
    body: bytes | None = None
    link: tuple[str, str] | None = None


def _names_none(reach: Reach) -> str:
    return f"{_only(reach)} {REQUEST} names none of them."


def _strict_json(body: bytes) -> Any:
    """A JSON text read as the judge reads every one (``_strict_object``);
    raises ``ValueError``."""
    return json.loads(body.decode("utf-8"), object_pairs_hook=_strict_object, parse_constant=_no_constant)


def _media_type(content_type: str) -> str:
    """The media type of a body the rule reads as UTF-8, ``""`` for a
    header it cannot take so: more than one type (the exit joins a repeated
    header with a comma) or a ``charset`` that is not UTF-8."""
    text = content_type or ""
    if "," in text:
        return ""
    media, *parameters = (part.strip() for part in text.split(";"))
    for parameter in parameters:
        key, _, value = parameter.partition("=")
        if key.strip().lower() == _CHARSET and value.strip().strip('"').lower() != _UTF_8:
            return ""
    return media.lower()


def _request_object(body: bytes, content_type: str, content_encoding: str, types: tuple[str, ...]) -> dict | None:
    """A request's body as one JSON object, None when it cannot be read as
    the server will read it: another content type or two, a charset that is
    not UTF-8, any content encoding, a text that is not strict JSON, a list."""
    if _media_type(content_type) not in types or (content_encoding or "").strip().lower() not in ("", "identity"):
        return None
    try:
        doc = _strict_json(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


def _judge_git_request(reach: Reach, verb: str, path: str, segments: list[str], query: str,
                       content_type: str, content_encoding: str, body: bytes | None) -> RequestVerdict:
    """A request to a git host: the first two segments name the repository,
    the service and the method say whether it writes."""
    kind = KIND_LFS if segments[2:4] == _LFS else KIND_GIT
    if reach.repositories and len(segments) < 2:
        return RequestVerdict(refusal=_names_none(reach), kind=kind)
    refusal = judge_git(reach, path.lstrip("/"))
    if refusal:
        return RequestVerdict(refusal=refusal, kind=kind)
    if not reach.read_only:
        return RequestVerdict()
    last = segments[-1] if segments else ""
    if last == _RECEIVE_PACK or (segments[-2:] == _INFO_REFS and _RECEIVE_PACK in unquote(query).lower()):
        return RequestVerdict(refusal=push_refused(reach), kind=kind)
    if verb in _REQUEST_READS or (verb == _POST and last == _UPLOAD_PACK):
        return RequestVerdict()
    if verb == _POST and segments[-4:] == _LFS_BATCH:
        if body is None:
            return RequestVerdict(wants_body=REQUEST_BODY_MAX, kind=kind)
        doc = _request_object(body, content_type, content_encoding, (_LFS_TYPE, _JSON_TYPE))
        operation = doc.get("operation") if doc is not None else None
        if operation == _LFS_DOWNLOAD and doc is not None:
            # What goes on is what was judged, never the caller's own bytes.
            return RequestVerdict(body=json.dumps(doc).encode())
        if operation == _LFS_UPLOAD:
            return RequestVerdict(refusal=read_only_sentence(reach, "This upload to GitHub"), kind=kind)
        return RequestVerdict(refusal=UNREADABLE, kind=kind)
    return RequestVerdict(refusal=read_only_sentence(reach, REQUEST), kind=kind)


def _judge_graphql(reach: Reach, verb: str, query: str, content_type: str, content_encoding: str,
                   body: bytes | None) -> RequestVerdict:
    """GitHub's GraphQL address. A repository list is not applied: the
    document names its repositories inside itself and walks from one to
    another. Read only is: a document that may change something is refused."""
    if verb != _POST or query:
        return RequestVerdict(refusal=METHOD_REFUSED)
    if not reach.read_only:
        return RequestVerdict()
    if body is None:
        return RequestVerdict(wants_body=REQUEST_BODY_MAX)
    doc = _request_object(body, content_type, content_encoding, (_JSON_TYPE,))
    document = doc.get("query") if doc is not None else None
    if doc is None or set(doc) - _GRAPHQL_KEYS or not isinstance(document, str) or not document:
        return RequestVerdict(refusal=UNREADABLE)
    if any(word in document for word in _GRAPHQL_WRITES):
        return RequestVerdict(refusal=read_only_sentence(reach, "This GraphQL request"))
    return RequestVerdict(body=json.dumps(doc).encode())


def _firm(asked: str, m: re.Match[str]) -> bool:
    """Whether a counted ``repo:`` is a qualifier in every reading of the
    text: a word of its own (the text's start or a plain space before it, a
    plain space or the text's end after its value), and outside every
    quoted phrase however the quotation marks are paired. So either no
    quotation mark stands before it, or those before it are whole phrases:
    each opens at a word's start and closes at a word's end, none behind a
    backslash."""
    start = m.start()
    if asked[start - 1:start] not in _WORD_END or asked[m.end():m.end() + 1] not in _WORD_END:
        return False
    quotes = [i for i, c in enumerate(asked[:start]) if c == _QUOTE]
    if len(quotes) % 2:
        return False
    for n, i in enumerate(quotes):
        before, after = asked[i - 1:i], asked[i + 1:i + 2]
        if n % 2 == 0 and before not in _PHRASE_START:
            return False
        if n % 2 and (after not in _WORD_END or before == _ESCAPE):
            return False
    return True


def _search_fault(reach: Reach, asked: str) -> str:
    """Why a search's words cannot be judged, as what to change ("" when
    they can). ``named_in_query`` counts a ``repo:`` with a value at the
    text's start, after a blank or after a parenthesis. A scope word
    anywhere else that is not the inside of another word could be a
    qualifier in GitHub's reading that the count missed (after a comma, a
    quotation mark, a hyphen, with its value after a space), so it is
    refused: for ``repo:`` with what to write, for the others as widening."""
    if any(unicodedata.category(c) == _CONTROL and c not in _PASTED_BLANKS for c in asked):
        return f"{_only(reach)} {SEARCH_CONTROL}"
    counted = {m.start() + len(m.group(1)) for m in _REPO_QUALIFIER.finditer(asked)}
    for m in _SCOPE_WORD.finditer(asked):
        if asked[m.start() - 1:m.start()] in _INSIDE_A_WORD:
            continue
        if m.group(1).lower() != _REPO_KEY:
            return _WIDENING_SENTENCE.format(only=_only(reach))
        if m.start() not in counted:
            return f"{_only(reach)} {SEARCH_OWN_WORD}"
    return ""


def _judge_search(reach: Reach, query: str) -> RequestVerdict:
    """A REST search under a list: held to its ``repo:`` qualifiers as every
    search is, and its query to the keys the rule knows. The address cannot
    be rewritten, so the search goes only when it is held to the list in
    every reading of its words: every ``repo:`` any reading could take as a
    qualifier names a listed repository, and one of them is a qualifier in
    every reading (``_firm``). Each refusal says what to change."""
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return RequestVerdict(refusal=f"{_only(reach)} {SEARCH_ADDRESS}")
    keys = [k for k, _v in pairs]
    asked = dict(pairs).get(_SEARCH_QUERY)
    # Only the keys the rule knows, each once, and no second separator: a
    # key it does not read could name a repository another way, and a server
    # that splits on a semicolon too would read other keys from the same text.
    if ";" in query or len(set(keys)) != len(keys) or set(keys) - _SEARCH_KEYS or asked is None:
        return RequestVerdict(refusal=f"{_only(reach)} {SEARCH_ADDRESS}")
    if widens(asked):
        return RequestVerdict(refusal=_WIDENING_SENTENCE.format(only=_only(reach)))
    fault = _search_fault(reach, asked)
    if fault:
        return RequestVerdict(refusal=fault)
    named = named_in_query(asked)
    if not named:
        return RequestVerdict(refusal=f"{_only(reach)} Add repo:owner/name to the search.")
    for n in named:
        if n not in reach.repositories:
            return RequestVerdict(refusal=outside_sentence(reach, n))
    if not any(_firm(asked, m) for m in _REPO_QUALIFIER.finditer(asked) if not m.group(1)):
        return RequestVerdict(refusal=f"{_only(reach)} {SEARCH_FIRST}")
    return RequestVerdict()


def judge_request(reach: Reach, method: str, host: str, target: str, *, content_type: str = "",
                  content_encoding: str = "", body: bytes | None = None) -> RequestVerdict:
    """One HTTPS request that will carry git's and gh's token, as the
    credential exit is about to send it: ``host`` the connection's, ``target``
    the request's path and query. ``body`` is None at the first ask; the
    rule asks for it (``wants_body``) only where the address does not say
    enough. git's addresses and GitHub's REST addresses name their
    repository and are held to the list and to read only; GraphQL is held
    to read only alone."""
    if not reach.narrows:
        return RequestVerdict()
    verb = (method or "").upper()
    name = (host or "").lower().rstrip(".")
    path, _, query = (target or "").partition("?")
    on_git = name in _GITHUB_HOSTS
    if not path.startswith("/") or has_traversal(path) or "//" in path or _RESHAPED & set(target or ""):
        return RequestVerdict(refusal=PATH_REFUSED, kind=KIND_GIT if on_git else KIND_API)
    segments = [s for s in path.split("/") if s]
    if on_git:
        return _judge_git_request(reach, verb, path, segments, query, content_type, content_encoding, body)
    if name == _API_HOST and segments == _GRAPHQL:
        return _judge_graphql(reach, verb, query, content_type, content_encoding, body)
    reading = verb in _REQUEST_READS
    if reach.read_only and not reading:
        return RequestVerdict(refusal=read_only_sentence(reach, REQUEST))
    if not reach.repositories:
        return RequestVerdict()
    if name in (_API_HOST, _UPLOADS_HOST) and len(segments) >= 3 and segments[0] == _REPOS:
        repository = name_of_url(f"https://{_API_HOST}{path}") or f"{segments[1]}/{segments[2]}".lower()
        if repository not in reach.repositories:
            return RequestVerdict(refusal=outside_sentence(reach, repository))
        # The words after the name as a server that decodes an escape and
        # folds case would read them.
        rest = [unquote(s).lower() for s in segments[3:]]
        if not reading and ((len(rest) == 1 and rest[0] in _NEW_REPOSITORY)
                            or (len(rest) == 3 and rest[0] == _ADVISORIES and rest[2] == _FORKS)):
            return RequestVerdict(refusal=f"{_only(reach)} A new repository is outside them.")
        if not reading and len(rest) >= 3 and rest[0] == _ISSUES and rest[2] in _LINKED_BY_ID:
            return RequestVerdict(refusal=f"{_only(reach)} {LINKS_REFUSED}")
        link = (_LINK_BY_NUMBER, f"https://{_API_HOST}/{_REPOS}/{repository}/") if name == _API_HOST else None
        return RequestVerdict(link=link)
    if name == _API_HOST and reading:
        if segments in _IDENTITY_PATHS:
            return RequestVerdict()
        if len(segments) == 2 and segments[0] == _SEARCH:
            return _judge_search(reach, query)
    return RequestVerdict(refusal=_names_none(reach))


# ---------------------------------------------------------------------------
# GitHub's own narrowing of an installation token
# ---------------------------------------------------------------------------

# GitHub App permissions that have no read level.
_WRITE_ONLY_PERMISSIONS = frozenset({"workflows", "organization_copilot_seat_management", "profile"})
_LEVELS = frozenset({"read", "write", "admin"})


def read_only_permissions(granted: Mapping[str, Any] | None) -> dict[str, str]:
    """The installation's permissions at ``read``: what a read-only
    installation token asks GitHub for (a permission without a read level
    is left out)."""
    return {
        str(k): "read" for k, v in (granted or {}).items()
        if str(k) not in _WRITE_ONLY_PERMISSIONS and str(v) in _LEVELS
    }


def repository_names(reach: Reach, account: str) -> list[str]:
    """The names an installation token is narrowed to. GitHub takes them
    without the owner (one installation is one account), so a name of
    another owner raises ``ValueError``: cut to its name, it could be one of
    this account's other repositories."""
    owner = (account or "").strip().lower()
    names: list[str] = []
    for full in sorted(reach.repositories):
        name_owner, _, name = full.partition("/")
        if not owner or name_owner != owner:
            raise ValueError(f"{full} is not a repository of the installation's account")
        names.append(name)
    return names
