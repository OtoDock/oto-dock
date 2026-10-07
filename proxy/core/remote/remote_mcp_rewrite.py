"""MCP config rewriting for remote (satellite) execution.

Pure functions that rewrite an agent's MCP config (TOML for Codex, JSON for the
CLI) so stdio servers, paths, venvs and env land correctly on the satellite host,
plus the proxy-terminable-HTTP bearer-swap bookkeeping. Split out of
remote_execution.py; the payload builders (remote_start_payload.py) call them by
bare name and remote_execution re-exports them. NOTE: the internally-called helpers
(_resolve_satellite_mcp_path_info / _rewrite_stdio_paths / _rewrite_env_for_remote)
must be monkeypatched HERE (core.remote.remote_mcp_rewrite), not on remote_execution.
"""

import json
import re
from core import host_os
from core.credentials.mcp_gateway import GATEWAY_PATH_PREFIX, gateway_key_of

# ---------------------------------------------------------------------------
# MCP config rewriting for remote execution
# ---------------------------------------------------------------------------

# Duplicated from satellite/session_manager.py — the satellite package is not
# importable proxy-side (vendored one-way via scripts/sync-satellite-code.sh).
_TOML_MCP_SECTION_RE = re.compile(r"^\[mcp_servers\.([^\]]+)\]\s*$")
_TOML_ANY_SECTION_RE = re.compile(r"^\[[^\]]+\]\s*$")


def _strip_toml_mcp_sections(toml: str, drop_keys: set[str]) -> str:
    """Remove whole ``[mcp_servers.<key>]`` blocks (header + body up to the
    next top-level ``[section]`` or EOF) for keys in ``drop_keys``.

    Line-based, so it can't be fooled by a ``[`` inside an inline
    ``args = [...]`` value — the bug in the old ``[^\\[]*`` regex, which
    stopped at the first ``[`` and left half the block (orphaned args/env
    lines) behind, corrupting the TOML.
    """
    out: list[str] = []
    dropping = False
    for line in toml.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        m = _TOML_MCP_SECTION_RE.match(stripped)
        if m:
            # First dotted segment is the server key; this also catches the
            # server's `[mcp_servers.<key>.env]` sub-table so a dropped MCP's
            # env (with credentials) goes too.
            dropping = m.group(1).strip('"').split(".")[0] in drop_keys
            if dropping:
                continue
        elif dropping and _TOML_ANY_SECTION_RE.match(stripped):
            # A new (kept) section begins — stop dropping and keep this header.
            dropping = False
        if not dropping:
            out.append(line)
    return "".join(out)


def _resolve_satellite_mcp_path_info(slug: str):
    """Resolve an MCP slug → ``(category, satellite_name, platform_dir, manifest)``.

    The satellite installs MCPs under ``mcps/<category>/<name>`` keyed on the
    MANIFEST's ``category`` + ``name`` — which can disagree with the platform's
    historical on-disk ``mcps/<dir-group>/<dir-name>`` layout (e.g.
    ``custom/notifications-mcp`` on disk but ``category: core`` → installed at
    ``core/notifications-mcp``; ``community/workspace-mcp`` dir but
    ``name: google-workspace``). The remote path rewrite MUST use the manifest
    values or the satellite spawns a binary at a path that doesn't exist and the
    MCP fails to start. Shared by the JSON (Claude CLI) and TOML (Codex) twins
    so both rewrite identically.

    ``slug`` is the mcpServers key / TOML ``[mcp_servers.<slug>]`` section name —
    the manifest's ``server_name`` (or ``name``). Resolve by name first, then
    scan by ``server_name``; ``satellite_name`` is the manifest's canonical name
    when found via the server_name fallback, else the slug.
    """
    from services.mcp import mcp_registry
    sat_category = ""
    sat_name = slug
    platform_dir = ""
    manifest = None
    try:
        manifest = mcp_registry.get_manifest(slug)
        if manifest is None:
            for m in mcp_registry._manifests.values():
                if getattr(m, "server_name", "") == slug:
                    manifest = m
                    sat_name = m.name
                    break
        if manifest is not None:
            sat_category = manifest.category or ""
            platform_dir = str(manifest.mcp_dir.resolve())
    except Exception:
        pass
    return sat_category, sat_name, platform_dir, manifest


