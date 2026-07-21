"""Standard Agent Skills (SKILL.md) format helpers.

The platform's skill files follow the agentskills.io format: a folder whose
``SKILL.md`` starts with a YAML frontmatter fence (``---`` … ``---``)
carrying at minimum ``name`` and ``description``, followed by the markdown
instruction body.

Three operations, used at different points of the pipeline:

- ``strip_frontmatter``   — the INLINE path (``loading: always`` skills):
  the body is injected into the system prompt; the frontmatter is index
  metadata and must not ship as prompt text.
- ``scrub_frontmatter``   — the MATERIALIZATION + INSTALL paths: rewrite the
  frontmatter keeping ONLY the declarative whitelist below. This is a
  security boundary, not cosmetics: Claude Code honors ``allowed-tools``
  from skill frontmatter, which would pre-authorize tools past the
  platform's ask-tier on interactive sessions. Whitelist, never blacklist —
  unknown keys may acquire CLI-side meaning in future versions.
- ``parse_frontmatter``   — consistency checks (manifest ``description`` vs
  frontmatter) and installer validation.

Legacy skill files without a frontmatter fence pass through every helper
unchanged (all pre-migration bundled skills are stamped ``loading: always``
and only ever inlined).

Pure stdlib + pyyaml — no config / registry imports, mirroring
``mcp_manifest_types``'s import-anywhere contract.
"""

import logging

import yaml

logger = logging.getLogger("proxy.mcp.skill_format")

# The declarative agentskills.io keys the platform renders or preserves.
# Everything else — notably ``allowed-tools`` and any future permission/
# hook/settings-bearing key — is dropped at scrub time.
FRONTMATTER_ALLOWED_KEYS = ("name", "description", "license", "compatibility",
                            "metadata")

_FENCE = "---"


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Split ``text`` into (frontmatter_yaml, body).

    Returns ``(None, text)`` when there is no leading frontmatter fence.
    The fence must open on the very first line (spec behavior); the closing
    fence is the next ``---`` line. An unterminated fence is treated as
    no-frontmatter rather than swallowing the whole file.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FENCE:
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            return "".join(lines[1:i]), "".join(lines[i + 1:])
    return None, text


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse the frontmatter into a dict; ``({}, body)`` when absent/invalid."""
    fm, body = split_frontmatter(text)
    if fm is None:
        return {}, body
    try:
        data = yaml.safe_load(fm)
    except yaml.YAMLError as e:
        logger.warning("SKILL.md frontmatter is not valid YAML: %s", e)
        return {}, body
    return (data, body) if isinstance(data, dict) else ({}, body)


def strip_frontmatter(text: str) -> str:
    """Return the instruction body with any frontmatter fence removed."""
    _, body = split_frontmatter(text)
    return body.lstrip("\n")


def _salvage_descriptive_keys(fm: str) -> dict:
    """Tolerant line-wise recovery of ``name``/``description`` from
    YAML-invalid frontmatter. The classic authoring footgun is an unquoted
    colon inside a description ("description: Do X: then Y") — invalid YAML,
    but the intended value is unambiguous line-wise. Only these two purely
    DESCRIPTIVE string keys are salvaged (never ``allowed-tools`` or any
    other authorization-bearing key), so the scrub stays fail-closed where
    it matters while a punctuation slip no longer silently destroys the
    skill (the CLIs reject a frontmatter-less SKILL.md outright)."""
    out: dict = {}
    for line in fm.splitlines():
        for key in ("name", "description"):
            prefix = f"{key}:"
            if line.startswith(prefix):
                value = line[len(prefix):].strip().strip("\"'")
                if value:
                    out[key] = value
    return out


def scrub_frontmatter(text: str, origin: str = "") -> str:
    """Rewrite frontmatter keeping only ``FRONTMATTER_ALLOWED_KEYS``.

    Files without frontmatter are returned unchanged. Invalid-YAML
    frontmatter falls back to the line-wise ``name``/``description``
    salvage above (re-emitted as VALID YAML via safe_dump, so the CLIs
    accept the skill); only a fully unrecoverable block is dropped (fail
    closed — better a skill the CLI ignores than un-vetted keys reaching
    it). ``origin`` names the file in logs.
    """
    fm, body = split_frontmatter(text)
    if fm is None:
        return text
    try:
        data = yaml.safe_load(fm)
    except yaml.YAMLError:
        data = None
    if not isinstance(data, dict):
        data = _salvage_descriptive_keys(fm)
        if data:
            logger.warning(
                "SKILL.md frontmatter is invalid YAML (%s) — salvaged "
                "name/description only", origin or "unknown source",
            )
        else:
            logger.warning(
                "SKILL.md frontmatter unparseable — dropped at scrub (%s)",
                origin or "unknown source",
            )
            return body.lstrip("\n")
    kept = {k: data[k] for k in FRONTMATTER_ALLOWED_KEYS if k in data}
    dropped = sorted(set(data) - set(kept))
    if dropped:
        logger.info("SKILL.md frontmatter scrub dropped keys: %s", dropped)
    rebuilt = yaml.safe_dump(kept, sort_keys=False, allow_unicode=True,
                             default_flow_style=False)
    return f"{_FENCE}\n{rebuilt}{_FENCE}\n{body}"
