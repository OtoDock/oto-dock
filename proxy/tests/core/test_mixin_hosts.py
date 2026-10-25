"""The mixin families' host Protocols stay honest.

Four composed classes are built from mixins, and each mixin reads its
siblings' state through a ``Protocol`` that exists for the type checker
only (``DashboardHost``, ``RemoteLayerHost``, ``SatelliteManagerHost``,
``PumpHost``). Three rules keep such a Protocol a description of the
composed class rather than a second source of truth:

- every attribute it declares is assigned somewhere in the family (a
  declaration nothing assigns would hide an ``AttributeError``), and every
  method and property it declares is defined there; so is every attribute
  a mixin declares in its own class body without a value (the checker
  trusts those the same way);
- a mixin takes the Protocol as its base only under ``TYPE_CHECKING`` (at
  run time the base is ``object``);
- the composed class's run-time MRO holds no ``Protocol``.
"""

import ast
import importlib
import importlib.util
import sys
import typing
from pathlib import Path

import pytest

# (composed class module, composed class, host module, host Protocol)
FAMILIES = [
    ("ws.dashboard", "DashboardConnection", "ws.dashboard_host", "DashboardHost"),
    ("core.remote.remote_execution", "RemoteExecutionLayer",
     "core.remote.remote_host", "RemoteLayerHost"),
    ("core.remote.satellite_connection", "SatelliteConnectionManager",
     "core.remote.satellite_host", "SatelliteManagerHost"),
    ("core.events.stream_pump", "ChatStreamPump", "core.events.pump_host", "PumpHost"),
]
_IDS = [f[1] for f in FAMILIES]


def _class_node(path: str, name: str) -> ast.ClassDef:
    tree = ast.parse(Path(path).read_text())
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)


def _source(cls: type) -> str:
    """The file a class is defined in, "" for a built-in."""
    return getattr(sys.modules.get(cls.__module__), "__file__", None) or ""


def _family(mod: str, name: str) -> list[type]:
    """The composed class and every class of its MRO defined in this tree."""
    cls = getattr(importlib.import_module(mod), name)
    root = str(Path(__file__).resolve().parents[2])
    return [c for c in cls.__mro__
            if _source(c).startswith(root + "/") and "/venv/" not in _source(c)]


def _self_targets(node: ast.AST) -> set[str]:
    out: set[str] = set()
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
            and node.value.id == "self":
        out.add(node.attr)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            out |= _self_targets(elt)
    elif isinstance(node, ast.Starred):
        out |= _self_targets(node.value)
    return out


def _provided(family: list[type]) -> tuple[set[str], set[str]]:
    """What the family assigns (``self.x = ...`` anywhere in its classes, a
    class-level assignment) and what it defines (methods, properties)."""
    assigned: set[str] = set()
    defined: set[str] = set()
    for cls in family:
        node = _class_node(_source(cls), cls.__name__)
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(stmt.name)
            elif isinstance(stmt, ast.Assign):
                assigned |= {t.id for t in stmt.targets if isinstance(t, ast.Name)}
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None \
                    and isinstance(stmt.target, ast.Name):
                assigned.add(stmt.target.id)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                for t in sub.targets:
                    assigned |= _self_targets(t)
            elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)) and (
                    not isinstance(sub, ast.AnnAssign) or sub.value is not None):
                assigned |= _self_targets(sub.target)
            elif isinstance(sub, (ast.For, ast.AsyncFor)):
                assigned |= _self_targets(sub.target)
            elif isinstance(sub, (ast.With, ast.AsyncWith)):
                for item in sub.items:
                    if item.optional_vars is not None:
                        assigned |= _self_targets(item.optional_vars)
    return assigned, defined


def _bare_declarations(family: list[type]) -> dict[str, set[str]]:
    """The attributes each class of the family declares in its body with no
    value (``x: "T"``), by class name."""
    out: dict[str, set[str]] = {}
    for cls in family:
        node = _class_node(_source(cls), cls.__name__)
        names = {s.target.id for s in node.body
                 if isinstance(s, ast.AnnAssign) and s.value is None
                 and isinstance(s.target, ast.Name)}
        if names:
            out[cls.__name__] = names
    return out


def _declared(host_mod: str, host: str) -> tuple[set[str], set[str]]:
    """The Protocol's attributes and its methods and properties."""
    path = importlib.util.find_spec(host_mod).origin
    node = _class_node(path, host)
    attrs = {s.target.id for s in node.body
             if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}
    members = {s.name for s in node.body
               if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return attrs, members


@pytest.mark.parametrize("mod,name,host_mod,host", FAMILIES, ids=_IDS)
def test_every_declared_attribute_is_assigned_in_the_family(mod, name, host_mod, host):
    attrs, _ = _declared(host_mod, host)
    family = _family(mod, name)
    assigned, defined = _provided(family)
    assert attrs, f"{host} declares no attribute: the parse found the wrong class"
    assert sorted(attrs - assigned - defined) == []
    unassigned = {cls: sorted(names - assigned - defined)
                  for cls, names in _bare_declarations(family).items()}
    assert {cls: names for cls, names in unassigned.items() if names} == {}


@pytest.mark.parametrize("mod,name,host_mod,host", FAMILIES, ids=_IDS)
def test_every_declared_method_is_defined_in_the_family(mod, name, host_mod, host):
    _, members = _declared(host_mod, host)
    _, defined = _provided(_family(mod, name))
    assert sorted(members - defined) == []


@pytest.mark.parametrize("mod,name,host_mod,host", FAMILIES, ids=_IDS)
def test_the_host_is_imported_for_the_type_checker_only(mod, name, host_mod, host):
    """No module of the family imports the Protocol outside an
    ``if TYPE_CHECKING:`` block."""
    for cls in _family(mod, name):
        tree = ast.parse(Path(_source(cls)).read_text())
        for stmt in tree.body:
            if isinstance(stmt, ast.ImportFrom) and stmt.module == host_mod:
                assert host not in {a.name for a in stmt.names}, (
                    f"{cls.__module__} imports {host} at run time")


@pytest.mark.parametrize("mod,name,host_mod,host", FAMILIES, ids=_IDS)
def test_no_protocol_in_the_run_time_mro(mod, name, host_mod, host):
    cls = getattr(importlib.import_module(mod), name)
    assert typing.Protocol not in cls.__mro__
    assert not [c.__name__ for c in cls.__mro__ if getattr(c, "_is_protocol", False)]
    family = _family(mod, name)
    assert len(family) > 1, f"{name} has no mixin: the family list is stale"
