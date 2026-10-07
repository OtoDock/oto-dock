"""A community MCP's catalog source changed: detection, the admin's plan, and
the switch that keeps the install's rows (COMMUNITY-MARKETPLACE.md "Source
changes").

The identity an update may never change on its own is
``community_installer.source_identity``; this module answers the same
question for a catalog entry, compares the two sides for every installed
community MCP, writes the pending change the MCP Servers page shows, and
runs the switch the admin accepts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException

import config
from services.community import community_catalog
from services.community import community_installer as installer
from services.mcp import mcp_manifest_parse as _mmp
from services.mcp import mcp_manifest_types as _mt
from storage.mcp import mcp_update_state_store as state_store

logger = logging.getLogger("claude-proxy.mcp-source-swap")

INTERRUPTED = "interrupted by a restart"

# The three manifest blocks a credential key may live in, each with its own
# store: a rename declared for one block is applied in that store only.
BLOCK_CREDENTIALS = "credentials"
BLOCK_INSTANCES = "instances"
BLOCK_CONFIG = "config"
BLOCKS = (BLOCK_CREDENTIALS, BLOCK_INSTANCES, BLOCK_CONFIG)

OAUTH_NONE = "none"
OAUTH_CARRY = "carry"
OAUTH_RECONNECT = "reconnect"


# ---------------------------------------------------------------------------
# Identity, both sides
# ---------------------------------------------------------------------------

def _server(data: dict) -> dict:
    server = data.get("server") if isinstance(data, dict) else None
    return server if isinstance(server, dict) else {}


def manifest_identity(data: dict) -> tuple[str, str]:
    """``source_identity`` of a raw manifest dict (an installed manifest.json
    or a catalog manifest)."""
    s = _server(data)
    return installer.source_identity(
        runtime=s.get("runtime"), source=s.get("source", ""),
        image=s.get("image", ""), url_template=s.get("url_template", ""),
    )


def manifest_runtime(data: dict) -> str:
    """The runtime word of a manifest: ``server.runtime``, or ``remote`` for a
    vendor-hosted entry that declares none (the catalog's own convention)."""
    s = _server(data)
    runtime = s.get("runtime")
    if isinstance(runtime, str) and runtime:
        return runtime
    if str(s.get("source") or "").startswith("remote:"):
        return "remote"
    return ""


def judged(identity: tuple[str, str]) -> bool:
    """Whether an identity can be compared at all: an empty or unknown
    source is never judged a change."""
    return identity[0] not in ("none", "unknown")


def entry_identity(entry: dict) -> tuple[str, str] | None:
    """``source_identity`` of a registry entry, from the fields the registry
    carries: ``source`` for npm, pypi and git+, ``image`` for a container,
    ``url_host`` for a remote entry. ``None`` when the entry lacks the field
    its runtime needs (a registry generated before the fields existed): the
    caller falls back to the catalog manifest."""
    runtime = str(entry.get("runtime") or "")
    source = str(entry.get("source") or "")
    if runtime == _mt.RUNTIME_DOCKER:
        if "image" not in entry or not isinstance(entry.get("image"), str):
            return None
        return installer.source_identity(
            runtime=runtime, source=source, image=entry["image"], url_template="",
        )
    if runtime == "remote" or source.startswith("remote:"):
        host = entry.get("url_host")
        if "url_host" not in entry or not isinstance(host, str):
            return None
        return "remote", host.strip().lower()
    return installer.source_identity(
        runtime=runtime, source=source, image="", url_template="",
    )


def entry_folder(entry: dict) -> str:
    """The catalog folder of an entry, from its ``manifest_url``
    (``./<folder>/manifest.json``); the name when the URL has another shape.
    A folder can differ from the manifest name (``workspace-mcp`` holds
    ``google-workspace``)."""
    url = str(entry.get("manifest_url") or "")
    if url.startswith("./"):
        url = url[2:]
    parts = url.split("/")
    if len(parts) == 2 and parts[1] == "manifest.json" and installer._is_safe_name(parts[0]):
        return parts[0]
    return str(entry.get("name") or "")


def source_url(kind: str, identity: str, *, source: str = "", image: str = "",
               url_template: str = "") -> str:
    """What the dialog shows for a source: the registry page of a package,
    the repository of a git source, the image reference, the host of a hosted
    MCP."""
    if kind == "npm":
        return f"https://www.npmjs.com/package/{identity}"
    if kind == "pypi":
        return f"https://pypi.org/project/{identity}/"
    if kind == "git":
        base, _, subdir = identity.partition("#")
        base = base.removeprefix("git+")
        return f"{base}#subdirectory={subdir}" if subdir else base
    if kind == "image":
        return identity or "(built from the folder)"
    if kind == "remote":
        return identity or str(urlsplit(url_template).hostname or "")
    return installer.source_display(kind, source=source, image=image,
                                    url_template=url_template)


def _declared_identity(source: str, installed_runtime: str,
                       from_kind: str) -> tuple[str, str]:
    """The identity a ``replaces[].source`` string names, read the way the
    installed source is: an image reference for a container, a host or a
    URL for a hosted MCP, a package pointer otherwise."""
    s = source.strip()
    if from_kind == "image":
        return "image", installer._image_repository(s)
    if from_kind == "remote":
        bare = s.removeprefix("remote:")
        parts = urlsplit(bare)
        host = parts.hostname if parts.scheme and parts.netloc else bare.split("/")[0]
        return "remote", (host or "").strip().lower()
    return installer.source_identity(
        runtime=installed_runtime, source=s, image="", url_template="",
    )


def _declared_by(data: dict, installed_runtime: str,
                 from_identity: tuple[str, str]) -> dict | None:
    """The catalog manifest's ``replaces`` entry that names the installed
    source (``_declared_identity``). ``None`` when none does."""
    for entry in _mmp.parse_replaces(data.get("replaces"), str(data.get("name") or "")):
        if _declared_identity(entry.source, installed_runtime, from_identity[0]) == from_identity:
            return {"source": entry.source, "credentials": dict(entry.credentials)}
    return None


# ---------------------------------------------------------------------------
# The credential plan
# ---------------------------------------------------------------------------

def _oauth_provider(data: dict) -> str:
    creds = data.get("credentials") if isinstance(data, dict) else None
    oauth = creds.get("oauth") if isinstance(creds, dict) else None
    if not isinstance(oauth, dict):
        return ""
    return str(oauth.get("provider_id") or "")


def _names_its_server(data: dict) -> bool:
    """Whether the manifest's MCP server names its own authorization server
    (``credentials.oauth.authorization_server``): its tokens come from
    another issuer than an admin's app or the relay would use."""
    creds = data.get("credentials") if isinstance(data, dict) else None
    oauth = creds.get("oauth") if isinstance(creds, dict) else None
    return isinstance(oauth, dict) and bool(oauth.get("authorization_server"))


def _block_keys(data: dict, block: str) -> set[str]:
    """The keys a manifest declares in one block. The credentials block adds
    the OAuth account keys an OAuth MCP writes beside its fields."""
    if block == BLOCK_CREDENTIALS:
        creds = data.get("credentials") if isinstance(data, dict) else None
        creds = creds if isinstance(creds, dict) else {}
        keys = {
            str(f.get("key")) for f in (creds.get("fields") or [])
            if isinstance(f, dict) and f.get("key")
        }
        provider = _oauth_provider(data)
        if provider:
            from services.oauth.oauth_account_store import resolve_account_credential_keys
            keys.update(resolve_account_credential_keys(creds.get("oauth") or {}, provider))
        return keys
    if block == BLOCK_INSTANCES:
        inst = data.get("instances") if isinstance(data, dict) else None
        inst = inst if isinstance(inst, dict) else {}
        return {
            str(f.get("key")) for f in (inst.get("fields") or [])
            if isinstance(f, dict) and f.get("key")
        }
    cfg = data.get("config") if isinstance(data, dict) else None
    return {
        str(f.get("key")) for f in (cfg or [])
        if isinstance(f, dict) and f.get("key")
    }


def credential_plan(installed: dict, catalog: dict, renames: dict[str, str],
                    stored: dict[str, set[str]]) -> dict:
    """What a switch does to the install's credentials, per block: the keys
    both manifests declare carry; a declared rename applies when the
    installed manifest declares its old key and the catalog manifest its new
    key in the same block; an installed key with a stored value that is
    neither carried nor renamed needs a reconnect. OAuth accounts carry when
    the provider is unchanged and still issues its tokens the same way (an
    ``authorization_server`` block that appears or goes is a reconnect).
    ``stored`` maps a block to the keys holding a value
    (``mcp_update_state_store``)."""
    blocks: dict[str, dict] = {}
    carry_all: list[str] = []
    rename_all: dict[str, str] = {}
    reconnect_all: list[str] = []
    for block in BLOCKS:
        old = _block_keys(installed, block)
        new = _block_keys(catalog, block)
        block_renames = {
            o: n for o, n in renames.items()
            if o in old and n in new and o != n
            and not o.startswith("_") and not n.startswith("_")
        }
        carry = sorted(old & new)
        held = stored.get(block) or set()
        reconnect = sorted(
            k for k in old if k in held and k not in carry and k not in block_renames
        )
        blocks[block] = {"carry": carry, "rename": block_renames, "reconnect": reconnect}
        carry_all.extend(carry)
        rename_all.update(block_renames)
        reconnect_all.extend(reconnect)
    old_provider = _oauth_provider(installed)
    new_provider = _oauth_provider(catalog)
    if not old_provider:
        oauth = OAUTH_NONE
    elif old_provider == new_provider and _names_its_server(installed) == _names_its_server(catalog):
        oauth = OAUTH_CARRY
    else:
        # Another provider, or the same one that now issues its tokens the
        # other way (the server's own authorization server appeared or
        # went): the old grants were issued by another server.
        oauth = OAUTH_RECONNECT
    old_server = _server(installed)
    new_server = _server(catalog)
    return {
        "runtime": {"from": manifest_runtime(installed), "to": manifest_runtime(catalog)},
        "credentials": {
            "carry": sorted(set(carry_all)),
            "rename": rename_all,
            "reconnect": sorted(set(reconnect_all)),
            "oauth": oauth,
        },
        "blocks": blocks,
        # A hosted MCP that needs the bearer header reaches its new host
        # only once the admin allows it (the allowlist is keyed by host).
        "bearer_host_change": bool(
            manifest_identity(installed)[0] == "remote"
            and manifest_identity(catalog)[0] == "remote"
            and ((installed.get("credentials") or {}).get("oauth") or {}).get("bearer_required")
            and urlsplit(str(old_server.get("url_template") or "")).hostname
            != urlsplit(str(new_server.get("url_template") or "")).hostname
        ),
    }


def collect_stored_keys(mcp_name: str) -> dict[str, set[str]]:
    """The keys holding a value in each block's store. Synchronous."""
    return {
        BLOCK_CREDENTIALS: state_store.stored_credential_keys(mcp_name),
        BLOCK_INSTANCES: state_store.stored_instance_keys(mcp_name),
        BLOCK_CONFIG: state_store.stored_config_keys(mcp_name),
    }


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _read_manifest(mcp_dir) -> dict | None:
    try:
        data = json.loads((Path(mcp_dir) / "manifest.json").read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


async def detect_changes(manifests: dict, catalog: dict[str, dict]) -> tuple[dict[str, dict], set[str]]:
    """Compare every installed community MCP that has a catalog entry with
    the catalog's source identity. Returns ``(changes, unchanged)``:
    ``changes`` maps a name to the change record (the ``from_*`` and ``to_*``
    columns, ``declared``, ``plan``); ``unchanged`` holds the names whose
    pending change, if any, is moot: the identities agree, or the entry left
    the catalog (nothing to switch to). An MCP whose identity cannot be
    judged on either side (an unknown or empty source, a catalog manifest
    that could not be read) is in neither."""
    changes: dict[str, dict] = {}
    unchanged: set[str] = set()
    candidates = [
        (name, m, catalog[name]) for name, m in manifests.items()
        if m.category == "community" and name in catalog
    ]
    try:
        rows = await asyncio.to_thread(state_store.list_source_changes)
    except Exception:
        rows = []
    for row in rows:
        if row.get("status") == state_store.STATUS_PENDING and row["mcp_name"] not in catalog:
            unchanged.add(row["mcp_name"])

    installed_data = await asyncio.to_thread(
        lambda: {name: _read_manifest(m.mcp_dir) for name, m, _e in candidates},
    )
    for name, m, entry in candidates:
        data = installed_data.get(name)
        if data is None:
            continue
        from_identity = manifest_identity(data)
        if not judged(from_identity):
            continue
        to_identity = entry_identity(entry)
        catalog_manifest: dict | None = None
        if to_identity is None:
            catalog_manifest = await _catalog_manifest(entry)
            if catalog_manifest is None:
                continue
            to_identity = manifest_identity(catalog_manifest)
        if not judged(to_identity):
            continue
        if to_identity == from_identity:
            unchanged.add(name)
            continue
        if catalog_manifest is None:
            catalog_manifest = await _catalog_manifest(entry)
            if catalog_manifest is None:
                continue
        try:
            stored = await asyncio.to_thread(collect_stored_keys, name)
        except Exception:
            logger.exception("Stored keys of %s could not be read", name)
            continue
        declared = _declared_by(catalog_manifest, manifest_runtime(data), from_identity)
        renames = dict(declared["credentials"]) if declared else {}
        plan = credential_plan(data, catalog_manifest, renames, stored)
        old_server = _server(data)
        new_server = _server(catalog_manifest)
        catalog_hash = str(entry.get("manifest_hash") or "") or \
            community_catalog.normalized_manifest_hash(catalog_manifest)
        changes[name] = {
            "from_kind": from_identity[0], "from_identity": from_identity[1],
            "from_url": source_url(
                from_identity[0], from_identity[1], source=old_server.get("source", ""),
                image=old_server.get("image", ""), url_template=old_server.get("url_template", ""),
            ),
            "from_runtime": manifest_runtime(data),
            "to_kind": to_identity[0], "to_identity": to_identity[1],
            "to_url": source_url(
                to_identity[0], to_identity[1], source=new_server.get("source", ""),
                image=new_server.get("image", ""), url_template=new_server.get("url_template", ""),
            ),
            "to_runtime": manifest_runtime(catalog_manifest),
            "to_version": str(entry.get("version") or catalog_manifest.get("version") or ""),
            "to_manifest_hash": catalog_hash,
            "declared": declared is not None,
            "plan": plan,
        }
    return changes, unchanged


async def _catalog_manifest(entry: dict) -> dict | None:
    """The catalog manifest of an entry, ``None`` when it cannot be read: a
    failed fetch never fails the whole check."""
    folder = entry_folder(entry)
    if not folder:
        return None
    try:
        data = await community_catalog.fetch_manifest(folder)
    except Exception as exc:
        logger.warning("Catalog manifest of %s could not be read: %s", folder, exc)
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# What the page shows
# ---------------------------------------------------------------------------

def _projection(row: dict) -> dict:
    return {
        "status": row.get("status", ""),
        "from": {
            "kind": row.get("from_kind", ""), "identity": row.get("from_identity", ""),
            "url": row.get("from_url", ""), "runtime": row.get("from_runtime", ""),
        },
        "to": {
            "kind": row.get("to_kind", ""), "identity": row.get("to_identity", ""),
            "url": row.get("to_url", ""), "runtime": row.get("to_runtime", ""),
            "version": row.get("to_version", ""),
        },
        "declared": bool(row.get("declared")),
        "plan": row.get("plan") or {},
        "to_manifest_hash": row.get("to_manifest_hash") or "",
        "detected_at": row.get("detected_at") or "",
        "accepted_at": row.get("accepted_at") or "",
        "result": row.get("result") or {},
    }


def merge_update_state(ordinary: dict[str, dict], rows: list[dict],
                       current_versions: dict[str, str]) -> dict[str, dict]:
    """The ``updates`` map of the page: the ordinary results, with a pending
    or switching source change replacing an MCP's entry (``reason:
    source``) and a switched one riding beside it (``reason: switched`` when
    the MCP has no other update)."""
    updates = {name: dict(info) for name, info in ordinary.items()}
    for row in rows:
        name = row["mcp_name"]
        projection = _projection(row)
        if row.get("status") in (state_store.STATUS_PENDING, state_store.STATUS_SWITCHING):
            updates[name] = {
                "current": current_versions.get(name, ""),
                "latest": row.get("to_version") or current_versions.get(name, ""),
                "registry": row.get("to_kind", ""), "package": name,
                "reason": "source", "source_change": projection,
            }
        elif name in updates:
            updates[name]["source_change"] = projection
        else:
            updates[name] = {
                "current": current_versions.get(name, ""),
                "latest": current_versions.get(name, ""),
                "registry": row.get("to_kind", ""), "package": name,
                "reason": "switched", "source_change": projection,
            }
    return updates


def build_update_state() -> dict:
    """The persisted check as the page reads it: ``{updates, checked,
    checked_at}``. Synchronous."""
    from services.mcp import mcp_registry
    ordinary = state_store.get_check_results()
    rows = state_store.list_source_changes()
    versions = {name: m.version for name, m in mcp_registry.get_all_manifests().items()}
    updates = merge_update_state(ordinary, rows, versions)
    return {
        "updates": updates,
        "checked": len(ordinary),
        "checked_at": state_store.last_checked_at(),
    }


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _interrupted() -> dict:
    return {"error": INTERRUPTED, "failed_at": _now()}


def apply_renames(mcp_name: str, blocks: dict) -> dict:
    """Apply a plan's per-block renames in their stores. Returns
    ``{"renamed": {old: new}, "kept": [old]}``: a key whose new name
    already held a value is kept under its old name."""
    renamed: dict[str, str] = {}
    kept: set[str] = set()

    def _pairs(block: str) -> list[tuple[str, str]]:
        raw = (blocks.get(block) or {}).get("rename") or {}
        return [
            (str(o), str(n)) for o, n in raw.items()
            if _mt.REPLACES_KEY_RE.fullmatch(str(o)) and _mt.REPLACES_KEY_RE.fullmatch(str(n))
            and o != n
        ]

    for old, new in _pairs(BLOCK_CREDENTIALS):
        out = state_store.rename_credential_key(mcp_name, old, new)
        if out["renamed"]:
            renamed[old] = new
        if out["kept"]:
            kept.add(old)
    instance_pairs = dict(_pairs(BLOCK_INSTANCES))
    if instance_pairs:
        out = state_store.rename_instance_field_keys(mcp_name, instance_pairs)
        renamed.update(out["renamed"])
        kept.update(out["kept"])
        if out["unreadable"]:
            logger.warning(
                "Instances %s of %s could not be decrypted; their keys were not renamed",
                out["unreadable"], mcp_name,
            )
    for old, new in _pairs(BLOCK_CONFIG):
        out = state_store.rename_config_key(mcp_name, old, new)
        if out["renamed"]:
            renamed[old] = new
        if out["kept"]:
            kept.add(old)
    return {"renamed": renamed, "kept": sorted(kept)}


def _reconnect_after(mcp_name: str, installed: dict, switched: dict) -> dict:
    """What still needs a reconnect once the renames are applied: the old
    keys that hold a value and are neither carried nor renamed (judged on
    the stores as they are now), and the OAuth verdict."""
    plan = credential_plan(installed, switched, {}, collect_stored_keys(mcp_name))
    return {"reconnect": plan["credentials"]["reconnect"], "oauth": plan["credentials"]["oauth"]}


async def _finish(name: str, row: dict, *, installed: dict | None, result: dict,
                  old_image: str, error: str = "", ensure_start: bool = False) -> dict:
    """The irreversible half of a switch. The install has replaced the folder
    and dropped its backup, so whatever fails from here on is recorded on a
    ``switched`` row, never undone: the declared renames, the container's
    refreshed env, the old image's removal, the reconnect list. ``installed``
    is the manifest before the switch (``None`` when the switch is being
    finished after the fact: the reconnect list then comes from the plan);
    ``ensure_start`` starts a container runtime even without a rename."""
    from core.config import deployment
    from services.mcp import docker_manager, mcp_registry

    swap_result: dict = {
        "version": result.get("version", ""), "renamed": {}, "kept": [],
        "reconnect_needed": [], "oauth": OAUTH_NONE,
        "container_started": result.get("container_started"), "switched_at": _now(),
    }
    if error:
        swap_result["error"] = error
    try:
        blocks = (row.get("plan") or {}).get("blocks") or {}
        renames = await asyncio.to_thread(apply_renames, name, blocks)
        swap_result["renamed"], swap_result["kept"] = renames["renamed"], renames["kept"]
        refreshed = mcp_registry.get_manifest(name)
        new_is_container = refreshed is not None and _mt.is_container(refreshed.server)
        self_host = deployment.current_mode() != deployment.EXTERNAL_POOL
        container_started = swap_result["container_started"] if new_is_container else None
        if new_is_container and self_host and (
            ensure_start or (renames["renamed"] and container_started is not None)
        ):
            # The installer started the container with the keys as they were
            # before the renames: give it the renamed ones.
            try:
                await asyncio.to_thread(docker_manager._inject_mcp_env, refreshed)
                container_started = bool(await asyncio.to_thread(
                    docker_manager.start_container, refreshed, force_recreate=True,
                ))
            except Exception as exc:
                logger.warning("Container refresh of %s after the switch failed: %s", name, exc)
                container_started = False
        swap_result["container_started"] = container_started
        new_image = (refreshed.server.image or "") if new_is_container else ""
        if (
            old_image and old_image != new_image and self_host
            and (not new_is_container or container_started)
        ):
            await asyncio.to_thread(docker_manager.remove_image, old_image)
        switched = await asyncio.to_thread(_read_manifest, refreshed.mcp_dir) if refreshed else None
        reconnect: dict
        if installed is not None and switched is not None:
            try:
                reconnect = await asyncio.to_thread(_reconnect_after, name, installed, switched)
            except Exception:
                logger.exception("Reconnect list of %s could not be computed", name)
                reconnect = {"reconnect": [], "oauth": OAUTH_NONE}
        else:
            creds = (row.get("plan") or {}).get("credentials") or {}
            reconnect = {
                "reconnect": [k for k in creds.get("reconnect") or [] if k not in renames["renamed"]],
                "oauth": creds.get("oauth") or OAUTH_NONE,
            }
        swap_result["reconnect_needed"], swap_result["oauth"] = reconnect["reconnect"], reconnect["oauth"]
    except Exception as exc:
        logger.exception("The switch of %s ended after its install with an error", name)
        note = f"{type(exc).__name__}: {exc}"
        swap_result["error"] = f"{swap_result['error']}; {note}" if swap_result.get("error") else note
    await asyncio.to_thread(
        state_store.set_source_change_status, name, state_store.STATUS_SWITCHED,
        result=swap_result,
    )
    logger.info(
        "Switched %s from %s to %s (renamed %s, reconnect %s%s)",
        name, row.get("from_url"), row.get("to_url"), sorted(swap_result["renamed"]),
        swap_result["reconnect_needed"],
        f", error: {swap_result['error']}" if swap_result.get("error") else "",
    )
    return swap_result


def _reply(name: str, row: dict, swap_result: dict, *, install_log: str = "",
           recovered: bool = False) -> dict:
    return {
        "status": "switched", "name": name, "from": row["from_url"], "to": row["to_url"],
        "version": swap_result.get("version", ""), "install_log": install_log,
        "result": swap_result, "recovered": recovered,
    }


async def switch(name: str, *, from_url: str, to_url: str, manifest_hash: str,
                 admin_sub: str) -> dict:
    """Switch an installed community MCP to the catalog source its pending
    change names, keeping its rows, credentials, assignments and settings
    (``mcp_source_swap`` in COMMUNITY-MARKETPLACE.md). ``from_url`` and
    ``to_url`` must be exactly the pair the row shows and ``manifest_hash``
    the catalog manifest the card described (the row's); the pair and the
    hash are judged before the lock, the installed identity under it. A
    folder that already holds the new source (a switch that ended after its
    install) is finished rather than installed again. Raises
    ``HTTPException``."""
    from core.credentials import catalog_install_registry
    from services.mcp import mcp_registry

    if not installer._is_safe_name(name):
        raise HTTPException(400, f"Invalid MCP name: {name!r}")
    lock = catalog_install_registry.lock_for(name)
    row = await asyncio.to_thread(state_store.get_source_change, name)
    if row is None:
        raise HTTPException(
            409, f"No source change is pending for '{name}'. Run Check updates first.",
        )
    if row["status"] == state_store.STATUS_SWITCHING:
        if lock.locked():
            raise HTTPException(409, f"A switch of '{name}' is already running.")
        # Nothing holds the lock: the switch that set the row never finished.
        row = await asyncio.to_thread(
            state_store.set_source_change_status, name, state_store.STATUS_PENDING,
            result=_interrupted(),
        )
    if row["status"] != state_store.STATUS_PENDING:
        raise HTTPException(
            409, f"The source change of '{name}' is not pending (status: {row['status']}).",
        )
    if (from_url, to_url) != (row["from_url"], row["to_url"]):
        raise HTTPException(
            409,
            "The pair does not match the pending source change. Reload the page "
            "and look at the change again.",
        )
    if not row.get("to_manifest_hash"):
        raise HTTPException(
            409, f"The source change of '{name}' carries no catalog manifest. Run Check updates again.",
        )
    if manifest_hash != row["to_manifest_hash"]:
        raise HTTPException(
            409,
            "The catalog entry changed since the page showed this change. Reload the "
            "page and look at the change again.",
        )

    async with lock:
        manifest = mcp_registry.get_manifest(name)
        if manifest is None:
            raise HTTPException(404, f"MCP '{name}' not found")
        installed = await asyncio.to_thread(_read_manifest, manifest.mcp_dir)
        identity = manifest_identity(installed) if installed is not None else ("unknown", "")
        to_identity = (row["to_kind"], row["to_identity"])
        if identity == to_identity:
            # A previous switch ended after its install (a crash, an error in
            # the half below): the folder is the new source already.
            await asyncio.to_thread(
                state_store.set_source_change_status, name, state_store.STATUS_SWITCHING,
                accepted_by=admin_sub,
            )
            swap_result = await _finish(
                name, row, installed=None, old_image="", ensure_start=True,
                result={"version": str((installed or {}).get("version") or ""),
                        "container_started": False},
                error=str((row.get("result") or {}).get("error") or ""),
            )
            return _reply(name, row, swap_result, recovered=True)
        if identity != (row["from_kind"], row["from_identity"]):
            raise HTTPException(
                409, "The installed source moved since the check. Run Check updates again.",
            )
        await asyncio.to_thread(
            state_store.set_source_change_status, name, state_store.STATUS_SWITCHING,
            accepted_by=admin_sub,
        )
        old_image = (manifest.server.image or "") if _mt.is_container(manifest.server) else ""
        try:
            result = await installer.install_from_catalog(
                name,
                accepted_source=to_identity,
                accepted_manifest_hash=row["to_manifest_hash"],
            )
        except Exception as exc:
            if isinstance(exc, HTTPException):
                detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            else:
                logger.exception("Switch of %s failed", name)
                detail = f"{type(exc).__name__}: {exc}"
            after = await asyncio.to_thread(_read_manifest, manifest.mcp_dir)
            if after is not None and manifest_identity(after) == to_identity:
                # The installer failed past its point of no return (the folder
                # replaced, the backup gone): the MCP runs from the new source,
                # so finish on it and record what went wrong.
                swap_result = await _finish(
                    name, row, installed=installed, old_image=old_image, error=detail,
                    result={"version": str(after.get("version") or ""), "container_started": False},
                )
                return _reply(name, row, swap_result)
            await asyncio.to_thread(
                state_store.set_source_change_status, name, state_store.STATUS_PENDING,
                result={"error": detail, "failed_at": _now()},
            )
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(500, f"Switch failed: {detail}")

        swap_result = await _finish(name, row, installed=installed, result=result, old_image=old_image)
        return _reply(name, row, swap_result, install_log=result.get("install_log", ""))


async def dismiss(name: str) -> None:
    """Drop a switched row; a pending or switching one is decided by the
    switch or the catalog, never dismissed."""
    row = await asyncio.to_thread(state_store.get_source_change, name)
    if row is None:
        raise HTTPException(404, f"No source change recorded for '{name}'")
    if row["status"] != state_store.STATUS_SWITCHED:
        raise HTTPException(
            409, f"The source change of '{name}' is {row['status']}; only a finished switch is dismissed.",
        )
    await asyncio.to_thread(state_store.delete_source_change, name)


async def reconcile_interrupted() -> list[str]:
    """After a restart, every ``switching`` row whose lock nobody holds is
    settled by what the restart left on disk: an install backup beside the
    folder means the install was cut short, so the backup (the old source,
    runtime dirs included) is restored and the row goes back to ``pending``
    with the interrupted error; a folder that already holds the new source
    means the switch ended after its install, so it is finished on that
    source (the renames applied, the row ``switched`` with the error noted);
    otherwise the row goes back to ``pending``. Then a ``<dir>.bak`` a crash
    left beside any other community install (scanned as a same-name manifest
    at every scan) is removed when the install's own manifest is in place.
    Returns the names moved back to pending."""
    from core.credentials import catalog_install_registry
    from services.mcp import mcp_registry

    moved: list[str] = []
    finished: list[str] = []
    restored = False
    for name in await asyncio.to_thread(state_store.interrupted_switches):
        if catalog_install_registry.lock_for(name).locked():
            continue
        row = await asyncio.to_thread(state_store.get_source_change, name)
        manifest = mcp_registry.get_manifest(name)
        mcp_dir = Path(manifest.mcp_dir) if manifest is not None else \
            Path(config.MCPS_DIR) / "community" / re.sub(r"[^a-zA-Z0-9_-]", "-", name)
        bak = mcp_dir.with_suffix(".bak")
        data = await asyncio.to_thread(_read_manifest, mcp_dir)
        if bak.is_dir():
            await asyncio.to_thread(installer._rollback_extracted_files, mcp_dir, bak)
            restored = True
        elif row is not None and data is not None and \
                manifest_identity(data) == (row["to_kind"], row["to_identity"]):
            await _finish(
                name, row, installed=None, old_image="", error=INTERRUPTED,
                result={"version": str(data.get("version") or ""), "container_started": None},
            )
            finished.append(name)
            continue
        await asyncio.to_thread(
            state_store.set_source_change_status, name, state_store.STATUS_PENDING,
            result=_interrupted(),
        )
        moved.append(name)
    if restored:
        await asyncio.to_thread(mcp_registry.scan_manifests)
    if moved:
        logger.warning("Source switches interrupted by a restart: %s", ", ".join(moved))
    if finished:
        logger.warning(
            "Source switches finished after a restart (their install had completed): %s",
            ", ".join(finished),
        )

    def _sweep() -> list[str]:
        removed: list[str] = []
        for m in list(mcp_registry.get_all_manifests().values()):
            if m.category != "community":
                continue
            mcp_dir = Path(m.mcp_dir)
            bak = mcp_dir.with_suffix(".bak")
            if (
                bak.is_dir() and (mcp_dir / "manifest.json").is_file()
                and not catalog_install_registry.lock_for(m.name).locked()
            ):
                shutil.rmtree(bak, ignore_errors=True)
                removed.append(str(bak))
        return removed

    removed = await asyncio.to_thread(_sweep)
    if removed:
        logger.warning("Removed install backups left by an interrupted update: %s", removed)
    return moved
