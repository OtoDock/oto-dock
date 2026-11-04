"""The parts of a package checked against the ECMA-376 transitional schemas
(``ooxml/xsd/ecma``, Ecma's files unchanged), as WARNINGs: Office opens many
files that break the schema in small ways (Google Docs leaves attributes
out that Office fills in), so a finding says Office may repair the part,
never that the file is broken, and never stops a save.

A part is checked as an application that knows only the standard reads it
(Markup Compatibility first, ``validate.mc_processed``), and with Office's
own extension namespaces (``http://schemas.microsoft.com/office/...``)
dropped as Office reads them natively, declared ignorable or not. Checked:
the format's main parts, themes, charts, drawings, diagrams and the
document properties; not VML, custom XML, the core properties or any part
whose root the schemas do not declare. A part past SCHEMA_MAX_BYTES is not
checked and says so. A Strict package is not checked.

Findings are per part and per kind of fault (one element out of place, one
attribute missing...), with how many times it occurs; names and values come
from the document, so they go into the excerpt. The schemas are compiled
once per process, never fetched: an import outside the folder fails.
"""

from __future__ import annotations

import functools
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import checks
from checks import CheckContext, Finding, PartRefused, Severity

SCHEMA_CHECK = "schema"
XML_PATCH_OP = "xml_patch"
SCHEMA_DIR = Path(__file__).resolve().parent / "xsd"
ECMA_DIR = SCHEMA_DIR / "ecma"
XML_NS = "http://www.w3.org/XML/1998/namespace"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
OFFICE_EXTENSIONS = "http://schemas.microsoft.com/office/"
STRICT_PREFIX = "http://purl.oclc.org/ooxml/"
SCHEMA_MAX_BYTES = 8 * 1024 * 1024
# The time the check takes per document at most.
SCHEMA_BUDGET_S = 8.0
# Distinct findings listed per part; the rest are counted in one note.
MAX_PER_PART = 25
VALUE_SHOWN = 80

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
CDR = "http://schemas.openxmlformats.org/drawingml/2006/chartDrawing"
XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
EP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
CP = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
PREFIXES = {
    W: "w", S: "x", P: "p", A: "a", C: "c", CDR: "cdr", XDR: "xdr", DGM: "dgm", EP: "ep", CP: "op",
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships": "r",
    "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing": "wp",
    "http://schemas.openxmlformats.org/drawingml/2006/picture": "pic",
    "http://schemas.openxmlformats.org/officeDocument/2006/math": "m",
    "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes": "vt",
    "urn:schemas-microsoft-com:vml": "v", "urn:schemas-microsoft-com:office:office": "o",
    XML_NS: "xml",
}
_SHARED = ("dml-chart.xsd", "dml-chartDrawing.xsd", "dml-diagram.xsd", "shared-documentPropertiesExtended.xsd",
           "shared-documentPropertiesCustom.xsd")
WRAPPERS = {
    "docx": ("wml.xsd", *_SHARED),
    "xlsx": ("sml.xsd", "dml-spreadsheetDrawing.xsd", *_SHARED),
    "pptx": ("pml.xsd", *_SHARED),
}
_COMMON_ROOTS = frozenset({A, C, CDR, DGM, EP, CP})
ROOT_NAMESPACES = {"docx": _COMMON_ROOTS | {W}, "xlsx": _COMMON_ROOTS | {S, XDR}, "pptx": _COMMON_ROOTS | {P}}
# Roots the schema declares with content it cannot judge (an XML map holds
# whole schemas of its own under a strict wildcard).
SKIPPED_ROOTS = frozenset({f"{{{S}}}MapInfo"})

