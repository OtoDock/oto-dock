"""The MCP sections of a Codex session's ``config.toml``: the tool-approval
stanzas and the per-session values each ``[mcp_servers.*]`` entry carries
(its ``env`` additions, the session token bearers, the session id).
"""

import json
import logging
import re
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    pass

logger = logging.getLogger("codex-layer")


_TOML_MCP_SECTION_RE = re.compile(r"^\[mcp_servers\.([a-zA-Z0-9_-]+)\]")

# The writer puts each inline ``env`` table on one line, and a value may
# carry braces (a JSON list of objects, a credential blob): the table ends at
# the line's LAST brace, never the first.
_TOML_ENV_LINE_RE = re.compile(r'^(\s*env\s*=\s*\{)(.*)\}(\s*)$')

_TOML_ENV_LINES_RE = re.compile(r'^([ \t]*env[ \t]*=[ \t]*\{)(.*)\}([ \t]*)$', re.MULTILINE)

def with_tool_approvals(mcp_toml: str) -> str:
    """An app-server session's MCP sections with Codex's approvals where its
    own rule and the platform's tier disagree: a server whose glob-ruled
    sensitive tools are not known by name yet gets ``default_tools_approval_mode
    = "prompt"`` (``mcp_permissions.codex_server_default``), and each tool the
    two disagree on a ``[mcp_servers.<k>.tools."<t>"]`` table
    (``codex_tool_approvals``): the critical tools approved (the person's card
    is their one judge), a tool the platform allows that Codex would ask about
    approved, a sensitive one Codex would run asked. A terminal gets none: its
    own prompt is its gate."""
    from services.mcp.mcp_permissions import codex_server_default, codex_tool_approvals
    out: list[str] = []
    tables: list[str] = []
    for line in mcp_toml.splitlines():
        out.append(line)
        m = _TOML_MCP_SECTION_RE.match(line.strip())
        if not m:
            continue
        default = codex_server_default(m.group(1))
        if default:
            out.append(f'default_tools_approval_mode = "{default}"')
        for tool, mode in codex_tool_approvals(m.group(1)).items():
            esc = tool.replace("\\", "\\\\").replace('"', '\\"')
            tables.append(f'[mcp_servers.{m.group(1)}.tools."{esc}"]')
            tables.append(f'approval_mode = "{mode}"')
    text = "\n".join(out).rstrip()
    return text + ("\n\n" + "\n".join(tables) if tables else "") + "\n"

def _inject_proxy_vars_toml(toml_content: str, proxy_vars: dict[str, str]) -> str:
    """Append each of ``proxy_vars`` to every ``env = { ... }`` inline table
    that does not declare the key yet. ``config_builder.inject_credential_env_into_toml``
    already injects ``PROXY_API_KEY`` (with the user_sub), so re-appending it
    produced a DUPLICATE TOML key, invalid TOML: the Codex app-server's
    parser tolerated it (last wins) but the strict interactive ``codex`` TUI
    parser rejects it ("duplicate key", exit 1, a blank terminal).
    Idempotent both ways: inject if absent, skip if present."""
    for key, val in proxy_vars.items():
        escaped_key = key.replace('"', '\\"')
        escaped_val = val.replace('\\', '\\\\').replace('"', '\\"')
        needle = f'"{key}"'

        def _inject(m, _needle=needle, _ek=escaped_key, _ev=escaped_val):
            if _needle in m.group(2):
                return m.group(0)
            return f'{m.group(1)}{m.group(2)}, "{_ek}" = "{_ev}" }}{m.group(3)}'
        toml_content = _TOML_ENV_LINES_RE.sub(_inject, toml_content)
    return toml_content

