#!/usr/bin/env python3
"""The credential helper: a session's command-line tools get the connected
account's token from the platform at the moment of use.

Two modes, both reached from the session's shell, never by the engine:

* ``credential_helper.py git <get|store|erase>`` is git's credential helper
  for the hosts the session's manifests name (the resolver points
  ``credential.https://<host>.helper`` at this script through git's
  env-config). ``get`` reads the block git writes on stdin (``protocol``,
  ``host``, an optional ``path``), asks the broker for the host and answers
  ``username=`` and ``password=``; ``store`` and ``erase`` do nothing, the
  platform being the source of truth. A refusal, or a platform that cannot
  be reached, is written to stderr with ``quit=1`` on stdout: git stops at
  once, tries no other helper and opens no prompt, on a terminal or not.
* ``credential_helper.py run <host> <command> -- <args...>`` is what the
  command wrappers call (``bin/gh``): the real command is found on the
  PATH with the wrapper directory removed and run with the token set under
  the names the broker gives, in that one process's environment only. When
  the session holds no account for the host (the broker's quiet 404) the
  command runs plainly, as it would without the platform; every other
  refusal (an account that needs a reconnect, a platform that cannot be
  reached, a satellite that needs its update) is printed and the command
  does not run, so an account the platform holds but refuses is never
  replaced by whatever identity the machine's own command would use.

Environment (set by every spawn path; the hooks read the same three):
  PROXY_URL         the platform, or a satellite's loopback tunnel
  PROXY_API_KEY     the session's own token (the bearer)
  OTO_SESSION_ID    the session

The token is held in memory for the one call and written nowhere; it never
appears in an argument list. Stdlib only, run under ``python -I``.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

# The loopback tunnel answers 502 or 503 while a satellite reconnects; a few
# short retries cover that, and an unreachable platform is reported within
# seconds (git asks a helper at most twice per request).
_BACKOFF_S = (0.5, 1.0, 2.0)
_TIMEOUT_S = 10.0
_REFUSE_UPDATE = (
    "this machine's satellite needs the 1.8.0 update before git and gh can "
    "authenticate here"
)
_REFUSE_UNREACHABLE = "the platform cannot be reached to fetch the credential"
# A satellite below 0.5.139 refuses the route on its tunnel with exactly
# this body; a 403 with any other body is a refusal of the platform's.
_OLD_TUNNEL_REFUSAL = {"error": "path-not-allowlisted"}
# The wrapper directories a command has already passed through, so a
# helper never hands the command back to a wrapper (``_real_command``).
_GUARD = "OTO_WRAPPED"


class _Refused(Exception):
    """The broker gave no credential: ``reason`` says why (empty for a
    plain "not connected", which the wrappers keep silent)."""

    def __init__(self, reason: str, quiet: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.quiet = quiet


def _fetch(host: str) -> dict:
    """The broker's answer for ``host``: ``{username, password, env}``."""
    proxy_url = os.environ.get("PROXY_URL", "").rstrip("/")
    token = os.environ.get("PROXY_API_KEY", "")
    session_id = os.environ.get("OTO_SESSION_ID", "")
    if not proxy_url or not token or not session_id:
        raise _Refused("the session carries no platform link (PROXY_URL, PROXY_API_KEY, OTO_SESSION_ID)")
    body = json.dumps({"session_id": session_id, "host": host}).encode()
    for attempt, wait in enumerate((*_BACKOFF_S, None)):
        req = urllib.request.Request(
            f"{proxy_url}/v1/hooks/cli-credential", data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                answer = json.loads(resp.read().decode("utf-8"))
            if not isinstance(answer, dict) or not answer.get("password"):
                raise _Refused("the platform answered without a credential")
            return answer
        except urllib.error.HTTPError as e:
            if e.code in (502, 503, 504) and wait is not None:
                time.sleep(wait)
                continue
            body = _body_of(e)
            detail = body.get("detail") if isinstance(body, dict) else None
            detail = detail if isinstance(detail, str) else ""
            if e.code == 404:
                raise _Refused(detail or "no account is connected for this session", quiet=True)
            if e.code == 403 and body == _OLD_TUNNEL_REFUSAL:
                raise _Refused(_REFUSE_UPDATE)
            if e.code in (502, 503, 504):
                raise _Refused(_REFUSE_UNREACHABLE)
            raise _Refused(detail or f"the platform refused the credential (HTTP {e.code})")
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            # A connection refused, a reset, a half-written or malformed
            # answer: a short retry, then the platform is reported as
            # unreachable.
            if wait is not None:
                time.sleep(wait)
                continue
            raise _Refused(_REFUSE_UNREACHABLE)
    raise _Refused(_REFUSE_UNREACHABLE)


def _body_of(e: urllib.error.HTTPError):
    try:
        return json.loads(e.read().decode("utf-8"))
    except Exception:
        return None


def _reason(e: Exception) -> str:
    """What the caller is told: the broker's reason, or the failure's
    kind, never a traceback (git would fall through to a prompt)."""
    return e.reason if isinstance(e, _Refused) else f"the helper failed ({type(e).__name__})"


def _read_git_request() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            break
        key, sep, value = line.partition("=")
        if sep:
            out[key] = value
    return out


def _git(operation: str) -> int:
    if operation != "get":
        return 0
    request = _read_git_request()
    host = request.get("host", "")
    try:
        if not host:
            raise _Refused("git named no host")
        answer = _fetch(host)
    except Exception as e:
        sys.stderr.write(f"otodock credential helper: {_reason(e)}\n")
        sys.stdout.write("quit=1\n")
        sys.stdout.flush()
        return 0
    sys.stdout.write(f"username={answer.get('username') or 'x-access-token'}\n")
    sys.stdout.write(f"password={answer['password']}\n")
    sys.stdout.flush()
    return 0


def _real_dir(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _real_command(command: str) -> tuple[str | None, set[str]]:
    """The real binary, found on the PATH with the wrapper directories taken
    out: this script's own ``bin`` and every one an outer helper passed
    through (``OTO_WRAPPED``, so two wrappers never hand a command to each
    other), each compared by its real path, however PATH spells it (a
    relative entry, a link). Returns the binary and the directories left
    out, which the real command's process carries on for a command it
    starts in turn."""
    here = os.path.dirname(os.path.realpath(__file__))
    excluded = {_real_dir(os.path.join(here, "bin"))}
    excluded.update(_real_dir(d) for d in os.environ.get(_GUARD, "").split(os.pathsep) if d)
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and _real_dir(d) not in excluded]
    return shutil.which(command, path=os.pathsep.join(dirs)), excluded


def _run(host: str, command: str, args: list[str]) -> int:
    real, excluded = _real_command(command)
    if not real:
        sys.stderr.write(f"{command}: not found\n")
        return 127
    if _real_dir(os.path.dirname(real)) in excluded:
        # The search ended in a wrapper directory after all (the current
        # directory, which Windows searches first): never hand off again.
        sys.stderr.write(
            f"otodock credential helper: the real {command} was not found past its "
            f"wrapper ({os.path.dirname(real)}); put it on PATH\n"
        )
        return 1
    env = dict(os.environ)
    env[_GUARD] = os.pathsep.join(sorted(excluded))
    try:
        answer = _fetch(host)
    except Exception as e:
        if not (isinstance(e, _Refused) and e.quiet):
            sys.stderr.write(f"otodock credential helper: {_reason(e)}\n")
            return 1
    else:
        for name in answer.get("env") or []:
            if isinstance(name, str) and name:
                env[name] = answer["password"]
    argv = [real, *args]
    if os.name == "posix":
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(real, argv, env)
    return subprocess.run(argv, env=env).returncode


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "git":
        return _git(argv[1])
    if len(argv) >= 4 and argv[0] == "run" and argv[3] == "--":
        return _run(argv[1], argv[2], argv[4:])
    sys.stderr.write(
        "usage: credential_helper.py git <get|store|erase>\n"
        "       credential_helper.py run <host> <command> -- <args...>\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
