"""The skills teach only what the tools have.

The always-loaded card, the on-demand guide and the guide's references name
tools, operations, keys, values and check ids. Each name must exist for the
tool the text is about, so the skills cannot drift from the code: an
operation renamed, a key dropped or a value no longer offered fails here.

The rules, per paragraph or list item (a unit):
- its tools are the tools it names, plus the tools of its reference file or
  of its guide section; a unit of the card or the guide with neither belongs
  to the tools of the operations it names, else to every tool;
- an operation shown as JSON (``{"op": ...}``) resolves through its tool's
  catalogue, with canonical names only, values among the options the help
  enumerates, and nested keys the operation's help names;
- every other name in backticks (a lower-case word, a snake_case word, the
  key of a ``key: value`` span, a ``fmt.check`` id) and every snake_case word
  of the prose is a tool, an input of one of the unit's tools, an operation
  or key of their catalogues, a snake_case word or a listed option of a
  key's help, a value an operation the unit names offers (a listed option,
  a quoted default, an "e.g." value, an example's value), a word of the help
  of a key the unit names, a registered check, or an entry of ``_ALLOWED``
  below. A Word address
  (``p12``, ``t0``) is an address, not a name. A plain word of some
  help text is not enough: the guard catches a wrong word, not only an
  invented one.
Notation with angle brackets or an ellipsis (``"<op>"``, ``...``) is not an
example and is skipped.

The card also keeps its prompt budget, and the guide stands alone for an
engine that cannot open the references: it names every tool.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_HERE = Path(__file__).parent.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(Path(__file__).parent))

SKILLS = _HERE / "skills"
CARD = SKILLS / "file-tools.md"
GUIDE = SKILLS / "file-tools-guide" / "SKILL.md"
REFERENCES = GUIDE.parent / "references"
CARD_CAP = 4500
# The guide stands alone for an engine that cannot open the references, so
# it is read whole on every activation: a soft ceiling keeps it light.
GUIDE_CAP = 16_000

# The tools a reference file is about, beside the ones a unit names.
_REFERENCE_TOOLS = {
    "word.md": {"write_docx", "read_document"},
    "excel.md": {"write_xlsx", "read_document"},
    "powerpoint.md": {"write_pptx", "read_document"},
    "pdf.md": {"write_pdf", "edit_pdf", "read_document"},
}
# The guide's per-format sections, by a word of their heading.
_SECTION_TOOLS = {
    "Word": {"write_docx"},
    "Excel": {"write_xlsx"},
    "PowerPoint": {"write_pptx"},
    "PDF": {"write_pdf", "edit_pdf"},
}
# A feature the skills may promise only once an operation delivers it.
_FEATURES = {
    "forms": "fill_form",
    "contracts": "accept_all_changes",
    "tracked changes": "accept_all_changes",
    "comments": "add_comment",
    "footnotes": "add_footnote",
    "animations": "add_animation",
    "themes": "set_theme",
}
# The format whose checks each reference lists.
_REFERENCE_CHECKS = {"word.md": "docx", "excel.md": "xlsx", "powerpoint.md": "pptx"}
_ORDINALS = ("first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth "
             "fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth").split()
# Names the skills may use that no catalogue declares, each with its reason.
_ALLOWED = {
    # The display MCP's tools, named to route the agent away from them.
    "display_images", "display_ui", "send_file",
    # Theme colour slots every writer takes; the help names them in its
    # colour note, not as one key's options.
    "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "dk1", "dk2", "lt1", "lt2",
    # write_xlsx number-format presets, listed in the prose of write_cells' help.
    "date", "percent", "number", "integer", "currency", "text",
    # A tesseract language code for edit_pdf's OCR.
    "ell",
}

_IDENT = re.compile(r"[a-z][a-z0-9_]*")
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_KEY_VALUE = re.compile(r"([a-z][a-z0-9_]*)\s*:\s*\S.*")
_CHECK_ID = re.compile(r"(docx|xlsx|pptx|pdf)\.[a-z_]+")
_ADDRESS = re.compile(r"(?:p|t|r|c|sdt|textbox|body|header|footer)\d+")
_SPAN = re.compile(r"`([^`\n]+)`")
_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)
_LINK_TARGET = re.compile(r"\]\([^)]*\)")


# ------------------------------------------------------------------ the tools

def _tools() -> dict[str, dict]:
    import server

    return {tool.name: {"description": tool.description, "schema": tool.inputSchema} for tool in server.TOOLS}


def _catalogues() -> dict[str, object]:
    """Every catalogue of the folder, by the tool it serves."""
    from contract import catalogue_modules

    found = {}
    for name in catalogue_modules():
        catalog = getattr(importlib.import_module(name), "CATALOG", None)
        if catalog is not None:
            found[catalog.tool] = catalog
    return found


def _schema_words(schema: dict) -> set[str]:
    words: set[str] = set()
    for name, prop in (schema.get("properties") or {}).items():
        words.add(name)
        words.update(str(v) for v in prop.get("enum", ()))
        words |= _schema_words(prop)
        if isinstance(prop.get("items"), dict):
            words |= _schema_words(prop["items"])
    return words


def _split_top(text: str) -> list[str]:
    """``text`` split at the commas outside brackets."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return [p for p in parts if p]


