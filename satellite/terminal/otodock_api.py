#!/usr/bin/env python3
"""The ``otodock`` command's verbs that use your OtoDock through a personal
API key: ``login``, ``logout``, ``agents``, ``chat``, ``tasks``, ``files`` and
``notify``.

Paired machines get them through their ``otodock`` command. On any other
machine this one file is the command: download it from User Settings (or
``GET /v1/api/otodock.py``) and run it with Python 3.9 or later. It needs
nothing but the standard library::

    python3 otodock.py login
    python3 otodock.py chat send --agent my-agent --wait "Summarise today's tickets"

``login`` proves the key and writes it to a file only you can read: under the
machine's own OtoDock folder on a paired machine, else
``~/.config/otodock/cli.json``. Every verb refuses when it finds an agent's
session around it, and on a paired machine the platform keeps its agents off
that file and off these verbs. Those are guards, not a wall: an agent's
session runs as your own user there, so keep no key on a machine whose agents
you would not trust with it.

The key goes to the address you gave and nowhere else: a redirect is never
followed, and ``OTODOCK_URL`` is taken only together with ``OTODOCK_API_KEY``,
so a saved key is never sent to another address.

Exit codes: 0 done, 1 refused or failed, 2 wrong usage, 3 the turn still runs
at the timeout.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

VERBS = frozenset({"login", "logout", "agents", "chat", "tasks", "files", "notify"})

# The words of the answers this command reads (the proxy's ``api/public/``).
STATE_DONE, STATE_RUNNING = "done", "running"
RUN_PENDING, RUN_RUNNING, RUN_COMPLETED = "pending", "running", "completed"
ROLE_USER = "user"
NODE_DIR = "dir"
PLAIN, SECURE = "http://", "https://"

# The environment a platform session runs in: its token and its id.
_SESSION_ENV = ("OTO_SESSION_ID", "PROXY_API_KEY")
_STATE_DIRS = (".oto-dock", "OtoDock")
_WAIT_WINDOW_S = 55
_RETRIES = 3
EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_STILL_RUNNING = 0, 1, 2, 3
_REDIRECTS = (301, 302, 303, 307, 308)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is never followed: the key would travel with it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class Refused(Exception):
    """The server's refusal, or a connection that did not answer."""


def _config_path() -> Path:
    home = Path.home()
    for name in _STATE_DIRS:
        if (home / name / "satellite.conf").is_file():
            return home / name / "cli.json"
    # APPDATA: Windows sets it, no other system does.
    if os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "otodock" / "cli.json"
    return home / ".config" / "otodock" / "cli.json"


def _load_login() -> tuple[str, str]:
    url, key = os.environ.get("OTODOCK_URL", ""), os.environ.get("OTODOCK_API_KEY", "")
    if url and key:
        return url.rstrip("/"), key
    if url:
        # The saved key belongs to the saved address.
        raise Refused("Set OTODOCK_API_KEY together with OTODOCK_URL. A saved key is never "
                      "sent to another address.")
    try:
        saved = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    url, key = url or saved.get("url", ""), key or saved.get("key", "")
    if not url or not key:
        raise Refused("You are not logged in. Run: otodock login")
    return url.rstrip("/"), key


def _save_login(url: str, key: str) -> Path:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"url": url, "key": key}, f)
    # An older file keeps its mode through the open above.
    os.chmod(path, 0o600)
    return path


