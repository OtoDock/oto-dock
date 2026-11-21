"""The bearer allowlist follows the manifest.

An MCP's ``proposed_hosts`` become rows it owns on ``oauth_bearer_allowlist``
when its provenance vouches for them: the community catalog (curated, every
entry reviewed: its install, update, source switch and approved request) or
an admin who allowed them on the ZIP install card, in the MCP's row or in
Admin, Security, where the hosts and the sign-in they receive are shown. A
catalog entry's host applies by itself only in the narrow shape a review
can read at a glance: the MCP's own literal https server host, ``localhost``
for its container, or a literal host a ``credentials.exit`` entry sends a
declared secret to (keyed by the manifest's name, as an API-key-header MCP's
are). Anything else it declares (a wildcard, another host, an address, a
host of a templated URL, a key another kind of MCP uses) waits for the
admin's Allow like a ZIP's. An update replaces the
MCP's rows with the new manifest's (a ZIP update only narrows them), a
disable takes them down and an enable puts back what was approved, a
removal forgets them.

The credential exit's hosts are rows like the tool's other hosts: the live
rows and the ledger are one set, and the boot settles every MCP that
declares an exit entry, so its literal exit hosts have their rows.

A row going live takes over the admin row of exactly its pair, unless
another installed MCP still leans on that row. An admin row does not hide a
host that waits: it keeps serving until the Allow takes it over. An MCP
installed before its origin was recorded hands over at its enable each
own-shaped pair an exact admin row allows: before the record, such a row was
the only way any MCP reached a host, so the handover opens nothing.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
from urllib.parse import urlsplit

from services.mcp import mcp_registry
from storage.identity import bearer_allowlist

logger = logging.getLogger("otodock.manifest-hosts")

VIA_CATALOG = "catalog"
VIA_CONFIRMED = "confirmed"
# An exact admin row an MCP installed before its origin was recorded handed over.
VIA_ADMIN_ROW = "admin-row"
ORIGIN_CATALOG = "catalog"
ORIGIN_ZIP = "zip"
# The allowlist host of a container on this machine (``gateway_entry``'s).
_SIDECAR_HOST = "localhost"
# The only scheme a remote MCP's own host applies by itself on.
_TLS_SCHEME = "https"


def _key_and_hosts(manifest) -> tuple[str, list] | None:
    """The allowlist key the gateway and the credential exit check this MCP
    under (``mcp_server_resolve.gateway_entry``: a bearer MCP's provider id;
    an API-key-header or exit MCP's own name) and the host lists it
    declares: the bearer's or the header key's ``proposed_hosts``, each exit
    entry's ``hosts``."""
    from services.mcp.mcp_session_config import exit_entry_hosts
    creds = getattr(manifest, "credentials", None)
    if creds is None:
        return None
    oauth = creds.oauth or {}
    if oauth.get("bearer_required"):
        return str(oauth.get("provider_id") or ""), [oauth.get("proposed_hosts")]
    exits = [list(exit_entry_hosts(e)) for e in (creds.exit or []) if isinstance(e, dict)]
    if isinstance(creds.api_key_header, dict):
        return manifest.name, [creds.api_key_header.get("proposed_hosts"), *exits]
    if exits:
        return manifest.name, exits
    return None


def _norm(host: str) -> str:
    return host.strip().rstrip(".").lower()


def hosts_of(manifest) -> tuple[str, list[str]] | None:
    """The allowlist key and the declared hosts, the exit's among them, as
    the gateway keys its check."""
    found = _key_and_hosts(manifest)
    if found is None:
        return None
    key, lists = found[0], [hs for hs in found[1] if isinstance(hs, list)]
    if not key or not lists:
        return None
    out = sorted({_norm(h) for hs in lists for h in hs if isinstance(h, str) and h.strip()})
    return key, out


def _exit_hosts(manifest) -> set[str]:
    """The hosts the credential exit may send a declared secret to
    (``credentials.exit``, ``mcp_session_config.exit_entry_hosts``), as
    ``hosts_of`` lists them."""
    from services.mcp.mcp_session_config import exit_entry_hosts
    out: set[str] = set()
    for entry in manifest.credentials.exit or []:
        if isinstance(entry, dict):
            out |= {_norm(h) for h in exit_entry_hosts(entry) if h.strip()}
    return out


def _declared(manifest) -> set[tuple[str, str]]:
    found = hosts_of(manifest)
    if found is None:
        return set()
    key, hosts = found
    return {(key, h) for h in hosts if not _never(h)}


def _ledger_pairs(manifest) -> set[tuple[str, str]]:
    """What the ledger keeps for the MCP: its declared pairs, the exit's
    among them."""
    return _declared(manifest)


def _live_pairs(manifest, approved) -> set[tuple[str, str]]:
    """Which approved pairs stand as rows now: every one. The live rows and
    the ledger are one set; every writer reads it."""
    return set(approved)


def _never(host: str) -> bool:
    """A pattern no provenance applies: everything, or a glob beyond ``*.``."""
    return host in ("*", "*.") or any(c in host for c in "?[]") or "*" in host[1:]


def _is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _keyed_by_name(manifest) -> bool:
    """An MCP whose allowlist key is its own name: an API-key-header or exit MCP."""
    creds = manifest.credentials
    return isinstance(creds.api_key_header, dict) or bool(creds.exit)


def _key_clash(manifest, key: str) -> bool:
    """A key the other kind of MCP also uses: a bearer MCP's provider id
    that is an API-key-header or exit MCP's name, or the reverse."""
    from auth.oauth_providers import hardcoded_provider_ids
    oauth = manifest.credentials.oauth or {}
    others = [m for m in mcp_registry.get_all_manifests().values() if m.name != manifest.name]
    if oauth.get("bearer_required"):
        return any(m.name == key and _keyed_by_name(m) for m in others)
    return key in hardcoded_provider_ids() or bool(mcp_registry.get_mcps_by_provider(key))


def _applies_by_itself(manifest, pair: tuple[str, str]) -> bool:
    """A catalog host that needs no admin's Allow: the MCP's own literal
    https server host (an exact name, never an address), ``localhost`` for
    its container, or a literal host an exit entry sends a declared secret
    to (a wildcard waits like any other)."""
    from services.mcp import mcp_manifest_types as _mt
    key, host = pair
    if _key_clash(manifest, key) or "*" in host or _is_address(host):
        return False
    if host in _exit_hosts(manifest):
        return True
    if _mt.is_container(manifest.server):
        return host == _SIDECAR_HOST
    url = urlsplit(manifest.server.url_template or "")
    if "${" in url.netloc or url.scheme != _TLS_SCHEME:
        return False
    return (url.hostname or "").lower() == host


def _bridged(manifest, declared: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """The own-shaped pairs an exact admin row allows, for an MCP installed
    before its origin was recorded (none for one installed since)."""
    if not declared or bearer_allowlist.has_origin(manifest.name):
        return set()
    admin = set().union(*(bearer_allowlist.admin_pairs(k) for k in {k for k, _ in declared}))
    return {p for p in declared & admin if _applies_by_itself(manifest, p)}


def _relied_on(manifest, pairs: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Which of ``pairs`` the MCP may reach under any row: what it declares,
    and the host the gateway judges its forward under (its server host, or
    ``localhost`` for its container; any host of its key when the server
    host is templated, since which one is known only at the session)."""
    from services.mcp import mcp_manifest_types as _mt
    found = _key_and_hosts(manifest)
    if found is None or not found[0]:
        return set()
    key = found[0]
    out = _declared(manifest) & pairs
    if _mt.is_container(manifest.server):
        return out | ({(key, _SIDECAR_HOST)} & pairs)
    url = urlsplit(manifest.server.url_template or "")
    if "${" in url.netloc:
        return out | {p for p in pairs if p[0] == key}
    return out | ({(key, (url.hostname or "").lower())} & pairs)


def _take_over(name: str, live: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """The live pairs whose admin row may go: exact hosts (an admin's
    wildcard stays the admin's, even one of the same pattern) that no other
    installed MCP relies on without owning (it still leans on the admin row)."""
    out = {p for p in live if "*" not in p[1]}
    if not out:
        return out
    for other in mcp_registry.get_all_manifests().values():
        if other.name in (name, bearer_allowlist.ADMIN_SOURCE) or not out:
            continue
        leaning = _relied_on(other, out)
        if leaning:
            out -= leaning - bearer_allowlist.approved_hosts(other.name)
    return out


def _enabled(name: str) -> bool:
    from storage.mcp import mcp_store
    return bool((mcp_store.get_mcp_state(name) or {}).get("enabled"))


def _told(removed: bool) -> None:
    if removed:
        # A paired machine holding a token under a lease learns at once.
        from core.remote import mcp_gateway_push
        mcp_gateway_push.tick_soon()


def digest(manifest) -> str:
    """What the confirm card showed: the key, the hosts and the server URL."""
    found = hosts_of(manifest) or ("", [])
    blob = json.dumps([found[0], found[1], manifest.server.url_template or ""])
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def pending(manifest) -> dict | None:
    """The declared hosts that wait for an admin's Allow (not approved, not
    handed over at the next enable, not allowed by another MCP's row; an
    admin's row does not count), with what the card shows, or None."""
    found = hosts_of(manifest)
    if found is None or manifest.name == bearer_allowlist.ADMIN_SOURCE:
        return None
    declared = _declared(manifest)
    settled = bearer_allowlist.approved_hosts(manifest.name) | _bridged(manifest, declared)
    hosts = sorted(h for k, h in declared
                   if (k, h) not in settled and not bearer_allowlist.mcp_row_allows(k, h))
    if not hosts:
        return None
    siblings = sorted(m.label for m in mcp_registry.get_mcps_by_provider(found[0]) if m.name != manifest.name)
    return {"mcp_name": manifest.name, "mcp_label": manifest.label, "provider": found[0],
            "hosts": hosts, "shared_with": siblings, "digest": digest(manifest)}


def after_install(name: str, *, from_catalog: bool, by: str = "") -> dict | None:
    """An install or update landed (the manifest is in the registry): from
    the catalog the MCP's own host applies and anything it still declares
    of what an admin allowed stays; from a ZIP nothing new applies and what
    the new manifest no longer declares goes. Where it came from is
    recorded. Answers what waits for an Allow. Never raises: the install
    stands either way."""
    manifest = mcp_registry.get_manifest(name)
    if manifest is None or name == bearer_allowlist.ADMIN_SOURCE:
        return None
    try:
        _settle(manifest, from_catalog=from_catalog, by=by or VIA_CATALOG,
                origin=ORIGIN_CATALOG if from_catalog else ORIGIN_ZIP)
        return pending(manifest)
    except Exception:  # the install stands; the admin can add the hosts by hand
        logger.exception("allowlist rows of %s not updated after its install", name)
        return None


def _settle(manifest, *, from_catalog: bool, by: str, origin: str = "") -> None:
    """The MCP's ledger from its manifest and its rows from the ledger: what
    an admin allowed that it still declares stays, a catalog's own-shaped
    hosts apply by themselves, and the rows are the live pairs of it while
    the MCP is enabled. ``origin`` is recorded when given."""
    name = manifest.name
    declared = _ledger_pairs(manifest)
    approved = dict.fromkeys(bearer_allowlist.approved_hosts(name) & declared, VIA_CONFIRMED)
    if from_catalog:
        for p in declared:
            if _applies_by_itself(manifest, p):
                approved.setdefault(p, VIA_CATALOG)
    live = _live_pairs(manifest, approved) if _enabled(name) else set()
    _told(bearer_allowlist.set_mcp_hosts(
        name, approved=approved, live=live, take_over=_take_over(name, live), origin=origin, by=by))


def _shipped(manifest) -> bool:
    """One of the platform's own tools: its manifest arrives with a release."""
    return getattr(manifest, "category", "") == "custom"


def seed_shipped() -> int:
    """The platform's own tools (category ``custom``) join the allowlist at
    boot as a catalog install does: their manifests arrive with a release,
    not through the installer, so a host a release adds (a
    ``credentials.exit`` entry's, the tool's own server host) gets its row
    here by the same rule, and rows already right stay as they are. Then
    every other MCP that declares an exit entry is settled
    (``settle_exits``). How many shipped manifests were applied."""
    applied = 0
    for manifest in list(mcp_registry.get_all_manifests().values()):
        if not _shipped(manifest) or hosts_of(manifest) is None:
            continue
        after_install(manifest.name, from_catalog=True, by="platform")
        applied += 1
    settle_exits()
    return applied


def settle_exits() -> int:
    """At boot, every installed MCP that declares a ``credentials.exit``
    entry, other than the platform's own, gets the rows its ledger gives. A
    boot approves nothing: the catalog rule runs where a manifest arrives
    (the installer, and ``seed_shipped`` for the platform's own tools), so a
    host the ledger lacks waits for the Allow. Nothing is handed over and no
    origin is recorded. How many MCPs were settled. Never raises."""
    settled = 0
    for manifest in list(mcp_registry.get_all_manifests().values()):
        if manifest.name == bearer_allowlist.ADMIN_SOURCE or not _exit_hosts(manifest):
            continue
        if _shipped(manifest):
            continue  # seeded above, by the same rule
        try:
            _settle(manifest, from_catalog=False, by="")
            settled += 1
        except Exception:  # one MCP's rows never stop the boot
            logger.exception("allowlist rows of %s not settled at boot", manifest.name)
    return settled


class Refused(ValueError):
    """The hosts confirmed are not the ones the card showed."""


def confirm(name: str, *, provider: str, hosts: list[str], shown: str, by: str) -> None:
    """The admin allowed exactly what the card showed (the provider, the
    hosts, the digest of the manifest): approved, and live when enabled,
    taking over the admin row of the same pair."""
    manifest = mcp_registry.get_manifest(name)
    if manifest is None:
        raise LookupError(name)
    waiting = pending(manifest)
    if (waiting is None or waiting["provider"] != provider or sorted(set(hosts)) != waiting["hosts"]
            or shown != waiting["digest"]):
        raise Refused("This MCP's addresses changed since they were shown. Reload and try again.")
    approved = dict.fromkeys(bearer_allowlist.approved_hosts(name) | {(provider, h) for h in waiting["hosts"]},
                             VIA_CONFIRMED)
    live = _live_pairs(manifest, approved) if _enabled(name) else set()
    bearer_allowlist.set_mcp_hosts(name, approved=approved, live=live, take_over=_take_over(name, live), by=by)


def after_enable(name: str) -> None:
    """What was approved goes live again, with the pairs an MCP installed
    before its origin was recorded hands over (``_bridged``). The caller
    holds the MCP's install lock."""
    manifest = mcp_registry.get_manifest(name)
    if manifest is None or name == bearer_allowlist.ADMIN_SOURCE:
        return
    approved = dict.fromkeys(bearer_allowlist.approved_hosts(name) & _ledger_pairs(manifest), VIA_CONFIRMED)
    handover = _bridged(manifest, _declared(manifest)) - set(approved)
    approved.update(dict.fromkeys(handover, VIA_ADMIN_ROW))
    live = _live_pairs(manifest, approved)
    bearer_allowlist.set_mcp_hosts(name, approved=approved, live=live, take_over=_take_over(name, live),
                                   handover=handover, by="")


def after_disable(name: str) -> None:
    if name == bearer_allowlist.ADMIN_SOURCE:
        return
    approved = dict.fromkeys(bearer_allowlist.approved_hosts(name), VIA_CONFIRMED)
    _told(bearer_allowlist.set_mcp_hosts(name, approved=approved, live=set(), by=""))


def after_removal(name: str) -> None:
    if name == bearer_allowlist.ADMIN_SOURCE:
        return
    _told(bearer_allowlist.forget_mcp(name))


def all_pending() -> list[dict]:
    """Every installed MCP's hosts that wait for an Allow (Admin, Security)."""
    out = []
    for m in mcp_registry.get_all_manifests().values():
        waiting = pending(m)
        if waiting:
            out.append(waiting)
    return sorted(out, key=lambda w: w["mcp_label"].lower())