NOT_EXPECTED = "not_expected"
MISSING_CHILD = "missing_child"
ATTRIBUTE_NOT_ALLOWED = "attribute_not_allowed"
ATTRIBUTE_MISSING = "attribute_missing"
BAD_VALUE = "bad_value"
TEXT_NOT_ALLOWED = "text_not_allowed"
OTHER = "other"
_KINDS = {
    NOT_EXPECTED: ("an element stands where the schema does not allow it",
                   "Move the element to a place the schema allows, or remove it."),
    MISSING_CHILD: ("an element lacks a child element the schema requires",
                    "Add the child element, or remove the empty element."),
    ATTRIBUTE_NOT_ALLOWED: ("an element carries an attribute the schema does not allow",
                            "Remove the attribute, or declare its namespace ignorable (mc:Ignorable)."),
    ATTRIBUTE_MISSING: ("an element lacks an attribute the schema requires", "Add the attribute."),
    BAD_VALUE: ("a value is not one the schema allows", "Use a value of the type the schema names."),
    TEXT_NOT_ALLOWED: ("an element holds text where the schema allows only elements", "Remove the text."),
    OTHER: ("the part does not follow the schema", "Write the part again with the tool that made it."),
}
# Faults Office accepts although the schema refuses them, each seen in a
# file Office opens unrepaired: (kind, element, attribute or child) -> the
# values tolerated (None: any).
TOLERATED: dict[tuple[str, str, str], re.Pattern[str] | None] = {
    # Google Docs writes page margins without the header, footer and gutter
    # distances; Word takes 0 for each.
    (ATTRIBUTE_MISSING, "w:pgMar", "w:header"): None,
    (ATTRIBUTE_MISSING, "w:pgMar", "w:footer"): None,
    (ATTRIBUTE_MISSING, "w:pgMar", "w:gutter"): None,
    # Excel, LibreOffice and every library keep a string's spaces with
    # xml:space on its text, which the schema leaves out of ST_Xstring.
    (ATTRIBUTE_NOT_ALLOWED, "x:t", "xml:space"): None,
    # python-docx's own template sets the zoom as "bestFit" with no percent.
    (ATTRIBUTE_MISSING, "w:zoom", "w:percent"): None,
    # python-pptx's chart templates carry PowerPoint's signed axis ids.
    (BAD_VALUE, "c:axId", "val"): re.compile(r"-[0-9]{1,10}"),
    (BAD_VALUE, "c:crossAx", "val"): re.compile(r"-[0-9]{1,10}"),
    # The schema takes a bullet size only as "45%"; PowerPoint and
    # LibreOffice write thousandths of a percent (its own decimal type).
    (BAD_VALUE, "a:buSzPct", "val"): re.compile(r"0*(2[5-9]|[3-9][0-9]|[1-3][0-9][0-9]|400)[0-9]{3}"),
}

_ELEMENT_RE = re.compile(r"^Element '([^']+)'(?:, attribute '([^']+)')?: (.*)$", re.S)
_ATTRIBUTE_RE = re.compile(r"The attribute '([^']+)'")
_EXPECTED_RE = re.compile(r"Expected is(?: one of)? \( ([^)]*) \)")
_VALUE_RE = re.compile(r"(?:The value '|^')((?:[^']|'(?!\s))*)'")
_CLARK_RE = re.compile(r"^\{([^}]*)\}(.*)$")
_LOCK = threading.Lock()


class SchemaSetError(RuntimeError):
    """The schema set could not be compiled from its own folder."""


_LOCAL_SCHEMES = ("", "file")


def _local_file(url: str) -> Path | None:
    parts = urlsplit(url)
    if parts.scheme not in _LOCAL_SCHEMES or parts.netloc:
        return None
    path = Path(unquote(parts.path)).resolve()
    if path.suffix != ".xsd" or not path.is_file() or SCHEMA_DIR not in path.parents:
        return None
    return path


def schema_parser() -> Any:
    """A parser for the schema files: no network, no entities, and every
    import resolved inside the schema folder or refused."""
    from lxml import etree

    class _Local(etree.Resolver):
        def resolve(self, url, pubid, context):
            path = _local_file(url or "")
            if path is None:
                raise SchemaSetError(f"the schema set asked for {url}, outside its own folder")
            return self.resolve_filename(str(path), context)

    parser = etree.XMLParser(no_network=True, resolve_entities=False, load_dtd=False)
    parser.resolvers.add(_Local())
    return parser


def compile_schema(text: str, name: str) -> Any:
    """``text`` (a schema importing files of the folder) compiled as if it
    were ``SCHEMA_DIR/name``; a failure or a warning raises."""
    from lxml import etree

    try:
        doc = etree.fromstring(text.encode(), schema_parser(), base_url=str(SCHEMA_DIR / name)).getroottree()
        schema = etree.XMLSchema(doc)
    except (etree.XMLSchemaParseError, etree.XMLSyntaxError, SchemaSetError) as exc:
        raise SchemaSetError(f"the schemas did not compile: {exc}") from exc
    if len(schema.error_log):
        raise SchemaSetError(f"the schemas compiled with warnings: {schema.error_log[0].message}")
    return schema


@functools.cache
def _schema(fmt: str) -> Any:
    """The compiled schema of a format's wrapper, once per process."""
    return compile_format(fmt)


def compile_format(fmt: str) -> Any:
    """The schema of a format's wrapper, compiled now."""
    from lxml import etree

    imports = [f'<xs:import namespace="{XML_NS}" schemaLocation="xml.xsd"/>']
    for name in WRAPPERS[fmt]:
        target = etree.parse(str(ECMA_DIR / name), schema_parser()).getroot().get("targetNamespace")
        imports.append(f'<xs:import namespace="{target}" schemaLocation="ecma/{name}"/>')
    return compile_schema(f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">{"".join(imports)}</xs:schema>',
                          f"{fmt}.xsd")


