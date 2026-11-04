"""The operation catalogue of a write tool.

One object per operation carries its keys, its handler and its help, so the
help an agent reads, the validation that answers it and the dispatch that
runs can never disagree: the catalogue IS the dispatch table.

``Catalog.resolve`` turns what an agent sent into the canonical operation:
the op found by any of its dispatch keys and spellings, every key checked by
name, aliases mapped, values coerced where the intent is plain (``"12"`` for
an int) and refused with the fix where it is not. The parent resolves a
whole batch before any work, so a typo in operation 59 costs no document
load; the worker resolves again (idempotent) and refuses a path the proxy
did not judge.
"""

from __future__ import annotations

import difflib
import math
import posixpath
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import colors
import units
from results import Code, OpCtx, OpError, ResolvedPath, short_repr


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()

T_STR = "str"
T_INT = "int"
T_FLOAT = "float"
T_BOOL = "bool"
T_LIST = "list"
T_DICT = "dict"
T_LENGTH = "length"
T_SPACING = "spacing"
T_COLOR = "color"
T_PATH = "path"
T_OUT_PATH = "out_path"
T_ANY = "any"
TYPES = frozenset({T_STR, T_INT, T_FLOAT, T_BOOL, T_LIST, T_DICT, T_LENGTH, T_SPACING, T_COLOR,
                   T_PATH, T_OUT_PATH, T_ANY})
PATH_TYPES = frozenset({T_PATH, T_OUT_PATH})
_SCALAR_TYPES = TYPES - {T_LIST, T_DICT}

HELP_OPS = frozenset({"help", "describe", "describe_ops", "catalog", "catalogue", "list_ops", "?"})
HELP_NAME_KEYS = ("name", "op_name", "operation_name", "topic")
HELP_NAME = "help"
DISPATCH_KEYS = ("op", "operation", "action", "type")
OP_KEY = "op"
IGNORED_KEYS = frozenset({"comment", "description", "note", "_comment"})
META_GROUP = "META"
_HELP_LINE = ("name? — the whole catalogue, or one operation's shape when name is given; "
              "never touches the file")

_TRUE = "true"
_FALSE = "false"
_BOOL_WORDS = {_TRUE: True, _FALSE: False}
_INT_DIGITS = re.compile(r"^\s*[+-]?\d+\s*$", re.ASCII)
_TYPE_WORDS = {
    T_STR: "text", T_INT: "a whole number", T_FLOAT: "a number", T_BOOL: "true or false",
    T_LIST: "a list", T_DICT: "an object", T_LENGTH: "a length (a number or e.g. '2.5cm')",
    T_SPACING: "a spacing (e.g. 1.5x or 12pt)", T_COLOR: "a colour (e.g. '#1F4E79')",
    T_PATH: "a file path", T_OUT_PATH: "an output path", T_ANY: "any value",
}


@dataclass(frozen=True)
class KeySpec:
    """One key of an operation. ``type`` is a tuple of type names (a union);
    ``unit`` is the unit of a bare number for a length (``in``, ``pt``,
    ``cm``...) or the kind of a bare number for a spacing (``pt`` or ``x``);
    ``fields`` checks a dict value's keys, ``item`` each dict of a list
    value, ``of`` each scalar of a list value. ``default`` is printed by
    help and never filled in."""

    name: str
    type: tuple[str, ...] = (T_ANY,)
    required: bool = False
    aliases: tuple[str, ...] = ()
    default: Any = MISSING
    unit: str | None = None
    fields: tuple[KeySpec, ...] | None = None
    item: tuple[KeySpec, ...] | None = None
    of: tuple[str, ...] | None = None
    help: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.type, str):
            object.__setattr__(self, "type", (self.type,))
        unknown = set(self.type) - TYPES
        if unknown:
            raise ValueError(f"key '{self.name}': unknown type(s) {sorted(unknown)}")
        if self.fields is not None and T_DICT not in self.type:
            raise ValueError(f"key '{self.name}': fields need the dict type")
        if (self.item is not None or self.of is not None) and T_LIST not in self.type:
            raise ValueError(f"key '{self.name}': item and of need the list type")
        if self.item is not None and self.of is not None:
            raise ValueError(f"key '{self.name}': give item or of, not both")
        if self.of is not None and set(self.of) - _SCALAR_TYPES:
            raise ValueError(f"key '{self.name}': of takes scalar types only")
        if self.required and self.default is not MISSING:
            raise ValueError(f"key '{self.name}': a required key has no default")


