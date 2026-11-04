"""Registers the structural package checks of ``ooxml.validate`` for docx,
xlsx and pptx and the "Office refuses this" rules of ``ooxml.refusals``
(found by name, like every ``*_checks.py``)."""

from ooxml import refusals, validate


def register_all() -> None:
    validate.register_checks()
    refusals.register_checks()


register_all()