def _gateway_json_entry(
    server: dict, name: str, facts: dict, sat_port: int, session_id: str, gateway_mode: bool,
) -> dict:
    """A credential-gateway entry (``mcp_gateway.entry_url``) as the
    satellite config carries it: a sidecar keeps the tunnel's
    ``/mcp/<name>/`` shape with the session id (the tunnel's forward adds
    the credential there); a vendor entry dials the machine's own loopback
    gateway when the machine runs one, and otherwise keeps the inline shape
    (the vendor URL and the header's value) until the machine updates. The
    Authorization sentinel stays where the session token belongs and is
    removed where it does not: no vendor ever receives a session token."""
    path = facts.get("path") or "/"
    headers = dict(server.get("headers") or {})
    if facts.get("proxy_local"):
        sess = f"?session_id={session_id}" if session_id else ""
        server["url"] = f"http://127.0.0.1:{sat_port}/mcp/{name}{path}{sess}"
    elif gateway_mode:
        server["url"] = f"http://127.0.0.1:{sat_port}{GATEWAY_PATH_PREFIX}/{name}{path}"
    else:
        server["url"] = f"{facts.get('upstream', '')}{path}"
        headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
        if facts.get("value"):
            headers[str(facts.get("header") or "Authorization")] = str(facts["value"])
        server["headers"] = headers
        if not headers:
            server.pop("headers", None)
    return server