def _key_segments(catalog, op: str) -> dict[str, str]:
    """Each key of ``op`` with the text of its help segment (``key? (...)``)."""
    first = catalog.help(op).split("\n", 1)[0]
    head = first.split(": ", 1)[1] if ": " in first else ""
    head = head.split(" — ", 1)[0]
    segments = {}
    for part in _split_top(head):
        match = re.match(r"([a-z][a-z0-9_]*)\??\s*(.*)", part)
        if match:
            segments[match.group(1)] = match.group(2)
    return segments


def _options(segment: str, *, strict: bool = True) -> set[str] | None:
    """The values a help segment enumerates (``(a | b | c; default 'a')`` or
    ``(a, b, c)``), or None when it describes rather than lists. Not strict,
    a list with a describing piece still yields its one-word pieces
    (``(title, body, or a placeholder number)``)."""
    if not segment.startswith("("):
        return None
    inner = segment[1:-1] if segment.endswith(")") else segment[1:]
    inner = re.split(r";\s*default\b", inner)[0]
    pieces = [p.strip() for p in (inner.split("|") if "|" in inner else _split_top(inner))]
    values = set()
    for piece in pieces:
        bare = re.sub(r"\s*\(.*\)\s*$", "", piece)
        if _IDENT.fullmatch(bare):
            values.add(bare)
        elif strict:
            return None
    return values if len(values) > 1 else None


@dataclass
class Vocabulary:
    """What one tool lets the skills name."""

    tool: str
    words: set[str] = field(default_factory=set)
    ops: set[str] = field(default_factory=set)
    op_words: dict[str, set[str]] = field(default_factory=dict)
    key_words: dict[str, set[str]] = field(default_factory=dict)
    catalog: object | None = None


def _vocabularies() -> dict[str, Vocabulary]:
    tools, catalogues = _tools(), _catalogues()
    out = {}
    for name, tool in tools.items():
        vocab = Vocabulary(name, words={name} | _schema_words(tool["schema"]))
        catalog = catalogues.get(name)
        if catalog is not None:
            vocab.catalog = catalog
            for op, spec in catalog.ops.items():
                vocab.ops.add(op)
                for key in spec.keys:
                    vocab.words |= _key_names(key)
                for key, segment in _key_segments(catalog, op).items():
                    vocab.words.update(_SNAKE.findall(segment))
                    vocab.words |= _options(segment, strict=False) or set()
                    vocab.key_words.setdefault(key, set()).update(_IDENT.findall(segment))
                vocab.op_words[op] = _offered_values(catalog, op)
        vocab.words |= vocab.ops
        out[name] = vocab
    return out