def _call(url: str, key: str, method: str, path: str, *, body: dict | None = None,
          query: dict | None = None, timeout: float = 30.0, raw: bool = False):
    """One call. The answer's JSON (or bytes with ``raw``); ``Refused`` with
    the server's own sentence on a refusal."""
    target = url + path
    if query:
        target += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(target, data=data, method=method)
    request.add_header("Authorization", f"Bearer {key}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as e:
        if e.code in _REDIRECTS:
            raise Refused(_redirected(url, e.headers.get("Location") or "")) from e
        raise Refused(_detail(e)) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ConnectionError(f"{url} did not answer: {e}") from e
    return payload if raw else json.loads(payload or b"{}")


def _redirected(url: str, location: str) -> str:
    where = urllib.parse.urlparse(location).netloc or "another page"
    return (f"{url} answered with a redirect to {where}. The key was not sent there. If a login "
            "gateway sits in front of your OtoDock, let /v1/api/ through it.")


def _detail(error: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(error.read() or b"{}").get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, dict):
        detail = detail.get("message")
    return str(detail or f"The server answered {error.code}.")


def _retrying(fn):
    """A call that may repeat: a dropped connection tries again, a refusal
    never does."""
    for attempt in range(_RETRIES):
        try:
            return fn()
        except ConnectionError as e:
            if attempt == _RETRIES - 1:
                raise Refused(str(e)) from e
            time.sleep(2)
    raise Refused("The server did not answer.")


def _say(text: str) -> None:
    print(text, file=sys.stderr)


# -- login ------------------------------------------------------------------

def cmd_login(args) -> int:
    url = (args.url or os.environ.get("OTODOCK_URL") or input("Address of your OtoDock: ")).strip().rstrip("/")
    if not url.startswith((PLAIN, SECURE)):
        raise Refused("The address starts with https:// or http://.")
    if url.startswith(PLAIN):
        # Said before the key is asked for or sent.
        _say("This address is not encrypted: the key travels in the clear.")
    key = os.environ.get("OTODOCK_API_KEY") or getpass.getpass("Your API key: ")
    key = key.strip()
    try:
        me = _call(url, key, "GET", "/v1/api/me")
    except ConnectionError as e:
        raise Refused(str(e)) from e
    path = _save_login(url, key)
    scopes = ", ".join(me["key"]["scopes"]) or "none"
    print(f"Logged in as {me['name']} with the key \"{me['key']['name']}\" ({scopes}).")
    print(f"The key is saved in {path}.")
    return EXIT_OK


def cmd_logout(_args) -> int:
    path = _config_path()
    if path.is_file():
        path.unlink()
        print("Logged out. The key is still valid: revoke it in your settings if you no longer need it.")
    else:
        print("You were not logged in.")
    return EXIT_OK


# -- agents -----------------------------------------------------------------

def cmd_agents(args) -> int:
    url, key = _load_login()
    agents = _retrying(lambda: _call(url, key, "GET", "/v1/api/agents"))["agents"]
    if args.json:
        print(json.dumps(agents, indent=2))
        return EXIT_OK
    for a in agents:
        print(f"{a['agent']}\t{a['role']}\t{a['name']}")
    if not agents:
        _say("This key reaches no agent.")
    return EXIT_OK


# -- chat -------------------------------------------------------------------

def _text_of(words: list[str]) -> str:
    text = " ".join(words).strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        raise SystemExit(_usage("Write the message, or pipe it in."))
    return text


def _usage(message: str) -> int:
    _say(f"otodock: {message}")
    return EXIT_USAGE


def _finish_turn(turn: dict, *, as_json: bool, told: bool = False) -> int:
    state = turn.get("state")
    if as_json:
        print(json.dumps(turn, indent=2))
    elif state == STATE_DONE:
        print(turn.get("answer") or "")
    if state == STATE_DONE:
        return EXIT_OK
    if not as_json and not (told and turn.get("waiting_for_you")):
        _say(turn.get("detail") or f"The turn ended as {state}.")
    return EXIT_STILL_RUNNING if state == STATE_RUNNING else EXIT_REFUSED


def cmd_chat_send(args) -> int:
    url, key = _load_login()
    text = _text_of(args.text)
    chat_id = args.chat
    if bool(chat_id) == bool(args.agent):
        return _usage("Name one of --agent (a new chat) or --chat (an existing one).")
    if not chat_id:
        chat_id = _retrying(lambda: _call(url, key, "POST", "/v1/api/chats",
                                          body={"agent": args.agent, "mode": args.mode}))["id"]
    request_id = str(uuid.uuid4())
    deadline = time.monotonic() + args.timeout
    wait = _WAIT_WINDOW_S if args.wait else 0
    # The same request id on a retry: a dropped connection never sends twice.
    turn = _retrying(lambda: _call(
        url, key, "POST", f"/v1/api/chats/{chat_id}/messages",
        body={"text": text, "request_id": request_id, "wait_seconds": min(wait, args.timeout)},
        timeout=wait + 30))
    told = False
    read = f"/v1/api/chats/{chat_id}/messages/{turn.get('message_id')}"
    while args.wait and turn.get("state") == STATE_RUNNING and time.monotonic() < deadline:
        if turn.get("waiting_for_you") and not told:
            told = True
            _say(turn.get("detail") or "The turn waits for your answer in the dashboard.")
        left = int(deadline - time.monotonic())
        window = max(1, min(_WAIT_WINDOW_S, left))
        turn = _retrying(lambda w=window: _call(url, key, "GET", read,
                                                query={"wait_seconds": w}, timeout=w + 30))
    if not args.wait and turn.get("state") == STATE_RUNNING:
        print(f"Sent. Chat {chat_id}, message {turn.get('message_id')}.")
        return EXIT_OK
    return _finish_turn(turn, as_json=args.json, told=told)


def cmd_chat_list(args) -> int:
    url, key = _load_login()
    chats = _retrying(lambda: _call(url, key, "GET", "/v1/api/chats",
                                    query={"agent": args.agent, "limit": args.limit}))["chats"]
    if args.json:
        print(json.dumps(chats, indent=2))
        return EXIT_OK
    for c in chats:
        print(f"{c['id']}\t{c['agent']}\t{c['title']}")
    return EXIT_OK


def cmd_chat_read(args) -> int:
    url, key = _load_login()
    page = _retrying(lambda: _call(url, key, "GET", f"/v1/api/chats/{args.chat}",
                                   query={"limit": args.limit}))
    if args.json:
        print(json.dumps(page, indent=2))
        return EXIT_OK
    for m in page["messages"]:
        who = "you" if m["role"] == ROLE_USER else page["chat"]["agent"]
        print(f"[{who}] {m['content']}\n")
    return EXIT_OK


# -- tasks ------------------------------------------------------------------

def _follow_run(url: str, key: str, run_id: str, timeout: int) -> int:
    deadline = time.monotonic() + timeout
    while True:
        run = _retrying(lambda: _call(url, key, "GET", f"/v1/api/runs/{run_id}"))
        if run["status"] not in (RUN_PENDING, RUN_RUNNING):
            break
        if time.monotonic() >= deadline:
            _say(f"The run {run_id} still runs. Read it with: otodock tasks status {run_id}")
            return EXIT_STILL_RUNNING
        time.sleep(3)
    if run.get("output_text"):
        print(run["output_text"])
    if run["status"] == RUN_COMPLETED:
        return EXIT_OK
    _say(run.get("error_message") or f"The run ended as {run['status']}.")
    return EXIT_REFUSED


def cmd_tasks_run(args) -> int:
    url, key = _load_login()
    if args.task:
        started = _retrying(lambda: _call(url, key, "POST", f"/v1/api/tasks/{args.task}/run"))
    else:
        if not args.agent or not args.prompt:
            return _usage("Name a task id, or --agent with a prompt.")
        prompt = " ".join(args.prompt)
        body = {"agent": args.agent, "name": args.name or prompt[:60], "prompt": prompt,
                "scope": args.scope}
        started = _call_once_refusing(url, key, "/v1/api/tasks", body)
    run_id = started.get("run_id") or ""
    print(f"Task {started.get('task_id')}, run {run_id}.")
    return _follow_run(url, key, run_id, args.timeout) if args.wait and run_id else EXIT_OK


def _call_once_refusing(url: str, key: str, path: str, body: dict) -> dict:
    """A call that creates something: never repeated by itself."""
    try:
        return _call(url, key, "POST", path, body=body)
    except ConnectionError as e:
        raise Refused(str(e)) from e


def cmd_tasks_list(args) -> int:
    url, key = _load_login()
    tasks = _retrying(lambda: _call(url, key, "GET", "/v1/api/tasks",
                                    query={"agent": args.agent}))["tasks"]
    if args.json:
        print(json.dumps(tasks, indent=2))
        return EXIT_OK
    for t in tasks:
        print(f"{t['id']}\t{t['agent']}\t{t['scope']}\t{t['name']}")
    return EXIT_OK


def cmd_tasks_status(args) -> int:
    url, key = _load_login()
    if args.wait:
        return _follow_run(url, key, args.run, args.timeout)
    run = _retrying(lambda: _call(url, key, "GET", f"/v1/api/runs/{args.run}"))
    print(json.dumps(run, indent=2))
    return EXIT_OK


# -- files ------------------------------------------------------------------

def _print_tree(nodes: list[dict], indent: str = "") -> None:
    for n in nodes:
        print(f"{indent}{n['name']}{'/' if n['type'] == NODE_DIR else ''}")
        _print_tree(n.get("children") or [], indent + "  ")


def cmd_files_list(args) -> int:
    url, key = _load_login()
    tree = _retrying(lambda: _call(url, key, "GET", f"/v1/api/agents/{args.agent}/files"))["tree"]
    _print_tree(tree)
    return EXIT_OK


def _quote(path: str) -> str:
    return urllib.parse.quote(path.lstrip("/"), safe="/")


def cmd_files_get(args) -> int:
    url, key = _load_login()
    data = _retrying(lambda: _call(url, key, "GET",
                                   f"/v1/api/agents/{args.agent}/files/{_quote(args.path)}",
                                   query={"download": "true"}, raw=True, timeout=120))
    if args.output:
        Path(args.output).write_bytes(data)
        print(f"Saved {args.output}.")
    else:
        sys.stdout.buffer.write(data)
    return EXIT_OK


def cmd_files_put(args) -> int:
    url, key = _load_login()
    content = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    saved = _retrying(lambda: _call(url, key, "PUT",
                                    f"/v1/api/agents/{args.agent}/files/{_quote(args.path)}",
                                    body={"content": content}, timeout=120))
    print(f"Saved {saved['path']}.")
    return EXIT_OK


# -- notify -----------------------------------------------------------------

def cmd_notify(args) -> int:
    url, key = _load_login()
    _call_once_refusing(url, key, "/v1/api/notifications",
                        {"title": args.title, "body": args.body or "", "severity": args.severity})
    print("Sent to you.")
    return EXIT_OK


# -- the parser -------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="otodock", description="Use your OtoDock with a personal API key.")
    verbs = parser.add_subparsers(dest="verb", required=True)

    p = verbs.add_parser("login", help="Save your address and key")
    p.add_argument("--url", default="")
    p.set_defaults(fn=cmd_login)
    verbs.add_parser("logout", help="Forget the saved key").set_defaults(fn=cmd_logout)
    p = verbs.add_parser("agents", help="The agents the key reaches")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_agents)

    chat = verbs.add_parser("chat", help="Your chats").add_subparsers(dest="action", required=True)
    p = chat.add_parser("send", help="Send a message, in a new chat or an existing one")
    p.add_argument("--agent", default="", help="Start a new chat on this agent")
    p.add_argument("--chat", default="", help="Send into this chat")
    p.add_argument("--wait", action="store_true", help="Wait for the turn's end and print the answer")
    p.add_argument("--timeout", type=int, default=600, help="The longest to wait, in seconds")
    p.add_argument("--mode", default="ask", help="A new chat's mode: ask or plan (autoReview and dontAsk take a key made to run without asking)")
    p.add_argument("--json", action="store_true")
    p.add_argument("text", nargs="*")
    p.set_defaults(fn=cmd_chat_send)
    p = chat.add_parser("list", help="Your chats, newest first")
    p.add_argument("--agent", default="")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_chat_list)
    p = chat.add_parser("read", help="A chat's messages")
    p.add_argument("chat")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_chat_read)

    tasks = verbs.add_parser("tasks", help="Your tasks").add_subparsers(dest="action", required=True)
    p = tasks.add_parser("run", help="Run a task now, or create one and run it")
    p.add_argument("--task", default="", help="The id of a task to run")
    p.add_argument("--agent", default="")
    p.add_argument("--name", default="")
    p.add_argument("--scope", default="user", choices=("user", "agent"))
    p.add_argument("--wait", action="store_true", help="Wait for the run's end and print its result")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("prompt", nargs="*")
    p.set_defaults(fn=cmd_tasks_run)
    p = tasks.add_parser("list", help="The tasks the key may run")
    p.add_argument("--agent", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_tasks_list)
    p = tasks.add_parser("status", help="A run's state and result")
    p.add_argument("run")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=int, default=600)
    p.set_defaults(fn=cmd_tasks_status)

    files = verbs.add_parser("files", help="Your files on an agent").add_subparsers(dest="action", required=True)
    p = files.add_parser("list", help="The tree the key reaches")
    p.add_argument("--agent", required=True)
    p.set_defaults(fn=cmd_files_list)
    p = files.add_parser("get", help="Download a file")
    p.add_argument("--agent", required=True)
    p.add_argument("-o", "--output", default="")
    p.add_argument("path")
    p.set_defaults(fn=cmd_files_get)
    p = files.add_parser("put", help="Write a text file, from a file or stdin")
    p.add_argument("--agent", required=True)
    p.add_argument("path")
    p.add_argument("file", nargs="?", default="")
    p.set_defaults(fn=cmd_files_put)

    p = verbs.add_parser("notify", help="Send yourself a notification")
    p.add_argument("title")
    p.add_argument("body", nargs="?", default="")
    p.add_argument("--severity", default="info", choices=("info", "success", "warning", "danger"))
    p.set_defaults(fn=cmd_notify)
    return parser


def main(argv: list[str] | None = None) -> int:
    if any(os.environ.get(name) for name in _SESSION_ENV):
        _say("otodock: this command does not run inside an agent's session.")
        return EXIT_REFUSED
    args = _parser().parse_args(argv)
    try:
        return args.fn(args)
    except Refused as e:
        _say(f"otodock: {e}")
        return EXIT_REFUSED
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
