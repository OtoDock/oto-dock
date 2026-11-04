"""A guarded edit of one XML part of a document the library holds open.

``apply_patch`` selects elements of a part with an XPath and replaces,
removes or inserts around them, on the library's IN-MEMORY part (python-docx
and python-pptx; a part the library keeps as bytes is parsed once through
``parts.xml_root`` and written back), so it sees the batch's earlier edits
and the library's save writes it.

What it never does:

- edit the package's plumbing or a part that holds code, extensions,
  controls, embedded objects, custom data or signatures (judged by the
  part's content type, the relationships that reach it and its name);
- add a relationship;
- add, change or remove a field: a patch whose XML holds a field element,
  whose selected elements hold one, or whose selected elements sit inside
  a field's code (between its begin and its separate, in any run or mark)
  is refused (fields are made by their own operations);
- edit a part that holds an element or attribute in a Strict-conformance
  namespace, or take XML that uses one (a transitional part never needs
  one, and the applications read them as the real elements);
- let in anything else that runs, fetches or embeds something when the file
  opens: the part's active content (every field by its instruction, embedded
  objects and controls, VML, linked media, references to outside targets
  from anything but a hyperlink, settings that attach templates or data
  sources, slide actions and timings that open or run something) is listed
  as signatures before and after, each in full, and a signature the patch
  would add or change refuses it.

The patch is applied to the live part with every touched parent's children
saved first. The relationship ids of the inserted elements, the active
content of the whole part, the schema order at every touched parent and the
children no parent's type holds (in the touched parents and the inserted
elements) are then checked, and the caller's own read-back (``verify``)
runs last; a refusal puts each touched parent's own children back (the
part is byte for byte as it was, and the library's objects still hold the
same elements) and raises PatchError with the reason and the fix.
Duplicate paragraph, drawing and shape ids in the inserted elements are
renumbered, each with a note.

The XPath is held to a grammar whose cost grows with the part's size and
no faster (lxml cannot be interrupted while it evaluates; two descendant
searches, unions, parent steps, a path from the root inside a predicate,
a comparison of two node-sets and a predicate on a wildcard step across
the whole part each make it quadratic or worse, measured): one location
path, at most one descendant search, then child, self and attribute steps;
predicates one level deep that test attributes, text, children and
position through relative paths, where a comparison has a quoted value, a
number or a function's result on one side; arithmetic on numbers,
``position()`` and ``last()`` only (a node there would be read in full as
a number); a named element (not ``*``) under a predicate after ``//``; a
few string functions, ``contains`` and ``starts-with`` looking for a
quoted value; and two budgets set so the worst XPath they allow runs in
well under a second on 80,000 paragraphs: the part's nodes (elements,
attributes, comments) times the steps, tests and operators, and its text
(attribute values and comments included) times its depth, the tests that
read text and the length of the longest quoted value. A part with an
element of more than ATTRIBUTES_MAX attributes is not patched.
"""

from __future__ import annotations

import functools
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from results import Code, OpError

ACTIONS = ("replace", "remove", "insert_before", "insert_after", "insert_child")
MATCH_ONE = "one"
MATCH_ALL = "all"
XPATH_MAX_CHARS = 2_000
XPATH_MAX_STEPS = 12
XPATH_VISIT_BUDGET = 8_000_000
XPATH_TEXT_BUDGET = 128_000_000
# A quoted value's characters, per character of text a test reads, cost
# as much as reading it once (measured on libxml2's search).
_NEEDLE_CHARS_PER_READ = 20
FRAGMENT_MAX_BYTES = 1024 * 1024
MATCH_MAX = 1_000
# The most attributes an element of a patched part may carry (Office writes
# at most a few dozen; lxml reads an element's attribute values in time
# that grows with the square of their number).
ATTRIBUTES_MAX = 256

R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
O_NS = "urn:schemas-microsoft-com:office:office"
V_NS = "urn:schemas-microsoft-com:vml"
ACTIVEX_NS = "http://schemas.microsoft.com/office/2006/activeX"
STRICT_NS = "http://purl.oclc.org/ooxml/"
_BASE_NAMESPACES = {
    "r": R_NS, "w": W_NS, "a": A_NS, "p": P_NS, "mc": MC_NS, "wp": WP_NS, "o": O_NS, "v": V_NS, "ax": ACTIVEX_NS,
    "wp14": "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "wpg": "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup",
    "wpc": "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas",
    "w10": "urn:schemas-microsoft-com:office:word",
    "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
}

# Field elements: a patch never adds, edits or removes one.
_FLD_CHAR = f"{{{W_NS}}}fldChar"
_FLD_SIMPLE = f"{{{W_NS}}}fldSimple"
_INSTR_TEXT = f"{{{W_NS}}}instrText"
_DEL_INSTR_TEXT = f"{{{W_NS}}}delInstrText"
_TEXT_FIELD = f"{{{A_NS}}}fld"
FIELD_TAGS = frozenset({_FLD_CHAR, _FLD_SIMPLE, _INSTR_TEXT, _DEL_INSTR_TEXT, f"{{{W_NS}}}fldData", _TEXT_FIELD})
_FIELD_CODE_TEXT = frozenset({_INSTR_TEXT, _DEL_INSTR_TEXT, f"{{{W_NS}}}t"})

# Elements that embed, link or run something, judged with their whole
# subtree (a change anywhere inside is a change of the signature).
_ACTIVE_SUBTREES = {
    f"{{{W_NS}}}altChunk": "an embedded chunk (w:altChunk)",
    f"{{{W_NS}}}object": "an embedded object (w:object)",
    f"{{{W_NS}}}control": "an ActiveX control (w:control)",
    f"{{{W_NS}}}subDoc": "a linked subdocument (w:subDoc)",
    f"{{{W_NS}}}mailMerge": "a mail-merge data source (w:mailMerge)",
    f"{{{W_NS}}}attachedTemplate": "an attached template (w:attachedTemplate)",
    f"{{{W_NS}}}saveThroughXslt": "a save transform (w:saveThroughXslt)",
    f"{{{W_NS}}}frameset": "a frameset (w:frameset)",
    f"{{{W_NS}}}frame": "a frame (w:frame)",
    f"{{{P_NS}}}oleObj": "an OLE object (p:oleObj)",
    f"{{{P_NS}}}control": "an ActiveX control (p:control)",
}
# Elements judged by their own attributes only.
_ACTIVE_ELEMENTS = {
    f"{{{W_NS}}}entryMacro": "a form-field macro (w:entryMacro)",
    f"{{{W_NS}}}exitMacro": "a form-field macro (w:exitMacro)",
    f"{{{W_NS}}}updateFields": "fields updated on open (w:updateFields)",
    f"{{{W_NS}}}linkStyles": "styles linked to a template (w:linkStyles)",
    f"{{{O_NS}}}OLEObject": "an OLE object (o:OLEObject)",
}
_ACTIVE_NAMESPACES = {ACTIVEX_NS: "an ActiveX control", V_NS: "VML (v:)", O_NS: "VML (o:)"}
_SLIDE_ACTIONS = ("ppaction://program", "ppaction://ole", "ppaction://macro", "ppaction://hlinkfile",
                  "ppaction://hlinkpres")
