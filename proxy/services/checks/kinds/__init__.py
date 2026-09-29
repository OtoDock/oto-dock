"""The four kinds (CHECKS.md): each is ``run(check, target, changed, *,
round_no) -> Verdict``, registered by section name; the evaluator runs
the present sections in the fixed order schema → script → handler → judge
and stops at the first failure."""

from __future__ import annotations

import contextlib
from typing import Awaitable, Callable

RUNNERS: dict[str, Callable[..., Awaitable]] = {}


def register(section: str, runner: Callable[..., Awaitable]) -> None:
    RUNNERS[section] = runner


def install_all() -> None:
    from services.checks.kinds import schema_kind  # noqa: F401
    for mod in ("script_kind", "handler_kind", "judge_kind"):
        with contextlib.suppress(ImportError):
            __import__(f"services.checks.kinds.{mod}")