def _offered_values(catalog, op: str) -> set[str]:
    """The values ``op``'s help offers: options listed in a key's segment,
    quoted defaults, "e.g." values, and the string values of its examples."""
    values: set[str] = set()
    for segment in _key_segments(catalog, op).values():
        values |= _options(segment, strict=False) or set()
        values.update(v for v in re.findall(r"'([^']+)'", segment) if _IDENT.fullmatch(v))
        values.update(re.findall(r"e\.g\.\s+([a-z][a-z0-9_]*)", segment))
    stack = list(catalog.ops[op].examples)
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str) and _IDENT.fullmatch(item):
            values.add(item)
    return values


def _key_names(key) -> set[str]:
    names = {key.name}
    for nested in (key.fields or ()) + (key.item or ()):
        names |= _key_names(nested)
    return names


def _check_ids() -> set[str]:
    import checks

    checks.load_check_modules()
    return {cid for fmt in ("docx", "xlsx", "pptx", "pdf") for cid in checks.check_ids(fmt)}


# ------------------------------------------------------------ the skill text

@dataclass
class Unit:
    source: str
    text: str
    tools: set[str]


def _named_ops(text: str, tools: set[str], vocab: dict[str, Vocabulary]) -> set[tuple[str, str]]:
    """The (tool, operation) pairs ``text`` names."""
    return {(t, op) for t in tools for op in vocab[t].ops if re.search(rf"\b{re.escape(op)}\b", text)}


def _units(path: Path, text: str, vocab: dict[str, Vocabulary]) -> list[Unit]:
    """The paragraphs and list items of a skill file with their tools."""
    all_tools = set(vocab)
    base = _REFERENCE_TOOLS.get(path.name, set()) if path.parent == REFERENCES else set()
    units, section, block = [], set(), []

    def close():
        if block:
            body = "\n".join(block)
            named = {t for t in all_tools if re.search(rf"\b{t}\b", body)}
            owners = {t for t, _op in _named_ops(body, all_tools, vocab)}
            tools = (named | base | section) or owners or set(all_tools)
            units.append(Unit(f"{path.name}: {body.strip()[:60]}", body, tools))
            block.clear()

    for line in text.split("\n"):
        if line.startswith("#"):
            close()
            section = set().union(*(t for word, t in _SECTION_TOOLS.items() if word in line)) \
                if path == GUIDE else set()
            continue
        if not line.strip() or re.match(r"\s*(?:[-*]|\d+\.)\s", line) or line.startswith("|"):
            close()
        if line.strip():
            block.append(line)
    close()
    return units


def _skill_files() -> list[Path]:
    return [CARD, GUIDE, *sorted(REFERENCES.glob("*.md"))]


def _strip_front_matter(text: str) -> str:
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)


# ------------------------------------------------------------------ checking

def _is_notation(span: str) -> bool:
    return "<" in span or "..." in span or "…" in span


def _check_op(raw: dict, unit: Unit, vocab: dict[str, Vocabulary]) -> list[str]:
    """Problems of one example operation, against the unit's tools."""
    op = raw.get("op")
    homes = [vocab[t] for t in unit.tools if t in vocab and op in vocab[t].ops]
    if not homes:
        return [f"{unit.source}: operation '{op}' belongs to none of {sorted(unit.tools)}"]
    best: list[str] | None = None
    for home in homes:
        problems = _check_op_in(raw, home)
        if not problems:
            return []
        best = problems if best is None or len(problems) < len(best) else best
    return [f"{unit.source}: {p}" for p in best or []]


