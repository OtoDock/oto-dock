"""Generates ``order_table.json``: the schema order of the children of every
OOXML element the writers touch, from the Open XML SDK's schema data.

Source: https://github.com/dotnet/Open-XML-SDK, ``data/namespaces.json`` and
the ``data/schemas/<name>.json`` files named in ``SCHEMAS``, at the commit in
``SOURCE_COMMIT``; MIT licence, Copyright (c) .NET Foundation and
Contributors. Download those files into one folder, then run:

    python ooxml/generate_order.py <that folder> ooxml/order_table.json

The table keeps, per element type (the SDK's ``<type>/<element>`` name), its
children in schema order as ``[child, [rank, ...]]``, plus the child's type
when the child's name has several (``w:rPr`` under ``w:pPr`` is not
``w:rPr`` under ``w:r``). Children of one choice share a rank. A child the
sequence allows at several places keeps every rank: a table's range markup
(``w:bookmarkEnd``) comes before ``w:tblPr`` and again among the rows.
``elements`` lists the types of each name that has several; any other
name's type is the one entry of ``types`` that ends in ``/<name>``.
``required`` lists the types whose content needs at least one child
element (a list that may not be empty, such as ``p:childTnLst``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SOURCE_REPO = "https://github.com/dotnet/Open-XML-SDK"
SOURCE_COMMIT = "431ab05cf160248cc3885a4a766026d4f8243792"
SCHEMAS = (
    "schemas_openxmlformats_org_wordprocessingml_2006_main",
    "schemas_microsoft_com_office_word_2010_wordml",
    "schemas_microsoft_com_office_word_2012_wordml",
    "schemas_openxmlformats_org_officeDocument_2006_math",
    "schemas_openxmlformats_org_drawingml_2006_main",
    "schemas_openxmlformats_org_drawingml_2006_picture",
    "schemas_openxmlformats_org_drawingml_2006_wordprocessingDrawing",
    "schemas_openxmlformats_org_drawingml_2006_chart",
    "schemas_openxmlformats_org_drawingml_2006_spreadsheetDrawing",
    "schemas_openxmlformats_org_presentationml_2006_main",
    "schemas_microsoft_com_office_powerpoint_2010_main",
    "schemas_openxmlformats_org_spreadsheetml_2006_main",
    "schemas_microsoft_com_office_spreadsheetml_2009_9_main",
)
SEQUENCE = "Sequence"
CHOICE = "Choice"
GROUP = "Group"
ALL = "All"
ANY = "Any"


def element_of(type_name: str) -> str:
    """``w:CT_PPr/w:pPr`` -> ``w:pPr`` (empty for an abstract type)."""
    return type_name.split("/", 1)[1] if "/" in type_name else ""


def flatten(particle: dict, start: int) -> tuple[list[tuple[str, int, str]], int]:
    """The particle's elements in order with their ranks, and the next free
    rank."""
    kind = particle.get("Kind")
    if "Name" in particle and kind is None:
        name = particle["Name"]
        return [(element_of(name), start, name)], start + 1
    items = particle.get("Items", [])
    if kind == ANY:
        return [], start
    if kind == CHOICE:
        found: list[tuple[str, int, str]] = []
        end = start
        for item in items:
            sub, after = flatten(item, start)
            found += sub
            end = max(end, after)
        return found, max(end, start + 1)
    if kind == ALL:
        found = []
        for item in items:
            sub, _ = flatten(item, start)
            found += sub
        return found, start + 1
    found = []
    rank = start
    for item in items:
        sub, rank = flatten(item, rank)
        found += sub
    return found, rank


def least(particle: dict) -> int:
    """The fewest child elements the particle allows. The SDK writes no
    ``Occurs`` for exactly once, and within one a missing ``Min`` is 0
    (``{}`` is any number, ``{"Max": 1}`` optional)."""
    occurs = particle.get("Occurs")
    times = 1 if occurs is None else occurs[0].get("Min", 0)
    kind = particle.get("Kind")
    if "Name" in particle and kind is None or kind == ANY:
        return times
    counts = [least(item) for item in particle.get("Items", [])]
    if kind == CHOICE:
        return times * min(counts, default=0)
    return times * sum(counts)


def build(folder: Path) -> dict:
    namespaces = {n["Prefix"]: n["Uri"] for n in json.loads((folder / "namespaces.json").read_text()) if n["Prefix"]}
    types: dict[str, list[list]] = {}
    elements: dict[str, list[str]] = {}
    required: set[str] = set()
    for stem in SCHEMAS:
        for entry in json.loads((folder / f"{stem}.json").read_text())["Types"]:
            name = entry["Name"]
            element = element_of(name)
            if not element:
                continue
            elements.setdefault(element, [])
            if name not in elements[element]:
                elements[element].append(name)
            if "Particle" not in entry:
                continue
            if least(entry["Particle"]) > 0:
                required.add(name)
            children, _ = flatten(entry["Particle"], 0)
            seen: dict[str, list] = {}
            for child, rank, child_type in children:
                if not child:
                    continue
                if child not in seen:
                    seen[child] = [child, [rank], child_type]
                elif rank not in seen[child][1]:
                    seen[child][1].append(rank)
            for row in seen.values():
                row[1].sort()
            if seen:
                types[name] = list(seen.values())
    ambiguous = {element for element, names in elements.items() if len(names) > 1}
    for rows in types.values():
        for row in rows:
            if row[0] not in ambiguous:
                del row[2:]
    used = {q.split(":", 1)[0] for q in elements} | {
        c[0].split(":", 1)[0] for rows in types.values() for c in rows}
    return {
        "source": f"{SOURCE_REPO} data/schemas at {SOURCE_COMMIT}",
        "license": "MIT, Copyright (c) .NET Foundation and Contributors",
        "namespaces": {p: namespaces[p] for p in sorted(used) if p in namespaces},
        "elements": {e: names for e, names in sorted(elements.items()) if len(names) > 1},
        "types": dict(sorted(types.items())),
        "required": sorted(required),
    }


def dump(table: dict) -> str:
    """The table as the committed file holds it."""
    return json.dumps(table, separators=(",", ":"), sort_keys=False) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    table = build(Path(argv[1]))
    Path(argv[2]).write_text(dump(table), encoding="utf-8")
    print(f"{len(table['types'])} types, {len(table['elements'])} elements -> {argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
