"""The registry of what the rig checks, and the run's shared state.

A scenario is a function of the run's context, in a file under ``scenarios/``,
``live/`` or ``claims/``::

    @scenario("queue", needs=("turn-claude",), about="A message sent during a turn waits its turn.")
    def queue(ctx):
        ...
        check(rows_in_order(rows), "the rows are in the order sent")

``needs`` skips it when a scenario it builds on did not pass. ``known`` says a
product bug is known here and ``known_match`` is the text of that failure: a failure
that carries the text reports ``known``, any other failure stays ``fail``, and a pass
reports ``fixed``, so the line gets taken out. ``changes_with`` names the planned
change that will change the expectation. ``Inconclusive`` is raised when the
platform did its part and a real model did not do what it was asked: reported apart
from a failure.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

PASS, FAIL, SKIPPED, KNOWN, FIXED = "pass", "fail", "skipped", "known", "fixed"
INCONCLUSIVE, NOT_SUPPORTED, CARRIED = "inconclusive", "not-supported", "carried"


class CheckFailed(Exception):
    """A structural check did not hold."""


class Inconclusive(Exception):
    """The platform answered correctly, the model did not do what was asked."""


class NotSupported(Exception):
    """The engine does not declare this behaviour."""


def check(condition, what: str) -> None:
    if not condition:
        raise CheckFailed(what)


@dataclass
class Scenario:
    name: str
    func: Callable
    layer: str = "scripted"
    needs: tuple[str, ...] = ()
    known: str = ""
    known_match: str = ""
    changes_with: str = ""
    about: str = ""
    needs_rig: bool = False
    retries: int = 0
    machine: bool = False


@dataclass
class Registry:
    scenarios: list[Scenario] = field(default_factory=list)

    def add(self, item: Scenario) -> None:
        if any(s.name == item.name and s.layer == item.layer for s in self.scenarios):
            raise ValueError(f"two scenarios named {item.name}")
        if bool(item.known) != bool(item.known_match):
            raise ValueError(f"{item.name}: a known failure needs its text, and the text needs the bug it is")
        self.scenarios.append(item)

    def layer(self, name: str) -> list[Scenario]:
        return [s for s in self.scenarios if s.layer == name]


REGISTRY = Registry()


def scenario(name: str, *, layer: str = "scripted", needs: tuple[str, ...] = (), known: str = "",
             known_match: str = "", changes_with: str = "", about: str = "", needs_rig: bool = False,
             retries: int = 0, machine: bool = False):
    """Register the function as a scenario. ``needs_rig``: it installs or
    restarts something on this host, so it runs only where the rig's marker is.
    ``machine``: it runs on the paired machine, which the runner finds online first."""
    def wrap(func: Callable) -> Callable:
        REGISTRY.add(Scenario(name, func, layer, needs, known, known_match, changes_with,
                              about or (func.__doc__ or "").strip().split("\n")[0], needs_rig, retries, machine))
        return func
    return wrap


# What a Claude Code terminal says when it is not signed in.
NOT_LOGGED_IN = "Not logged in"