def _rewrite_mcp_json_for_remote(
    mcp_config: dict, sat_port: int, *, target_os: str = "linux",
    session_id: str = "", secret_bundle_keys: set = frozenset(),
    proxy_api_key: str = "", gateway: dict | None = None, gateway_mode: bool = False,
) -> dict:
    """Rewrite JSON MCP config (CLI format) for remote execution.

    - stdio MCPs: paths rewritten to the satellite mcps dir, OS-aware
      (``~/.oto-dock/mcps/...venv/bin/...`` on Unix, ``~/OtoDock/mcps/
      ...venv/Scripts/...exe`` on Windows). They are installed by
      ``sync_mcps_for_session`` before the CLI starts; failures get
      filtered out afterward by the engine's ``RemoteEngineAdapter.without_mcps``.
    - SSE/HTTP MCPs (Docker on platform): URL rewritten to
      ``http://127.0.0.1:{sat_port}/mcp/{name}/...`` so the satellite's
      local tunnel server forwards over WS to the platform-side dispatcher.
    - credential-gateway entries (``gateway``, by key): ``_gateway_json_entry``.
    - env: ``PROXY_URL`` rewritten to ``http://127.0.0.1:{sat_port}``.
    """
    servers = mcp_config.get("mcpServers", {})
    rewritten = {}

    for name, server in servers.items():
        server = dict(server)

        facts = (gateway or {}).get(name) if "url" in server else None
        if facts is not None and gateway_key_of(server["url"]) == name:
            server = _gateway_json_entry(server, name, facts, sat_port, session_id, gateway_mode)
        # Rewrite SSE/HTTP URLs to point at the satellite's local tunnel.
        # `name` here is the mcpServers key — that's the same slug used
        # in the tunnel allowlist `/mcp/<slug>/...`.
        elif "url" in server and sat_port:
            from urllib.parse import urlparse
            from services.mcp import mcp_registry
            from core.config import deployment
            parsed = urlparse(server["url"])
            # Only tunnel proxy-local Docker MCPs. External hosted MCPs
            # (Linear/Slack/Notion/Zoom) carry a real public host and must reach
            # the internet directly — tunneling them to the satellite loopback
            # would 404. Proxy-local = loopback (T1) OR the MCP's service-DNS
            # name (T2, Docker-Compose); resolving the manifest from the slug
            # lets the T2 case recognise its own service name. Mirrors the TOML
            # twin's host guard.
            _manifest = mcp_registry.get_manifest_by_config_key(name)
            if deployment.is_proxy_local_mcp_host(parsed.hostname or "", _manifest):
                path = parsed.path or "/"
                # Preserve any existing query AND inject the OtoDock session_id
                # so Docker MCPs (file-tools) can call back to the proxy hooks
                # (resolve-path, document-preview) — mirrors the local CLI's
                # _inject_session_id_into_sse. Without it the MCP has no
                # session, skips resolve-path, and rejects every non-/agents
                # path + drops the preview push.
                from urllib.parse import parse_qs, urlencode
                params = parse_qs(parsed.query, keep_blank_values=True)
                if session_id:
                    params["session_id"] = [session_id]
                query = f"?{urlencode(params, doseq=True)}" if params else ""
                server["url"] = (
                    f"http://127.0.0.1:{sat_port}/mcp/{name}{path}{query}"
                )

        # Rewrite stdio command paths to satellite mcps dir
        if "command" in server:
            cmd = server["command"]
            args = server.get("args", [])
            # Resolve the manifest category/name so we rewrite to the path the
            # satellite actually installs to (see _resolve_satellite_mcp_path_info
            # — the on-disk dir-group/name can disagree with the manifest). The
            # TOML (Codex) twin uses the same resolver so both stay in lockstep.
            sat_category, sat_name, platform_dir, manifest = (
                _resolve_satellite_mcp_path_info(name)
            )
            server["command"], server["args"] = _rewrite_stdio_paths(
                cmd, args, mcp_name=sat_name, satellite_category=sat_category,
                platform_dir=platform_dir, target_os=target_os,
            )

            # Attach the manifest's tool_arg_paths declarations
            # as an env var. The satellite's session manager wraps the
            # command with the stdio interceptor before CLI spawn — using
            # an env var (rather than rewriting command/args here) keeps
            # the proxy out of the satellite's path-resolution business
            # (interceptor + Python interpreter paths live on the
            # satellite). Empty list → omit (interceptor short-circuits
            # to passthrough).
            if manifest is not None and manifest.tool_arg_paths:
                import json as _json
                env_dict = server.get("env") or {}
                env_dict["OTO_TOOL_ARG_PATHS"] = _json.dumps([
                    {
                        "tool": d.tool,
                        "json_path": d.json_path,
                        "mode": d.mode,
                        "optional": d.optional,
                    }
                    for d in manifest.tool_arg_paths
                ])
                server["env"] = env_dict

            # Credential broker: a stdio MCP with a secret bundle gets a
            # per-(session, mcp) cap-token in its env. Its presence makes the
            # satellite wrap the command with the interceptor, which fetches the
            # bundle at spawn. `name` is the mcpServers key = the bundle key.
            if name in secret_bundle_keys:
                from core.credentials import mcp_broker
                env_dict = server.get("env") or {}
                env_dict["OTO_MCP_FETCH_TOKEN"] = mcp_broker.mint_token(
                    session_id, name,
                )
                server["env"] = env_dict

        # Rewrite PROXY_URL in env
        if "env" in server and sat_port:
            server["env"] = _rewrite_env_for_remote(server["env"], sat_port)

        # Per-session JWT for Docker MCPs that call back to the proxy hooks
        # (file-tools, server.proxy_callbacks) and for every gateway entry
        # that keeps the sentinel. The shared build config carries the
        # sentinel bearer; overwrite it with the real session JWT
        # (proxy_api_key already IS one, minted per-session above). The
        # tunnel forwards it unchanged and the proxy judges it.
        if proxy_api_key:
            from auth.session_token import SESSION_JWT_SENTINEL_BEARER
            headers = dict(server.get("headers") or {})
            if headers.get("Authorization") == SESSION_JWT_SENTINEL_BEARER:
                headers["Authorization"] = f"Bearer {proxy_api_key}"
                server["headers"] = headers

        rewritten[name] = server

    return {"mcpServers": rewritten}