_SLIDE_COMMAND = f"{{{P_NS}}}cmd"
_HYPERLINKS = frozenset({f"{{{W_NS}}}hyperlink", *(f"{{{A_NS}}}{n}" for n in ("hlinkClick", "hlinkHover",
                                                                               "hlinkMouseOver"))})
_BLIP = f"{{{A_NS}}}blip"
_IMAGE_RELTYPES = ("/image", "/hdphoto")
_R_PREFIX = f"{{{R_NS}}}"
_STRICT_PREFIX = f"{{{STRICT_NS}"
_O_RELID = f"{{{O_NS}}}relid"

# Parts a patch never edits, by content type, by the type of a relationship
# that reaches them, and by a folder or name anywhere in their path.
_REFUSED_CONTENT_TYPES = frozenset({
    "application/vnd.openxmlformats-package.relationships+xml",
    "application/vnd.openxmlformats-package.digital-signature-xmlsignature+xml",
    "application/vnd.openxmlformats-package.digital-signature-origin",
    "application/vnd.openxmlformats-package.digital-signature-certificate",
    "application/vnd.openxmlformats-officedocument.customXmlProperties+xml",
    "application/vnd.openxmlformats-officedocument.oleObject",
    "application/vnd.openxmlformats-officedocument.vmlDrawing",
    "application/vnd.ms-office.vbaProject",
    "application/vnd.ms-office.vbaProjectSignature",
    "application/vnd.ms-office.vbaProjectSignatureAgile",
    "application/vnd.ms-office.vbaProjectSignatureV3",
    "application/vnd.ms-word.vbaData+xml",
    "application/vnd.ms-office.webextension+xml",
    "application/vnd.ms-office.webextensiontaskpanes+xml",
    "application/vnd.ms-office.activeX+xml",
    "application/vnd.ms-office.activeX",
    "application/vnd.ms-word.keyMapCustomizations+xml",
    "application/vnd.ms-word.attachedToolbars",
    "application/vnd.ms-office.legacyDocTextInfo",
})
_REFUSED_RELTYPES = (
    "/vbaProject", "/wordVbaData", "/vbaProjectSignature", "/webextension", "/webextensiontaskpanes", "/control",
    "/activeXControlBinary", "/oleObject", "/package", "/customXml", "/customXmlProps", "/ui/extensibility",
    "/ui/userCustomization", "/digital-signature/signature", "/digital-signature/origin",
    "/digital-signature/certificate", "/keyMapCustomizations", "/attachedToolbars", "/attachedTemplate",
    "/subDocument", "/aFChunk", "/vmlDrawing",
)
_REFUSED_FOLDERS = frozenset({"customxml", "customui", "webextensions", "activex", "embeddings", "_xmlsignatures"})

# The XPath grammar.
_FUNCTIONS_ALLOWED = frozenset({"position", "last", "text", "node", "not", "contains", "starts-with",
                                "normalize-space", "local-name", "name", "string"})
_NODE_TYPES = frozenset({"text", "node"})
_SINGLE_VALUE_FUNCTIONS = _FUNCTIONS_ALLOWED - _NODE_TYPES
_STRING_VALUE_FUNCTIONS = frozenset({"contains", "starts-with", "normalize-space", "string"})
_NEEDLE_FUNCTIONS = frozenset({"contains", "starts-with"})
_AXES_OUTSIDE = frozenset({"child", "descendant", "descendant-or-self", "self", "attribute"})
_AXES_INSIDE = frozenset({"child", "self", "attribute"})
_DESCENDANT_AXES = frozenset({"descendant", "descendant-or-self"})
_OPERATOR_NAMES = frozenset({"and", "or", "div", "mod"})
_COMPARISONS = frozenset({"=", "!=", "<", "<=", ">", ">="})
_ARITHMETIC_NAMES = frozenset({"*", "div", "mod"})
_NUMERIC_FUNCTIONS = frozenset({"position", "last"})
# Two kinds of token, named as the groups of _TOKEN_RE.
_LITERAL, _NAME = "literal", "name"
_TOKEN_RE = re.compile(r"""\s*(?:
    (?P<literal>'[^']*'|"[^"]*"|\d+(?:\.\d*)?|\.\d+)
   |(?P<symbol>//|/|::|\.\.|\.|@|\(|\)|\[|\]|,|\||!=|<=|>=|=|<|>|\+|-|\*|\$)
   |(?P<name>[^\W\d][\w.-]*(?::(?:[^\W\d][\w.-]*|\*))?)
)""", re.X)
# XPath's abbreviated step to a parent.
_PARENT_STEP = re.compile(r"\.\.")

_PARA_ID = "{http://schemas.microsoft.com/office/word/2010/wordml}paraId"
_DOC_PR = f"{{{WP_NS}}}docPr"
_SHAPE_ID = f"{{{P_NS}}}cNvPr"
_MC_IGNORABLE = f"{{{MC_NS}}}Ignorable"
# MC attributes whose values name namespace prefixes.
_MC_PREFIX_LISTS = frozenset({_MC_IGNORABLE, f"{{{MC_NS}}}PreserveElements", f"{{{MC_NS}}}PreserveAttributes",
                              f"{{{MC_NS}}}ProcessContent", f"{{{MC_NS}}}MustUnderstand", "Requires"})
_DETAIL_SHOWN = 120


class PatchError(OpError):
    """A patch refused or not applicable: the message says why, the hint
    how to fix it. Nothing was changed."""

    unchanged = True

    def __init__(self, message: str, fix: str) -> None:
        super().__init__(Code.BAD_VALUE, message, hint=fix)


@dataclass
class PatchResult:
    part: str
    matched: int
    action: str
    notes: list[str] = field(default_factory=list)


@functools.cache
def namespaces() -> dict[str, str]:
    """The prefixes an XPath and a fragment may use without declaring them."""
    from ooxml import order

    table = json.loads(order.TABLE_FILE.read_text(encoding="utf-8"))["namespaces"]
    return {**table, **_BASE_NAMESPACES}


def find_part(package: Any, part_name: str) -> Any:
    """The package part named ``part_name`` (with or without a leading
    ``/``, compared case-insensitively, as package names are)."""
    wanted = part_name.strip().lstrip("/").casefold()
    for part in package.iter_parts():
        if str(part.partname).lstrip("/").casefold() == wanted:
            return part
    raise PatchError(f"the document has no part '{part_name}'",
                     "Name a part the document holds, such as word/document.xml or ppt/slides/slide1.xml.")


