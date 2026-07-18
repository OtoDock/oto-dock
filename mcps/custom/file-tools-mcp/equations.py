"""LaTeX equation rendering for the file-tools MCP.

One LaTeX front-end feeds every document format so coverage quirks stay
consistent: latex2mathml (LaTeX → MathML), then mathml2omml for native Word
equations, ziamath for SVG (PDF), and cairosvg for PNG (PPTX/XLSX images).
dwml converts existing OMML back to LaTeX on the read side.

All entry points funnel through latex_to_mathml(), which validates the
generated MathML — latex2mathml degrades silently on unknown commands and can
emit ill-formed XML, and a corrupt equation must fail loudly with the
offending LaTeX named rather than corrupt a document.
"""

import re

from shared import logger

OMML_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"

# OMML container elements that carry a properties child (<m:fPr> etc.).
# mathml2omml omits empty ones; Word always writes them, and OMML consumers
# built against Word's output (dwml among them) crash on their absence.
_OMML_PR_TAGS = {
    "acc", "bar", "borderBox", "box", "d", "eqArr", "f", "func", "groupChr",
    "limLow", "limUpp", "m", "nary", "phant", "rad", "sPre", "sSub",
    "sSubSup", "sSup",
}


class EquationError(ValueError):
    """A LaTeX string could not be converted to valid math markup."""

    def __init__(self, latex: str, detail: str):
        self.latex = latex
        super().__init__(
            f"Could not render equation from LaTeX: {detail}. "
            f"Offending LaTeX: {latex!r}. Rewrite it using standard LaTeX "
            "math (amsmath subset — sums, fractions, scripts, Greek, "
            "matrices, cases, align*); exotic packages are not supported."
        )


def normalize_latex(latex: str) -> str:
    """Normalize model-supplied LaTeX before conversion.

    - Strips math delimiters the model may include ($...$, $$...$$, \\(...\\),
      \\[...\\]) — the tools take bare LaTeX.
    - Rewrites aligned → align* (latex2mathml emits ill-formed XML for
      aligned) and align → align* (align auto-numbers its rows).
    - Brace-wraps a matrix-family environment directly followed by ^/_
      (latex2mathml builds a 3-child msup that downstream converters reject).
    """
    tex = latex.strip()
    for opener, closer in (("$$", "$$"), ("$", "$"), (r"\(", r"\)"), (r"\[", r"\]")):
        if tex.startswith(opener) and tex.endswith(closer) and len(tex) > len(opener) + len(closer):
            tex = tex[len(opener):-len(closer)].strip()
            break
    tex = re.sub(r"\\begin\{aligned\}", r"\\begin{align*}", tex)
    tex = re.sub(r"\\end\{aligned\}", r"\\end{align*}", tex)
    tex = re.sub(r"\\begin\{align\}", r"\\begin{align*}", tex)
    tex = re.sub(r"\\end\{align\}", r"\\end{align*}", tex)
    tex = re.sub(
        r"(?<!\{)(\\begin\{(p|b|v|V|B)?matrix\}.*?\\end\{(p|b|v|V|B)?matrix\})(?=\s*[\^_])",
        r"{\1}",
        tex,
        flags=re.DOTALL,
    )
    return tex


def latex_to_mathml(latex: str, display: bool = True) -> str:
    """Convert LaTeX to validated MathML. Raises EquationError on failure."""
    import latex2mathml.converter
    from lxml import etree

    tex = normalize_latex(latex)
    if not tex:
        raise EquationError(latex, "empty equation")
    try:
        mathml = latex2mathml.converter.convert(
            tex, display="block" if display else "inline"
        )
    except Exception as exc:
        raise EquationError(latex, f"LaTeX parse failed ({exc})") from None
    try:
        tree = etree.fromstring(mathml.encode())
    except etree.XMLSyntaxError as exc:
        # latex2mathml silently emitted broken markup (stray &, unknown env…)
        raise EquationError(latex, f"produced ill-formed MathML ({exc})") from None
    # Unknown commands don't raise — they degrade into literal "\foo" text
    # nodes, which would render verbatim into the document. Catch them here.
    for node in tree.iter():
        residue = re.search(r"\\[a-zA-Z]{2,}", node.text or "")
        if residue:
            raise EquationError(latex, f"unsupported command '{residue.group(0)}'")
    return mathml