# Remote Codex MCPs spawn via the satellite stdio interceptor, fetch their
# credentials over the WS tunnel BEFORE the child process starts, and load off
# the satellite's disk — so the local warm-gate default
# (``startup_timeout_sec = 10``, ``mcp_registry._servers_to_toml``) is too short
# on a cold remote start and Codex closes the connection ("MCP startup
# incomplete"). We raise every remote MCP to a floor, and give known
# heavy-import MCPs extra room. This rewrite is REMOTE-ONLY, so local Codex
# keeps the snappy 10s warm-gate.
_REMOTE_MCP_STARTUP_FLOOR = 60
_REMOTE_MCP_STARTUP_OVERRIDES = {
    # google-workspace imports the entire Google API surface (Gmail/Drive/
    # Calendar/Docs/Sheets/Slides/Forms/Tasks/Contacts/Chat/Apps Script) +
    # FastMCP on first cold start — needs more than the floor over the tunnel.
    "google-workspace": 120,
}


def _rewrite_mcp_toml_for_remote(
    toml_content: str, sat_port: int, *, target_os: str = "linux",
    session_id: str = "", proxy_api_key: str = "",
    secret_bundle_keys: set = frozenset(),
    gateway: dict | None = None, gateway_mode: bool = False,
) -> str:
    """Rewrite TOML MCP config (Codex format) for remote execution.

    - ``command``/``args`` platform mcps paths → the satellite install path,
      resolved category/name-aware via ``_resolve_satellite_mcp_path_info`` +
      ``_rewrite_stdio_paths`` (the exact logic the JSON/CLI twin uses). A naive
      global ``mcps`` prefix swap is WRONG here: it preserves the on-disk
      dir-group/name, but the satellite installs under
      ``mcps/<manifest-category>/<manifest-name>`` — so every MCP whose dir-group
      ≠ category (``custom/`` dir + ``core`` category: memory/task/display/
      meetings/triggers/location/notifications/...) or whose dir-name ≠ name
      (``workspace-mcp`` → ``google-workspace``) got a non-existent path and
      failed to start under Codex. (Claude was immune — its JSON twin always
      resolved via the manifest.)
    - ``url = "http://localhost:<port>/<path>"`` → satellite tunnel URL
      (loopback Docker MCPs only; slug comes from the ``[mcp_servers.<slug>]``
      header so the tunnel path matches the allowlist regex); a
      credential-gateway entry (``gateway``, by slug) takes the shape
      ``_gateway_json_entry`` gives the JSON twin, its header line too.
    - Each stdio MCP's ``env`` inline table gets ``PROXY_URL`` (satellite tunnel)
      + ``PROXY_API_KEY`` (per-session JWT) appended. Codex — unlike Claude CLI —
      does NOT propagate the daemon's process env to MCP subprocesses, so every
      core/custom MCP that calls back to the proxy (task / memory / notifications
      / location / triggers / agent-config / meetings / ...) needs these in its
      TOML ``env`` or it sends an empty ``Authorization: Bearer `` and fails.
      Mirrors the LOCAL codex layer's injection (``layer.py::start_session``).
      HTTP MCPs have no ``env`` block → skipped (they auth via http_headers /
      the session_id URL). The on-disk TOML never carries these (per-session),
      so a plain append before the closing brace is duplicate-key-safe.
    """
    import re
    from auth.session_token import SESSION_JWT_SENTINEL_BEARER
    from services.mcp import mcp_registry
    from core.config import deployment

    section_re = re.compile(r"^\[mcp_servers\.([a-zA-Z0-9_-]+)\]")
    env_re = re.compile(r'^(\s*env\s*=\s*\{)(.*)\}(\s*)$')
    # Capture the host (group 3) instead of hardcoding loopback in the pattern,
    # so the proxy-local check below can also recognise a Docker MCP's service-
    # DNS name in T2. Port (group 4) is optional — vendor URLs may omit it and
    # the rewritten URL uses sat_port anyway; the gate then leaves vendor hosts
    # untouched.
    url_re = re.compile(
        r'^(\s*url\s*=\s*")(https?://)([^/:"]+)(:\d+)?(/[^"]*)?(")'
    )
    cmd_re = re.compile(r'^(\s*command\s*=\s*)"(.*)"\s*$')
    args_re = re.compile(r'^(\s*args\s*=\s*)(\[.*\])\s*$')
    http_headers_re = re.compile(r"^\[mcp_servers\.([a-zA-Z0-9_-]+)\.http_headers\]")
    auth_re = re.compile(r'^(\s*)"?Authorization"?\s*=\s*"Bearer\s+[^"]*"(\s*)$')
    sto_re = re.compile(r'^(\s*startup_timeout_sec\s*=\s*)(\d+)(\s*)$')

    def _esc(v: str) -> str:
        return v.replace("\\", "\\\\").replace('"', '\\"')

    # The inline shape's header line per slug: a vendor entry on a machine
    # below the gateway version.
    inline_headers: dict[str, tuple[str, str]] = {
        slug: (str(f.get("header") or "Authorization"), str(f.get("value") or ""))
        for slug, f in (gateway or {}).items()
        if not f.get("proxy_local") and not gateway_mode
    }

    out_lines: list[str] = []
    current_slug: str | None = None
    for line in toml_content.splitlines(keepends=True):
        nl = "\n" if line.endswith("\n") else ""
        body = line[: -len(nl)] if nl else line

        m = section_re.match(body.strip())
        if m:
            current_slug = m.group(1)
            out_lines.append(line)
            continue

        # Track the `[mcp_servers.<slug>.http_headers]` sub-table so the
        # Authorization swap below resolves to the right MCP's bundle.
        hm = http_headers_re.match(body.strip())
        if hm:
            current_slug = hm.group(1)
            out_lines.append(line)
            continue

        # startup_timeout_sec → raise to the remote floor (per-MCP override for
        # heavy servers). The local warm-gate ships 10s; remote cold starts pay
        # the tunnel broker-fetch + on-satellite import, so 10s times out.
        sm2 = sto_re.match(body)
        if sm2:
            floor = _REMOTE_MCP_STARTUP_OVERRIDES.get(
                current_slug or "", _REMOTE_MCP_STARTUP_FLOOR
            )
            value = max(int(sm2.group(2)), floor)
            out_lines.append(f"{sm2.group(1)}{value}{sm2.group(3)}{nl}")
            continue

        # url → satellite loopback tunnel (proxy-local Docker MCPs only),
        # or the credential gateway's shape for a gateway entry.
        um = url_re.match(body)
        facts = (gateway or {}).get(current_slug) if (um and current_slug) else None
        if um and facts is not None and gateway_key_of(
                f"{um.group(2)}{um.group(3)}{um.group(4) or ''}{um.group(5) or ''}") == current_slug:
            entry = _gateway_json_entry(
                {"url": "", "headers": {}}, current_slug, facts, sat_port, session_id, gateway_mode,
            )
            out_lines.append(f'{um.group(1)}{_esc(entry["url"])}{um.group(6)}{nl}')
            continue
        if um and current_slug and sat_port:
            _host = um.group(3)
            _manifest = mcp_registry.get_manifest_by_config_key(current_slug)
            # Only tunnel proxy-local Docker MCPs (loopback in T1, service-DNS in
            # T2). A vendor URL on a public host (slack/linear) fails this gate
            # and falls through to the loop's default append — left untouched so
            # it still reaches the internet directly. Mirrors the JSON twin.
            if deployment.is_proxy_local_mcp_host(_host or "", _manifest):
                _ind = um.group(1)
                path = um.group(5) or "/"
                end = um.group(6)
                # Inject the OtoDock session_id so Docker MCPs (file-tools) can
                # call back to the proxy hooks — mirrors the JSON twin.
                sess = ""
                if session_id:
                    sess = f"{'&' if '?' in path else '?'}session_id={session_id}"
                out_lines.append(
                    f'{_ind}http://127.0.0.1:{sat_port}/mcp/{current_slug}{path}{sess}{end}{nl}'
                )
                continue

        # command / args → category/name-aware satellite path. Only swaps path
        # prefixes (never introduces quotes/backslashes), so the TOML escaping of
        # the original value is preserved as-is.
        if current_slug:
            cm = cmd_re.match(body)
            if cm:
                cat, sname, pdir, _ = _resolve_satellite_mcp_path_info(current_slug)
                new_cmd, _ = _rewrite_stdio_paths(
                    cm.group(2), [], mcp_name=sname, satellite_category=cat,
                    platform_dir=pdir, target_os=target_os,
                )
                out_lines.append(f'{cm.group(1)}"{new_cmd}"{nl}')
                continue
            am = args_re.match(body)
            if am:
                arg_list = None
                try:
                    parsed = json.loads(am.group(2))
                    if isinstance(parsed, list) and all(isinstance(a, str) for a in parsed):
                        arg_list = parsed
                except (json.JSONDecodeError, ValueError):
                    arg_list = None
                if arg_list is not None:
                    cat, sname, pdir, _ = _resolve_satellite_mcp_path_info(current_slug)
                    _, new_args = _rewrite_stdio_paths(
                        "", arg_list, mcp_name=sname, satellite_category=cat,
                        platform_dir=pdir, target_os=target_os,
                    )
                    # JSON array ≈ TOML inline array of basic strings for these
                    # (the satellite's interceptor-wrap relies on the same).
                    out_lines.append(f'{am.group(1)}{json.dumps(new_args)}{nl}')
                    continue

        # env inline table → append PROXY_URL (satellite tunnel) + PROXY_API_KEY
        # (per-session JWT). Codex doesn't inherit the daemon env into MCP
        # subprocesses, so every callback MCP needs these here (see docstring).
        # HTTP MCPs have no env block → naturally skipped.
        em = env_re.match(body)
        if em and sat_port and proxy_api_key:
            head, inner, trail = em.group(1), em.group(2), em.group(3)
            # Codex gates configured MCP **tool calls** natively, via the
            # mcpServer/elicitation/request approval (handled by the codex
            # approval bridge → decide_tool_permission) — NOT via a transport
            # gate. So the interceptor never does permission gating here (that
            # would double-prompt). We inject the proxy callback creds Codex
            # doesn't propagate to MCP subprocesses, plus the credential
            # broker cap-token for MCPs that have a secret bundle — its presence
            # makes the satellite wrap this section with the interceptor, which
            # fetches the bundle at spawn (credential delivery, not gating).
            # IDEMPOTENT: only add a key that this env block doesn't already
            # declare. ``config_builder.inject_credential_env_into_toml`` bakes
            # PROXY_API_KEY into the on-disk TOML, so re-appending it produced a
            # DUPLICATE key — which the strict ``codex`` TUI / app-server config
            # parser rejects ("duplicate key" → load fails, session dies before
            # any MCP starts). Mirrors the LOCAL codex layer's skip-if-present
            # (``layer.py::start_session``). PROXY_URL is NOT baked on disk
            # (config_builder only injects PROXY_API_KEY) so it's added fresh
            # here, correctly pointing at the satellite tunnel.
            pairs = []
            if '"PROXY_URL"' not in inner:
                pairs.append(("PROXY_URL", f"http://127.0.0.1:{sat_port}"))
            if '"PROXY_API_KEY"' not in inner:
                pairs.append(("PROXY_API_KEY", proxy_api_key))
            if current_slug in secret_bundle_keys and '"OTO_MCP_FETCH_TOKEN"' not in inner:
                from core.credentials import mcp_broker
                pairs.append((
                    "OTO_MCP_FETCH_TOKEN",
                    mcp_broker.mint_token(session_id, current_slug),
                ))
            if not pairs:
                out_lines.append(line)
                continue
            adds = ", ".join(f'"{k}" = "{_esc(v)}"' for k, v in pairs)
            sep = "" if inner.strip() == "" else ", "
            out_lines.append(f'{head}{inner.rstrip()}{sep}{adds} }}{trail}{nl}')
            continue

        # The inline shape: a vendor entry below the gateway version carries
        # its header's value where the sentinel stood; an empty value drops
        # the line (the vendor answers the MCP), and no session token ever
        # reaches a vendor.
        am2 = auth_re.match(body)
        if am2 and current_slug in inline_headers:
            header, value = inline_headers[current_slug]
            if value:
                out_lines.append(
                    f'{am2.group(1)}"{_esc(header)}" = "{_esc(value)}"{am2.group(2)}{nl}'
                )
            continue

        # Per-session JWT for Docker MCPs that call back to the proxy hooks
        # (file-tools, server.proxy_callbacks) and for every gateway entry
        # that keeps the sentinel: swap it for the real session JWT; the
        # tunnel forwards it unchanged and the proxy judges it.
        if am2 and proxy_api_key and SESSION_JWT_SENTINEL_BEARER in body:
            out_lines.append(
                f'{am2.group(1)}"Authorization" = "Bearer {_esc(proxy_api_key)}"'
                f'{am2.group(2)}{nl}'
            )
            continue

        out_lines.append(line)

    return "".join(out_lines)