@dataclass(frozen=True)
class OpSpec:
    """One operation: its handler ``(job, ctx, op) -> None``, its keys, the
    help group it belongs to, the groups of keys of which at least one must
    be given (``one_of``), its other spellings, its help text, the examples
    the key-coverage contract runs (after ``setup``), whether it writes side
    files only, and whether its handler reads the operation whole (iterates
    it: safe, since a resolved operation holds declared keys only, and
    exempt from the contract's whole-operation rule)."""

    name: str
    handler: Callable[[Any, OpCtx, dict], None]
    keys: tuple[KeySpec, ...]
    group: str
    one_of: tuple[tuple[str, ...], ...] = ()
    aliases: tuple[str, ...] = ()
    note: str = ""
    detail: str = ""
    examples: tuple[dict, ...] = ()
    setup: tuple[dict, ...] = ()
    side_only: bool = False
    reads_whole_op: bool = False


def _did_you_mean(word: str, choices: Iterable[str]) -> list[str]:
    return difflib.get_close_matches(word, list(choices), n=3, cutoff=0.6)


def _suggestion(word: str, choices: Iterable[str] | Mapping[str, str]) -> str:
    """"Did you mean ...?" over ``choices``; a mapping of spellings to
    canonical names suggests each canonical name once."""
    close = _did_you_mean(word, choices)
    if isinstance(choices, Mapping):
        close = list(dict.fromkeys(choices[c] for c in close))
    if not close:
        return ""
    return "Did you mean " + " or ".join(f"'{c}'" for c in close) + "?"


def _fmt_default(value: Any) -> str:
    if isinstance(value, bool):
        return _TRUE if value else _FALSE
    if isinstance(value, str):
        return f"'{value}'"
    return repr(value)


def _types_text(types: tuple[str, ...]) -> str:
    return " or ".join(_TYPE_WORDS[t] for t in types)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite_number(value: Any) -> bool:
    """A number a float can hold: a 400-digit integer is not one."""
    try:
        return _is_number(value) and math.isfinite(value)
    except OverflowError:
        return False


def _short_repr(value: Any) -> str:
    return short_repr(value)