def _check_op_in(raw: dict, home: Vocabulary) -> list[str]:
    catalog = home.catalog
    if catalog is None:
        return [f"'{k}' is not a key {home.tool} shows" for k in raw if k != "op" and k not in home.words]
    if catalog.is_help(raw):
        return []
    try:
        spec, _resolved, _notes = catalog.resolve(raw)
    except Exception as exc:  # noqa: BLE001  # an OpError names the problem
        return [f"{home.tool} refuses {json.dumps(raw)[:80]}: {exc}"]
    problems = []
    if raw["op"] != spec.name:
        problems.append(f"'{raw['op']}' is an alias; teach '{spec.name}'")
    keys = {k.name: k for k in spec.keys}
    segments = _key_segments(catalog, spec.name)
    whole_help = catalog.help(spec.name)
    for name, value in raw.items():
        if name == "op":
            continue
        if name not in keys:
            problems.append(f"'{name}' is an alias or unknown in {spec.name}; teach the canonical key")
            continue
        options = _options(segments.get(name, ""))
        if options and isinstance(value, str) and value not in options:
            problems.append(f"{spec.name} {name}: '{value}' is not one of {sorted(options)}")
        nested = [value] if isinstance(value, dict) else [v for v in value if isinstance(v, dict)] \
            if isinstance(value, list) else []
        declared = {n for k in (keys[name].fields or ()) + (keys[name].item or ()) for n in _key_names(k)}
        for item in nested:
            for inner in item:
                if inner not in declared and not re.search(rf"\b{re.escape(inner)}\b", whole_help):
                    problems.append(f"{spec.name} {name}: the nested key '{inner}' is not in its help")
    return problems


def _examples(text: str) -> list[object]:
    """Every JSON value shown as an operation in ``text``."""
    found = []
    candidates = [s for s in _SPAN.findall(text) if s.lstrip().startswith(("{", "["))]
    candidates += [b for b in _FENCE.findall(text) if b.lstrip().startswith(("{", "["))]
    for raw in candidates:
        if _is_notation(raw) or '"op"' not in raw:
            continue
        found.append(raw)
    return found


def _problems(path: Path, vocab: dict[str, Vocabulary], check_ids: set[str]) -> list[str]:
    text = _strip_front_matter(path.read_text(encoding="utf-8"))
    problems = []
    # Fenced blocks first, against the tools of the reference file or every tool.
    base = _REFERENCE_TOOLS.get(path.name) if path.parent == REFERENCES else None
    for block in _FENCE.findall(text):
        if not block.lstrip().startswith(("{", "[")) or _is_notation(block):
            continue
        unit = Unit(f"{path.name} (code block)", block, set(base or vocab))
        problems += _check_json(block, unit, vocab)
    prose = _FENCE.sub("", text)
    for unit in _units(path, prose, vocab):
        words = set().union(*(vocab[t].words for t in unit.tools)) | _ALLOWED
        for tool, op in _named_ops(unit.text, unit.tools, vocab):
            words |= vocab[tool].op_words.get(op, set())
        named = {m.group(1) if (m := _KEY_VALUE.fullmatch(s.strip())) else s.strip() for s in _SPAN.findall(unit.text)}
        for tool in unit.tools:
            for key in named & set(vocab[tool].key_words):
                words |= vocab[tool].key_words[key]
        for span in _SPAN.findall(unit.text):
            span = span.strip()
            if span.startswith(("{", "[")):
                if not _is_notation(span) and '"op"' in span:
                    problems += _check_json(span, unit, vocab)
                continue
            if _CHECK_ID.fullmatch(span):
                if span not in check_ids:
                    problems.append(f"{unit.source}: '{span}' is not a registered check")
                continue
            match = _KEY_VALUE.fullmatch(span)
            name = match.group(1) if match else span
            if _IDENT.fullmatch(name) and name not in words and not _ADDRESS.fullmatch(name):
                problems.append(f"{unit.source}: '{name}' is not known to {sorted(unit.tools)}")
        bare = _LINK_TARGET.sub("", _SPAN.sub("", unit.text))
        for name in _SNAKE.findall(bare):
            if name not in words:
                problems.append(f"{unit.source}: '{name}' is not known to {sorted(unit.tools)}")
    return problems


def _check_json(raw: str, unit: Unit, vocab: dict[str, Vocabulary]) -> list[str]:
    try:
        value = json.loads(raw)
    except ValueError as exc:
        return [f"{unit.source}: an example operation is not valid JSON ({exc})"]
    items = value if isinstance(value, list) else [value]
    problems = []
    for item in items:
        if isinstance(item, dict) and "op" in item:
            problems += _check_op(item, unit, vocab)
    return problems