def _rewrite_stdio_paths(
    command: str, args: list, *,
    mcp_name: str = "", satellite_category: str = "",
    platform_dir: str = "", target_os: str = "linux",
) -> tuple[str, list]:
    """Rewrite stdio MCP command and args to satellite-side paths.

    Platform paths like ``<platform>/mcps/custom/X/venv/bin/python``
    become ``~/<dirname>/mcps/<satellite_category>/X/venv/<venv_bin>/python<exe_suffix>``
    on the satellite — the per-OS bits (``.oto-dock`` / ``OtoDock``,
    ``venv/bin/<bin>`` / ``venv/Scripts/<bin>.exe``, ``python3`` collapsing
    to the ``python`` Windows ships) are the machine's row in the host OS
    table (``core/host_os.py``, the twin of the satellite's own
    ``config.py``), looked up by the reported ``os`` word; an unreported or
    unknown word rewrites for Linux, as it always did.

    Why the category swap is load-bearing: the platform's on-disk
    grouping (``mcps/{core,custom,community}/``) is historical, but the
    manifest's ``category`` field is the canonical answer for where the
    satellite installs them. These can disagree (e.g.
    ``mcps/custom/notifications-mcp/`` with ``category: "core"``). When
    they disagree, the path rewrite must use the manifest category —
    otherwise the CLI on the satellite tries to spawn a binary at a
    path that doesn't exist and MCP init fails.

    ``platform_dir`` is the manifest's absolute on-disk source directory
    (e.g. ``/<platform>/mcps/community/workspace-mcp``). When provided,
    we do an exact-prefix substitution to ``~/<otodock>/mcps/<cat>/<name>``
    — load-bearing for MCPs whose manifest ``name`` differs from the
    platform dir name (e.g. ``workspace-mcp`` dir / ``google-workspace``
    manifest name). The earlier regex variant matched
    ``<platform_mcps>/<any-cat>/<mcp_name>`` and would silently miss
    those, leaving Linux paths in the satellite config.

    Falls back to plain prefix swap when ``platform_dir`` is empty.
    """
    import config as app_config
    platform_mcps = str(app_config.MCPS_DIR.resolve())

    row = host_os.of(target_os) or host_os.ROWS[host_os.LINUX]

    def _rewrite_one(s: str) -> str:
        if platform_mcps not in s:
            return s
        if platform_dir and satellite_category and mcp_name:
            new_prefix = f"~/{row.dirname}/mcps/{satellite_category}/{mcp_name}"
            s = s.replace(platform_dir, new_prefix)
        else:
            s = s.replace(platform_mcps, f"~/{row.dirname}/mcps")
        return host_os.translate_venv(s, row)

    command = _rewrite_one(command)
    new_args = [
        _rewrite_one(arg) if isinstance(arg, str) else arg
        for arg in args
    ]
    return command, new_args


def _rewrite_env_for_remote(env: dict, sat_port: int) -> dict:
    """Rewrite environment variables for remote execution.

    PROXY_URL becomes the satellite's loopback tunnel — hook scripts
    inside spawned subprocesses append ``/v1/hooks/...`` themselves so
    no path is included here.
    """
    rewritten = dict(env)
    for key, value in rewritten.items():
        if isinstance(value, str) and key == "PROXY_URL":
            rewritten[key] = f"http://127.0.0.1:{sat_port}"
    return rewritten