def refuse_name(name: str) -> None:
    """Refuse a part ``name`` a patch never edits by its name alone (the
    plumbing, code, extensions, controls, embedded objects, custom data)."""
    folded = name.casefold()
    segments = folded.split("/")
    base = segments[-1]
    if folded == "[content_types].xml" or folded.endswith(".rels"):
        raise PatchError(f"'{name}' is package plumbing (content types or relationships), which a patch never "
                         "edits", "Patch the part's own XML; the write operations keep the plumbing.")
    if base.startswith("vbaproject") or base == "vbadata.xml" or _REFUSED_FOLDERS.intersection(segments[:-1]):
        raise PatchError(f"'{name}' holds code, extensions, controls, embedded objects or custom data, which a "
                         "patch never edits", "Patch a content part instead.")


def _reltypes_to(package: Any, part: Any) -> set[str]:
    found = set()
    for rel in package.iter_rels():
        if not rel.is_external and rel.target_part is part:
            found.add(rel.reltype)
    return found


def _refuse_part(package: Any, part: Any, name: str) -> None:
    refuse_name(name)
    content_type = (part.content_type or "").split(";")[0].strip()
    reached_as = _reltypes_to(package, part)
    if content_type in _REFUSED_CONTENT_TYPES or any(r.endswith(_REFUSED_RELTYPES) for r in reached_as):
        raise PatchError(f"'{name}' ({content_type}) holds code, extensions, controls, embedded objects, "
                         "signatures or custom data, which a patch never edits", "Patch a content part instead.")


@dataclass
class _Token:
    kind: str  # literal, symbol or name
    text: str
    role: str = ""  # a name or *: function, axis, operator or test
    level: int = 0  # 1 inside a predicate
    call: str = ""  # a ")" or ",": the function it closes or separates the arguments of ("" for a group)


def _operand_end(token: _Token | None) -> bool:
    return token is not None and (token.kind == _LITERAL or token.text in (")", "]", ".") or token.role == "test"
                                  or bool(_PARENT_STEP.fullmatch(token.text)))


def _tokens(xpath: str) -> list[_Token]:
    """XPath 1.0's tokens, with the roles its lexical rules give a name or
    a ``*`` (an operator after an operand, else a function before ``(``,
    an axis before ``::``, or a name test)."""
    tokens: list[_Token] = []
    pos = 0
    while pos < len(xpath):
        found = _TOKEN_RE.match(xpath, pos)
        if found is None or found.end() == pos:
            if not xpath[pos:].strip():
                break
            raise PatchError(f"the xpath is not valid near '{xpath[pos:pos + 20].strip()}'",
                             "Use XPath 1.0 with the usual prefixes (w, a, p, r, wp, pic, c, m).")
        pos = found.end()
        kind = found.lastgroup or ""
        tokens.append(_Token(kind, found.group(kind)))
    level, opened = 0, []
    for index, token in enumerate(tokens):
        before = tokens[index - 1] if index else None
        after = tokens[index + 1] if index + 1 < len(tokens) else None
        if token.kind == _NAME or token.text == "*":
            if _operand_end(before):
                token.role = "operator"
            elif token.kind == _NAME and after is not None and after.text == "(":
                token.role = "function"
            elif token.kind == _NAME and after is not None and after.text == "::":
                token.role = "axis"
            else:
                token.role = "test"
        if token.role == "operator" and token.kind == _NAME and token.text not in _OPERATOR_NAMES:
            raise PatchError(f"the xpath is not valid near '{token.text}'",
                             "Use XPath 1.0 with the usual prefixes (w, a, p, r, wp, pic, c, m).")
        if token.text == "[":
            level += 1
            if level > 1:
                raise PatchError("the xpath nests a predicate inside a predicate", "Use one level of [...] per step.")
        elif token.text == "]":
            level = max(0, level - 1)
        elif token.text == "(":
            opened.append(before.text if before is not None and before.role == "function" else "")
        elif token.text == ")":
            token.call = opened.pop() if opened else ""
        elif token.text == ",":
            token.call = opened[-1] if opened else ""
        token.level = level if token.text != "]" else 1
    return tokens


def check_xpath(xpath: str, nodes: int = 0, depth: int = 0, chars: int = 0) -> None:
    """Refuse, before it runs, an XPath outside the bounded grammar (see the
    module), and one whose cost on a part of ``nodes`` elements, ``depth``
    levels and ``chars`` characters of text would pass the budgets."""
    if not xpath or len(xpath) > XPATH_MAX_CHARS:
        raise PatchError(f"the xpath is empty or longer than {XPATH_MAX_CHARS} characters",
                         "Give a short XPath that selects the elements to change.")
    tokens = _tokens(xpath)
    for token in tokens:
        if token.role == "function" and token.text not in _FUNCTIONS_ALLOWED:
            raise PatchError(f"the xpath calls {token.text}(), which a patch does not allow",
                             f"Use only {', '.join(sorted(_FUNCTIONS_ALLOWED))}.")
        if token.text == "$":
            raise PatchError("the xpath uses a variable, which a patch does not allow", "Write the value itself.")
    outer = [t for t in tokens if not t.level]
    for index, token in enumerate(tokens):
        before = tokens[index - 1] if index else None
        after = tokens[index + 1] if index + 1 < len(tokens) else None
        if token.role == "function" and token.text in _NODE_TYPES and (
                after is None or after.text != "(" or index + 2 >= len(tokens) or tokens[index + 2].text != ")"):
            raise PatchError(f"{token.text}() takes no argument", f"Write {token.text}().")
        if token.level:
            _check_in_predicate(token, before, after)
            if _arithmetic(token):
                _check_arithmetic(tokens, index)
        else:
            _check_in_path(token)
        if token.text == "," and token.call in _NEEDLE_FUNCTIONS and not (
                after is not None and after.kind == _LITERAL and index + 2 < len(tokens)
                and tokens[index + 2].text == ")"):
            raise PatchError(f"{token.call}() looks for a value the xpath computes, which costs the product of two "
                             "texts", f"Look for a quoted value, such as {token.call}(., 'x').")
    _check_searches(tokens)
    if sum(t.text == "//" or (t.role == "axis" and t.text in _DESCENDANT_AXES) for t in outer) > 1:
        raise PatchError("the xpath searches the part more than once (two // or descendant steps), which "
                         "costs too much on a large part", "Use one // and child steps after it.")
    steps = sum(2 if t.text == "//" else 1 for t in tokens if t.text in ("/", "//")) + 1
    if steps > XPATH_MAX_STEPS * 2:  # a // step counts twice
        raise PatchError(f"the xpath has more than {XPATH_MAX_STEPS} steps", "Give a shorter XPath.")
    tests = sum(t.text in ("[", "+", "-", "|") or t.text in _COMPARISONS or t.role in ("function", "operator")
                for t in tokens)
    visits = nodes * (steps + tests)
    if visits > XPATH_VISIT_BUDGET:
        raise PatchError(f"the xpath would visit about {visits:,} nodes of this part, past the "
                         f"{XPATH_VISIT_BUDGET:,} a patch allows", "Give a narrower XPath (fewer steps and tests, "
                                                                  "a path from the root instead of //).")
    text_tests = sum(_reads_text(tokens, index) for index in range(len(tokens)))
    if text_tests:
        # Every candidate's text is read once per test (at most the part's
        # text times its depth), and libxml2 compares a quoted value at
        # every character it reads.
        needle = max((len(t.text) - 2 for t in tokens if t.kind == _LITERAL and t.text[0] in "'\""), default=0)
        read = chars * max(depth, 1) * text_tests * (_NEEDLE_CHARS_PER_READ + needle) // _NEEDLE_CHARS_PER_READ
        if read > XPATH_TEXT_BUDGET:
            raise PatchError(f"the xpath would read about {read:,} characters of this part's text, past the "
                             f"{XPATH_TEXT_BUDGET:,} a patch allows", "Give fewer text tests, or name the element "
                                                                      "they test (//w:p[...] instead of //*[...]), "
                                                                      "or look for a shorter value.")