# --------------------------------------------------------------------- tests

@pytest.fixture(scope="module")
def vocab():
    for library in ("docx", "openpyxl", "pptx", "fitz", "PIL"):
        pytest.importorskip(library)
    return _vocabularies()


@pytest.fixture(scope="module")
def check_ids():
    return _check_ids()


def test_the_card_keeps_its_budget_and_names_its_guide():
    text = CARD.read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) <= CARD_CAP, len(text.encode("utf-8"))
    assert "`file-tools-guide`" in text


def test_the_guide_is_a_skill_folder_and_links_every_reference():
    from_disk = {p.name for p in REFERENCES.glob("*.md")}
    text = GUIDE.read_text(encoding="utf-8")
    front = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    assert front and re.search(r"^name: file-tools-guide$", front.group(1), re.M)
    assert re.search(r"^description: \S", front.group(1), re.M)
    named = set(re.findall(r"references/([a-z0-9-]+\.md)", text))
    assert named == from_disk and from_disk


def test_the_guide_stays_light():
    size = len(GUIDE.read_text(encoding="utf-8").encode("utf-8"))
    assert size <= GUIDE_CAP, size


def test_the_guide_names_every_tool(vocab):
    body = _strip_front_matter(GUIDE.read_text(encoding="utf-8"))
    missing = [name for name in vocab if not re.search(rf"\b{name}\b", body)]
    assert missing == []


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.name)
def test_every_name_in_the_skill_exists_in_its_tool(path, vocab, check_ids):
    assert _problems(path, vocab, check_ids) == []


def test_every_check_of_a_format_is_in_its_reference(check_ids):
    for name, fmt in _REFERENCE_CHECKS.items():
        text = (REFERENCES / name).read_text(encoding="utf-8")
        missing = sorted(cid for cid in check_ids if cid.startswith(fmt + ".") and f"`{cid}`" not in text)
        assert missing == [], f"{name} lists no {missing}"


def test_the_skills_promise_no_feature_the_tools_lack(vocab):
    offered = set().union(*(v.ops for v in vocab.values()))
    front = re.match(r"\A---\n(.*?)\n---\n", GUIDE.read_text(encoding="utf-8"), re.S).group(1)
    for where, text in (("the card", CARD.read_text(encoding="utf-8")), ("the guide's description", front)):
        for feature, op in _FEATURES.items():
            if re.search(rf"\b{feature}\b", text, re.I):
                assert op in offered, f"{where} promises {feature}, and no tool has {op} yet"


def test_address_examples_count_from_zero():
    """An address explained in words names its block counted from 1:
    ``p12`` is the thirteenth paragraph, ``t2/r3/c1`` the third table's
    fourth row; a header address names its section."""
    text = (REFERENCES / "word.md").read_text(encoding="utf-8")
    for n, said in re.findall(r"`p(\d+)` is the (\w+)", text):
        assert said == _ORDINALS[int(n)], f"p{n} is the {_ORDINALS[int(n)]} paragraph, not the {said}"
    for tbl, row, col, said in re.findall(r"`t(\d+)/r(\d+)/c(\d+)` is ([^,;]+(?:,[^,;]+){0,2})", text):
        expected = [_ORDINALS[int(tbl)], _ORDINALS[int(row)], _ORDINALS[int(col)]]
        assert all(word in said for word in expected), f"t{tbl}/r{row}/c{col} is not '{said}'"
    for explained in re.findall(r"`header\d+/p\d+` is ([^;]+);", text):
        assert "section" in explained, "a header address names its section"


def test_no_placeholder_inside_a_link():
    for path in _skill_files():
        found = re.findall(r"\]\([^)]*<[^)]*\)", path.read_text(encoding="utf-8"))
        assert found == [], f"{path.name}: an agent would copy {found}"


