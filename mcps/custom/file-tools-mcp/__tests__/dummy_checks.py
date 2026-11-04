"""Checks registered for the tests, before any format has checks of its own:
they prove the after-write and check_document paths, a spawn child
included (it imports this module by name)."""

import checks

DOCUMENT_PART = "word/document.xml"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
TODO = "TODO"


def leftover_todo(ctx: checks.CheckContext) -> list[checks.Finding]:
    body = ctx.part_xml(DOCUMENT_PART)
    if body is None:
        return []
    found = []
    for index, paragraph in enumerate(body.iter(f"{_W}p")):
        text = "".join(t.text or "" for t in paragraph.iter(f"{_W}t"))
        if TODO in text:
            found.append(checks.Finding(checks.Severity.WARNING, "", location=f"paragraph index {index}",
                                        message="a leftover TODO marker", fix="Replace it with the real text.",
                                        excerpt=text, part=DOCUMENT_PART, key="todo"))
    return found


def final_pass(ctx: checks.CheckContext) -> list[checks.Finding]:
    return [checks.Finding(checks.Severity.INFO, "", message="the final check ran")]


def register_all() -> None:
    checks.register("docx", "dummy_todo", leftover_todo, baseline=True)
    checks.register("docx", "dummy_final", final_pass, final_only=True)


register_all()
