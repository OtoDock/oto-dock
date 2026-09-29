"""MCP manifest webhook-receiver validation.

Strict validators for the manifest ``credentials.webhooks`` block — signature /
url_verification / registration / event_catalog / payload_normalization /
vendor_target_spec — and the per-registration-call validator. Raise
``ValueError`` on any structural defect so a malformed block is rejected at
install time, not at first event. Pure ``re`` + dict-walking, no I/O.

``services.mcp.mcp_registry`` re-exports ``_validate_webhooks_block`` (the entry
point `_parse_manifest` and the tests call); the ``_WEBHOOK_*`` enums and
``_validate_webhook_registration_call`` are internal to this module.
"""

import re
from typing import Any


# ---------------------------------------------------------------------------
# Webhook receiver validation
# ---------------------------------------------------------------------------

_WEBHOOK_SIGNATURE_ALGORITHMS = {
    "hmac-sha1",
    "hmac-sha256",
    "hmac-sha512",
    # ``client_state_echo`` signals "no HMAC; provider override handles
    # verification" (Microsoft Graph compares ``value[].clientState`` to the
    # row's signing secret). Generic provider rejects it with
    # ``unsupported_algorithm`` — only valid when a hardcoded subclass is in
    # the registry for that provider_id. The validator skips the
    # non-empty-header rule for this algorithm (header / signed_payload_template
    # / version_prefix are all ignored by the subclass).
    "client_state_echo",
}
_WEBHOOK_URL_VERIFICATION_KINDS = {
    "none",
    "slack_challenge",
    "ms_graph_validation_token",
    "zoom_endpoint_validation",
    # Notion-class: the vendor's one-time UNSIGNED setup POST carries the
    # permanent signing secret (`request_field`) in-band; the dispatcher
    # stores it on the row (first-writer) and ACKs. Requires
    # signature.per_subscription_secret=true + request_source="body".
    "verification_token_capture",
}
_WEBHOOK_REGISTRATION_MODES = {"auto", "manual"}
_WEBHOOK_VENDOR_TARGET_KINDS = {"free_text", "remote_list", "static_list"}
_WEBHOOK_TIMESTAMP_FORMATS = {"unix", "unix_ms", "iso8601"}
_WEBHOOK_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _validate_webhooks_block(raw: Any, mcp_name: str) -> None:
    """Strict validator for the ``credentials.webhooks`` block.

    Mirrors ``_validate_oauth_services`` — raises ``ValueError`` on any
    structural defect so manifest install rejects bad blocks before runtime.
    ``raw=None`` is allowed (no webhooks block declared = nothing to validate).

    The block lets a vendor MCP declare:
      * ``signature`` — HMAC algorithm + header + timestamp window
      * ``url_verification`` — vendor handshake on first POST / on subscribe
      * ``registration`` — vendor API templates for auto-register / renew / delete
      * ``event_catalog`` — subscribable event types with required scopes
      * ``payload_normalization`` — vendor payload → ${trigger.*} mapping
      * ``vendor_target_spec`` — what the user picks at subscribe time
      * ``event_id_field`` — dedup key path

    A future contributor adds a new vendor receiver by writing this block
    (and at most a thin Python subclass for signature quirks). The framework
    rejects malformed blocks here so contributors get a clear error at
    install time, not at first event.
    """
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ValueError(
            f"credentials.webhooks must be an object, got {type(raw).__name__}"
        )

    # `available` — boolean (default false). When false, the block is
    # parsed but the receiver framework treats this MCP as having no
    # webhook capability. Lets contributors stage manifests with
    # incomplete webhook blocks without breaking install.
    available = raw.get("available", False)
    if not isinstance(available, bool):
        raise ValueError(
            "credentials.webhooks.available must be a boolean when declared"
        )
    if not available:
        # Skip deeper validation when feature-flagged off.
        return

    provider_id = raw.get("provider_id", "")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError(
            "credentials.webhooks.provider_id must be a non-empty string"
        )
    # Reserved-word guard: vendor webhook URLs live at
    # /v1/webhooks/{provider_id}/{subscription_id}; generic webhook URLs live
    # at /v1/webhooks/{scope}/{owner}/{slug} where scope ∈ {agent, user};
    # relay-forwarded events land at /v1/webhooks/relay/{provider_id}.
    # Reserving these as provider_ids prevents a future MCP from accidentally
    # shadowing either URL family.
    if provider_id in ("agent", "user", "relay"):
        raise ValueError(
            f"credentials.webhooks.provider_id={provider_id!r} is reserved "
            f"(would collide with the generic /v1/webhooks/agent|user/... or "
            f"relay /v1/webhooks/relay/... URL families)"
        )

    # --- signature sub-block (required when available=true) ---
    sig = raw.get("signature")
    if not isinstance(sig, dict):
        raise ValueError(
            "credentials.webhooks.signature must be an object when available=true"
        )
    algo = sig.get("algorithm", "")
    if algo not in _WEBHOOK_SIGNATURE_ALGORITHMS:
        raise ValueError(
            f"credentials.webhooks.signature.algorithm={algo!r} is not valid; "
            f"valid: {sorted(_WEBHOOK_SIGNATURE_ALGORITHMS)}"
        )
    header = sig.get("header", "")
    if not isinstance(header, str):
        raise ValueError(
            "credentials.webhooks.signature.header must be a string"
        )
    # ``client_state_echo`` doesn't read any header — verification is in the
    # subclass override (compares body's ``value[].clientState`` to the
    # signing secret). All other algorithms must declare a non-empty header.
    if algo != "client_state_echo" and not header.strip():
        raise ValueError(
            "credentials.webhooks.signature.header must be a non-empty string "
            "for HMAC algorithms"
        )
    for str_field in ("prefix", "version_prefix", "signed_payload_template",
                      "timestamp_header", "secret_credential_key"):
        v = sig.get(str_field, "")
        if not isinstance(v, str):
            raise ValueError(
                f"credentials.webhooks.signature.{str_field} must be a string when declared"
            )
    ts_format = sig.get("timestamp_format", "unix")
    if ts_format not in _WEBHOOK_TIMESTAMP_FORMATS:
        raise ValueError(
            f"credentials.webhooks.signature.timestamp_format={ts_format!r} is not valid; "
            f"valid: {sorted(_WEBHOOK_TIMESTAMP_FORMATS)}"
        )
    max_age = sig.get("max_age_seconds", 300)
    if not isinstance(max_age, int) or max_age < 0:
        raise ValueError(
            "credentials.webhooks.signature.max_age_seconds must be a non-negative integer"
        )
    per_sub = sig.get("per_subscription_secret", False)
    if not isinstance(per_sub, bool):
        raise ValueError(
            "credentials.webhooks.signature.per_subscription_secret must be a boolean"
        )
    # When secret is NOT per-subscription, we need to know which infra_credential
    # key to read at verify time. Cross-check that secret_credential_key is set.
    if not per_sub and not sig.get("secret_credential_key", "").strip():
        raise ValueError(
            "credentials.webhooks.signature.secret_credential_key must be set when "
            "per_subscription_secret=false (so the dispatcher knows which infra "
            "credential to read for HMAC verification)"
        )

    # --- url_verification sub-block (required when available=true) ---
    uv = raw.get("url_verification")
    if not isinstance(uv, dict):
        raise ValueError(
            "credentials.webhooks.url_verification must be an object when available=true"
        )
    kind = uv.get("kind", "")
    if kind not in _WEBHOOK_URL_VERIFICATION_KINDS:
        raise ValueError(
            f"credentials.webhooks.url_verification.kind={kind!r} is not valid; "
            f"valid: {sorted(_WEBHOOK_URL_VERIFICATION_KINDS)}"
        )
    if kind != "none":
        for str_field in ("request_field", "request_source", "response_field",
                          "response_content_type"):
            v = uv.get(str_field, "")
            if not isinstance(v, str) or not v.strip():
                raise ValueError(
                    f"credentials.webhooks.url_verification.{str_field} must be a "
                    f"non-empty string when kind={kind!r}"
                )
        rs = uv.get("request_source", "")
        if rs not in ("body", "query"):
            raise ValueError(
                f"credentials.webhooks.url_verification.request_source={rs!r} must be "
                f"'body' or 'query'"
            )
    if kind == "verification_token_capture":
        if uv.get("request_source") != "body":
            raise ValueError(
                "credentials.webhooks.url_verification.request_source must be "
                "'body' when kind='verification_token_capture' (the token "
                "arrives in the vendor's setup POST body)"
            )
        if not (raw.get("signature") or {}).get("per_subscription_secret", False):
            raise ValueError(
                "credentials.webhooks.url_verification.kind="
                "'verification_token_capture' requires "
                "signature.per_subscription_secret=true (the captured token "
                "is stored per subscription row)"
            )

    # --- registration sub-block (required when available=true) ---
    reg = raw.get("registration")
    if not isinstance(reg, dict):
        raise ValueError(
            "credentials.webhooks.registration must be an object when available=true"
        )
    mode = reg.get("mode", "")
    if mode not in _WEBHOOK_REGISTRATION_MODES:
        raise ValueError(
            f"credentials.webhooks.registration.mode={mode!r} must be one of "
            f"{sorted(_WEBHOOK_REGISTRATION_MODES)}"
        )
    if mode == "manual":
        instructions = reg.get("manual_instructions_url", "")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError(
                "credentials.webhooks.registration.manual_instructions_url must be a "
                "non-empty URL when mode='manual' (admins need a vendor-side setup link)"
            )
    if mode == "auto":
        # `create` is required for auto-register
        create = reg.get("create")
        _validate_webhook_registration_call(create, mcp_name, "create", required=True)
        # `delete` is optional but recommended (graceful cleanup on disconnect)
        delete = reg.get("delete")
        _validate_webhook_registration_call(delete, mcp_name, "delete", required=False)
        # `renew` is optional (only vendors with expiring subscriptions)
        renew = reg.get("renew")
        _validate_webhook_registration_call(renew, mcp_name, "renew", required=False)
        if renew is not None:
            renew_before = renew.get("renew_before_seconds", 86400)
            if not isinstance(renew_before, int) or renew_before <= 0:
                raise ValueError(
                    "credentials.webhooks.registration.renew.renew_before_seconds must "
                    "be a positive integer"
                )

    lifetime = reg.get("lifetime_seconds")
    if lifetime is not None:
        if not isinstance(lifetime, int) or lifetime <= 0:
            raise ValueError(
                "credentials.webhooks.registration.lifetime_seconds must be a positive "
                "integer when declared (vendor subscription TTL in seconds)"
            )

    # --- event_catalog sub-block ---
    catalog = raw.get("event_catalog")
    if not isinstance(catalog, list) or not catalog:
        raise ValueError(
            "credentials.webhooks.event_catalog must be a non-empty list when available=true"
        )
    seen_event_keys: set[str] = set()
    for idx, entry in enumerate(catalog):
        if not isinstance(entry, dict):
            raise ValueError(
                f"credentials.webhooks.event_catalog[{idx}] must be an object"
            )
        key = entry.get("key", "")
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                f"credentials.webhooks.event_catalog[{idx}].key must be a non-empty string"
            )
        if key in seen_event_keys:
            raise ValueError(
                f"credentials.webhooks.event_catalog[{idx}] duplicates key={key!r}"
            )
        seen_event_keys.add(key)
        label = entry.get("label", "")
        if not isinstance(label, str) or not label.strip():
            raise ValueError(
                f"credentials.webhooks.event_catalog[{idx}].label must be a non-empty string"
            )
        for opt_str in ("description",):
            v = entry.get(opt_str, "")
            if not isinstance(v, str):
                raise ValueError(
                    f"credentials.webhooks.event_catalog[{idx}].{opt_str} must be a string"
                )
        scopes = entry.get("required_scopes", [])
        if not isinstance(scopes, list):
            raise ValueError(
                f"credentials.webhooks.event_catalog[{idx}].required_scopes must be a list"
            )
        for sidx, s in enumerate(scopes):
            if not isinstance(s, str) or not s.strip():
                raise ValueError(
                    f"credentials.webhooks.event_catalog[{idx}].required_scopes[{sidx}] "
                    f"must be a non-empty string"
                )
        subevents = entry.get("subevents")
        if subevents is not None:
            if not isinstance(subevents, list):
                raise ValueError(
                    f"credentials.webhooks.event_catalog[{idx}].subevents must be a list"
                )
            for sidx, se in enumerate(subevents):
                if not isinstance(se, str) or not se.strip():
                    raise ValueError(
                        f"credentials.webhooks.event_catalog[{idx}].subevents[{sidx}] "
                        f"must be a non-empty string"
                    )
        if "default_selected" in entry and not isinstance(entry["default_selected"], bool):
            raise ValueError(
                f"credentials.webhooks.event_catalog[{idx}].default_selected must be a boolean"
            )
        delivery = entry.get("delivery", "user")
        if delivery not in ("user", "bot"):
            raise ValueError(
                f"credentials.webhooks.event_catalog[{idx}].delivery={delivery!r} "
                f"must be 'user' or 'bot'"
            )
        if "admin_only" in entry and not isinstance(entry["admin_only"], bool):
            raise ValueError(
                f"credentials.webhooks.event_catalog[{idx}].admin_only must be a boolean"
            )
        # Optional `match` block: maps a raw vendor payload to this catalog
        # key (catalog keys are SUBSCRIPTION names — slack's message.channels —
        # while payloads carry raw types like event.type="message"). Entries
        # without `match` match when the raw event type equals `key`.
        match = entry.get("match")
        if match is not None:
            if not isinstance(match, dict):
                raise ValueError(
                    f"credentials.webhooks.event_catalog[{idx}].match must be an object"
                )
            met = match.get("event_type", "")
            if not isinstance(met, str) or not met.strip():
                raise ValueError(
                    f"credentials.webhooks.event_catalog[{idx}].match.event_type "
                    f"must be a non-empty string"
                )
            conditions = match.get("conditions")
            if conditions is not None:
                if not isinstance(conditions, dict):
                    raise ValueError(
                        f"credentials.webhooks.event_catalog[{idx}].match.conditions "
                        f"must be an object"
                    )
                for cpath, expected in conditions.items():
                    if not (
                        isinstance(cpath, str)
                        and (cpath.startswith("body.") or cpath.startswith("headers."))
                    ):
                        raise ValueError(
                            f"credentials.webhooks.event_catalog[{idx}].match.conditions "
                            f"key {cpath!r} must start with 'body.' or 'headers.'"
                        )
                    if isinstance(expected, str):
                        continue
                    if isinstance(expected, list) and expected and all(
                        isinstance(e, str) for e in expected
                    ):
                        continue
                    raise ValueError(
                        f"credentials.webhooks.event_catalog[{idx}].match.conditions"
                        f"[{cpath!r}] must be a string or a non-empty list of strings"
                    )
        # Optional `resource_contains` (batched-vendor canonicalization):
        # MS-Graph-style vendors deliver value[] items whose `resource`
        # string names the subscribed resource ("Users/{id}/Events/{id}").
        # The request-level `match` mechanism can't see per-item context in
        # a batched body, so providers that canonicalize per item
        # (microsoft) map an item to this catalog key when the lowercased
        # substring appears in the item's resource.
        rc = entry.get("resource_contains")
        if rc is not None and (not isinstance(rc, str) or not rc.strip()):
            raise ValueError(
                f"credentials.webhooks.event_catalog[{idx}].resource_contains "
                f"must be a non-empty string when declared"
            )
        # Optional `vendor_create_fields`: top-level body fields merged over
        # the substituted registration.create body when THIS event is among
        # the subscription's selected events (MS Graph driveItem accepts
        # only changeType="updated" while the shared template sends
        # "created,updated").
        vcf = entry.get("vendor_create_fields")
        if vcf is not None:
            if not isinstance(vcf, dict) or not vcf:
                raise ValueError(
                    f"credentials.webhooks.event_catalog[{idx}].vendor_create_fields "
                    f"must be a non-empty object when declared"
                )
            for fk, fv in vcf.items():
                if not isinstance(fk, str) or not fk.strip():
                    raise ValueError(
                        f"credentials.webhooks.event_catalog[{idx}].vendor_create_fields "
                        f"keys must be non-empty strings"
                    )
                if not isinstance(fv, str):
                    raise ValueError(
                        f"credentials.webhooks.event_catalog[{idx}].vendor_create_fields"
                        f"[{fk!r}] must be a string"
                    )

    # --- payload_normalization sub-block ---
    norm = raw.get("payload_normalization")
    if not isinstance(norm, dict):
        raise ValueError(
            "credentials.webhooks.payload_normalization must be an object when available=true"
        )
    event_type_path = norm.get("event_type_path", "")
    if not isinstance(event_type_path, str) or not event_type_path.strip():
        raise ValueError(
            "credentials.webhooks.payload_normalization.event_type_path must be a "
            "non-empty string (path into the inbound payload to read the event type)"
        )
    for ns in ("actor", "subject", "target"):
        block = norm.get(ns)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ValueError(
                f"credentials.webhooks.payload_normalization.{ns} must be an object when declared"
            )
        # Allow `type` as a literal (target.type='repository') or `type_path` as a path.
        # `*_path` may be a single string OR a list of fallback paths (first
        # non-empty wins) so one manifest can cover multiple vendor event
        # shapes (e.g. GitHub PRs vs issues vs releases use different keys).
        for k, v in block.items():
            if k == "type":
                if not isinstance(v, str):
                    raise ValueError(
                        f"credentials.webhooks.payload_normalization.{ns}.type must be a string"
                    )
            elif k.endswith("_path"):
                if isinstance(v, str):
                    continue
                if isinstance(v, list):
                    if not v:
                        raise ValueError(
                            f"credentials.webhooks.payload_normalization.{ns}.{k} must "
                            f"be a non-empty list when declared as a fallback array"
                        )
                    for idx, path in enumerate(v):
                        if not isinstance(path, str):
                            raise ValueError(
                                f"credentials.webhooks.payload_normalization.{ns}.{k}[{idx}] "
                                f"must be a string"
                            )
                    continue
                raise ValueError(
                    f"credentials.webhooks.payload_normalization.{ns}.{k} must be a "
                    f"string or list of strings (fallback paths)"
                )
            # Unknown keys silently allowed — manifest authors may add custom fields.

    # --- vendor_target_spec sub-block ---
    vts = raw.get("vendor_target_spec")
    if not isinstance(vts, dict):
        raise ValueError(
            "credentials.webhooks.vendor_target_spec must be an object when available=true"
        )
    vts_kind = vts.get("kind", "")
    if vts_kind not in _WEBHOOK_VENDOR_TARGET_KINDS:
        raise ValueError(
            f"credentials.webhooks.vendor_target_spec.kind={vts_kind!r} must be one of "
            f"{sorted(_WEBHOOK_VENDOR_TARGET_KINDS)}"
        )
    label = vts.get("label", "")
    if not isinstance(label, str) or not label.strip():
        raise ValueError(
            "credentials.webhooks.vendor_target_spec.label must be a non-empty string"
        )
    # Optional: the bound account's `extra` key whose value prefills the
    # vendor target in the subscribe UI (slack: "team_id").
    aek = vts.get("account_extra_key", "")
    if aek and not isinstance(aek, str):
        raise ValueError(
            "credentials.webhooks.vendor_target_spec.account_extra_key must be "
            "a string when declared"
        )
    if vts_kind == "remote_list":
        le = vts.get("list_endpoint")
        _validate_webhook_registration_call(le, mcp_name, "vendor_target_spec.list_endpoint",
                                            required=True, allow_no_body=True)
        items_path = le.get("items_path", "") if isinstance(le, dict) else ""
        if not isinstance(items_path, str) or not items_path.strip():
            raise ValueError(
                "credentials.webhooks.vendor_target_spec.list_endpoint.items_path must "
                "be a non-empty string"
            )
        for fname in ("id_field", "label_field"):
            v = le.get(fname, "") if isinstance(le, dict) else ""
            if not isinstance(v, str) or not v.strip():
                raise ValueError(
                    f"credentials.webhooks.vendor_target_spec.list_endpoint.{fname} "
                    f"must be a non-empty string"
                )
    if vts_kind == "static_list":
        opts = vts.get("static_options")
        if not isinstance(opts, list) or not opts:
            raise ValueError(
                "credentials.webhooks.vendor_target_spec.static_options must be a "
                "non-empty list when kind='static_list'"
            )
        for oidx, opt in enumerate(opts):
            if not isinstance(opt, dict):
                raise ValueError(
                    f"credentials.webhooks.vendor_target_spec.static_options[{oidx}] "
                    f"must be an object"
                )
            for fname in ("value", "label"):
                v = opt.get(fname, "")
                if not isinstance(v, str) or not v.strip():
                    raise ValueError(
                        f"credentials.webhooks.vendor_target_spec.static_options[{oidx}]"
                        f".{fname} must be a non-empty string"
                    )

    # --- target_kinds (optional): the choices a subscriber picks between ---
    # Each kind carries its own field hints and scope, and may restate any
    # registration call (a shallow override of the top-level block: GitHub's
    # organization kind changes the URL and nothing else). What is validated
    # is the merged block, the one that will run.
    kinds = vts.get("target_kinds")
    if kinds is not None:
        if not isinstance(kinds, list) or not kinds:
            raise ValueError(
                "credentials.webhooks.vendor_target_spec.target_kinds must be a "
                "non-empty list when declared"
            )
        seen_keys: set[str] = set()
        for kidx, kind in enumerate(kinds):
            where = f"credentials.webhooks.vendor_target_spec.target_kinds[{kidx}]"
            if not isinstance(kind, dict):
                raise ValueError(f"{where} must be an object")
            for fname in ("key", "label"):
                v = kind.get(fname, "")
                if not isinstance(v, str) or not v.strip():
                    raise ValueError(f"{where}.{fname} must be a non-empty string")
            if kind["key"] in seen_keys:
                raise ValueError(f"{where}.key={kind['key']!r} repeats an earlier kind")
            seen_keys.add(kind["key"])
            for fname in ("placeholder", "validation_regex", "help_text"):
                v = kind.get(fname)
                if v is not None and not isinstance(v, str):
                    raise ValueError(f"{where}.{fname} must be a string when declared")
            if kind.get("validation_regex"):
                try:
                    re.compile(kind["validation_regex"])
                except re.error as e:
                    raise ValueError(f"{where}.validation_regex does not compile: {e}") from e
            rs = kind.get("required_scopes")
            if rs is not None and (
                not isinstance(rs, list) or not all(isinstance(x, str) and x for x in rs)
            ):
                raise ValueError(f"{where}.required_scopes must be a list of non-empty strings")
            kreg = kind.get("registration")
            if kreg is not None:
                if not isinstance(kreg, dict) or not kreg:
                    raise ValueError(f"{where}.registration must be a non-empty object")
                unknown = set(kreg) - {"create", "delete", "renew"}
                if unknown:
                    raise ValueError(
                        f"{where}.registration may only restate create/delete/renew, "
                        f"not {sorted(unknown)}"
                    )
                for call, override in kreg.items():
                    if not isinstance(override, dict) or not override:
                        raise ValueError(f"{where}.registration.{call} must be a non-empty object")
                    base = (raw.get("registration") or {}).get(call)
                    if not isinstance(base, dict):
                        raise ValueError(
                            f"{where}.registration.{call} restates a call the top-level "
                            f"registration block does not declare"
                        )
                    _validate_webhook_registration_call(
                        {**base, **override}, mcp_name,
                        f"{call} (target kind {kind['key']!r})", required=True,
                    )

    # --- event_id_field (optional but recommended for dedup) ---
    eid = raw.get("event_id_field", "")
    if eid and not isinstance(eid, str):
        raise ValueError(
            "credentials.webhooks.event_id_field must be a string when declared "
            "(dot-path into the payload for event-id dedup)"
        )

    # --- workspace_id_path (optional; required for relay-mode delivery) ---
    # Extracts the tenant/workspace id from a raw payload (slack:
    # body.team_id). The relay-forwarded ingest fans in on subscriptions whose
    # vendor_target equals this value — the mis-route defense.
    wip = raw.get("workspace_id_path", "")
    if wip:
        if not isinstance(wip, str) or not (
            wip.startswith("body.") or wip.startswith("headers.")
        ):
            raise ValueError(
                "credentials.webhooks.workspace_id_path must be a string starting "
                "with 'body.' or 'headers.' when declared"
            )

    # --- enrichment sub-block (optional) ---
    # Manifest-driven ID→name lookups run fail-open just before a matched
    # trigger fires (vendor API call with the subscription's bound token).
    enrich = raw.get("enrichment")
    if enrich is not None:
        if not isinstance(enrich, dict):
            raise ValueError(
                "credentials.webhooks.enrichment must be an object when declared"
            )
        lookups = enrich.get("lookups")
        if not isinstance(lookups, list) or not lookups:
            raise ValueError(
                "credentials.webhooks.enrichment.lookups must be a non-empty list"
            )
        for lidx, lk in enumerate(lookups):
            if not isinstance(lk, dict):
                raise ValueError(
                    f"credentials.webhooks.enrichment.lookups[{lidx}] must be an object"
                )
            sf = lk.get("source_field", "")
            if not isinstance(sf, str) or not sf.startswith(
                ("actor.", "subject.", "target.")
            ):
                raise ValueError(
                    f"credentials.webhooks.enrichment.lookups[{lidx}].source_field "
                    f"must start with 'actor.', 'subject.' or 'target.'"
                )
            _validate_webhook_registration_call(
                lk.get("request"), mcp_name, f"enrichment.lookups[{lidx}].request",
                required=True, allow_no_body=True,
            )
            outputs = lk.get("outputs")
            if not isinstance(outputs, dict) or not outputs:
                raise ValueError(
                    f"credentials.webhooks.enrichment.lookups[{lidx}].outputs must "
                    f"be a non-empty object"
                )
            for okey, opath in outputs.items():
                if not (
                    isinstance(okey, str)
                    and okey.startswith(("actor.", "subject.", "target."))
                ):
                    raise ValueError(
                        f"credentials.webhooks.enrichment.lookups[{lidx}].outputs "
                        f"key {okey!r} must start with 'actor.', 'subject.' or 'target.'"
                    )
                if isinstance(opath, str):
                    continue
                if isinstance(opath, list) and opath and all(
                    isinstance(p, str) for p in opath
                ):
                    continue
                raise ValueError(
                    f"credentials.webhooks.enrichment.lookups[{lidx}].outputs"
                    f"[{okey!r}] must be a string or a non-empty list of strings"
                )
            ttl = lk.get("ttl_seconds")
            if ttl is not None and (not isinstance(ttl, int) or ttl <= 0):
                raise ValueError(
                    f"credentials.webhooks.enrichment.lookups[{lidx}].ttl_seconds "
                    f"must be a positive integer when declared"
                )