def latex_to_omml_element(latex: str, display: bool = True):
    """Convert LaTeX to an lxml <m:oMath> element for DOCX insertion."""
    import mathml2omml
    from lxml import etree

    mathml = latex_to_mathml(latex, display=display)
    try:
        omml = mathml2omml.convert(mathml)
        wrapper = etree.fromstring(f'<w xmlns:m="{OMML_NS}">{omml}</w>'.encode())
    except Exception as exc:
        raise EquationError(latex, f"MathML→OMML conversion failed ({exc})") from None
    omath = wrapper[0]
    _inject_word_props(omath)
    return omath


def _inject_word_props(omath) -> None:
    """Insert the empty properties child Word always writes.

    mathml2omml omits them; consumers built against Word's OMML (incl. our
    own read-side dwml) expect <m:fPr> and friends to exist.
    """
    from lxml import etree

    for el in omath.iter():
        qn = etree.QName(el)
        if qn.namespace != OMML_NS or qn.localname not in _OMML_PR_TAGS:
            continue
        pr_tag = f"{{{OMML_NS}}}{qn.localname}Pr"
        if not any(ch.tag == pr_tag for ch in el):
            el.insert(0, el.makeelement(pr_tag, None, None))


def omml_to_latex(omath_element) -> str | None:
    """Best-effort OMML → LaTeX for read_document. Returns None on failure —
    dwml handles Word-authored OMML but is fragile on exotic input, and a
    read must never crash over one equation."""
    try:
        import warnings

        with warnings.catch_warnings():
            # dwml 0.3 trips SyntaxWarnings at import (old codebase)
            warnings.simplefilter("ignore", SyntaxWarning)
            from dwml.omml import oMath2Latex

        return str(oMath2Latex(omath_element))
    except Exception as exc:
        logger.debug(f"omml_to_latex failed: {exc}")
        return None


def latex_to_svg(latex: str, display: bool = True, size: float = 14) -> tuple[str, float]:
    """Render LaTeX to (svg_markup, baseline_offset_px) via ziamath.

    The baseline offset feeds vertical-align for inline math — without it an
    inline SVG floats above the text baseline.
    """
    import ziamath as zm

    tex = normalize_latex(latex)
    latex_to_mathml(latex, display=display)  # validation gate, same error path
    try:
        expr = zm.Latex(tex, size=size)
        return expr.svg(), expr.getyofst()
    except Exception as exc:
        raise EquationError(latex, f"SVG rendering failed ({exc})") from None


def latex_to_png(latex: str, path: str, display: bool = True, height_px: int = 40) -> tuple[int, int]:
    """Render LaTeX to a PNG file for PPTX/XLSX embedding.

    Renders at 4× the target height (print-grade ~300 DPI when placed at
    height_px CSS pixels) and returns the (width, height) of the written PNG.
    """
    import io

    import cairosvg
    from PIL import Image

    svg, _ = latex_to_svg(latex, display=display, size=14)
    png_bytes = cairosvg.svg2png(bytestring=svg.encode(), scale=1.0)
    with Image.open(io.BytesIO(png_bytes)) as probe:
        natural_h = probe.height
    scale = max((height_px * 4) / max(natural_h, 1), 0.1)
    png_bytes = cairosvg.svg2png(bytestring=svg.encode(), scale=scale)
    with open(path, "wb") as fh:
        fh.write(png_bytes)
    with Image.open(io.BytesIO(png_bytes)) as final:
        return final.width, final.height
