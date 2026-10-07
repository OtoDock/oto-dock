"""MCP manifest OAuth (``credentials.oauth``) validation.

Strict validator for a manifest's OAuth service declarations — flows, base
scopes, per-service scope formats, proposed bearer-injection hosts — raising
``ValueError`` on any structural defect so a malformed block is rejected at
install time. Provider-specific scope patterns + the accepted OAuth flow set
live here; the generic transport / env-var-name patterns are shared from
``mcp_manifest_types``.

``services.mcp.mcp_registry`` re-exports ``_validate_oauth_services`` (called by
``_parse_manifest`` and the validator tests).
"""

import fnmatch
import re
from typing import Any
from urllib.parse import urlsplit

from services.mcp.mcp_manifest_types import _ENV_VAR_NAME_RE, _HTTP_TRANSPORTS


# Google scope URL pattern. The per-provider map below applies this
# regex only for `provider_id == "google"`; other providers accept any
# non-empty string for scopes (their formats differ).
_GOOGLE_SCOPE_RE = re.compile(r"^https://www\.googleapis\.com/auth/.+")

# Known provider_ids whose scope strings have a vendor-specific URL
# format. Unknown providers skip the URL pattern check (only enforce
# "non-empty string").
_PROVIDER_SCOPE_PATTERNS = {
    "google": _GOOGLE_SCOPE_RE,
}

# Allowed OAuth flow types. ``authorization_code`` is the only flow
# the runtime exercises today; the others are accepted by the validator
# so manifests can declare them for future implementations.
_OAUTH_FLOWS = {
    "authorization_code",
    "authorization_code_pkce",
    "device_code",
    "client_credentials",
    "service_account",
    "personal_access_token",
}

# Hosts in `proposed_hosts` must look like valid hostnames (no scheme,
# no path). Allow letters, digits, hyphen, dot, and ``*`` for wildcard
# subdomains (matches the allowlist matcher in storage.identity.bearer_allowlist).
_HOSTNAME_RE = re.compile(r"^[*A-Za-z0-9](?:[-A-Za-z0-9.*])*$")

# ``credentials.oauth.authorization_server``: the MCP server names its own
# authorization server (RFC 9728 and RFC 8414) and the install registers
# itself there (RFC 7591) instead of carrying an admin's app credentials.
_REGISTRATION_MODES = {"dynamic"}
# A token issued for the MCP server never reaches a shell, another host or
# an authorize-URL quirk, and the registered client has one flow: these keys
# are refused beside the block. The app-flow URLs (``authorization_url``,
# ``token_url``, ``revoke_url``) may stay: they serve an admin's own app when
# one is configured, and the registered-client flow discovers its own.
_KEYS_EXCLUDED_WITH_AUTHORIZATION_SERVER = (
    "device_authorization_url", "app_credential_variants", "authorize_params",
    "env_injection", "mcp_env_injection", "git_credential_helper",
)
_IDENTITY_FIELDS = ("label_field", "display_field", "id_field")