def _validate_webhook_registration_call(
    block: Any,
    mcp_name: str,
    section: str,
    *,
    required: bool,
    allow_no_body: bool = False,
) -> None:
    """Shared validator for a templated vendor-API call (create/delete/renew/list).

    Each call block declares ``method``, ``url_template``, optional ``headers``
    dict, optional ``body_template`` (dict or string), optional
    ``expected_status`` list of int, and (for create) ``response_id_path``.

    When ``required=False`` and the block is missing, validation passes.
    """
    if block is None:
        if required:
            raise ValueError(
                f"credentials.webhooks.registration.{section} must be declared "
                f"(required={required})"
            )
        return
    if not isinstance(block, dict):
        raise ValueError(
            f"credentials.webhooks.registration.{section} must be an object"
        )
    method = block.get("method", "")
    if method not in _WEBHOOK_HTTP_METHODS:
        raise ValueError(
            f"credentials.webhooks.registration.{section}.method={method!r} must be one of "
            f"{sorted(_WEBHOOK_HTTP_METHODS)}"
        )
    url_template = block.get("url_template", "")
    if not isinstance(url_template, str) or not url_template.strip():
        raise ValueError(
            f"credentials.webhooks.registration.{section}.url_template must be a "
            f"non-empty string"
        )
    headers = block.get("headers")
    if headers is not None:
        if not isinstance(headers, dict):
            raise ValueError(
                f"credentials.webhooks.registration.{section}.headers must be an object"
            )
        for hk, hv in headers.items():
            if not isinstance(hk, str) or not hk.strip():
                raise ValueError(
                    f"credentials.webhooks.registration.{section}.headers keys must be "
                    f"non-empty strings"
                )
            if not isinstance(hv, str):
                raise ValueError(
                    f"credentials.webhooks.registration.{section}.headers[{hk!r}] must "
                    f"be a string (templated)"
                )
    body_template = block.get("body_template")
    if not allow_no_body and body_template is not None:
        if not isinstance(body_template, (dict, str)):
            raise ValueError(
                f"credentials.webhooks.registration.{section}.body_template must be a "
                f"dict or string when declared"
            )
    expected = block.get("expected_status", [200])
    if not isinstance(expected, list) or not expected:
        raise ValueError(
            f"credentials.webhooks.registration.{section}.expected_status must be a "
            f"non-empty list of integers"
        )
    for sidx, s in enumerate(expected):
        if not isinstance(s, int) or not (100 <= s < 600):
            raise ValueError(
                f"credentials.webhooks.registration.{section}.expected_status[{sidx}] "
                f"must be an HTTP status integer"
            )
    if section == "create":
        rip = block.get("response_id_path", "")
        if rip and not isinstance(rip, str):
            raise ValueError(
                "credentials.webhooks.registration.create.response_id_path must be a "
                "string when declared (path into the vendor's create response to "
                "capture vendor_subscription_id)"
            )
        # ``success_path`` is for GraphQL-style vendors (Linear) that return
        # HTTP 200 even on user errors — the value at this path must be
        # truthy in the response, else we treat the call as failed.
        sp = block.get("success_path", "")
        if sp and not isinstance(sp, str):
            raise ValueError(
                "credentials.webhooks.registration.create.success_path must be a "
                "string when declared (path into the vendor's create response that "
                "must resolve to a truthy value for the call to be considered successful)"
            )