def test_the_card_keeps_its_load_bearing_rules(vocab):
    card = CARD.read_text(encoding="utf-8")
    assert re.search(r"`preview_document` yourself to show a file the person asks to see", card)
    assert re.search(r"never screenshot or convert", card)
    batch_rule = next(line for line in card.splitlines() if "all or nothing" in line)
    batch_tools = {name for name, tool in _tools().items() if "best_effort" in tool["schema"].get("properties", {})}
    assert batch_tools and all(f"`{name}`" in batch_rule for name in batch_tools), batch_tools
    assert re.search(r"never guess", card, re.I)
    assert re.search(r"owner password", card)
    # The final walk: no agent ran check_document after clean writes, and one
    # named every previewed file by its whole workspace path.
    guide = GUIDE.read_text(encoding="utf-8")
    for text in (card, guide):
        assert re.search(r"`check_document`[^.]*even when every write's checks were clean", text)
        assert re.search(r"by its file name alone[^.]*not even in backticks", text)
        # The operator's 08:26 rules: resend only the fix, build a large file in parts.
        assert re.search(r"`fix`[^.]*corrections", text)
        assert re.search(r"[Bb]uild a large file in parts", text)


# Phrases the guide quotes from a result, as the person's agent will meet
# them: each must still be what the code writes.
_QUOTED_RESULTS = (
    "Nothing saved",
    "failed and changed nothing",
    "failed and may be partly applied",
    "already in the file",
    "No check of this format reads the rendering yet",
    "kept from before this edit",
    "formulas use functions the calculation engine lacks",
    "Would be lost:",
)
_FUNCTION = re.compile(r"\b[A-Z][A-Z0-9]*(?:\.[A-Z0-9]+)*\b")


def _function_claims(text: str, known: set[str]) -> tuple[set[str], set[str]]:
    """The functions ``text`` says are computed (named in a sentence that
    says so) and the ones it says an engine lacks (any other name in a
    passage about what the engine lacks)."""
    computed, lacking = set(), set()
    for unit in re.split(r"\n\s*\n|\n(?=\s*[-*] )", text):
        for sentence in re.split(r"(?<=[.:;])\s+(?=[A-Z])", unit):
            bare = re.sub(r'"[^"]*"', "", sentence)  # a quoted result is not a claim
            names = {n for n in _FUNCTION.findall(bare) if n in known}
            if "are computed" in sentence:
                computed |= names
            elif "lack" in unit:
                lacking |= names
    return computed, lacking


def test_the_functions_named_match_both_engines():
    """XLOOKUP and its kind are computed by every engine; a function the
    guide says is lacking is lacking in at least one (the image's
    LibreOffice or the install's Collabora)."""
    pytest.importorskip("openpyxl")
    from formats.xlsx import coverage, functions

    known = set(coverage.probe_names())
    for path in (GUIDE, REFERENCES / "excel.md"):
        computed, lacking = _function_claims(path.read_text(encoding="utf-8"), known)
        assert computed and lacking, path.name
        assert sorted(computed - functions.EVERY_ENGINE) == [], f"{path.name} says these are computed"
        assert sorted(lacking & functions.EVERY_ENGINE) == [], f"{path.name} says these are lacking"


@pytest.mark.parametrize("text, wrong", [
    ("- XLOOKUP and GROUPBY are computed.", "GROUPBY"),
    ("- The engine lacks a few: XLOOKUP and PIVOTBY.", "XLOOKUP"),
])
def test_the_function_rule_catches_a_wrong_claim(text, wrong):
    pytest.importorskip("openpyxl")
    from formats.xlsx import coverage, functions

    computed, lacking = _function_claims(text, set(coverage.probe_names()))
    assert wrong in (computed - functions.EVERY_ENGINE) | (lacking & functions.EVERY_ENGINE)