def _check_searches(tokens: list[_Token]) -> None:
    """A wildcard step (``*``, ``w:*``, ``node()``) whose context is every
    node of the part (right after ``//``, or after wildcard steps that
    follow one) takes no predicate: libxml2 evaluates it per context node
    and merges the results, in time that grows with the cube of the part
    (measured; the same predicate on a named step stays linear)."""
    steps: list[tuple[str, list[_Token], bool]] = []
    separator, current, predicate = "", [], False
    for token in tokens:
        if token.level:
            predicate = predicate or token.text == "["
        elif token.text in ("/", "//"):
            if current:
                steps.append((separator, current, predicate))
            separator, current, predicate = token.text, [], False
        else:
            current.append(token)
    if current:
        steps.append((separator, current, predicate))
    whole = False
    for separator, parts, predicate in steps:
        whole = whole or separator == "//"
        axis = next((t.text for t in parts if t.role == "axis"), "child")
        attribute = axis == "attribute" or any(t.text == "@" for t in parts)
        wild = not attribute and any(t.role == "test" and (t.text == "*" or t.text.endswith(":*"))
                                     or (t.role == "function" and t.text == "node") for t in parts)
        if whole and wild and predicate:
            raise PatchError("the xpath tests every element of the part (a predicate on * or node() after //), "
                             "which costs too much on a large part", "Name the element after //, for example "
                                                                     "//w:p[...] instead of //*[...].")
        whole = wild and (whole or axis in _DESCENDANT_AXES)


def _check_in_path(token: _Token) -> None:
    """The path outside the predicates is one location path: steps, axes,
    name tests, text() and node(), and the predicates."""
    if _PARENT_STEP.fullmatch(token.text):
        raise PatchError("the xpath steps up to a parent (..), which a patch does not allow",
                         "Select the parent itself with a predicate, for example //w:p[w:r/w:t='x'].")
    if token.text == "|":
        raise PatchError("the xpath joins several paths with |, which a patch does not allow",
                         "Give one path; match: 'all' with a predicate such as [position()=1 or position()=3] "
                         "selects several elements.")
    if token.role == "axis" and token.text not in _AXES_OUTSIDE:
        raise PatchError(f"the xpath uses the {token.text} axis, which a patch does not allow",
                         "Use child, self or attribute steps, after at most one //.")
    allowed = token.text in ("/", "//", "::", "@", ".", "[", "(", ")") or token.role in ("test", "axis") or (
        token.role == "function" and token.text in _NODE_TYPES)
    if not allowed:
        raise PatchError(f"the xpath is not one path: it has '{token.text}' outside a predicate",
                         "Give one location path and put every test inside [...], for example "
                         "//w:p[w:r/w:t='x'].")


def _check_in_predicate(token: _Token, before: _Token | None, after: _Token | None) -> None:
    """A predicate tests the step's own attributes, text, children and
    position: no search of the part (//, a parent, a path from the root)
    and no comparison of two sets of nodes."""
    if token.text == "//" or _PARENT_STEP.fullmatch(token.text):
        raise PatchError("a predicate searches the part again (// or .. inside [...])",
                         "Test attributes, text, children or position inside a predicate.")
    if token.text == "/" and not (before is not None and (before.role == "test" or before.text in (")", "."))):
        raise PatchError("a predicate starts a path at the root of the part (/ inside [...])",
                         "Use a relative path inside a predicate, such as [w:r/w:t='x'].")
    if token.role == "axis" and token.text not in _AXES_INSIDE:
        raise PatchError(f"a predicate uses the {token.text} axis, which a patch does not allow there",
                         "Test attributes, text, children or position inside a predicate.")
    if token.text in _COMPARISONS:
        left = before is not None and (before.kind == _LITERAL or before.call in _SINGLE_VALUE_FUNCTIONS)
        right = after is not None and (after.kind == _LITERAL or (
            after.role == "function" and after.text in _SINGLE_VALUE_FUNCTIONS))
        if not (left or right):
            raise PatchError(f"a predicate compares two sets of nodes ({token.text}), which costs the product "
                             "of their sizes", "Compare with a quoted value, a number or a function's result, "
                                               "such as [w:r/w:t='x'].")


def _reads_text(tokens: list[_Token], index: int) -> bool:
    """Whether the token at ``index`` reads its candidates' text: a string
    function, or a comparison with a set of nodes on one side (a value on
    both sides, such as position() = last(), reads none)."""
    token = tokens[index]
    if token.role == "function":
        return token.text in _STRING_VALUE_FUNCTIONS
    if token.text not in _COMPARISONS:
        return False
    before = tokens[index - 1] if index else None
    after = tokens[index + 1] if index + 1 < len(tokens) else None
    left = before is not None and (before.kind == _LITERAL or before.call in _SINGLE_VALUE_FUNCTIONS)
    right = after is not None and (after.kind == _LITERAL or (
        after.role == "function" and after.text in _SINGLE_VALUE_FUNCTIONS) or after.text == "-")
    return not (left and right)


def _arithmetic(token: _Token) -> bool:
    return token.text in ("+", "-") or (token.role == "operator" and token.text in _ARITHMETIC_NAMES)


def _check_arithmetic(tokens: list[_Token], index: int) -> None:
    """Arithmetic works on numbers, position() and last() only: a node
    there is turned into a number by reading its whole text, which no
    budget can price (the operands are as many as the xpath is long)."""
    token = tokens[index]
    before = tokens[index - 1] if index else None
    after = tokens[index + 1] if index + 1 < len(tokens) else None
    left = token.text != "-" or _operand_end(before)  # a minus after an operator is unary
    left_ok = before is not None and (_is_number(before) or (before.text == ")" and before.call in _NUMERIC_FUNCTIONS))
    right_ok = after is not None and (_is_number(after) or after.text == "-" or (
        after.role == "function" and after.text in _NUMERIC_FUNCTIONS))
    if (left and not left_ok) or not right_ok:
        raise PatchError(f"arithmetic in a predicate ({token.text}) works on numbers, position() and last() only; "
                         "a node there is read in full as a number", "Compare text with a quoted value, such as "
                                                                     "[w:r/w:t='x'], or count positions, such as "
                                                                     "[position() mod 2 = 0].")