def _inject_fetch_tokens_toml(
    toml_content: str, bundle_keys: set, session_id: str,
) -> str:
    """Append ``OTO_MCP_FETCH_TOKEN`` to the env inline-table of each
    ``[mcp_servers.<slug>]`` whose slug has a secret bundle (local Codex broker).

    Section-aware + append-only — every stdio MCP already has an ``env`` block
    (the OTO_* set is injected at config build), so the bundle MCPs always have
    one to extend. Mirrors ``remote_mcp_rewrite._rewrite_mcp_toml_for_remote``'s
    env branch; the token's presence then triggers the interceptor wrap.
    """
    from core.credentials import mcp_broker
    out_lines: list[str] = []
    slug: str | None = None
    for line in toml_content.splitlines(keepends=True):
        nl = "\n" if line.endswith("\n") else ""
        body = line[: -len(nl)] if nl else line
        m = _TOML_MCP_SECTION_RE.match(body.strip())
        if m:
            slug = m.group(1)
            out_lines.append(line)
            continue
        em = _TOML_ENV_LINE_RE.match(body)
        if em and slug in bundle_keys:
            head, inner, trail = em.group(1), em.group(2), em.group(3)
            tok = mcp_broker.mint_token(session_id, cast("str", slug))
            esc = tok.replace("\\", "\\\\").replace('"', '\\"')
            sep = "" if inner.strip() == "" else ", "
            out_lines.append(
                f'{head}{inner.rstrip()}{sep}"OTO_MCP_FETCH_TOKEN" = "{esc}" }}{trail}{nl}'
            )
            continue
        out_lines.append(line)
    return "".join(out_lines)

def _inject_enabled_tools_toml(content: str, grants) -> str:
    """``enabled_tools = [...]`` under each ``[mcp_servers.<key>]`` section
    whose outside pick names functions (``grants``: ``(key, names | None)``
    pairs); a server picked whole, or not in the grants, keeps every tool."""
    limits = {key: names for key, names in (grants or ()) if names is not None}
    if not limits:
        return content
    out: list[str] = []
    for line in content.splitlines():
        out.append(line)
        m = _TOML_MCP_SECTION_RE.match(line.strip())
        if m and m.group(1) in limits and not line.strip().endswith("]]"):
            names = ", ".join(json.dumps(n) for n in sorted(limits[m.group(1)]))
            out.append(f"enabled_tools = [{names}]")
    return "\n".join(out) + ("\n" if content.endswith("\n") else "")

def _inject_session_jwt_toml(
    toml_content: str, session_id: str, agent_name: str
) -> str:
    """Swap each sentinel ``Authorization`` bearer for the token it stands
    for in the ``http_headers`` sub-tables: the session sentinel of a
    gateway entry for the session's own token, the callback sentinel of a
    Docker tool with ``server.proxy_callbacks`` for the callback kind. This
    is Codex's analog of the CLI ``_swap_session_jwt`` swap. The sentinel
    strings are unique, so a direct value replace is safe; real vendor
    bearers (``_inject_real_bearers_toml``) are untouched.
    """
    from auth.session_token import (
        CALLBACK_JWT_SENTINEL_BEARER,
        SESSION_JWT_SENTINEL_BEARER,
        swap_session_jwt_bearer,
    )
    for sentinel in (SESSION_JWT_SENTINEL_BEARER, CALLBACK_JWT_SENTINEL_BEARER):
        if sentinel not in toml_content:
            continue
        new_bearer = swap_session_jwt_bearer(sentinel, session_id, agent_name)
        if not new_bearer:
            continue
        esc = new_bearer.replace("\\", "\\\\").replace('"', '\\"')
        toml_content = toml_content.replace(f'"{sentinel}"', f'"{esc}"')
    return toml_content

def _inject_session_id_toml(toml_content: str, session_id: str) -> str:
    """Set ``?session_id=`` on every HTTP MCP ``url`` in the TOML (same purpose
    as the CLI's ``_inject_session_id_into_sse`` — Docker MCPs need it for
    per-session path resolution), mirroring the CLI's append logic: strip any
    existing ``session_id`` param, then join with ``&`` when the URL already
    carries a query string (a bare ``?`` append would corrupt e.g.
    ``...?api_key=x`` into a double-``?`` URL).
    """
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    def _sub(m: re.Match) -> str:
        parsed = urlparse(m.group(2))
        params = parse_qs(parsed.query, keep_blank_values=True)
        params.pop("session_id", None)
        base = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))
        sep = "&" if "?" in base else "?"
        return f"{m.group(1)}{base}{sep}session_id={session_id}"

    return re.sub(r'(url\s*=\s*")(http[^"]+)', _sub, toml_content)