def _validate_oauth_services(raw: Any, mcp_name: str, server_raw: Any = None) -> None:
    """Strict validator for the ``credentials.oauth`` block.

    Mirrors the ``_parse_costs_block`` precedent — raises ``ValueError`` on
    any structural defect so the install pipeline rejects bad manifests with
    a clear error before they reach runtime. ``raw=None`` is allowed (no
    oauth block declared = nothing to validate).

    Fields validated:
      * ``provider_id`` (required when oauth is declared)
      * ``flow``, ``authorization_url``, ``token_url``, ``revoke_url``,
        ``userinfo_url``, ``userinfo_*_field`` — manifest declares URLs
        so a ``GenericOAuthProvider`` can be built without a Python subclass
      * ``token_format`` (``schema`` + ``filename_pattern``)
      * ``refresh`` (``strategy`` + ``min_remaining_seconds``)
      * ``supports_multi_account``, ``registered_app_required``
      * ``bearer_required`` + ``proposed_hosts`` for remote-HTTP MCPs
        that need ``Authorization: Bearer`` injection. When
        ``bearer_required=true``, validator also cross-checks
        ``server.transport`` is HTTP-class.
      * ``authorization_server`` for an MCP server that names its own
        authorization server (``_validate_authorization_server``): the
        block's own fields, the keys it excludes, and the literal https
        ``server.url_template`` host matching ``proposed_hosts``.

    Scope validation uses per-provider regex: Google scopes validated by
    ``_GOOGLE_SCOPE_RE``; other providers only enforce non-empty strings.
    """
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ValueError(
            f"credentials.oauth must be an object, got {type(raw).__name__}"
        )

    # Reject the older `provider` field name — manifests must use
    # `provider_id` (matches OAuthProvider.provider_id).
    if "provider" in raw and "provider_id" not in raw:
        raise ValueError(
            "credentials.oauth.provider is not a valid key — use 'provider_id' "
            "(matches OAuthProvider.provider_id)"
        )

    provider_id = raw.get("provider_id", "")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError(
            "credentials.oauth.provider_id must be a non-empty string"
        )

    # `flows` is a non-empty list of valid grant types the user can pick
    # at connect time. Single-flow MCPs declare a one-element list. The
    # first element is the default the engine uses for the
    # authorization_code-style routes (other entries get picked via the
    # dashboard's flow picker).
    if "flow" in raw:
        raise ValueError(
            "credentials.oauth.flow (singular) is not supported — declare "
            "flows: [\"...\"] (a list) instead"
        )
    flows_list = raw.get("flows")
    if not isinstance(flows_list, list) or not flows_list:
        raise ValueError(
            "credentials.oauth.flows must be a non-empty list of grant types"
        )
    for fidx, fv in enumerate(flows_list):
        if not isinstance(fv, str) or fv not in _OAUTH_FLOWS:
            raise ValueError(
                f"credentials.oauth.flows[{fidx}]={fv!r} is not a valid flow; "
                f"valid: {sorted(_OAUTH_FLOWS)}"
            )

    # Identity-probe request shape: vendors without a REST userinfo endpoint
    # (Linear is GraphQL-only) declare POST + a static JSON body.
    um = raw.get("userinfo_method")
    if um is not None and um not in ("GET", "POST"):
        raise ValueError(
            "credentials.oauth.userinfo_method must be 'GET' or 'POST'"
        )
    ub = raw.get("userinfo_body")
    if ub is not None and not isinstance(ub, dict):
        raise ValueError(
            "credentials.oauth.userinfo_body must be an object when declared"
        )

    # Static string→string maps: extra authorize-URL query params (Notion's
    # mandatory `owner=user`) and extra userinfo request headers (Notion's
    # mandatory `Notion-Version`). Both optional; consumed by oauth_start /
    # GenericOAuthProvider.fetch_userinfo.
    for map_field in ("authorize_params", "userinfo_headers"):
        mv = raw.get(map_field)
        if mv is None:
            continue
        if not isinstance(mv, dict) or not all(
            isinstance(k, str) and k and isinstance(v, str)
            for k, v in mv.items()
        ):
            raise ValueError(
                f"credentials.oauth.{map_field} must be an object of "
                "non-empty string keys to string values"
            )

    # URL fields — required for non-hardcoded providers but optional for
    # hardcoded ones (their Python class supplies the URLs). The
    # GenericOAuthProvider builder will refuse to build if these are
    # missing for a manifest-driven provider, so we don't enforce here.
    # `device_authorization_url` is the device-code flow endpoint
    # (Microsoft `urn:ietf:params:oauth:grant-type:device_code`).
    # `tenant_id` lets single-tenant deployments override the default
    # `/common/` endpoint (Microsoft only).
    for url_field in (
        "authorization_url",
        "token_url",
        "revoke_url",
        "userinfo_url",
        "device_authorization_url",
        "tenant_id",
    ):
        val = raw.get(url_field, "")
        if val and not isinstance(val, str):
            raise ValueError(
                f"credentials.oauth.{url_field} must be a string when declared"
            )

    # token_format — optional; if declared must have a schema string and
    # filename_pattern string. `aliases` is the legacy-key map for MCPs
    # that read tokens via library-specific shapes (workspace-mcp's
    # google.auth expects `token`/`expiry`; aliases tell the writer to
    # ALSO emit those keys alongside canonical `access_token`/`expires_at`).
    tf = raw.get("token_format")
    if tf is not None:
        if not isinstance(tf, dict):
            raise ValueError(
                "credentials.oauth.token_format must be an object when declared"
            )
        schema = tf.get("schema", "generic_oauth_v1")
        if not isinstance(schema, str) or not schema.strip():
            raise ValueError(
                "credentials.oauth.token_format.schema must be a non-empty string"
            )
        if schema != "generic_oauth_v1":
            raise ValueError(
                f"credentials.oauth.token_format.schema={schema!r} is not recognized; "
                f"only 'generic_oauth_v1' is supported"
            )
        pattern = tf.get("filename_pattern", "{account_label}.json")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError(
                "credentials.oauth.token_format.filename_pattern must be a non-empty string"
            )
        aliases = tf.get("aliases")
        if aliases is not None:
            if not isinstance(aliases, dict):
                raise ValueError(
                    "credentials.oauth.token_format.aliases must be an object when declared"
                )
            for ak, av in aliases.items():
                if not isinstance(ak, str) or not ak.strip():
                    raise ValueError(
                        "credentials.oauth.token_format.aliases keys must be non-empty strings"
                    )
                if not isinstance(av, str) or not av.strip():
                    raise ValueError(
                        f"credentials.oauth.token_format.aliases[{ak!r}] must be a "
                        f"non-empty string (the canonical key name)"
                    )

    # app_credential_variants — optional map of {flow_name: credential_bundle_name}
    # for providers (Zoom) where OAuth and S2S need DIFFERENT app credential
    # rows. When set, the engine reads variants[flow] instead of `app_credential`.
    variants = raw.get("app_credential_variants")
    if variants is not None:
        if not isinstance(variants, dict) or not variants:
            raise ValueError(
                "credentials.oauth.app_credential_variants must be a non-empty object "
                "when declared"
            )
        for vk, vv in variants.items():
            if vk not in _OAUTH_FLOWS:
                raise ValueError(
                    f"credentials.oauth.app_credential_variants key {vk!r} is not a "
                    f"valid flow; valid: {sorted(_OAUTH_FLOWS)}"
                )
            if not isinstance(vv, str) or not vv.strip():
                raise ValueError(
                    f"credentials.oauth.app_credential_variants[{vk!r}] must be a "
                    f"non-empty credential bundle name"
                )

    # account_credential_keys — optional override for the DB credential
    # key names where (email, services) get stored. Defaults to
    # {PROVIDER_ID}_EMAIL / {PROVIDER_ID}_SERVICES; legacy workspace-mcp
    # uses GOOGLE_EMAIL / GOOGLE_SERVICES.
    ack = raw.get("account_credential_keys")
    if ack is not None:
        if not isinstance(ack, dict):
            raise ValueError(
                "credentials.oauth.account_credential_keys must be an object when declared"
            )
        for required_field in ("email", "services"):
            v = ack.get(required_field)
            if v is not None and (not isinstance(v, str) or not v.strip()):
                raise ValueError(
                    f"credentials.oauth.account_credential_keys.{required_field} must "
                    f"be a non-empty string when declared"
                )

    # refresh — optional; if declared must have a strategy string + integer threshold
    refresh = raw.get("refresh")
    if refresh is not None:
        if not isinstance(refresh, dict):
            raise ValueError(
                "credentials.oauth.refresh must be an object when declared"
            )
        strategy = refresh.get("strategy", "lazy")
        if strategy not in ("lazy", "eager"):
            raise ValueError(
                f"credentials.oauth.refresh.strategy={strategy!r} must be 'lazy' or 'eager'"
            )
        threshold = refresh.get("min_remaining_seconds", 300)
        if not isinstance(threshold, int) or threshold <= 0:
            raise ValueError(
                "credentials.oauth.refresh.min_remaining_seconds must be a positive integer"
            )

    # Boolean flags — type-check only.
    for bool_field in ("supports_multi_account", "registered_app_required", "bearer_required"):
        val = raw.get(bool_field, False)
        if not isinstance(val, bool):
            raise ValueError(
                f"credentials.oauth.{bool_field} must be a boolean when declared"
            )

    # env_injection / mcp_env_injection — optional lists of env-var names
    # the resolver fills with the bound account's canonical access_token at
    # session start. `env_injection` lands in the agent's BASH env so CLIs
    # (`git`, `gh`, `aws`, ...) authenticate with the same token the MCP
    # uses; `mcp_env_injection` lands in the MCP SERVER subprocess env
    # (broker-delivered, never in config files) for stdio MCPs whose
    # upstream reads its token from env (notion's NOTION_TOKEN). Names must
    # match POSIX env-var rules, no duplicates.
    for _inj_field in ("env_injection", "mcp_env_injection"):
        env_injection = raw.get(_inj_field)
        if env_injection is None:
            continue
        if not isinstance(env_injection, list) or not env_injection:
            raise ValueError(
                f"credentials.oauth.{_inj_field} must be a non-empty list of "
                "env-var name strings when declared"
            )
        seen_env_names: set[str] = set()
        for eidx, ev in enumerate(env_injection):
            if not isinstance(ev, str) or not ev.strip():
                raise ValueError(
                    f"credentials.oauth.{_inj_field}[{eidx}] must be a non-empty string"
                )
            if not _ENV_VAR_NAME_RE.match(ev):
                raise ValueError(
                    f"credentials.oauth.{_inj_field}[{eidx}]={ev!r} is not a valid "
                    f"POSIX env-var name (must match ^[A-Z_][A-Z0-9_]*$)"
                )
            if ev in seen_env_names:
                raise ValueError(
                    f"credentials.oauth.{_inj_field}[{eidx}]={ev!r} is a duplicate"
                )
            seen_env_names.add(ev)

    # git_credential_helper — optional; when set the resolver wires git's
    # credential helper (via GIT_CONFIG_*) so `git push`/`clone` authenticate
    # without the proxy-host system helper (e.g. on satellites). Shape:
    # {"host": "github.com", "helper": "!gh auth git-credential"}.
    git_cred = raw.get("git_credential_helper")
    if git_cred is not None:
        if not isinstance(git_cred, dict):
            raise ValueError(
                "credentials.oauth.git_credential_helper must be an object with "
                "'host' and 'helper' string fields when declared"
            )
        for _k in ("host", "helper"):
            _v = git_cred.get(_k)
            if not isinstance(_v, str) or not _v.strip():
                raise ValueError(
                    f"credentials.oauth.git_credential_helper.{_k} must be a "
                    f"non-empty string"
                )

    # bearer_required additional validation: when set, the manifest MUST
    # declare proposed_hosts (allowed vendor hosts), and the MCP's
    # transport MUST be HTTP-class. Cross-check `server.transport`.
    bearer_required = raw.get("bearer_required", False)
    if bearer_required:
        proposed = raw.get("proposed_hosts")
        if not isinstance(proposed, list) or not proposed:
            raise ValueError(
                "credentials.oauth.bearer_required=true requires "
                "credentials.oauth.proposed_hosts as a non-empty list"
            )
        for hidx, host in enumerate(proposed):
            if not isinstance(host, str) or not host.strip():
                raise ValueError(
                    f"credentials.oauth.proposed_hosts[{hidx}] must be a non-empty string"
                )
            if not _HOSTNAME_RE.match(host):
                raise ValueError(
                    f"credentials.oauth.proposed_hosts[{hidx}]={host!r} is not a valid hostname"
                )

        # Cross-check transport when server_raw was supplied (this happens
        # at manifest parse time — _parse_manifest passes it through).
        if isinstance(server_raw, dict):
            transport = server_raw.get("transport", "")
            if transport not in _HTTP_TRANSPORTS:
                raise ValueError(
                    f"credentials.oauth.bearer_required=true requires HTTP-class "
                    f"server.transport; got {transport!r}"
                )
            # The credential gateway confines every forward to the one
            # declared path, which an SSE endpoint event escapes (the
            # ``api_key_header`` validator's rule).
            if transport == "sse":
                raise ValueError(
                    "credentials.oauth.bearer_required=true requires a streamable-HTTP "
                    "server.transport: the credential gateway forwards only to the one "
                    f"declared path, which an SSE endpoint escapes; got {transport!r}"
                )

    _validate_authorization_server(raw, server_raw)

    # Per-provider scope regex. Unknown providers skip the URL pattern
    # check; only the "non-empty string" rule applies.
    scope_pattern = _PROVIDER_SCOPE_PATTERNS.get(provider_id)

    # base_scopes — optional but if present must be a non-empty list of valid scopes
    base = raw.get("base_scopes")
    if base is not None:
        if not isinstance(base, list) or not base:
            raise ValueError(
                "credentials.oauth.base_scopes must be a non-empty list when declared"
            )
        for idx, s in enumerate(base):
            if not isinstance(s, str) or not s.strip():
                raise ValueError(
                    f"credentials.oauth.base_scopes[{idx}] must be a non-empty string"
                )
            if scope_pattern is not None and s != "openid" and not scope_pattern.match(s):
                raise ValueError(
                    f"credentials.oauth.base_scopes[{idx}]={s!r} is not a valid "
                    f"{provider_id} scope"
                )

    # services — optional; if present must be a non-empty list of well-formed entries
    services = raw.get("services")
    if services is None:
        return
    if not isinstance(services, list) or not services:
        raise ValueError(
            "credentials.oauth.services must be a non-empty list when declared"
        )

    seen_keys: set[str] = set()
    for idx, svc in enumerate(services):
        if not isinstance(svc, dict):
            raise ValueError(
                f"credentials.oauth.services[{idx}] must be an object, "
                f"got {type(svc).__name__}"
            )
        key = svc.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                f"credentials.oauth.services[{idx}].key must be a non-empty string"
            )
        if key in seen_keys:
            raise ValueError(
                f"credentials.oauth.services[{idx}] duplicates an earlier key={key!r}"
            )
        seen_keys.add(key)

        for field_name in ("label", "description"):
            v = svc.get(field_name)
            if not isinstance(v, str) or not v.strip():
                raise ValueError(
                    f"credentials.oauth.services[{idx}].{field_name} must be a "
                    f"non-empty string (service key={key!r})"
                )

        scopes = svc.get("scopes")
        if not isinstance(scopes, list):
            raise ValueError(
                f"credentials.oauth.services[{idx}].scopes must be a list "
                f"(service key={key!r})"
            )
        # Empty scopes allowed (future: OAuth-login-only services); per-string
        # validation still applies if any are declared.
        for sidx, s in enumerate(scopes):
            if not isinstance(s, str) or not s.strip():
                raise ValueError(
                    f"credentials.oauth.services[{idx}].scopes[{sidx}] must be a "
                    f"non-empty string (service key={key!r})"
                )
            if scope_pattern is not None and s != "openid" and not scope_pattern.match(s):
                raise ValueError(
                    f"credentials.oauth.services[{idx}].scopes[{sidx}]={s!r} is not "
                    f"a valid {provider_id} scope (service key={key!r})"
                )

        # Optional capabilities list — surfaced as red-bordered consent
        # warnings in the User Settings UI ("posts as you in any channel"
        # for Slack `chat:write`, etc.).
        caps = svc.get("capabilities")
        if caps is not None:
            if not isinstance(caps, list):
                raise ValueError(
                    f"credentials.oauth.services[{idx}].capabilities must be a list "
                    f"(service key={key!r})"
                )
            for cidx, cap in enumerate(caps):
                if not isinstance(cap, str) or not cap.strip():
                    raise ValueError(
                        f"credentials.oauth.services[{idx}].capabilities[{cidx}] must "
                        f"be a non-empty string (service key={key!r})"
                    )

        # Optional per-service booleans (Microsoft admin consent /
        # Zoom S2S-vs-user-OAuth gating).
        for bf in ("requires_admin_consent", "requires_user_oauth"):
            if bf in svc and not isinstance(svc[bf], bool):
                raise ValueError(
                    f"credentials.oauth.services[{idx}].{bf} must be a boolean "
                    f"(service key={key!r})"
                )