def _is_number(token: _Token | None) -> bool:
    return token is not None and token.kind == _LITERAL and token.text[0] not in "'\""


def part_size(root: Any) -> tuple[int, int, int]:
    """The part's nodes (elements, their attributes, comments and
    processing instructions), depth and characters of text (in elements,
    attribute values, comments and processing instructions): an XPath can
    read all of them. An element's own text is read past the library's
    ``text`` property, which python-docx and python-pptx give a paragraph
    or run for its whole text. An element with more than ATTRIBUTES_MAX
    attributes refuses the part before any value is read."""
    from lxml import etree

    own_text = etree.ElementBase.text.__get__
    nodes = depth = level = chars = 0
    for event, node in etree.iterwalk(root, events=("start", "end", "comment", "pi")):
        if event == "start":
            attributes = node.attrib
            if len(attributes) > ATTRIBUTES_MAX:
                raise PatchError(f"the part holds a <{_local(node.tag)}> with {len(attributes):,} attributes, past "
                                 f"the {ATTRIBUTES_MAX} a patch reads", "Patch a part an Office application wrote; "
                                                                        "no Office element carries that many.")
            nodes += 1 + len(attributes)
            level += 1
            depth = max(depth, level)
            chars += len(own_text(node) or "") + sum(len(value) for value in attributes.values())
        elif event == "end":
            level -= 1
            chars += len(node.tail or "")
        else:
            nodes += 1
            chars += len(node.text or "") + len(node.tail or "")
    return nodes, depth, chars


def _no_match(xpath: str, fmt: str) -> PatchError:
    """The refusal for an XPath that matched nothing. One that names a
    chart's own elements (its axes, series, labels) looked on a slide or a
    page, which holds only the frame that points at the chart part."""
    chart_namespaces = {uri for uri in namespaces().values() if uri.endswith(("/chart", "/chartDrawing"))}
    prefixes = namespaces()
    for token in _tokens(xpath):
        prefix, _, local = token.text.partition(":")
        if token.role == "test" and local not in ("", "chart", "*") and prefixes.get(prefix) in chart_namespaces:
            return PatchError("the xpath matched no element: a chart's axes, series and labels sit in the chart's "
                              "own part, which a patch does not reach",
                              "Change a chart's labels, axes and colours with format_chart." if fmt == "pptx"
                              else "Read the part and correct the XPath.")
    return PatchError("the xpath matched no element", "Read the part and correct the XPath.")


def _select(root: Any, xpath: str, action: str, match: str, size: tuple[int, int, int], fmt: str) -> list[Any]:
    from lxml import etree

    check_xpath(xpath, *size)
    try:
        found = etree.XPath(xpath, namespaces=namespaces(), regexp=False, smart_strings=False)(root)
    except etree.XPathError as exc:
        raise PatchError(f"the xpath is not valid: {exc}",
                         "Use XPath 1.0 with the usual prefixes (w, a, p, r, wp, pic, c, m).") from None
    if not isinstance(found, list) or not all(isinstance(n, etree._Element) and isinstance(n.tag, str)
                                              for n in found):
        raise PatchError("the xpath must select elements, not text, attributes or a number",
                         "End the XPath at an element, for example //w:p[1].")
    if not found:
        raise _no_match(xpath, fmt)
    if match == MATCH_ONE and len(found) > 1:
        raise PatchError(f"the xpath matched {len(found)} elements and match is 'one'",
                         "Narrow the XPath (add a position such as [1]) or pass match: 'all'.")
    if len(found) > MATCH_MAX:
        raise PatchError(f"the xpath matched {len(found)} elements, past the {MATCH_MAX} a patch changes",
                         "Narrow the XPath.")
    if action != "insert_child" and any(n is root for n in found):
        raise PatchError("the xpath selects the part's root element, which a patch never replaces, removes "
                         "or sets siblings to", "Select an element inside the part.")
    chosen = set(found)
    if any(ancestor in chosen for n in found for ancestor in n.iterancestors()):
        raise PatchError("one match holds another", "Narrow the XPath so no match is inside another.")
    return found


def _prefixes_named(element: Any) -> set[str]:
    named: set[str] = set()
    for node in element.iter():
        if isinstance(node.tag, str):
            for attribute in _MC_PREFIX_LISTS:
                named.update((node.get(attribute) or "").split())
    return named


def _fragment(xml: str | None, action: str) -> list[bytes]:
    """The fragment's top-level elements, checked and serialized with only
    the namespace declarations they use; each target gets its own parse by
    the library."""
    from lxml import etree

    if action == "remove":
        if xml:
            raise PatchError("remove takes no xml", "Leave xml out of a remove.")
        return []
    if not xml or not xml.strip():
        raise PatchError(f"{action} needs the xml to put in", "Give the new element(s) as xml.")
    data = xml.encode("utf-8")
    if len(data) > FRAGMENT_MAX_BYTES:
        raise PatchError(f"the xml is {len(data) // 1024} KB, past the {FRAGMENT_MAX_BYTES // 1024} KB a patch "
                         "takes", "Patch in smaller pieces.")
    declared = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in namespaces().items())
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False,
                             remove_comments=True, remove_blank_text=True)
    try:
        wrapper = etree.fromstring(f"<patch-fragment {declared}>".encode() + data + b"</patch-fragment>", parser)
    except etree.XMLSyntaxError as exc:
        raise PatchError(f"the xml is not well-formed: {exc}",
                         "Give well-formed elements; the usual prefixes need no declaration.") from None
    if (wrapper.text or "").strip() or any((child.tail or "").strip() for child in wrapper):
        raise PatchError("the xml holds text outside an element", "Put text inside elements such as <w:t>.")
    if any(not isinstance(child.tag, str) for child in wrapper):
        raise PatchError("the xml holds a processing instruction", "Give elements only.")
    elements = list(wrapper)
    if not elements:
        raise PatchError("the xml holds no element", "Give at least one element.")
    for element in elements:
        strict = _strict_name(element)
        if strict:
            raise PatchError(f"the xml uses a Strict-conformance namespace ({strict}), which a transitional part "
                             "never holds", "Use the usual prefixes (w, a, p, r); they name the transitional "
                                            "namespaces.")
        for node in element.iter():
            for prefix in (node.get(_MC_IGNORABLE) or "").split():
                if prefix not in node.nsmap:
                    raise PatchError(f"mc:Ignorable names the prefix '{prefix}', which is not declared",
                                     "Declare every prefix mc:Ignorable names.")
        held = [n for n in element.iter() if n.tag in FIELD_TAGS]
        if held:
            raise PatchError(f"the xml holds a field element (<{_local(held[0].tag)}>), and a patch never adds, "
                             "changes or removes a field", "Leave fields out of the xml; fields are made by their "
                                                           "own operations.")
    serialized = []
    for element in elements:
        etree.cleanup_namespaces(wrapper, keep_ns_prefixes=sorted(_prefixes_named(element)))
        declared_strict = next((uri for node in element.iter() if isinstance(node.tag, str)
                                for uri in node.nsmap.values() if uri.startswith(STRICT_NS)), None)
        if declared_strict:
            raise PatchError(f"the xml declares a Strict-conformance namespace ({declared_strict}), which a "
                             "transitional part never holds", "Leave the declaration out.")
        serialized.append(etree.tostring(element))
    return serialized