class Catalog:
    """A write tool's operations, their validation, dispatch and help."""

    def __init__(self, tool: str, ops: Iterable[OpSpec], *, key_aliases: Mapping[str, str] | None = None,
                 dispatch_keys: tuple[str, ...] = DISPATCH_KEYS,
                 ignored_keys: frozenset[str] = IGNORED_KEYS, notes: str = "",
                 one_line_help: bool = False, color_argb: bool = False,
                 color_theme: bool = True) -> None:
        self.tool = tool
        self.ops: dict[str, OpSpec] = {}
        self.key_aliases = dict(key_aliases or {})
        self.dispatch_keys = tuple(dispatch_keys)
        self.ignored_keys = frozenset(ignored_keys)
        self.notes = notes
        self.one_line_help = one_line_help
        self.color_argb = color_argb
        self.color_theme = color_theme
        self._lookup: dict[str, str] = {}
        self._key_maps: dict[str, dict[str, str]] = {}
        self._key_specs: dict[str, dict[str, KeySpec]] = {}
        help_spellings = {h.casefold() for h in HELP_OPS}
        for spec in ops:
            if spec.name in self.ops:
                raise ValueError(f"{tool}: two operations named '{spec.name}'")
            self.ops[spec.name] = spec
        for spec in self.ops.values():
            for spelling in (spec.name, *spec.aliases):
                folded = spelling.casefold()
                if folded in help_spellings:
                    raise ValueError(f"{tool}: '{spelling}' is a help spelling, not an operation name")
                if folded in self._lookup and self._lookup[folded] != spec.name:
                    raise ValueError(f"{tool}: '{spelling}' names both '{self._lookup[folded]}' and '{spec.name}'")
                if folded in self._lookup:
                    raise ValueError(f"{tool}: '{spelling}' is given twice for '{spec.name}'")
                self._lookup[folded] = spec.name
            self._key_maps[spec.name], self._key_specs[spec.name] = self._build_key_map(spec)
        for spec in self.ops.values():
            for example in spec.examples:
                try:
                    self.resolve(example)
                except OpError as exc:
                    raise ValueError(f"{tool}: an example of '{spec.name}' does not resolve: {exc.message}") from None

    # ----------------------------------------------------------------- keys

    def _build_key_map(self, spec: OpSpec) -> tuple[dict[str, str], dict[str, KeySpec]]:
        mapping: dict[str, str] = {}
        specs: dict[str, KeySpec] = {}
        for key in spec.keys:
            if key.name in specs:
                raise ValueError(f"{self.tool}: '{spec.name}' declares '{key.name}' twice")
            specs[key.name] = key
            mapping[key.name] = key.name
        for key in spec.keys:
            for alias in key.aliases:
                if alias in mapping:
                    raise ValueError(f"{self.tool}: '{spec.name}': alias '{alias}' collides with a key or alias")
                mapping[alias] = key.name
        for alias, target in self.key_aliases.items():
            if target in specs:
                if alias in mapping and mapping[alias] != target:
                    raise ValueError(f"{self.tool}: '{spec.name}': the format alias '{alias}' collides with a key")
                mapping.setdefault(alias, target)
        declared = {m for group in spec.one_of for m in group}
        missing = declared - set(specs)
        if missing:
            raise ValueError(f"{self.tool}: '{spec.name}': one_of names undeclared keys {sorted(missing)}")
        return mapping, specs

    def key_spec(self, op_name: str, key: str) -> KeySpec:
        return self._key_specs[op_name][key]

    def declares(self, op_name: str, spelling: str) -> bool:
        return spelling in self._key_maps[op_name]

    # ------------------------------------------------------------- dispatch

    def lookup(self, name: Any) -> str | None:
        """The canonical name an op spelling stands for, ``HELP_NAME`` for a
        help spelling, else ``None``."""
        if not isinstance(name, str):
            return None
        folded = name.strip().casefold()
        if folded in HELP_OPS:
            return HELP_NAME
        return self._lookup.get(folded)

    def _naming(self, raw: dict) -> tuple[str, str]:
        """(the dispatch key that names the op, the canonical name)."""
        skipped: list[tuple[str, Any]] = []
        for position, key in enumerate(self.dispatch_keys):
            value = raw.get(key)
            if value is None or value == "":
                continue
            name = self.lookup(value)
            if name is None:
                skipped.append((key, value))
                continue
            if name != HELP_NAME:
                for early_key, early_value in skipped:
                    if not self.declares(name, early_key):
                        raise self._unknown_op(early_value)
                if position:
                    self._refuse_ambiguous(raw, key, name, position)
            return key, name
        if not skipped:
            raise OpError(Code.UNKNOWN_OP, "the item names no operation",
                          hint='Give the operation\'s name under "op", e.g. {"op": "<name>", ...}.')
        raise self._unknown_op(skipped[0][1])

    def _refuse_ambiguous(self, raw: dict, key: str, name: str, position: int) -> None:
        """Without the first dispatch key, two later keys naming two
        different operations leave the intent open: refuse rather than
        guess."""
        for later in self.dispatch_keys[position + 1:]:
            other = self.lookup(raw.get(later))
            if other not in (None, HELP_NAME, name):
                raise OpError(Code.UNKNOWN_OP, f"'{key}' names {name} and '{later}' names {other}",
                              hint=f"Send the operation's name under '{self.dispatch_keys[0]}'.", op=name)

    def _unknown_op(self, value: Any) -> OpError:
        text = str(value).strip()
        hint = _suggestion(text.casefold(), self._lookup)
        hint = (hint + " " if hint else "") + f"Valid operations: {', '.join(self.ops)}."
        return OpError(Code.UNKNOWN_OP, f"unknown operation '{text}'", hint=hint)

    def is_help(self, raw: Any) -> bool:
        if not isinstance(raw, dict):
            return False
        try:
            return self._naming(raw)[1] == HELP_NAME
        except OpError:
            return False

    # -------------------------------------------------------------- resolve

    def resolve(self, raw: Any, *, require_resolved_paths: bool = False) -> tuple[OpSpec, dict, list[str]]:
        """The canonical operation for ``raw``: ``(spec, {"op": name, **keys},
        notes)``, or ``OpError`` naming every problem of the op and its fix."""
        if not isinstance(raw, dict):
            raise OpError(Code.BAD_ITEM, "this item is not an operation object",
                          hint='Send {"op": "<name>", ...keys}.')
        naming_key, name = self._naming(raw)
        if name == HELP_NAME:
            raise OpError(Code.UNKNOWN_OP, "a help request is not an operation", op=HELP_NAME)
        spec = self.ops[name]
        key_map = self._key_maps[name]
        notes: list[str] = []
        items: dict[str, Any] = {}
        for key, value in raw.items():
            if key == naming_key:
                continue
            if key in self.dispatch_keys:
                if self.lookup(value) == name:
                    continue
                if key not in key_map:
                    notes.append(f"'{key}' ignored: the operation is named by '{naming_key}'")
                    continue
            if key in self.ignored_keys and key not in key_map:
                notes.append(f"'{key}' ignored: it is prose, not a key of {name}")
                continue
            items[key] = value
        unknown = [k for k in items if k not in key_map]
        if unknown:
            raise self._unknown_keys(spec, unknown, naming_key)
        try:
            keys = self._canonical_keys(spec, items, key_map, notes, location="",
                                        require_resolved_paths=require_resolved_paths)
            self._check_required(spec.keys, spec.one_of, keys, location="", naming_key=naming_key,
                                 op_name=name)
        except OpError as exc:
            exc.op = exc.op or name
            raise
        return spec, {OP_KEY: name, **keys}, notes

    def _unknown_keys(self, spec: OpSpec, unknown: list[str], naming_key: str) -> OpError:
        accepted = [k.name for k in spec.keys]
        parts = []
        for key in unknown:
            close = _suggestion(key, self._key_maps[spec.name])
            parts.append(f"'{key}'" + (f" ({close})" if close else ""))
        message = f"unknown key{'s' if len(unknown) > 1 else ''} {', '.join(parts)}"
        hint = f"Accepted keys: {', '.join(accepted) or '(none)'}."
        return OpError(Code.UNKNOWN_KEY, message, hint=hint, op=spec.name)

    def _canonical_keys(self, spec_or_none, items: dict, key_map: Mapping[str, str], notes: list[str], *,
                        location: str, require_resolved_paths: bool,
                        key_specs: Mapping[str, KeySpec] | None = None) -> dict[str, Any]:
        specs = key_specs if key_specs is not None else self._key_specs[spec_or_none.name]
        present = {k: v for k, v in items.items() if v is not None}
        out: dict[str, Any] = {}
        for key, value in present.items():
            if key_map[key] == key:
                out[key] = value
        for key, value in present.items():
            canonical = key_map[key]
            if canonical == key:
                continue
            if canonical in out:
                notes.append(f"'{location}{key}' ignored: '{location}{canonical}' was given too")
                continue
            out[canonical] = value
        ordered = {}
        for key in present:
            canonical = key_map[key]
            if canonical in out and canonical not in ordered:
                ordered[canonical] = self._check_value(
                    out[canonical], specs[canonical], location=f"{location}{canonical}",
                    require_resolved_paths=require_resolved_paths, notes=notes)
        return ordered

    def _check_required(self, keys: tuple[KeySpec, ...], one_of: tuple[tuple[str, ...], ...], given: dict, *,
                        location: str, naming_key: str = "", op_name: str = "") -> None:
        missing = [f"{location}{k.name}" for k in keys if k.required and k.name not in given]
        unmet = [" or ".join(f"{location}{m}" for m in group) for group in one_of
                 if not any(m in given for m in group)]
        if missing:
            hint = ""
            if naming_key and naming_key in {k.name for k in keys} and naming_key not in given:
                hint = (f"'{naming_key}' named the operation; send the operation's name under "
                        f"'{OP_KEY}' when it has its own '{naming_key}' key.")
            raise OpError(Code.MISSING_KEY, f"missing required key{'s' if len(missing) > 1 else ''} "
                          f"{', '.join(missing)}", hint=hint, op=op_name)
        if unmet:
            raise OpError(Code.ONE_OF, "give " + "; and ".join(unmet), op=op_name)

    # ---------------------------------------------------------------- types

    def _check_value(self, value: Any, key: KeySpec, *, location: str, require_resolved_paths: bool,
                     notes: list[str]) -> Any:
        first_problem: OpError | None = None
        for t in key.type:
            accepted, result, problem = self._accept(value, t, key, location=location,
                                                     require_resolved_paths=require_resolved_paths,
                                                     notes=notes)
            if accepted:
                return result
            first_problem = first_problem or problem
        for t in key.type:
            coerced = self._coerce(value, t)
            if coerced is not MISSING:
                return coerced
        if first_problem is not None:
            raise first_problem
        raise self._bad_type(value, key.type, location)

    def _bad_type(self, value: Any, types: tuple[str, ...], location: str) -> OpError:
        hint = ""
        if isinstance(value, str) and units.comma_decimal(value):
            hint = "Use a dot as the decimal separator."
        shown = _short_repr(value)
        return OpError(Code.BAD_TYPE, f"'{location}' expects {_types_text(types)}, got {shown}",
                       hint=hint, location="")

    def _accept(self, value: Any, t: str, key: KeySpec, *, location: str, require_resolved_paths: bool,
                notes: list[str]) -> tuple[bool, Any, OpError | None]:
        """(accepted as it is, the value to keep, a validation problem)."""
        try:
            if t == T_ANY:
                return True, value, None
            if t == T_STR:
                return isinstance(value, str), value, None
            if t == T_INT:
                return isinstance(value, int) and not isinstance(value, bool), value, None
            if t == T_FLOAT:
                ok = _finite_number(value)
                return ok, value, None
            if t == T_BOOL:
                return isinstance(value, bool), value, None
            if t in PATH_TYPES:
                if not isinstance(value, str):
                    return False, value, None
                if require_resolved_paths and not isinstance(value, ResolvedPath):
                    return False, value, self._unresolved(location)
                return True, value, None
            if t == T_LENGTH:
                if not (_is_number(value) or isinstance(value, str)):
                    return False, value, None
                units.check_length(value, default=key.unit or units.POINTS)
                return True, value, None
            if t == T_SPACING:
                if not (_is_number(value) or isinstance(value, str)):
                    return False, value, None
                units.parse_spacing(value, default=key.unit or units.POINTS)
                return True, value, None
            if t == T_COLOR:
                if not isinstance(value, (str, list, dict)):
                    return False, value, None
                colors.parse_color(value, argb=self.color_argb, allow_theme=self.color_theme)
                return True, value, None
            if t == T_DICT:
                if not isinstance(value, dict):
                    return False, value, None
                if key.fields is None:
                    return True, value, None
                return True, self._nested(value, key.fields, f"{location}.", require_resolved_paths,
                                          notes), None
            if t == T_LIST:
                if not isinstance(value, list):
                    return False, value, None
                return True, self._list(value, key, location, require_resolved_paths, notes), None
        except OpError as exc:
            if not exc.location and exc.code is Code.BAD_VALUE:
                exc.message = f"'{location}': {exc.message}"
            return False, value, exc
        return False, value, None

    def _unresolved(self, location: str) -> OpError:
        return OpError(Code.BAD_PATH, f"the path in '{location}' was not resolved before the worker",
                       hint="This is a fault in the tool, not in the request.")

    def _nested(self, value: dict, fields: tuple[KeySpec, ...], prefix: str, require_resolved_paths: bool,
                notes: list[str]) -> dict:
        specs = {k.name: k for k in fields}
        key_map = {k.name: k.name for k in fields}
        for k in fields:
            for alias in k.aliases:
                key_map.setdefault(alias, k.name)
        unknown = [k for k in value if k not in key_map]
        if unknown:
            parts = []
            for key in unknown:
                close = _suggestion(key, key_map)
                parts.append(f"'{prefix}{key}'" + (f" ({close})" if close else ""))
            raise OpError(Code.UNKNOWN_KEY, f"unknown key{'s' if len(unknown) > 1 else ''} {', '.join(parts)}",
                          hint=f"Accepted in {prefix.rstrip('.')}: {', '.join(specs)}.")
        keys = self._canonical_keys(None, value, key_map, notes, location=prefix,
                                    require_resolved_paths=require_resolved_paths, key_specs=specs)
        self._check_required(fields, (), keys, location=prefix)
        return keys

    def _list(self, value: list, key: KeySpec, location: str, require_resolved_paths: bool,
              notes: list[str]) -> list:
        if key.item is not None:
            out = []
            for i, element in enumerate(value):
                if not isinstance(element, dict):
                    raise OpError(Code.BAD_TYPE, f"'{location}[{i}]' expects an object, got {element!r}")
                out.append(self._nested(element, key.item, f"{location}[{i}].", require_resolved_paths, notes))
            return out
        if key.of is not None:
            element_spec = KeySpec(name=location, type=key.of, unit=key.unit)
            return [self._check_value(element, element_spec, location=f"{location}[{i}]",
                                      require_resolved_paths=require_resolved_paths, notes=notes)
                    for i, element in enumerate(value)]
        return list(value)

    def _coerce(self, value: Any, t: str) -> Any:
        """The value coerced to ``t`` when the intent is plain, else MISSING."""
        if isinstance(value, bool):
            return MISSING
        if t == T_INT:
            if isinstance(value, float) and math.isfinite(value) and value.is_integer():
                return int(value)
            if isinstance(value, str) and units.is_number_text(value):
                if _INT_DIGITS.match(value):
                    try:
                        return int(value)
                    except ValueError:
                        return MISSING
                number = float(value)
                if math.isfinite(number) and number.is_integer():
                    return int(number)
            return MISSING
        if t == T_FLOAT:
            if isinstance(value, str) and units.is_number_text(value):
                number = float(value)
                return number if math.isfinite(number) else MISSING
            return MISSING
        if t == T_BOOL:
            if isinstance(value, str) and value.strip().casefold() in _BOOL_WORDS:
                return _BOOL_WORDS[value.strip().casefold()]
            if isinstance(value, int) and value in (0, 1):
                return bool(value)
            return MISSING
        if t == T_STR and _finite_number(value):
            try:
                return str(value)
            except ValueError:
                return MISSING
        return MISSING

    # ------------------------------------------------------------- batches

    def resolve_batch(self, ops: list[tuple[int, dict]]) -> tuple[list[tuple[int, dict]], list[OpError],
                                                                    dict[int, list[str]]]:
        """Every op resolved: the canonical ops, every problem (each with
        the caller's index) and the notes per caller index."""
        resolved: list[tuple[int, dict]] = []
        problems: list[OpError] = []
        notes: dict[int, list[str]] = {}
        for index, raw in ops:
            try:
                _, op, op_notes = self.resolve(raw)
            except OpError as exc:
                exc.index = index
                if not exc.op and exc.code is not Code.UNKNOWN_OP and isinstance(raw, dict):
                    exc.op = self._raw_name(raw)
                problems.append(exc)
                continue
            resolved.append((index, op))
            if op_notes:
                notes[index] = op_notes
        return resolved, problems, notes

    def _raw_name(self, raw: dict) -> str:
        for key in self.dispatch_keys:
            if isinstance(raw.get(key), str) and raw[key].strip():
                return raw[key].strip()
        return ""

    async def preresolve(self, ops: list[tuple[int, dict]], *, main_path: str = "") -> tuple[
            list[tuple[int, dict]], list[OpError]]:
        """Every ``path`` and ``out_path`` value of canonical ``ops`` resolved
        through the proxy (``shared._resolve_path``) and marked; a failure
        fails its op with the proxy's reason."""
        import shared

        cache: dict[tuple[str, bool], ResolvedPath] = {}
        main = _normal(main_path)
        out: list[tuple[int, dict]] = []
        problems: list[OpError] = []
        for index, op in ops:
            spec = self.ops[op[OP_KEY]]
            try:
                keys = {k: v for k, v in op.items() if k != OP_KEY}
                resolved = await self._resolve_paths(keys, {k.name: k for k in spec.keys}, cache, shared, main)
            except OpError as exc:
                exc.index = index
                exc.op = op[OP_KEY]
                problems.append(exc)
                continue
            out.append((index, {OP_KEY: op[OP_KEY], **resolved}))
        return out, problems

    async def _resolve_paths(self, values: dict, specs: Mapping[str, KeySpec], cache: dict, shared: Any,
                             main: str) -> dict:
        out = dict(values)
        for name, value in values.items():
            key = specs.get(name)
            if key is None:
                continue
            if isinstance(value, str) and set(key.type) & PATH_TYPES:
                out[name] = await self._resolve_one(value, T_OUT_PATH in key.type, cache, shared, main)
            elif isinstance(value, dict) and key.fields is not None:
                out[name] = await self._resolve_paths(value, {k.name: k for k in key.fields}, cache, shared,
                                                      main)
            elif isinstance(value, list) and key.item is not None:
                fields = {k.name: k for k in key.item}
                out[name] = [await self._resolve_paths(v, fields, cache, shared, main)
                             if isinstance(v, dict) else v for v in value]
            elif isinstance(value, list) and key.of is not None and set(key.of) & PATH_TYPES:
                writing = T_OUT_PATH in key.of
                out[name] = [await self._resolve_one(v, writing, cache, shared, main)
                             if isinstance(v, str) else v for v in value]
        return out

    async def _resolve_one(self, value: str, writing: bool, cache: dict, shared: Any, main: str) -> ResolvedPath:
        cached = cache.get((value, writing))
        if cached is None:
            try:
                cached = ResolvedPath(await shared._resolve_path(value, writing=writing))
            except Exception as exc:  # noqa: BLE001  # the proxy's refusal, whatever its type, fails this op only
                raise OpError(Code.BAD_PATH, str(exc) or exc.__class__.__name__,
                              hint="Give a path inside this agent's folders.") from None
            cache[(value, writing)] = cached
        if writing and main and _normal(cached) == main:
            raise OpError(Code.BAD_PATH, "a side output cannot overwrite the file the batch edits",
                          hint="Choose another output path.")
        return cached

    def path_values(self, ops: list[tuple[int, dict]], *, writing: bool) -> list[str]:
        """Every resolved output path (``writing``) or input path of the
        canonical ``ops``."""
        found: list[str] = []
        wanted = T_OUT_PATH if writing else T_PATH

        def walk(values: dict, specs: Mapping[str, KeySpec]) -> None:
            for name, value in values.items():
                key = specs.get(name)
                if key is None:
                    continue
                if isinstance(value, str) and wanted in key.type:
                    found.append(value)
                elif isinstance(value, dict) and key.fields is not None:
                    walk(value, {k.name: k for k in key.fields})
                elif isinstance(value, list) and key.item is not None:
                    for v in value:
                        if isinstance(v, dict):
                            walk(v, {k.name: k for k in key.item})
                elif isinstance(value, list) and key.of is not None and wanted in key.of:
                    found.extend(v for v in value if isinstance(v, str))

        for _, op in ops:
            spec = self.ops.get(op.get(OP_KEY, ""))
            if spec is not None:
                walk(op, {k.name: k for k in spec.keys})
        return found

    # ----------------------------------------------------------------- help

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for spec in self.ops.values():
            out.setdefault(spec.group, []).append(spec.name)
        return out

    def keys_line(self, spec: OpSpec) -> str:
        one_of_members = {m for group in spec.one_of for m in group}
        parts = []
        for key in spec.keys:
            name = key.name if key.required or key.name in one_of_members else f"{key.name}?"
            extras = []
            if key.help:
                extras.append(key.help)
            if key.unit and key.unit not in key.help:
                extras.append(key.unit)
            if key.default is not MISSING:
                extras.append(f"default {_fmt_default(key.default)}")
            parts.append(name + (f" ({'; '.join(extras)})" if extras else ""))
        line = ", ".join(parts) if parts else "(no keys)"
        if spec.one_of:
            line += " — one of " + "; ".join(" | ".join(group) for group in spec.one_of) + " required"
        return line

    def help(self, op_name: str | None = None) -> str:
        """The whole catalogue, or one operation's shape."""
        if op_name:
            raw = str(op_name).strip()
            name = self.lookup(raw)
            if name == HELP_NAME:
                return f"{HELP_NAME}: {_HELP_LINE}"
            spec = self.ops.get(name or "")
            if spec is None:
                close = _suggestion(raw.casefold(), self._lookup)
                return (f"No operation named '{raw}'." + (f" {close}" if close else "")
                        + f" Valid operations: {', '.join(self.ops)}")
            lines = [f"{spec.name}: {self.keys_line(spec)}"]
            if not self.one_line_help:
                lines += [f"  {text}" for text in (spec.note, spec.detail) if text]
                lines += [f"  example: {_json(example)}" for example in spec.examples]
                if spec.aliases:
                    lines.append(f"  also accepted as: {', '.join(spec.aliases)}")
            return "\n".join(lines)
        lines = [
            f'{self.tool} operations: each is {{"op": "<name>", ...keys}}; ? marks an optional key. '
            'Unknown operations and keys are refused. {"op": "help", "name": "<op>"} shows one in full.'
        ]
        for group, names in self.groups().items():
            lines.append(f"\n{group}")
            for name in names:
                spec = self.ops[name]
                line = f"  {name}: {self.keys_line(spec)}"
                if spec.note:
                    line += f" — {spec.note}"
                lines.append(line)
        lines.append(f"\n{META_GROUP}")
        lines.append(f"  {HELP_NAME}: {_HELP_LINE}")
        if self.notes:
            lines.append("\n" + self.notes)
        return "\n".join(lines)

    def answer_help(self, help_ops: list[dict]) -> str:
        """The answers to the help items of one call, in order."""
        answers = []
        for op in help_ops:
            name = next((str(op[k]).strip() for k in HELP_NAME_KEYS if op.get(k)), "")
            answers.append(self.help(name or None))
        return "\n\n".join(answers)


def _normal(path: str) -> str:
    return posixpath.normpath(path) if path else ""


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