def test_every_result_phrase_the_guide_quotes_is_what_the_code_writes():
    guide = " ".join((GUIDE.read_text(encoding="utf-8") + (REFERENCES / "excel.md").read_text(encoding="utf-8"))
                     .split())
    code = "\n".join(path.read_text(encoding="utf-8") for path in _HERE.rglob("*.py")
                     if "__tests__" not in path.parts)
    for phrase in _QUOTED_RESULTS:
        assert phrase in guide, f"the guide no longer quotes {phrase!r}: drop it here"
        assert phrase in code, f"the code no longer writes {phrase!r}: correct the guide"


def test_the_skills_cite_no_page_a_reader_cannot_open():
    shipped = {"SKILL.md", "README.md"} | {p.name for p in REFERENCES.glob("*.md")}
    for path in _skill_files():
        for name in re.findall(r"\b[A-Z][A-Z0-9_-]+\.md\b", path.read_text(encoding="utf-8")):
            assert name in shipped, f"{path.name} cites {name}"


@pytest.mark.parametrize("text, expected", [
    ('Use `{"op": "add_photo", "image_path": "a.png"}` here.', "add_photo"),
    ('Use `write_xlsx` with `colour_scheme: warm`.', "colour_scheme"),
    ('Use `{"op": "set_transition", "slide_index": 0, "transition": "spin_twice"}`.', "spin_twice"),
    ('Use `write_xlsx`: `{"op": "add_chart", "anchor": "E2", "categories": ["a"], '
     '"series": [{"name": "A", "values": [1], "colour_x": "red"}]}`.', "colour_x"),
    ("Call `screenshot_document` with `grid_view: true`.", "grid_view"),
    ("Pass `password` to `create_chart`.", "password"),
    ("The `pptx.made_up` check finds it.", "pptx.made_up"),
    ("Call write_pptx with fake_option set.", "fake_option"),
    ('Use `{"op": "add_slide", "layot": "blank"}`.', "layot"),
    # Words some help text holds, wrong where they stand.
    ("Use `write_docx` with `block`.", "block"),
    ("Give `write_pptx` the `the` key.", "the"),
    ("Set `write_xlsx` `orientation` to `sideways`.", "sideways"),
    ("Use `write_cells` with `the` cells.", "the"),
])
def test_the_rules_catch_what_the_tools_lack(text, expected, vocab, check_ids, tmp_path):
    page = tmp_path / "page.md"
    page.write_text(text + "\n", encoding="utf-8")
    problems = _problems(page, vocab, check_ids)
    assert any(expected in p for p in problems), problems


def test_the_rules_pass_what_the_tools_have(vocab, check_ids, tmp_path):
    page = tmp_path / "page.md"
    page.write_text(
        "Call `write_pptx` with `{\"op\": \"set_transition\", \"slide_index\": 0, \"transition\": \"fade\"}`.\n\n"
        "Set `autofit` to `shrink` with `write_pptx`; `pptx.overflow` names the size.\n\n"
        "Read with `read_document` and `start_cell: B2`; never `{\"op\": \"<name>\"}` notation.\n\n"
        "`add_slide` takes a kind such as `comparison`; `set_text` fills the `body` placeholder.\n\n"
        "`import_csv` reads `cp1253` files.\n",
        encoding="utf-8")
    assert _problems(page, vocab, check_ids) == []


def _front_matter_description(path: Path) -> str:
    """The ``description`` of a skill file's front matter (a plain or a
    double-quoted one-line value, the form these skills use)."""
    head = path.read_text(encoding="utf-8").split("---", 2)[1]
    line = next(line for line in head.splitlines() if line.startswith("description:"))
    value = line.split(":", 1)[1].strip()
    return json.loads(value) if value.startswith('"') else value


def test_the_manifest_describes_each_skill_with_its_own_front_matter():
    """Claude and Codex index a skill by its front matter, Direct LLM's
    skill list by the manifest: the two say the same, word for word."""
    manifest = json.loads((_HERE / "manifest.json").read_text(encoding="utf-8"))
    checked = 0
    for entry in manifest["skills"]:
        path = _HERE / entry["file"]
        if path.name != "SKILL.md":
            continue
        assert entry["description"] == _front_matter_description(path), entry["id"]
        checked += 1
    assert checked >= 2