def _strict_name(element: Any) -> str | None:
    """The first element or attribute name under ``element`` in a
    Strict-conformance namespace, or None."""
    for node in element.iter():
        if not isinstance(node.tag, str):
            continue
        if node.tag.startswith(_STRICT_PREFIX):
            return node.tag
        for name in node.attrib:
            if name.startswith(_STRICT_PREFIX):
                return name
    return None


def _library_parse(fmt: str):
    if fmt == "docx":
        from docx.oxml.parser import parse_xml
    else:
        from pptx.oxml import parse_xml
    return parse_xml


class _Journal:
    """Every parent a patch touches with its children as they were, so a
    refusal puts the very same elements back in the very same order."""

    def __init__(self) -> None:
        self.saved: list[tuple[Any, list[Any]]] = []
        self.inserted: list[Any] = []

    def touch(self, parent: Any) -> None:
        if not any(held is parent for held, _ in self.saved):
            self.saved.append((parent, list(parent)))

    @property
    def parents(self) -> list[Any]:
        return [parent for parent, _ in self.saved]

    def undo(self) -> None:
        for parent, children in reversed(self.saved):
            parent[:] = children


_SUBTREE_ACTIONS = ("replace", "remove")


def _apply(targets: list[Any], action: str, fragments: list[bytes], fmt: str, journal: _Journal) -> None:
    from ooxml import order

    parse = _library_parse(fmt) if fragments else None
    for target in targets:
        journal.touch(target if action == "insert_child" else target.getparent())
    for target in targets:
        fresh = [parse(data) for data in fragments]
        journal.inserted.extend(fresh)
        if action in _SUBTREE_ACTIONS:
            parent = target.getparent()
            index = parent.index(target)
            parent.remove(target)
            for offset, element in enumerate(fresh):
                parent.insert(index + offset, element)
        elif action == "insert_before":
            for element in fresh:
                target.addprevious(element)
        elif action == "insert_after":
            for element in reversed(fresh):
                target.addnext(element)
        else:
            for element in fresh:
                order.insert_child(target, element)


def _normalized(instruction: str) -> str:
    words = instruction.split()
    return " ".join([words[0].upper(), *words[1:]]) if words else ""


_CLARK = re.compile(r"\{([^}]*)\}")


def _shown(text: str) -> str:
    """A signature for a message: prefixes for namespaces, shortened."""
    prefixes: dict[str, str] = {}
    for prefix, uri in namespaces().items():
        prefixes.setdefault(uri, prefix)
    text = _CLARK.sub(lambda m: f"{prefixes[m.group(1)]}:" if m.group(1) in prefixes else m.group(0), text)
    return text if len(text) <= _DETAIL_SHOWN else text[:_DETAIL_SHOWN] + "..."


def _target(rels: Any, value: str) -> str:
    rel = rels.get(value) if rels is not None else None
    if rel is None:
        return value
    where = rel.target_ref if rel.is_external else str(rel.target_part.partname)
    return f"{rel.reltype.rsplit('/', 1)[-1]}:{'outside ' if rel.is_external else ''}{where}"


def _attributes(node: Any, rels: Any) -> str:
    pairs = []
    for name, value in sorted(node.attrib.items()):
        if name.startswith(_R_PREFIX) or name == _O_RELID:
            value = _target(rels, value)
        pairs.append(f"{name}={value}")
    return f"{node.tag}[{' '.join(pairs)}]"


def _subtree(node: Any, rels: Any) -> str:
    return " ".join(_attributes(n, rels) + (n.text or "").strip() for n in node.iter() if isinstance(n.tag, str))


def _whole(node: Any, rels: Any) -> str:
    """An active element's subtree; one inside another active element is
    already in that one's subtree, so it is signed by its attributes alone
    and the signatures of a part stay as long as the part."""
    if any(a.tag in _ACTIVE_SUBTREES or a.tag == _SLIDE_COMMAND for a in node.iterancestors()):
        return _attributes(node, rels)
    return _subtree(node, rels)


def _in_a_field_code(root: Any, targets: list[Any]) -> Any | None:
    """The first target that sits inside a field's code (between a begin
    and its separate, or its end when it has none, a nested field's whole
    extent included), or None. A target holds no field character (refused
    before), so the state at its start holds inside it and right after it:
    one test covers replace, remove and the three insertions. The targets
    are held in a set while the part is walked (identity, as in
    ``_renumber``)."""
    held = set(targets)
    in_code: list[bool] = []
    for node in root.iter():
        if node in held and any(in_code):
            return node
        if node.tag == _FLD_CHAR:
            kind = node.get(f"{{{W_NS}}}fldCharType")
            if kind == "begin":
                in_code.append(True)
            elif kind == "separate" and in_code:
                in_code[-1] = False
            elif kind == "end" and in_code:
                in_code.pop()
    return None


class _Fields:
    """Word's complex fields as the walk meets their pieces: the code of
    each (instruction text, with ``{field}`` where a nested field sits),
    until its end, or the end of the part for one never closed."""

    def __init__(self) -> None:
        self.open: list[dict] = []

    def char(self, kind: str | None, found: Counter) -> None:
        if kind == "begin":
            if self.open and self.open[-1]["code"]:
                self.open[-1]["pieces"].append("{field}")
            self.open.append({"pieces": [], "code": True})
        elif kind == "separate" and self.open:
            self.open[-1]["code"] = False
        elif kind == "end" and self.open:
            found[("a field", _normalized("".join(self.open.pop()["pieces"])))] += 1

    def text(self, value: str) -> None:
        if self.open and self.open[-1]["code"]:
            self.open[-1]["pieces"].append(value)

    def placeholder(self) -> None:
        if self.open and self.open[-1]["code"]:
            self.open[-1]["pieces"].append("{field}")

    def unterminated(self, found: Counter) -> None:
        for entry in self.open:
            found[("an unterminated field", _normalized("".join(entry["pieces"])))] += 1