def compatible(root: Any) -> Any:
    """``root`` as an application that knows only the standard reads it: Markup
    Compatibility processed, Office's own extension namespaces and the mc:
    attributes dropped."""
    from ooxml import validate

    tree = validate.mc_processed(root)
    # An extension's content is anything by design (a lax wildcard): nothing
    # in it is dropped, and an extension whose content Markup Compatibility
    # dropped (an ignorable namespace) goes too, as a reader of the standard
    # never sees it.
    extensions = [ext for ext in tree.iter() if isinstance(ext.tag, str) and _local(ext.tag) == "ext"
                  and ext.getparent() is not None and _local(ext.getparent().tag) == "extLst"]
    for ext in extensions:
        if not any(isinstance(child.tag, str) for child in ext):
            ext.getparent().remove(ext)
    # The nodes themselves are held: an lxml proxy's id lasts only as long as
    # the proxy does.
    inside = [node for ext in extensions for node in ext.iterdescendants()]
    kept = {id(node) for node in inside}
    for node in list(tree.iter()):
        if id(node) in kept:
            continue
        if not isinstance(node.tag, str):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
            continue
        for name in [a for a in node.attrib if _drop_namespace(_namespace(a))]:
            del node.attrib[name]
        parent = node.getparent()
        if parent is not None and _drop_namespace(_namespace(node.tag)):
            parent.remove(node)
    return tree


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1] if isinstance(name, str) else ""


def _namespace(name: str) -> str:
    return name[1:].split("}", 1)[0] if name.startswith("{") else ""


def _drop_namespace(namespace: str) -> bool:
    return namespace == MC_NS or namespace.startswith(OFFICE_EXTENSIONS)


def _short(name: str) -> str:
    match = _CLARK_RE.match(name.strip())
    if not match:
        return name.strip()
    namespace, local = match.groups()
    prefix = PREFIXES.get(namespace)
    return f"{prefix}:{local}" if prefix else f"{{{namespace}}}{local}"


def classify(message: str) -> tuple[str, str, str, str] | None:
    """(kind, element, attribute or child, value) of a schema error; None
    for an error that is not a fault of the part (a root the schemas do not
    declare)."""
    match = _ELEMENT_RE.match(message.strip())
    if not match:
        return OTHER, "", "", ""
    element, attribute, rest = _short(match.group(1)), match.group(2), match.group(3)
    if "No matching global declaration" in rest:
        return None
    value = ""
    found = _VALUE_RE.search(rest)
    if found:
        value = found.group(1)[:VALUE_SHOWN]
    if "This element is not expected" in rest:
        return NOT_EXPECTED, element, "", ""
    if "Missing child element" in rest:
        expected = _EXPECTED_RE.search(rest)
        names = ", ".join(_short(n) for n in expected.group(1).split(", ")) if expected else ""
        return MISSING_CHILD, element, names, ""
    if "is required but missing" in rest:
        named = _ATTRIBUTE_RE.search(rest)
        return ATTRIBUTE_MISSING, element, _short(named.group(1)) if named else "", ""
    if attribute and "is not allowed" in rest:
        return ATTRIBUTE_NOT_ALLOWED, element, _short(attribute), ""
    if "content other than whitespace" in rest:
        return TEXT_NOT_ALLOWED, element, "", ""
    if ("[facet" in rest or "is not a valid value" in rest or "not accepted by the pattern" in rest
            or "has no corresponding namespace declaration" in rest):
        return BAD_VALUE, element, _short(attribute) if attribute else "", value
    return OTHER, element, _short(attribute) if attribute else "", ""


def validate_part(fmt: str, root: Any) -> list[tuple[str, str]]:
    """(message, path) of every schema error in ``root`` (already
    compatible), in document order."""
    if _LOCK.acquire(blocking=False):
        try:
            return _errors(_schema(fmt), root)
        finally:
            _LOCK.release()
    # The shared schema is held by a validation that ran past its time and
    # still runs: this one compiles its own.
    return _errors(compile_format(fmt), root)


def _errors(schema: Any, root: Any) -> list[tuple[str, str]]:
    schema.validate(root)
    return [(entry.message, entry.path or "") for entry in schema.error_log]


def validate_within(fmt: str, root: Any, seconds: float) -> list[tuple[str, str]] | None:
    """``validate_part`` given at most ``seconds``; None when it takes
    longer. libxml2 describes a child it does not expect inside a large
    content model (a Word paragraph, a body) slowly, about a second each;
    it releases the GIL while it validates, so a thread bounds it. A thread
    left running ends with the worker process."""
    result: list[Any] = []

    def work() -> None:
        try:
            result.append(validate_part(fmt, root))
        except Exception as exc:  # noqa: BLE001  # handed to the caller, which raises it
            result.append(exc)

    thread = threading.Thread(target=work, name="schema-check", daemon=True)
    thread.start()
    thread.join(max(0.0, seconds))
    if thread.is_alive() or not result:
        return None
    if isinstance(result[0], Exception):
        raise result[0]
    return result[0]


