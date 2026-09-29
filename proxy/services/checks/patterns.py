"""Every pattern a person writes into a check runs through RE2
(``google-re2``): a condition's ``commands``, its ``globs`` (compiled to a
pattern) and a schema section's ``pattern`` / ``patternProperties``. RE2
matches in linear time, so no pattern can stall the event loop the
conditions and the schema kind run on; it has no backreferences and no
lookaround, which a check refuses at save with RE2's own words.
"""

from __future__ import annotations

import functools
import logging

import re2

logger = logging.getLogger("checks")

# A pattern (a glob) longer than this is refused at save (RE2 is linear in
# the text AND the pattern; a condition runs on every turn).
MAX_PATTERN_CHARS = 512
MAX_GLOB_CHARS = 256

_OPTIONS = re2.Options()
_OPTIONS.log_errors = False


class PatternError(ValueError):
    """A pattern RE2 does not take; the message says why."""


def _why(e: Exception) -> str:
    arg = e.args[0] if e.args else ""
    text = arg.decode("utf-8", "replace") if isinstance(arg, bytes) else str(arg)
    return text or "not a pattern RE2 accepts"


@functools.lru_cache(maxsize=512)
def compile(pattern: str):  # noqa: A001 — the re-style name
    try:
        return re2.compile(pattern, options=_OPTIONS)
    except re2.error as e:
        raise PatternError(_why(e)) from None


def check(pattern: str) -> None:
    """``PatternError`` for a pattern a check may not carry."""
    if not isinstance(pattern, str):
        raise PatternError("a pattern is text")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise PatternError(f"longer than {MAX_PATTERN_CHARS} characters")
    compile(pattern)


def check_glob(glob: str) -> None:
    """``PatternError`` for a glob a check may not carry (its RE2 form is
    what runs)."""
    from services.checks import classify
    if len(glob) > MAX_GLOB_CHARS:
        raise PatternError(f"longer than {MAX_GLOB_CHARS} characters")
    compile(classify.glob_pattern(glob))


def search(pattern: str, text: str) -> bool:
    """RE2 ``search``; a pattern RE2 refuses matches nothing (a saved
    document never carries one: the validator refused it)."""
    try:
        return compile(pattern).search(text) is not None
    except PatternError:
        logger.warning("checks: a pattern RE2 refuses reached a match: %r", pattern[:80])
        return False


# ── a schema section's patterns ────────────────────────────────────────────

# Keywords whose value is data, never a subschema: a "pattern" key inside
# them is an instance's field, not a pattern.
_DATA_KEYWORDS = frozenset({"const", "enum", "default", "examples"})


def schema_patterns(schema) -> list[str]:
    """Every ``pattern`` and ``patternProperties`` key in a JSON schema."""
    out: list[str] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for x in node:
                walk(x)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in _DATA_KEYWORDS:
                continue
            if key == "pattern" and isinstance(value, str):
                out.append(value)
            elif key == "patternProperties" and isinstance(value, dict):
                out.extend(k for k in value if isinstance(k, str))
                walk(list(value.values()))
            else:
                walk(value)
    walk(schema)
    return out


def _uses(schema, keyword: str) -> bool:
    if isinstance(schema, list):
        return any(_uses(x, keyword) for x in schema)
    if not isinstance(schema, dict):
        return False
    return any(k == keyword or (k not in _DATA_KEYWORDS and _uses(v, keyword))
               for k, v in schema.items())


def check_schema(schema: dict) -> None:
    """``PatternError`` for a schema whose patterns RE2 refuses, or one
    that pairs ``unevaluatedProperties`` with ``patternProperties`` (the
    library evaluates that pair with Python's own engine)."""
    for p in schema_patterns(schema):
        try:
            check(p)
        except PatternError as e:
            raise PatternError(f"the pattern {p[:80]!r}: {e}") from None
    if _uses(schema, "unevaluatedProperties") and _uses(schema, "patternProperties"):
        raise PatternError("unevaluatedProperties together with patternProperties is not "
                           "supported in a check's schema; use additionalProperties")


@functools.lru_cache(maxsize=1)
def validator_class():
    """Draft 2020-12 with ``pattern``, ``patternProperties`` and
    ``additionalProperties`` matched by RE2 (the library's own use Python's
    backtracking engine)."""
    import jsonschema
    from jsonschema import ValidationError, validators

    def pattern(validator, patrn, instance, schema):
        if validator.is_type(instance, "string") and not search(patrn, instance):
            yield ValidationError(f"{instance!r} does not match {patrn!r}")

    def pattern_properties(validator, pattern_props, instance, schema):
        if not validator.is_type(instance, "object"):
            return
        for patrn, subschema in pattern_props.items():
            for k, v in instance.items():
                if search(patrn, k):
                    yield from validator.descend(v, subschema, path=k, schema_path=patrn)

    def additional_properties(validator, ap, instance, schema):
        if not validator.is_type(instance, "object"):
            return
        props = schema.get("properties", {})
        pats = list(schema.get("patternProperties", {}))
        extras = [k for k in instance
                  if k not in props and not any(search(p, k) for p in pats)]
        if validator.is_type(ap, "object"):
            for extra in extras:
                yield from validator.descend(instance[extra], ap, path=extra)
        elif not ap and extras:
            names = ", ".join(repr(e) for e in sorted(extras, key=str))
            if pats:
                yield ValidationError(f"{names} {'does' if len(extras) == 1 else 'do'} not match "
                                      f"any of the regexes: {', '.join(repr(p) for p in sorted(pats))}")
            else:
                yield ValidationError(f"Additional properties are not allowed ({names} "
                                      f"{'was' if len(extras) == 1 else 'were'} unexpected)")

    return validators.extend(jsonschema.Draft202012Validator, {
        "pattern": pattern,
        "patternProperties": pattern_properties,
        "additionalProperties": additional_properties,
    })