def active_content(root: Any, rels: Any = None) -> Counter[tuple[str, str]]:
    """Every piece of the part that runs, fetches, embeds or links
    something when the file opens, as ``(kind, detail)`` signatures,
    counted: each field by its instruction, each active element by its
    attributes (with relationship ids resolved to their targets) or its
    whole subtree, and every reference to an outside target from anything
    but a hyperlink. ``rels`` is the part's relationships."""
    found: Counter[tuple[str, str]] = Counter()
    fields = _Fields()
    for node in root.iter():
        tag = node.tag
        if not isinstance(tag, str):
            continue
        namespace = tag[1:].split("}", 1)[0] if tag.startswith("{") else ""
        if tag == _FLD_CHAR:
            fields.char(node.get(f"{{{W_NS}}}fldCharType"), found)
        elif tag in _FIELD_CODE_TEXT:
            fields.text(node.text or "")
        elif tag == _FLD_SIMPLE:
            fields.placeholder()
            found[("a field", _normalized(node.get(f"{{{W_NS}}}instr") or ""))] += 1
        elif tag == _TEXT_FIELD:
            found[("a text field", _attributes(node, rels))] += 1
        if tag in _ACTIVE_SUBTREES:
            found[(_ACTIVE_SUBTREES[tag], _whole(node, rels))] += 1
        elif tag in _ACTIVE_ELEMENTS:
            found[(_ACTIVE_ELEMENTS[tag], _attributes(node, rels))] += 1
        elif namespace in _ACTIVE_NAMESPACES:
            found[(_ACTIVE_NAMESPACES[namespace], _attributes(node, rels))] += 1
        if tag == _SLIDE_COMMAND and (node.get("type") or "").strip().lower() == "verb":
            found[("a slide timing that runs an object's verb (p:cmd type=verb)", _whole(node, rels))] += 1
        action = "".join((node.get("action") or "").split()).lower()
        if action.startswith(_SLIDE_ACTIONS):
            found[("a slide action that runs or opens something", _attributes(node, rels))] += 1
        if node.get(f"{_R_PREFIX}link") is not None:
            found[("a linked (outside) picture or media (r:link)", _attributes(node, rels))] += 1
        if rels is not None:
            _references(node, tag, rels, found)
    fields.unterminated(found)
    return found


def _references(node: Any, tag: str, rels: Any, found: Counter) -> None:
    for name, value in node.attrib.items():
        if not name.startswith(_R_PREFIX) or value not in rels:
            continue
        rel = rels[value]
        if rel.is_external and tag not in _HYPERLINKS:
            found[("a reference to an outside target from something that is not a hyperlink",
                   f"{tag} {name}={_target(rels, value)}")] += 1
        if tag == _BLIP and not rel.reltype.endswith(_IMAGE_RELTYPES):
            found[("a picture that points at a relationship that is not an image",
                   f"{name}={_target(rels, value)}")] += 1


# The containers whose children are whole shapes or blocks: a new one goes
# in beside a child that holds no field, which leaves the field untouched.
_SHAPE_TREES = frozenset({f"{{{P_NS}}}spTree", f"{{{P_NS}}}grpSp"})
_BLOCK_CONTAINERS = frozenset(f"{{{W_NS}}}{name}" for name in ("body", "tc", "hdr", "ftr", "txbxContent"))


def _field_fix(target: Any, action: str) -> str:
    """How to make the change without touching the field ``target`` holds."""
    if action == "insert_child" and target.tag in _SHAPE_TREES:
        return ("To add a shape, insert it after another shape that holds no field (action insert_after, with an "
                "XPath to that shape): it lands on the same slide.")
    if action == "insert_child" and target.tag in _BLOCK_CONTAINERS:
        return ("To add a paragraph or a table, insert it after a paragraph that holds no field (action "
                "insert_after, with an XPath to that paragraph).")
    return "Select elements that hold no field; fields are changed by their own operations."


def _fmt_of(package: Any) -> str:
    module = type(package).__module__
    if module.startswith("docx"):
        return "docx"
    if module.startswith("pptx"):
        return "pptx"
    raise PatchError("this document type takes no patch", "Patch a Word or PowerPoint document.")


def _order_problems(parents: Iterable[Any]) -> Counter[tuple[str, str]]:
    from ooxml import order

    found: Counter[tuple[str, str]] = Counter()
    for parent in parents:
        found.update(order.out_of_order(parent))
    return found


_ALTERNATE = f"{{{MC_NS}}}AlternateContent"


def _sheltered(element: Any) -> bool:
    """Inside an extension list or Markup Compatibility, which hold what
    they like."""
    return any(_local(a.tag) == "extLst" or a.tag == _ALTERNATE for a in [element, *element.iterancestors()]
               if isinstance(a.tag, str))


def _foreign(parents: Iterable[Any]) -> Counter[tuple[str, str]]:
    """(parent, child) for each child a parent's schema type never holds,
    in any namespace, the parent's type known (Markup Compatibility's own
    elements and extension lists hold what they like)."""
    from ooxml import order

    found: Counter[tuple[str, str]] = Counter()
    for parent in parents:
        if not isinstance(parent.tag, str) or _sheltered(parent):
            continue
        ranks = order.child_ranks(parent)
        if not ranks:
            continue
        found.update((parent.tag, child.tag) for child in parent
                     if isinstance(child.tag, str) and child.tag != _ALTERNATE and child.tag not in ranks)
    return found


def _in_subtrees(elements: Iterable[Any]) -> list[Any]:
    return [node for element in elements for node in element.iter() if isinstance(node.tag, str)]


def _unknown_rids(elements: list[Any], known: set[str]) -> list[str]:
    missing: list[str] = []
    for element in elements:
        for node in element.iter():
            if isinstance(node.tag, str):
                missing += [v for a, v in node.attrib.items()
                            if (a.startswith(_R_PREFIX) or a == _O_RELID) and v and v not in known and v not in missing]
    return missing


def _renumber(fmt: str, root: Any, inserted: list[Any], used_para: set[str]) -> list[str]:
    """Fresh ids for the inserted elements' paragraph ids (package-wide) and
    drawing or shape ids (in the part) that are already taken. The inserted
    nodes are held in a set while the part is walked, so lxml hands the
    walk the very same objects and membership is by identity."""
    from ooxml import parts

    notes = []
    if fmt == "docx":
        changed = sum(parts.refresh_para_ids(e, used_para) for e in inserted)
        if changed:
            notes.append(f"{changed} paragraph ids already in the document were renumbered")
    tag, kind = (_DOC_PR, "drawing") if fmt == "docx" else (_SHAPE_ID, "shape")
    new_nodes = [n for e in inserted for n in e.iter(tag)]
    held = set(new_nodes)
    taken = {n.get("id") for n in root.iter(tag) if n not in held}
    every = taken | {n.get("id") for n in new_nodes}
    next_id = max((int(v) for v in every if v and v.isdigit()), default=0) + 1
    renumbered = 0
    for node in new_nodes:
        if not node.get("id") or node.get("id") in taken:
            node.set("id", str(next_id))
            next_id += 1
            renumbered += 1
        taken.add(node.get("id"))
    if renumbered:
        notes.append(f"{renumbered} {kind} ids already in the part were renumbered")
    return notes