def _validate_authorization_server(raw: dict, server_raw: Any = None) -> None:
    """The ``credentials.oauth.authorization_server`` block.

    The server's own metadata names every endpoint the registered client
    uses; the install registers itself as a public client (``confidential: true`` asks for a secret)
    with PKCE, so ``flows`` is exactly ``["authorization_code_pkce"]``; the
    token is sent to the MCP server only, so ``bearer_required`` is set and
    the literal https ``server.url_template`` host matches ``proposed_hosts``
    (the allowlist's ``fnmatch`` rule). A hardcoded provider id (its Python
    class speaks another protocol) cannot carry the block.
    ``accepts_app_tokens`` says the server also takes the vendor's own app
    tokens (the relay's or an admin's app), which makes the registered
    client the fallback sign-in; by default the server takes its own
    authorization server's tokens only.
    """
    block = raw.get("authorization_server")
    if block is None:
        return
    if not isinstance(block, dict):
        raise ValueError(
            "credentials.oauth.authorization_server must be an object when declared"
        )
    if raw.get("flows") != ["authorization_code_pkce"]:
        raise ValueError(
            "credentials.oauth.authorization_server requires flows to be exactly "
            "[\"authorization_code_pkce\"]"
        )
    for key in _KEYS_EXCLUDED_WITH_AUTHORIZATION_SERVER:
        if key in raw:
            raise ValueError(
                f"credentials.oauth.{key} cannot be declared beside "
                "authorization_server (the registered client's token is sent to "
                "the MCP server only, through one flow)"
            )
    registration = block.get("registration", "dynamic")
    if registration not in _REGISTRATION_MODES:
        raise ValueError(
            f"credentials.oauth.authorization_server.registration={registration!r} "
            f"is not supported; valid: {sorted(_REGISTRATION_MODES)}"
        )
    if not isinstance(block.get("confidential", False), bool):
        raise ValueError(
            "credentials.oauth.authorization_server.confidential must be a boolean "
            "when declared"
        )
    if not isinstance(block.get("accepts_app_tokens", False), bool):
        raise ValueError(
            "credentials.oauth.authorization_server.accepts_app_tokens must be a "
            "boolean when declared"
        )
    issuer = block.get("issuer", "")
    if issuer:
        if not isinstance(issuer, str):
            raise ValueError(
                "credentials.oauth.authorization_server.issuer must be a string"
            )
        parts = urlsplit(issuer)
        if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
            raise ValueError(
                "credentials.oauth.authorization_server.issuer must be an https URL "
                "with no query or fragment"
            )
    client_name = block.get("client_name", "")
    if client_name is not None and not isinstance(client_name, str):
        raise ValueError(
            "credentials.oauth.authorization_server.client_name must be a string"
        )
    scopes = block.get("scopes")
    if scopes is not None:
        if not isinstance(scopes, list) or not all(
            isinstance(sc, str) and sc.strip() for sc in scopes
        ):
            raise ValueError(
                "credentials.oauth.authorization_server.scopes must be a list of "
                "non-empty strings when declared"
            )
    identity = block.get("identity")
    if identity is not None:
        if not isinstance(identity, dict) or not identity:
            raise ValueError(
                "credentials.oauth.authorization_server.identity must be a non-empty "
                "object when declared"
            )
        for ik, iv in identity.items():
            if ik not in _IDENTITY_FIELDS:
                raise ValueError(
                    f"credentials.oauth.authorization_server.identity.{ik} is not a "
                    f"known field; valid: {list(_IDENTITY_FIELDS)}"
                )
            if not isinstance(iv, str) or not iv.strip():
                raise ValueError(
                    f"credentials.oauth.authorization_server.identity.{ik} must be a "
                    "non-empty string"
                )
    userinfo_url = raw.get("userinfo_url", "")
    if userinfo_url and urlsplit(str(userinfo_url)).scheme != "https":
        raise ValueError(
            "credentials.oauth.userinfo_url must be an https URL beside "
            "authorization_server (the token reaches the MCP server's host only)"
        )
    from auth.oauth_providers import hardcoded_provider_ids
    provider_id = raw.get("provider_id", "")
    if provider_id in hardcoded_provider_ids():
        raise ValueError(
            f"credentials.oauth.provider_id={provider_id!r} is a provider with its "
            "own Python class and cannot carry authorization_server"
        )
    if not raw.get("bearer_required", False):
        raise ValueError(
            "credentials.oauth.authorization_server requires bearer_required=true "
            "(the token reaches the MCP server as its bearer)"
        )
    if isinstance(server_raw, dict):
        url_template = server_raw.get("url_template", "")
        if not isinstance(url_template, str) or not url_template:
            raise ValueError(
                "credentials.oauth.authorization_server requires server.url_template"
            )
        parts = urlsplit(url_template)
        if parts.scheme != "https" or "${" in parts.netloc or not parts.hostname:
            raise ValueError(
                "credentials.oauth.authorization_server requires an https "
                "server.url_template with a literal host"
            )
        host = parts.hostname.lower()
        proposed = [str(h).lower() for h in (raw.get("proposed_hosts") or [])]
        if not any(fnmatch.fnmatchcase(host, pat) for pat in proposed):
            raise ValueError(
                f"credentials.oauth.proposed_hosts must match the server.url_template "
                f"host {host!r} when authorization_server is declared"
            )

