"""The loose on/off values write_xlsx has always taken.

Before the catalogue checked value types, an on/off key read its value by
truthiness, so agents send "yes", "on" or "1" as often as true. Such a key
is declared with FLAG (a bool, or a word read as one) through flag_key, and
loosen turns the words into booleans before the handler runs.
"""

from catalog import KeySpec

FLAG = ("bool", "str")
OFF_WORDS = frozenset({"false", "0", "no", "off", "none", ""})
ON_WORDS = frozenset({"true", "1", "yes", "on"})

FLAG_KEYS: set[KeySpec] = set()


def flag_key(name: str, **kw) -> KeySpec:
    """An on/off key that also takes a word ("yes", "no", "on", "off")."""
    spec = KeySpec(name, FLAG, **kw)
    FLAG_KEYS.add(spec)
    return spec


def to_flag(value) -> bool:
    """On or off: a boolean as it is; a word is off when it is one of
    OFF_WORDS and on otherwise, as a truthy value always was."""
    if isinstance(value, str):
        return value.strip().lower() not in OFF_WORDS
    return bool(value)


def loosen(keys: tuple[KeySpec, ...] | None, values) -> None:
    """Every flag key among `keys` given as a word becomes a boolean, in
    nested objects and lists of objects too."""
    if not keys or not isinstance(values, dict):
        return
    for spec in keys:
        if spec.name not in values:
            continue
        value = values[spec.name]
        if spec in FLAG_KEYS and isinstance(value, str):
            values[spec.name] = to_flag(value)
        elif spec.fields is not None and isinstance(value, dict):
            loosen(spec.fields, value)
        elif spec.item is not None and isinstance(value, list):
            for item in value:
                loosen(spec.item, item)