def _snapshot(root: Any) -> list[tuple[Any, list[Any], dict[str, str]]]:
    """Every element of the part with its own children and attributes;
    holding the elements keeps the very same objects for ``_undo``."""
    from lxml import etree

    return [(element, list(element), dict(element.attrib)) for element in root.iter(etree.Element)]


def _undo(journal: _Journal, root: Any, before: bytes, fmt: str,
          snapshot: list[tuple[Any, list[Any], dict[str, str]]] | None = None) -> None:
    """Put the part back as it was, and prove it byte for byte. With a
    ``snapshot`` (a read-back ran, and may have changed any element), every
    element gets its own children and attributes back."""
    from lxml import etree

    journal.undo()
    for element, children, attrib in snapshot or ():
        if list(element) != children:
            element[:] = children
        if dict(element.attrib) != attrib:
            element.attrib.clear()
            element.attrib.update(attrib)
    if etree.tostring(root) != before:
        fresh = _library_parse(fmt)(before)
        root[:] = list(fresh)
        root.attrib.clear()
        root.attrib.update(fresh.attrib)


def apply_patch(package: Any, part_name: str, xpath: str, action: str, xml: str | None = None, *,
                match: str = MATCH_ONE, verify: Callable[[Any], None] | None = None) -> PatchResult:
    """Patch ``part_name`` of an open python-docx or python-pptx package.
    Raises PatchError (an OpError) with the reason and the fix; the part is
    then exactly as it was. ``verify(root)``, the caller's read-back, runs
    after every check of the guard, on the patched part (a part kept as
    bytes is written back first): an OpError it raises is the refusal, any
    other exception says the part cannot be read; either puts the part back
    with the very same element objects (and bytes)."""
    from lxml import etree

    from ooxml import parts

    if action not in ACTIONS:
        raise PatchError(f"'{action}' is not a patch action", f"Use one of: {', '.join(ACTIONS)}.")
    if match not in (MATCH_ONE, MATCH_ALL):
        raise PatchError(f"match is '{match}'", "Use match: 'one' (the default) or 'all'.")
    fmt = _fmt_of(package)
    refuse_name(part_name.strip().lstrip("/"))
    part = find_part(package, part_name)
    name = str(part.partname).lstrip("/")
    _refuse_part(package, part, name)
    try:
        root = parts.xml_root(part)
    except ValueError:
        raise PatchError(f"'{name}' is not an XML part", "Patch an XML part such as word/document.xml.") from None
    size = part_size(root)
    strict = _strict_name(root)
    if strict:
        raise PatchError(f"'{name}' holds an element or attribute in a Strict-conformance namespace ({strict}), "
                         "which a patch never edits", "Save the document in the usual (transitional) format "
                                                      "first.")
    fragments = _fragment(xml, action)
    targets = _select(root, xpath, action, match, size, fmt)
    for target in targets:
        held = next((n for n in target.iter() if n.tag in FIELD_TAGS), None)
        if held is not None:
            raise PatchError(f"the selected <{_local(target.tag)}> holds a field (<{_local(held.tag)}>), and a patch "
                             "never adds, changes or removes a field", _field_fix(target, action))
    inside = _in_a_field_code(root, targets)
    if inside is not None:
        raise PatchError(f"the selected <{_local(inside.tag)}> sits inside a field's code (between the field's begin "
                         "and its separate), and a patch never changes a field", "Select elements outside the "
                                                                                 "field's code; fields are changed "
                                                                                 "by their own operations.")
    known = set(part.rels.keys())
    before = etree.tostring(root)
    content_before = active_content(root, part.rels)
    touched = {id(p): p for p in (t if action == "insert_child" else t.getparent() for t in targets)}
    order_before = _order_problems(touched.values())
    taken = _in_subtrees(targets) if action in _SUBTREE_ACTIONS else []
    foreign_before = _foreign(touched.values()) + _foreign(taken)
    snapshot = _snapshot(root) if verify is not None else None
    blob = part.blob if verify is not None and getattr(part, "_element", None) is None else None
    journal = _Journal()
    _apply(targets, action, fragments, fmt, journal)
    try:
        missing = _unknown_rids(journal.inserted, known)
        if missing:
            raise PatchError(f"the xml uses relationship id {missing[0]}, which the part does not have",
                             "Use a relationship id the part already has; a patch never adds one.")
        added = active_content(root, part.rels) - content_before
        if added:
            kind, detail = next(iter(added))
            raise PatchError(f"the patch would add or change {kind} ({_shown(detail)}), which runs, fetches or "
                             "embeds something when the file opens", "Leave active content out of the patch.")
        misplaced = _order_problems(journal.parents) - order_before
        if misplaced:
            child, sibling = next(iter(misplaced))
            raise PatchError(f"the patch puts <{_local(child)}> after <{_local(sibling)}>, where the schema puts it "
                             "before, which Office refuses", "Insert it earlier, or use insert_child to let it "
                                                             "find its place.")
        foreign = _foreign(journal.parents) + _foreign(_in_subtrees(journal.inserted)) - foreign_before
        if foreign:
            parent, child = (_prefixed(tag) for tag in next(iter(foreign)))
            raise PatchError(f"<{parent}> never holds a <{child}>, and the patch puts one there, which Office "
                             "repairs or refuses", f"Put <{child}> where the schema holds it, or leave it out.")
        if verify is not None:
            if blob is not None:
                parts.flush_xml_roots(package, only=part)
            _verified(verify, root, name)
    except OpError as exc:
        _undo(journal, root, before, fmt, snapshot)
        if blob is not None:
            part._blob = blob
        exc.unchanged = True
        raise
    used_para = parts.used_para_ids(package) if fmt == "docx" and fragments else set()
    notes = _renumber(fmt, root, journal.inserted, used_para)
    if getattr(part, "_element", None) is None:
        parts.flush_xml_roots(package, only=part)
    return PatchResult(part=name, matched=len(targets), action=action, notes=notes)


def _verified(verify: Callable[[Any], None], root: Any, name: str) -> None:
    """The caller's read-back: its OpError stands as the refusal; anything
    else it raises is the patched part failing to read."""
    try:
        verify(root)
    except OpError:
        raise
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001  # a library reading a patched tree raises anything from its parser
        raise PatchError(f"after the patch {name} cannot be read ({str(exc) or type(exc).__name__}), which Office "
                         "repairs or refuses", "Keep every element and attribute its parent needs, with a value of "
                                               "its type.") from None


def _prefixed(tag: str) -> str:
    """``{uri}name`` with its usual prefix (``p:spTree``), or as it is."""
    uri, _, local = tag[1:].partition("}")
    prefix = next((p for p, u in namespaces().items() if u == uri), None)
    return f"{prefix}:{local}" if prefix else tag


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