def part_findings(fmt: str, part: str, errors: list[tuple[str, str]]) -> list[Finding]:
    """One finding per kind of fault in the part, with how often it occurs."""
    first: dict[tuple[str, str, str], tuple[str, str]] = {}
    count: dict[tuple[str, str, str], int] = {}
    for message, path in errors:
        found = classify(message)
        if found is None:
            continue
        kind, element, detail, value = found
        if (kind, element, detail) in TOLERATED:
            allowed = TOLERATED[(kind, element, detail)]
            if allowed is None or allowed.fullmatch(value):
                continue
        key = (kind, element, detail)
        count[key] = count.get(key, 0) + 1
        first.setdefault(key, (path, value))
    findings: list[Finding] = []
    for key in list(first)[:MAX_PER_PART]:
        kind, element, detail = key
        path, value = first[key]
        text, fix = _KINDS[kind]
        times = count[key]
        excerpt = " ".join(filter(None, [element, f"({detail})" if detail else "", f"value '{value}'" if value else ""]))
        findings.append(Finding(
            Severity.WARNING, f"{fmt}.{SCHEMA_CHECK}", location=f"part /{part}" + (f", near (approximately) {path}" if path else ""),
            message=f"Office may repair this part: {text}" + (f" ({times} times)" if times > 1 else "") + ".",
            fix=fix, excerpt=excerpt, part=part, key=f"{kind}:{element}:{detail}"))
    if len(first) > MAX_PER_PART:
        findings.append(Finding(
            Severity.INFO, f"{fmt}.{SCHEMA_CHECK}", location=f"part /{part}",
            message=f"{len(first) - MAX_PER_PART} more kinds of schema faults in this part are not listed",
            fix="Fix the faults listed, then check again to see the rest.",
            part=part, key="more"))
    return findings


def check_schema(ctx: CheckContext) -> list[Finding]:
    if ctx.ops is not None and XML_PATCH_OP not in ctx.ops:
        return []
    fmt = ctx.fmt
    archive = ctx.zip()
    findings: list[Finding] = []
    started = time.monotonic()
    # A caller with a time budget for its checks (a write) sets a deadline.
    deadline = getattr(ctx, "deadline", None)
    budget = SCHEMA_BUDGET_S if deadline is None else max(0.0, min(SCHEMA_BUDGET_S, deadline - started))
    for name in ctx.part_names():
        if not name.endswith(".xml") or name.startswith("customXml/"):
            continue
        left = budget - (time.monotonic() - started)
        if left <= 0:
            findings.append(_stopped(fmt, name, budget))
            break
        size = archive.getinfo(name).file_size
        if size > SCHEMA_MAX_BYTES:
            findings.append(Finding(
                Severity.INFO, f"{fmt}.{SCHEMA_CHECK}", location=f"part /{name}",
                message=f"the part is {size // (1024 * 1024)} MB, past what the schema check reads, so it was "
                        "not checked against the schema", part=name, key="skipped"))
            continue
        try:
            root = ctx.part_xml(name, cache=False)
        except PartRefused:
            continue
        if root is None or not isinstance(root.tag, str):
            continue
        namespace = _namespace(root.tag)
        if namespace.startswith(STRICT_PREFIX):
            return [Finding(Severity.INFO, f"{fmt}.{SCHEMA_CHECK}", message="the package is Strict Open XML, "
                            "which this check does not judge against the schema", key="strict")]
        if namespace not in ROOT_NAMESPACES[fmt] or root.tag in SKIPPED_ROOTS:
            continue
        errors = validate_within(fmt, compatible(root), left)
        if errors is None:
            findings.append(_stopped(fmt, name, budget))
            break
        findings.extend(part_findings(fmt, name, errors))
    return findings


def _stopped(fmt: str, part: str, budget: float) -> Finding:
    return Finding(Severity.INFO, f"{fmt}.{SCHEMA_CHECK}", location=f"part /{part}",
                   message=f"the schema check stopped after {budget:.0f} s at this part, so it and the "
                           "parts after it were not checked against the schema",
                   fix="Fix the faults the other checks list, then check again.", part=part, key="stopped")


def register_all() -> None:
    # After a write only when the batch patched XML (an operation's own
    # output is the writers' to keep valid). The baseline is the file before
    # the edit, read only for such a batch (collect_baseline passes the ops),
    # so the faults the file already had are told apart from the patch's.
    for fmt in ("docx", "pptx"):
        checks.register(fmt, SCHEMA_CHECK, check_schema, baseline=True)
    checks.register("xlsx", SCHEMA_CHECK, check_schema, final_only=True, baseline=True)


register_all()
