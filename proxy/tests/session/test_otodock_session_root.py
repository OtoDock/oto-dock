"""otodock-CLI — per-session allowed-root policy.

Standalone harness (the live proxy deadlocks pytest on the conftest DB pool):
    proxy/venv/bin/python tests/session/test_otodock_session_root.py

Verifies that an arbitrary out-of-home cwd is admitted ONLY when it is installed
as a per-session allowed root, that under-home paths work without one, that the
protected-path / .env denials still win over a session root, and that
context_from_security threads the roots off a SecurityContext.
"""
import os
import sys

# Standalone-run bootstrap: proxy/ onto sys.path (tests/<area>/<file>.py -> 3 up).
# Redundant under pytest (conftest handles it); kept for `python <file>` runs.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from services.path_policy_v2 import (  # noqa: E402
    PathPolicyContext,
    resolve_path_for_session,
    context_from_security,
)
from core import placement

HOME = "/home/alice"
AGENTS = "/home/alice/.oto-dock/agents"


def _ctx(roots=()):
    return PathPolicyContext(
        agent_slug="my-agent",
        role="manager",
        session_allowed_roots=tuple(roots),
        placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m1", home_dir=HOME, agents_dir=AGENTS, allow_full_fs=False, os="linux"),
    )


def _check(name, res, expect_allowed):
    ok = bool(res.allowed) == expect_allowed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: allowed={res.allowed}"
          + ("" if res.allowed else f" ({res.error})"))
    return ok


def main() -> int:
    results = []

    # 1. Under-home read works with NO session root (home branch).
    results.append(_check(
        "under-home read, no root",
        resolve_path_for_session(_ctx(), f"{HOME}/projects/x.py"),
        True,
    ))

    # 2. Out-of-home read DENIED without a session root.
    results.append(_check(
        "out-of-home read, no root → denied",
        resolve_path_for_session(_ctx(), "/srv/proj/main.py"),
        False,
    ))

    # 3. Out-of-home read ALLOWED with the matching session root.
    results.append(_check(
        "out-of-home read, with root → allowed",
        resolve_path_for_session(_ctx(["/srv/proj"]), "/srv/proj/main.py"),
        True,
    ))

    # 3b. ...and writes too.
    results.append(_check(
        "out-of-home write, with root → allowed",
        resolve_path_for_session(_ctx(["/srv/proj"]), "/srv/proj/main.py", writing=True),
        True,
    ))

    # 4. Denial wins: a .env under the session root is STILL protected.
    results.append(_check(
        "protected .env under root → denied",
        resolve_path_for_session(_ctx(["/srv/proj"]), "/srv/proj/.env"),
        False,
    ))

    # 5. A path OUTSIDE the session root (and out of home) → denied.
    results.append(_check(
        "out-of-root path → denied",
        resolve_path_for_session(_ctx(["/srv/proj"]), "/etc/passwd"),
        False,
    ))

    # 5b. `..` escape out of the root is collapsed before the check → denied.
    results.append(_check(
        "dotdot escape from root → denied",
        resolve_path_for_session(_ctx(["/srv/proj"]), "/srv/proj/../secret/x"),
        False,
    ))

    # 6. context_from_security threads session_allowed_roots off a duck-typed ctx.
    from types import SimpleNamespace
    sec = SimpleNamespace(
        role="manager", agent="my-agent",
        session_allowed_roots=("/srv/proj",),
        placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m1", agents_dir=AGENTS, home_dir=HOME, allow_full_fs=False),
    )
    built = context_from_security(sec)
    threaded = built.session_allowed_roots == ("/srv/proj",)
    print(f"  [{'PASS' if threaded else 'FAIL'}] context_from_security threads roots:"
          f" {built.session_allowed_roots}")
    results.append(threaded)

    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
