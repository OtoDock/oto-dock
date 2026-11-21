"""The per-session MCP config: ``build_session_mcp_config`` over one
``_SessionMcpBuild``, the file it writes, the sweep of old builds and the TOML
form of it that Codex reads.
"""

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config
from auth.session_token import CLI_TOKEN_ENV
from core import placement as _placement
from core.placement import LOCAL_PLACEMENT, PlacementCapabilities
from core.remote.tunnel_state import EXIT_INSTALLING
from core.stdio_path_interceptor import STANDIN_ENV
from services.mcp.mcp_egress import _rewrite_host_self_targets_to_loopback
from services.mcp.mcp_manifest_store import (
    _device_placement_reason,
    _get_agent_mcps_with_device_exclusions,
    get_manifest,
)
from services.mcp.mcp_manifest_types import McpManifest
from services.mcp.mcp_server_resolve import (
    _apply_browser_allowed_origins,
    _apply_browser_mode,
    _generate_instance_config_file,
    _inject_session_jwt_sentinel,
    gateway_entry,
    resolve_server_config,
)
from services.mcp.mcp_session_filter import (
    _exclusion_contexts,
    audience_refusal,
    session_exclusion_reason,
)
from services.mcp.mcp_templating import _SECRET_TEMPLATE_TOKENS
from services.oauth.credential_resolver import choice_kw
from storage.mcp import mcp_store

# The hosted key relay's per-person token in a tool's environment; its host
# is this install's relay base, not a name a manifest could declare.
RELAY_TOKEN_ENV = "OTODOCK_RELAY_TOKEN"

logger = logging.getLogger(__name__)


def exit_entry_hosts(entry: dict) -> tuple[str, ...]:
    """The hosts a ``credentials.exit`` entry's secret may go to: as declared,
    but the hosted relay's token toward this install's relay
    (``OTODOCK_RELAY_BASE``), whatever host the manifest names for it. The
    one answer the build's placeholders and the allowlist's rows read."""
    from urllib.parse import urlsplit
    if str(entry.get("key") or "") == RELAY_TOKEN_ENV:
        return (urlsplit(config.OTODOCK_RELAY_BASE).hostname or "",)
    hosts = entry.get("hosts")
    return tuple(str(h) for h in hosts) if isinstance(hosts, list) else ()


def build_session_mcp_config(
    agent_name: str,
    user_sub: str | None,
    *,
    phone_mode: bool = False,
    task_mode: bool = False,
    meeting_mode: bool = False,
    interactive_local: bool = False,
    shared_chat: bool = False,
    chat_worker: bool = False,
    task_scope: str = "user",
    delegation_targets: list[str] | None = None,
    extra_mcps: list[str] | None = None,
    mcp_config_format: str | None = "json",   # None = JSON consumed in-process (Direct LLM)
    username: str = "",
    user_role: str = "",
    chat_id: str = "",
    task_owner: str = "",
    task_username: str = "",
    placement: PlacementCapabilities = LOCAL_PLACEMENT,
    target_browser=None,
    external: bool = False,
    only_mcps: list[str] | None = None,
    session_id: str = "",
    call_ask_back: bool = False,
    call: bool = False,
    personal_owner: str = "",
    account_choices: dict[str, str] | None = None,
    outside_picks=None,
    kind_rule=None,
    limits=None,
) -> tuple[Path | None, dict[str, str], dict[str, str], dict, set]:
    """Main entry point: build a complete MCP config for a session.

    ``outside_picks`` (``services/mcp/outside_picks.OutsidePicks``): what a
    manager picked for outside people, read by the one rule on an
    ``external`` build; an external build without them attaches nothing.
    ``kind_rule`` (``mcp_session_filter.KindRule``): a session kind's own
    hard set and the platform tools it rides with (the wrap-up after a
    conversation, the agent's own phone line), read by the same rule.

    ``account_choices``: the chat's choice of account per MCP
    (``{mcp_name: account_label}``): a person's own chat's (user scope) or a
    Shared-only chat's among the agent's own accounts (agent scope), a wake of
    either chat included. Every account is read through it
    (``credential_resolver.pick_account``): the resolved credentials and the
    gateway's bearer and reach. A task, call, meeting, worker or outside
    person's build ignores it.

    ``call_ask_back`` is a phone session's fact (a daemon-driven outbound
    call: an agent waits for questions) exposed to stdio MCPs as
    ``${session.call_ask_back}``. ``call`` marks a call's session, whatever
    its ``phone_mode`` (an outside caller on any route mode): its stdio tools
    start beside the engine, so its first tool call pays no start.

    ``session_id`` writes the build into the session's own directory (the
    file keeps the identity's name), so a concurrent build of the same
    identity never rewrites it between this build and its start; without
    one the identity's shared file is written (a pooled build). It also
    notes which of the agent's own accounts the session took
    (``session_state.note_built_agent_accounts``).

    ``only_mcps`` (CHECKS.md, the judge profile) keeps ONLY the named MCPs
    of what the agent's assignment and the gates above resolved — an
    intersection, never an addition (``[]`` = no MCP at all).

    ``personal_owner``: a session on the agent's accounts on a user-paired
    machine whose person is that machine's owner
    (``config_builder.personal_owner_of``). Such a build reads every binding
    of the agent through the owner filter (``credential_resolver.pick_account``):
    the owner's own accounts go, nobody's when it is empty. It also decides
    the machine's browser token.

    Whose credentials the build resolves is asked once
    (``account_context.credential_identity``, from ``user_sub``, the mount
    ``task_scope`` and ``user_role``) and every credential read follows it:
    the resolver, the gateway's bearer, the owner filter, the network a tool
    may reach. The mount scope keeps its own uses (the audience, the paths,
    the session tokens).

    Args:
        target_browser: the target machine's ``remote_store.BrowserTargetSettings``
            (browser-control mode + extension token) — None / dedicated
            leaves the browser entry untouched; ``own`` is applied by
            ``_apply_browser_mode``. Local sessions never carry it (the MCP
            is satellite-only).
        meeting_mode: meeting-participant session. Meetings RIDE the task lane
            (callers also pass ``task_mode=True``), so their base exclusion
            context stays ``"task"`` — this flag ADDITIONALLY matches
            ``"meeting"`` in ``exclude_from``, letting a manifest opt out of
            meetings without leaving scheduled tasks. Needed for config/prompt
            agreement: the prompt catalog and skills filter by ``client_type``
            (which IS ``"meeting"`` there), so without this a
            meeting-excluded manifest would vanish from the prompt while its
            tools still loaded.
        chat_worker: a chat lane a person delegated from their own chat
            (``task_config_builder.attended_chat_worker``): it rides the task
            lane (``task_mode``) but matches the chat's context, not
            ``"task"``, so the MCPs that need a person present load in it.
        shared_chat: the session is not the person's own chat (the chat
            builder's fact; an agent-scope ``task_scope`` implies it): the
            ``shared_chat`` context joins the set, so a manifest that lists
            it loads only where a person drives their own chat.
        extra_mcps: MCP names to force-include even if not assigned to the agent
                    (e.g. meetings-mcp for meeting participants).
        mcp_config_format: "json" (Claude CLI) or "toml" (Codex CLI).
        username: session's username — passed to instance-config transforms
            so they can inject scope-aware values (e.g. ssh-server's
            ``allowedLocalPaths``).
        user_role: session's access level (viewer/manager/admin/"") —
            same purpose as username; lets transforms compute mount sets.
        chat_id, task_owner, task_username: per-session context exposed to
            stdio MCP ``agent_env`` blocks via ``${session.*}`` token
            resolution. ``task_owner``/``task_username`` are populated for
            scheduled-task and meeting sessions; empty for interactive chats.

    Returns: (config_path, credential_env_vars, exclusion_reasons, secret_bundles,
        bash_env_keys) — a 5-tuple. ``credential_env_vars`` is the NON-secret flat
        env (secrets are stripped to the per-MCP ``secret_bundles``);
        ``bash_env_keys`` are the git helper entries (GIT_CONFIG_*, no
        secret) that stay in the CLI parent env but are listed in
        ``OTO_STRIP_KEYS`` so the interceptor drops them from MCP children.

    ``secret_bundles`` maps each included MCP's mcpServers key (server_name or
    mcp name) to a ``core.credentials.mcp_broker.SecretBundle`` carrying that one MCP's
    secret material (resolver creds + relay token + instance field_values +
    HTTP bearer). The execution layer provisions these into the broker and the
    stdio interceptor fetches them at spawn. The PURE secrets ARE stripped from
    the returned flat env + the per-server config env here (see below); the
    interceptor re-injects identical values at spawn.
    """
    from core.credentials.mcp_broker import SecretBundle
    from core.session.visibility import SCOPE_AGENT
    from services.mcp import account_context
    from services.oauth import credential_resolver

    creds = account_context.credential_identity(
        agent_name, user_sub, mount_scope=task_scope, role=user_role)
    exclusion_contexts = _exclusion_contexts(
        phone_mode, task_mode, interactive_local, meeting_mode, external,
        shared_chat=shared_chat or task_scope == SCOPE_AGENT, chat_worker=chat_worker)
    # Get assigned + enabled MCPs for this agent. Device-local placement /
    # consent / display filtering happens here; the dropped device MCPs
    # come back as exclusion reasons so the prompt can surface them.
    assigned, exclusion_reasons = _get_agent_mcps_with_device_exclusions(
        agent_name, placement=placement,
    )
    # Whether the session's machine holds its own copy of the files (a paired
    # computer, a compute machine on another host): the compute machine
    # beside the platform reads the platform's folder, so a file the platform
    # writes for the session under the agent's folder is there.
    is_remote = placement.files_synced
    # The apps tool rides an outside session's picks as the call tools ride
    # its channel: every engine's build, and the Direct manager's rebuild,
    # take it from here (``outside_picks.APPS_MCP``).
    from services.mcp import outside_picks as _outside
    if external and outside_picks is not None and _outside.APPS_MCP in outside_picks.channel:
        extra_mcps = [*(extra_mcps or []), _outside.APPS_MCP]
    _force_include(assigned, extra_mcps, exclusion_reasons, placement)
    # A server the session forces in (the meetings tool in a meeting, the
    # call tools on a call) is the channel's own and never limited.
    if limits and extra_mcps:
        limits = {name: limit for name, limit in limits.items() if name not in extra_mcps}
    if only_mcps is not None:
        keep = {str(n) for n in only_mcps}
        assigned = [m for m in assigned if m.name in keep]

    if not assigned:
        # Preserve device-placement exclusion reasons even when nothing remains.
        # MUST return the full 5-tuple (callers unpack mcp_config, credential_env,
        # excluded_mcps, secret_bundles, bash_env_keys) — a 4-tuple here crashes any
        # session that resolves to zero MCPs ("not enough values to unpack").
        _note_left_out(session_id, agent_name, exclusion_reasons)
        return None, {}, exclusion_reasons, {}, set()

    # A session on the agent's accounts on a person's own machine: the owner
    # filter, on even when no owner was found (then it matches nobody).
    only_owner = personal_owner if creds.agents and placement.user_paired else None
    owner_kw: dict[str, Any] = {"only_owner": only_owner} if only_owner is not None else {}
    if creds.refusal:
        owner_kw["team_floor_refusal"] = creds.refusal
    # A chat's choice of account: a person's own chat's among their set, a
    # chat on the agent's accounts among the agent's own, a run's own
    # (``chat_reload.schedule_for_run``). A wake is that chat continuing
    # (``wake_session._wake_mcp_build``) and keeps it, so its tools, prompt
    # and token files name one account. Never a call's, a meeting's nor an
    # outside person's, whatever a caller hands in.
    unattended = phone_mode or meeting_mode or external
    choices = dict(account_choices or {}) if not unattended else {}
    # Whether the credential exit carries this session's declared secrets
    # (the one machine rule): the resolver's personal-machine sentence and
    # git's entries read the same answer the placeholders are minted on.
    from core.credentials import credential_exit
    exit_carries = not exit_machine_refusal(placement)
    git_exit = (credential_exit.machine_env(session_id or "", placement)
                if exit_carries and placement.is_remote and placement.kind != _placement.KIND_COMPUTE
                and not cli_per_command(placement) else {})
    # Resolve credentials for all assigned MCPs
    cred_result = credential_resolver.resolve_credentials(
        agent_name, creds.sub,
        task_scope=creds.scope, exit_carries=exit_carries, git_exit=git_exit or None,
        **owner_kw, **choice_kw(choices),
    )
    if session_id:
        # Which of the agent's own accounts this session holds: a removal
        # from the set closes the sessions built on what it removed.
        from core.session import session_state
        session_state.note_built_agent_accounts(session_id, agent_name, tuple(sorted(
            (mcp, ref.owner_sub, ref.label) for mcp, ref in cred_result.agent_accounts.items())))

    # The flat env that reaches the CLI/daemon process + the codex TOML is
    # stripped of PURE MCP secrets — each MCP gets those via its broker bundle
    # instead. The git helper entries + paths + OTO_* STAY (bash + the MCP need
    # them in-process); no token value is among them.
    flat_env = {
        k: v for k, v in cred_result.env_vars.items()
        if k not in cred_result.secret_keys
    }

    # Build mcpServers dict
    servers: dict[str, dict] = {}
    # Credential broker: per-MCP secret bundles, keyed by srv_key.
    secret_bundles: dict[str, SecretBundle] = {}
    build = _SessionMcpBuild(
        session_id=session_id or "",
        agent_name=agent_name,
        user_sub=user_sub,
        task_mode=task_mode,
        task_scope=task_scope,
        mcp_config_format=mcp_config_format,
        username=username,
        user_role=user_role,
        chat_id=chat_id,
        task_owner=task_owner,
        task_username=task_username,
        placement=placement,
        external=external,
        call_ask_back=call_ask_back,
        call=call,
        exclusion_contexts=exclusion_contexts,
        exclusion_reasons=exclusion_reasons,
        cred_result=cred_result,
        is_remote=is_remote,
        servers=servers,
        secret_bundles=secret_bundles,
        only_owner=only_owner,
        account_choices=choices,
        outside_picks=outside_picks,
        kind_rule=kind_rule,
        limits=limits or None,
        creds=creds,
    )
    for manifest in assigned:
        build.add(manifest)
    _note_left_out(session_id, agent_name, exclusion_reasons)
    if not servers:
        return None, flat_env, exclusion_reasons, {}, cred_result.bash_env_keys
    # A key's turn is an unattended run for the paired computer's own browser.
    from core.session import key_rows
    _finish_session_servers(
        servers, secret_bundles, cred_result, agent_name, delegation_targets, target_browser,
        task_mode=task_mode, phone_mode=phone_mode, meeting_mode=meeting_mode,
        key_turn=key_rows.builds(chat_id), only_owner=only_owner)
    config_path = _write_session_mcp_config(
        servers, agent_name, user_sub, session_id, mcp_config_format)
    return config_path, flat_env, exclusion_reasons, secret_bundles, cred_result.bash_env_keys


def _note_left_out(session_id: str, agent_name: str, reasons: dict[str, str]) -> None:
    """One line per build for the tools a session did not get, each with its
    reason: the sentences the prompt's excluded-tools section shows, which
    name no value and no path."""
    if reasons:
        logger.info("MCP build session=%s agent=%s: %d tool(s) left out: %s", (session_id or "-")[:8],
                    agent_name, len(reasons), "; ".join(f"{n}: {r}" for n, r in sorted(reasons.items())))


def _force_include(assigned: list[McpManifest], extra_mcps: list[str] | None,
                   exclusion_reasons: dict[str, str], placement: PlacementCapabilities) -> None:
    """Add the MCPs a session forces in (``extra_mcps``) to ``assigned``."""
    # Force-include extra MCPs (e.g. meetings-mcp for meeting participants).
    # NOTE: extra_mcps is an engine-level escape hatch — it BYPASSES both
    # visibility (admin authorization) AND enablement (manager toggle). Only
    # use it for system-essential MCPs that must always be available regardless
    # of agent config (currently: meetings-mcp during a meeting). Adding new
    # entries here amounts to giving an MCP "always-on" status. It does NOT,
    # however, bypass the device-local placement gate — a satellite_only MCP
    # must never be force-injected onto a local session.
    if extra_mcps:
        assigned_names = {m.name for m in assigned}
        for mcp_name in extra_mcps:
            if mcp_name in assigned_names:
                continue
            manifest = get_manifest(mcp_name)
            if not manifest:
                continue
            reason = _device_placement_reason(manifest, placement=placement)
            if reason:
                exclusion_reasons[mcp_name] = reason
                continue
            assigned.append(manifest)


#: What a person reads when a machine's exit is not up, with the advice that
#: fits the reason. A machine reports ``packages`` when its own install of
#: them failed in this run of its program: no build for its platform, or a
#: download or a disk that failed, which a second run of the installer
#: mends. A machine whose packages still install after an update is still
#: in its update (``machine_away.finishing_update``): no session is built
#: there, and a placement that names it reads the update's sentence.
EXIT_OFF = "Tools that need a key are off on {label}."
_EXIT_ADVICE = {
    "packages": "A package it needs is missing there. Run the OtoDock installer on it again.",
    "socks": "Its proxy is a SOCKS proxy, which OtoDock cannot use.",
}
_EXIT_RESTART = "Restart OtoDock on that computer."


def _exit_off(label: str, why: str = "") -> str:
    return f"{EXIT_OFF.format(label=label)} {_EXIT_ADVICE.get(why, _EXIT_RESTART)}"


def exit_machine_refusal(placement: PlacementCapabilities) -> str:
    """Why a session on this machine runs no credential exit, or ``""`` when
    the machine's own exit carries its secrets (every satellite at the
    platform's floor runs one): no listener reported or one that does not
    answer (``exit_port``, ``exit_ok``, with ``exit_why``: the live
    connection's report, the row's as the fallback), a paired computer
    whose satellite reports no OtoDock folder to name the trust bundle
    under. The one answer the build's exclusion
    (``_excluded``) and placeholders, both attach forms, the payload's block,
    the resolver's personal-machine sentence and the Direct layer read. A
    placement that is no machine is never refused here: no session runs
    there, and a preview builds on it."""
    if not placement.is_remote:
        return ""
    from services.remote import machine_away
    label = machine_away.label_inside(placement.label)
    if not placement.exit_port or not placement.exit_ok:
        if placement.exit_why == EXIT_INSTALLING:
            return machine_away.FINISHING.format(label=machine_away.label_at_start(placement.label))
        return _exit_off(label, placement.exit_why)
    if placement.kind != _placement.KIND_COMPUTE and not placement.reported_otodock_dir:
        return _exit_off(label)
    return ""


def holds_a_key_for_the_exit(manifest: McpManifest, cred_result, mcp_name: str,
                             placement: PlacementCapabilities) -> bool:
    """Whether the tool holds a secret the credential exit carries on this
    placement: a key its manifest declares for the exit (``credentials.exit``),
    or git's and gh's token where the session holds a placeholder for it
    (every place but a paired macOS or Windows computer, ``cli_per_command``).
    Such a tool is left out of a session on a machine the exit rule refuses
    (``_excluded``): no value is handed instead."""
    if getattr(getattr(manifest, "credentials", None), "exit", None):
        return True
    clis = getattr(cred_result, "cli_credentials", None) or {}
    return mcp_name in clis and not cli_per_command(placement)


def cli_per_command(placement: PlacementCapabilities) -> bool:
    """Whether git's and gh's GitHub token goes to the session itself, per
    command from the credential hook: on a paired macOS or Windows computer
    alone, where git and gh verify with the system store (SecureTransport,
    schannel) and read no trust variable, so they cannot trust the machine's
    exit. Everywhere else the session holds a placeholder, a paired computer
    that names no system among those two included. The one answer the build
    and the hook read."""
    from core import host_os
    paired = placement.is_remote and placement.kind != _placement.KIND_COMPUTE
    return paired and placement.os in (host_os.DARWIN, host_os.WINDOWS)


def _hands_a_secret(manifest: McpManifest) -> bool:
    """Whether the platform hands the tool something of its own that can be
    a secret: credential fields, a sign-in, a header key, an exit entry, or
    an instance it delivers to the tool."""
    creds = getattr(manifest, "credentials", None)
    if creds is not None and (getattr(creds, "type", "none") != "none" or getattr(creds, "fields", None)
                              or getattr(creds, "oauth", None) or getattr(creds, "api_key_header", None)
                              or getattr(creds, "exit", None)):
        return True
    inst = getattr(manifest, "instances", None)
    return inst is not None and getattr(inst, "delivery", "none") != "none"


def kept_off_a_persons_machine(manifest: McpManifest) -> bool:
    """Whether ``remote_policy: "admin_paired_only"`` keeps the tool off a
    person's own machine: for a tool the platform hands a secret of its own,
    since such a secret can reach a machine as a value. The platform keeps
    the SSH keys and signs, so a tool it hands no secret (SSH hosts) reaches
    a person's own machine. The one answer the build and a machine's wanted
    set read."""
    if getattr(manifest, "remote_policy", "any") != "admin_paired_only":
        return False
    return _hands_a_secret(manifest)


@dataclass
class _SessionMcpBuild:
    """One session's MCP config build: what every entry is built from, and
    what the build collects (the entries, their secret bundles, the reason
    each MCP left out was left out)."""

    session_id: str
    agent_name: str
    user_sub: str | None
    task_mode: bool
    task_scope: str
    mcp_config_format: str | None
    username: str
    user_role: str
    chat_id: str
    task_owner: str
    task_username: str
    placement: PlacementCapabilities
    external: bool
    call_ask_back: bool
    # A call's session (``build_session_mcp_config``): its stdio tools start
    # beside the engine (a warm stand-in), so its first call pays no start.
    call: bool
    exclusion_contexts: set[str]
    exclusion_reasons: dict[str, str]
    cred_result: Any
    is_remote: bool
    servers: dict[str, dict]
    secret_bundles: dict[str, Any]
    # The owner filter of an agent-scope session on a personal machine
    # (``build_session_mcp_config``); None elsewhere.
    only_owner: str | None = None
    # The chat's choice of account per MCP in a user-scope build with a
    # person (``build_session_mcp_config``); empty elsewhere.
    account_choices: dict[str, str] | None = None
    # The Outside people picks of an external build (``outside_picks``);
    # None elsewhere, and on an external build None admits nothing.
    outside_picks: Any = None
    # A session kind's own hard set (``kind_rule``); None elsewhere.
    kind_rule: Any = None
    # The agent's tool limits for this session's kind (``tool_limits``).
    limits: Any = None
    # Whose credentials the build resolves (``account_context.Identity``);
    # the person at the mount when a caller built the record without one.
    creds: Any = None

    def _creds(self):
        if self.creds is None:
            from services.mcp import account_context
            self.creds = account_context.Identity(self.user_sub, self.task_scope)
        return self.creds

    def add(self, manifest: McpManifest) -> None:
        """One assigned MCP: its entry and secret bundle, or the reason it
        is left out."""
        from core.credentials.mcp_broker import SecretBundle
        (agent_name, task_mode, task_scope, mcp_config_format, chat_id, task_owner,
         task_username, call_ask_back, exclusion_reasons, cred_result, servers, secret_bundles) = (
            self.agent_name, self.task_mode, self.task_scope,
            self.mcp_config_format, self.chat_id, self.task_owner, self.task_username,
            self.call_ask_back, self.exclusion_reasons, self.cred_result, self.servers,
            self.secret_bundles)
        mcp_name = manifest.name
        if self._excluded(manifest, mcp_name):
            return
        # Build server config (session ctx populated for ${session.*} token
        # resolution in manifest agent_env declarations)
        session_ctx = {
            "chat_id": chat_id,
            "task_owner": task_owner,
            "task_username": task_username,
            "task_scope": task_scope if (task_owner or task_mode) else "",
            "call_ask_back": "1" if call_ask_back else "",
        }
        server_entry = resolve_server_config(
            manifest, agent_name,
            mcp_config_format=mcp_config_format,
            session_ctx=session_ctx,
        )

        # Credential broker: collect THIS MCP's secret material into a
        # bundle the stdio interceptor fetches at spawn. Resolver creds come
        # from the per-MCP attribution; the relay token + instance
        # field_values are added at their injection points below; an HTTP
        # MCP's credential is the bundle's gateway half (``gateway_entry``).
        # The PURE secrets are stripped from the flat env + config below;
        # the interceptor re-fetches them from the broker at spawn.
        # Bundle carries ONLY this MCP's PURE secrets — NOT the
        # env_injection names in env_by_mcp, which stay in the flat env.
        bundle_env: dict[str, str] = {
            k: v for k, v in cred_result.env_by_mcp.get(mcp_name, {}).items()
            if k in cred_result.secret_keys
        }
        # Agent_env secrets: a manifest agent_env value resolved from a
        # SECRET template (see _SECRET_TEMPLATE_TOKENS — empty today) must
        # not stay in the session config file either — the resolver strip
        # above only covers credential-store secrets. Move the resolved value
        # into the bundle; the interceptor re-injects it at spawn.
        for _env_key, _template in (manifest.agent_env or {}).items():
            if any(tok in str(_template) for tok in _SECRET_TEMPLATE_TOKENS):
                _val = (server_entry.get("env") or {}).pop(_env_key, "")
                if _val:
                    bundle_env[_env_key] = _val
        # Per-session JWT for Docker/HTTP MCPs that call back to the proxy hooks
        # (manifest ``server.proxy_callbacks``; today only file-tools). See
        # _inject_session_jwt_sentinel.
        server_entry = _inject_session_jwt_sentinel(server_entry, manifest)
        if self._instance_delivery(manifest, mcp_name, server_entry, bundle_env):
            return
        # Use server_name (backward-compatible key) for the mcpServers dict
        srv_key = manifest.server_name or mcp_name

        # The credential gateway: a remote-HTTP MCP with a credential dials
        # the gateway route with the session token; the credential rides the
        # bundle and is added per request on the way out. After the instance
        # block, so an env-delivered key is in ``bundle_env``.
        owner_kw: dict[str, Any] = {"only_owner": self.only_owner} if self.only_owner is not None else {}
        creds = self._creds()
        gateway_cred, gateway_reason = gateway_entry(
            server_entry, manifest, srv_key=srv_key, user_sub=creds.sub,
            agent_name=agent_name, task_scope=creds.scope, bundle_env=bundle_env,
            **owner_kw, **choice_kw(self.account_choices),
        )
        if gateway_reason:
            exclusion_reasons[mcp_name] = gateway_reason
            return
        if gateway_cred is not None:
            from core.credentials.mcp_gateway import CONTAINER
            # An HTTP MCP has no process to fetch an env: the key's value
            # lives in the credential alone.
            bundle_env = {}
            # A call's Docker tool wakes at the call's handshake.
            gateway_cred.wakes_early = self.call and gateway_cred.kind == CONTAINER

        # Host-self target rewrite (T1): a homelab MCP configured with one of the
        # proxy host's OWN IPs (a co-located service, e.g. Prometheus on the
        # bare-metal box) must dial 127.0.0.1 — that IP is local inside the
        # agent's netns; resolve_sandbox_egress forwards the port to the host
        # loopback. Applied to both the broker bundle (instance/cred secrets) and
        # the plain env (config source).
        _rewrite_host_self_targets_to_loopback(
            manifest, bundle_env, agent_name, user_sub=creds.sub, task_scope=creds.scope,
        )
        if server_entry.get("env"):
            _rewrite_host_self_targets_to_loopback(
                manifest, server_entry["env"], agent_name,
                user_sub=creds.sub, task_scope=creds.scope,
            )

        # Claude expands ``${VAR}`` in an entry's values from its own env, so
        # an entry that names the shell's command-line token would be handed
        # it: no such MCP joins the session.
        if _names_cli_token(server_entry):
            logger.warning("MCP %s names %s in its configuration: left out of the session",
                           mcp_name, CLI_TOKEN_ENV)
            exclusion_reasons[mcp_name] = (
                f"its configuration names {CLI_TOKEN_ENV}, the session's command-line "
                "token, which no MCP server may hold")
            return

        servers[srv_key] = server_entry
        # The command-line credential (a ``git_credential_helper`` manifest):
        # by reference on the bundle, fetched per command through the broker;
        # the token itself only where the exit cannot carry it.
        cli_cred = cred_result.cli_credentials.get(mcp_name)
        if cli_cred is not None and cli_per_command(self.placement):
            cli_cred = dataclasses.replace(cli_cred, per_command=True)
        # An HTTP MCP has no process: its account token rides the gateway.
        token_cred = cred_result.token_credentials.get(mcp_name) if "command" in server_entry else None
        exit_creds, token_covered = self._exit_placeholders(manifest, bundle_env, cli_cred, token_cred)
        if token_covered and token_cred is not None:
            # The exit reads the token file per request: the tool holds a
            # placeholder that never expires, so no renewal is minted.
            token_cred = dataclasses.replace(token_cred, renews=False)
        if bundle_env or gateway_cred is not None or cli_cred is not None:
            secret_bundles[srv_key] = SecretBundle(
                env=bundle_env, gateway=gateway_cred, cli=cli_cred, exit=exit_creds, token=token_cred,
                account=self._account_of(mcp_name),
            )
            # A credentialed (wrapped) stdio MCP drops the shell-only keys it
            # would inherit from the CLI env (the git helper entries and the
            # command-line token): the interceptor strips OTO_STRIP_KEYS-named
            # vars at spawn. (No-op on HTTP MCPs — they aren't stdio-wrapped.)
            if "command" in server_entry:
                server_entry.setdefault("env", {})["OTO_STRIP_KEYS"] = _strip_keys(
                    cred_result.bash_env_keys)
        from core.session import session_kind
        from services.mcp import tool_listing
        if "command" in server_entry and tool_listing.starts_late(
                manifest, eager=session_kind.PHONE.name in self.exclusion_contexts and not self.call):
            self._stand_in(manifest, srv_key, server_entry)

    def _stand_in(self, manifest: McpManifest, srv_key: str, server_entry: dict) -> None:
        """A stdio tool whose program waits for the agent's first call: the
        stand-in's marker on its entry, and a bundle (an empty one when it has
        no secret), so every mint site gives it a fetch token and every wrap
        puts the stand-in in front of it (``services/mcp/tool_listing.py``).
        The marker names the account the build resolved, by a hash: a tool
        whose list depends on its account is answered per account."""
        from core.credentials.mcp_broker import SecretBundle
        from services.mcp import tool_listing
        bundle = self.secret_bundles.get(srv_key)
        fetch = bundle is not None and (bool(bundle.env) or bundle.token is not None)
        if bundle is None:
            self.secret_bundles[srv_key] = SecretBundle()
        server_entry.setdefault("env", {})[tool_listing.STANDIN_ENV] = tool_listing.marker(
            manifest, account=self._account_of(manifest.name), fetch=fetch, warm=self.call)

    def _account_of(self, mcp_name: str) -> str:
        """Which account an entry was built on (its owner and label, the
        resolver's ``accounts``, either scope), "" for a tool with none."""
        from core.credentials.mcp_gateway import account_text
        return account_text(self.cred_result.accounts.get(mcp_name))

    def _exit_placeholders(self, manifest: McpManifest, bundle_env: dict[str, str],
                           cli_cred, token_cred) -> tuple[tuple, bool]:
        """The credential exit's half of the bundle (the one machine rule
        admitting the session's place: a compute machine or a paired computer
        whose satellite runs the exit): each secret the manifest declares for the exit
        (``credentials.exit``) leaves ``bundle_env`` as a placeholder, its
        value held behind it for the exit to add in the declared header
        toward the declared hosts (the hosted relay's token toward this
        install's relay); a tool's account token goes by reference
        (``token_ref``, read fresh per request) when every one of its names
        is declared, and then without its expiry, since the placeholder
        never expires (the second answer says so, and the caller drops the
        renewal); a command-line credential gets a placeholder for its host
        and the host's subdomains, which the helper hands git and gh in place
        of the token, on compute and on a Linux computer (gh is a Go program
        and reads no trust variable on macOS or Windows). On a paired
        computer the bundle also carries the machine's exit variables
        (``credential_exit.machine_env``), so the tool's process alone
        routes through the exit."""
        from core.credentials import credential_exit
        from core.credentials.mcp_gateway import TOKEN_EXPIRES_ENV, ExitCredential
        if exit_machine_refusal(self.placement):
            return (), False
        out: list[ExitCredential] = []
        declared = getattr(manifest.credentials, "exit", None) or []
        token_names = set(token_cred.env_names) if token_cred is not None else set()
        token_covered = bool(token_names) and token_names <= {str(e.get("key") or "") for e in declared}
        for entry in declared:
            key = str(entry.get("key") or "")
            if key not in bundle_env or (key in token_names and not token_covered):
                continue
            hosts = exit_entry_hosts(entry)
            placeholder = credential_exit.new_placeholder()
            by_reference = key in token_names
            out.append(ExitCredential(
                placeholder=placeholder, header=str(entry.get("header") or ""), hosts=hosts,
                allowlist_key=manifest.name, manifest=manifest.name, env_name=key,
                prefix=str(entry.get("prefix") or ""), value="" if by_reference else bundle_env[key],
                token_ref=token_cred.token_ref if by_reference else None,
                streams_declared=entry.get("streams") is True,
            ))
            bundle_env[key] = placeholder
        if token_covered:
            bundle_env.pop(TOKEN_EXPIRES_ENV, None)
        paired = self.placement.is_remote and self.placement.kind != _placement.KIND_COMPUTE
        if cli_cred is not None and cli_cred.host and not cli_per_command(self.placement):
            host = cli_cred.host.lower()
            out.append(ExitCredential(
                placeholder=credential_exit.new_placeholder(), header="Authorization",
                hosts=(host, f"*.{host}"), allowlist_key=manifest.name, manifest=manifest.name,
                cli=cli_cred,
            ))
        if out and paired:
            bundle_env.update(credential_exit.machine_env(self.session_id, self.placement))
        return tuple(out), token_covered

    def _excluded(self, manifest: McpManifest, mcp_name: str) -> bool:
        """The checks before the entry is built; True when the MCP is left
        out (the reason recorded, or none for a context-only MCP)."""
        (task_scope, user_role, placement, external, exclusion_contexts, exclusion_reasons,
         cred_result) = (
            self.task_scope, self.user_role, self.placement, self.external,
            self.exclusion_contexts, self.exclusion_reasons, self.cred_result)
        # Context exclusion (manifest exclude_from + the external hard set) —
        # the ONE rule shared with the prompt builders and the route preview.
        context_reason = session_exclusion_reason(
            manifest, contexts=exclusion_contexts, external=external, picks=self.outside_picks,
            kind_rule=self.kind_rule, limits=self.limits,
        )
        if context_reason:
            exclusion_reasons[mcp_name] = context_reason
            return True

        # The manifest's audience: a person below the tier it names is not
        # handed the MCP (the prompt, the egress carve and the sandbox agree).
        audience_reason = audience_refusal(manifest, user_role=user_role, task_scope=task_scope)
        if audience_reason:
            exclusion_reasons[mcp_name] = audience_reason
            return True

        # Check credential exclusion
        if mcp_name in cred_result.excluded_mcps:
            exclusion_reasons[mcp_name] = cred_result.exclusion_reasons.get(
                mcp_name, "Credentials not configured"
            )
            return True

        # A session on the agent's accounts (a Shared-only chat, a team
        # account's) on a person's own machine: the agent's own accounts
        # stay on the platform (the config builder withholds their secret
        # files there too), except an account of the machine's owner, which
        # the owner filter alone let resolve.
        from services.oauth.credential_resolver import carries_to_personal_machine
        if (self._creds().agents and placement.user_paired
                and cred_result.env_by_mcp.get(mcp_name)
                and not (self.only_owner and carries_to_personal_machine(manifest))):
            exclusion_reasons[mcp_name] = (
                f"{manifest.label} uses the agent's own account, which stays "
                "on the platform"
            )
            return True

        # A tool that holds a key the exit carries, on a machine whose exit
        # does not run: left out with the rule's reason, never handed the
        # value instead. The session build alone reads it: the machine's
        # wanted set keeps the tool, since the answer moves with a heal.
        exit_reason = exit_machine_refusal(placement)
        if exit_reason and holds_a_key_for_the_exit(manifest, cred_result, mcp_name, placement):
            exclusion_reasons[mcp_name] = exit_reason
            return True

        # A community tool the session's compute machine has not built yet
        # (or holds in another version): left out with the reason. A
        # session's start never waits for an install. It asks for it.
        from services.remote import machine_tools
        build_reason = machine_tools.refusal(manifest, placement)
        if build_reason:
            exclusion_reasons[mcp_name] = build_reason
            return True

        # Admin-paired-only MCPs: the tool surface exists exactly where the
        # MCP's session data may go, the platform's own places and
        # admin-paired satellites, never a person's own place; a visible
        # exclusion there (``kept_off_a_persons_machine``: lifted for a tool
        # handed no secret, SSH hosts, whose keys the proxy keeps and signs
        # with).
        if placement.user_paired and kept_off_a_persons_machine(manifest):
            exclusion_reasons[mcp_name] = (
                f"{manifest.label} is unavailable on this machine — "
                "it is delivered only to admin-paired machines"
            )
            return True

        # Context-only MCPs (transport "none"): no server process, so nothing
        # to emit into mcpServers. Skills, dynamic prompt context, instance
        # authorization and network_targets all apply through their own
        # paths, and nothing of theirs rides to a machine.
        if manifest.server.transport == "none":
            return True
        return False

    def _instance_delivery(self, manifest: McpManifest, mcp_name: str, server_entry: dict,
                           bundle_env: dict[str, str]) -> bool:
        """An instance-based MCP's config file or env delivery; True when the
        MCP is left out (its reason recorded)."""
        agent_name, user_sub, username, user_role, exclusion_reasons, is_remote = (
            self.agent_name, self.user_sub, self.username, self.user_role,
            self.exclusion_reasons, self.is_remote)
        # Instance-based MCP config (generalized — replaces SSH-specific block)
        if manifest.instances:
            inst_cfg = manifest.instances
            if inst_cfg.delivery == "config_file":
                # config_file delivery: the generated instance file (and,
                # for ssh-server, the private keys its ``privateKey`` entries
                # point at) lives in the proxy's sessions folder, which no
                # machine mounts. A sandboxed Claude Code session gets a copy
                # in its config dir under the agent's folder
                # (``session_config_dir.prepare_mcp_config_for_sandbox``),
                # which the compute machine beside the platform shares. A
                # machine with its own copy of the folders (``files_synced``:
                # a paired computer, a compute machine on another host) gets
                # none, so the path would dangle there and the MCP would
                # spawn broken with no visible reason: exclude it with an
                # explicit reason instead (surfaced in the prompt's
                # "# Excluded MCPs"). Secure remote delivery (admin-paired
                # machines only) is a planned framework extension.
                if is_remote:
                    exclusion_reasons[mcp_name] = (
                        f"{manifest.label} is unavailable on remote machines — "
                        "its instance config and key files stay on the "
                        "platform host"
                    )
                    return True
                inst_config_dir = config.SESSIONS_DIR / "user-mcp-configs"
                inst_config_dir.mkdir(parents=True, exist_ok=True)
                inst_path = _generate_instance_config_file(
                    mcp_name, agent_name, inst_config_dir, manifest,
                    username=username, user_role=user_role,
                )
                if not inst_path:
                    exclusion_reasons[mcp_name] = (
                        f"No {manifest.label} instances assigned to this agent"
                    )
                    return True
                # Override config file arg if declared in manifest
                if inst_cfg.config_file_arg and "args" in server_entry:
                    new_args = []
                    skip_next = False
                    for arg in server_entry["args"]:
                        if skip_next:
                            skip_next = False
                            continue
                        if arg == inst_cfg.config_file_arg:
                            new_args.append(arg)
                            new_args.append(str(inst_path))
                            skip_next = True
                        else:
                            new_args.append(arg)
                    server_entry["args"] = new_args
            elif inst_cfg.delivery == "env":
                # Env delivery picks ONE instance via deterministic precedence:
                # explicit-agent assignment beats catch-all (assigned_to_all).
                # See get_instance_for_agent_env_delivery for full ordering.
                chosen = mcp_store.get_instance_for_agent_env_delivery(
                    mcp_name, agent_name,
                )
                if not chosen:
                    exclusion_reasons[mcp_name] = (
                        f"No {manifest.label} instance authorized for this agent"
                    )
                    return True
                akr = manifest.hosted.api_key_relay if manifest.hosted else None
                if chosen.get("hosted_mode") == "hosted" and akr and akr.available:
                    # HOSTED api_key_relay: point the MCP at the relay (NOT the
                    # vendor) with a per-USER token (the session user, even
                    # though the system instance is assigned_to_all) — this is
                    # the seam per-user metering plugs into. The relay
                    # holds the vendor key; field_values are NOT injected.
                    from services.billing import relay_client
                    env = server_entry.setdefault("env", {})
                    if relay_client.is_available():
                        try:
                            token = relay_client.mint_session_token(user_sub or "")
                            env["OTODOCK_RELAY_BASE"] = (
                                config.OTODOCK_RELAY_BASE + akr.relay_path
                            )
                            # RELAY_BASE is a URL (non-secret) and stays in the
                            # file; the per-user relay TOKEN is secret → broker
                            # only, never written to the config file.
                            bundle_env[RELAY_TOKEN_ENV] = token
                        except relay_client.RelayNotConfigured as e:
                            env["OTODOCK_RELAY_ERROR"] = str(e)
                    else:
                        # A member can meet it in a chat: no outside address
                        # (the store apps' payment rules); the admin finds billing.
                        env["OTODOCK_RELAY_ERROR"] = (
                            "OtoDock hosted relay not available yet. "
                            "An admin can set up billing in Setup, OtoDock."
                        )
                else:
                    # Instance field_values are secret → broker bundle only,
                    # never the config file.
                    bundle_env.update(chosen["field_values"])
        return False


def _finish_session_servers(servers: dict[str, dict], secret_bundles: dict, cred_result,
                            agent_name: str, delegation_targets: list[str] | None,
                            target_browser, *, task_mode: bool, phone_mode: bool,
                            meeting_mode: bool, key_turn: bool = False,
                            only_owner: str | None = None) -> None:
    """The entries' session-wide settings: the delegation targets, the
    browser entry's origins and mode, the command-line token blanked. With
    the owner filter (``only_owner``) the machine's browser token goes only
    when the filter names the machine's owner."""
    # Inject delegation targets into delegation-mcp env
    if delegation_targets is not None and "delegation-mcp" in servers:
        servers["delegation-mcp"].setdefault("env", {})["DELEGATION_MCP_TARGETS"] = ",".join(delegation_targets)

    # Inject the per-agent allowed browser origins into the browser-control
    # entry (server_name "local"). Empty list = no limit (the manifest ships
    # no blocked origins). Only hit the DB when the browser MCP actually
    # attached to this session.
    if "local" in servers:
        from storage.agents import agent_store as _agent_store
        _apply_browser_allowed_origins(
            servers["local"], _agent_store.get_browser_allowed_origins(agent_name)
        )
        owner = getattr(target_browser, "owner_sub", "")
        _apply_browser_mode(
            servers["local"], secret_bundles, target_browser,
            unattended=bool(task_mode or phone_mode or meeting_mode or key_turn),
            withheld=only_owner is not None and not (only_owner and only_owner == owner),
        )
        # A bundle created here (token) gets the same shell-key stripping the
        # loop applies to credentialed stdio MCPs.
        if "local" in secret_bundles and "command" in servers["local"]:
            servers["local"].setdefault("env", {})["OTO_STRIP_KEYS"] = _strip_keys(
                cred_result.bash_env_keys)

    # The shell's command-line token never reaches an MCP: Claude starts a
    # stdio MCP with its own env and the entry's on top, so every stdio entry
    # names the key empty (a wrapped one also strips it, above).
    for entry in servers.values():
        if isinstance(entry, dict) and "command" in entry:
            entry.setdefault("env", {})[CLI_TOKEN_ENV] = ""
    _settle_stand_ins(servers, secret_bundles)


def _settle_stand_ins(servers: dict[str, dict], secret_bundles: dict) -> None:
    """A stand-in's marker says whether its tool's start needs its fetch to
    answer: settled once every bundle is final (the browser's own-browser
    token joins its bundle above, after the entry was made)."""
    for key, entry in servers.items():
        env = entry.get("env") if isinstance(entry, dict) else None
        raw = env.get(STANDIN_ENV) if isinstance(env, dict) else None
        bundle = secret_bundles.get(key)
        if not isinstance(env, dict) or not raw or bundle is None or not (bundle.env or bundle.token is not None):
            continue
        marker = json.loads(raw)
        if not marker.get("fetch"):
            marker["fetch"] = True
            env[STANDIN_ENV] = json.dumps(marker, separators=(",", ":"), sort_keys=True)


def session_build_dir(session_id: str) -> Path:
    """The folder a session's own tool config is written in (swept a day
    after its last build)."""
    return (config.SESSIONS_DIR / "user-mcp-configs" / _SESSION_BUILDS
            / hashlib.sha256(session_id.encode()).hexdigest()[:16])


def _write_session_mcp_config(servers: dict[str, dict], agent_name: str, user_sub: str | None,
                              session_id: str, mcp_config_format: str | None) -> Path:
    """Write the session's config file (TOML for Codex, JSON otherwise) and
    answer its path."""
    # Write config file
    sub_hash = hashlib.sha256((user_sub or "agent").encode()).hexdigest()[:12]
    config_out_dir = config.SESSIONS_DIR / "user-mcp-configs"
    if session_id:
        _sweep_session_builds(config_out_dir / _SESSION_BUILDS)
        config_out_dir = session_build_dir(session_id)
    config_out_dir.mkdir(parents=True, exist_ok=True)

    if mcp_config_format == "toml":
        # Codex CLI: generate config.toml with [mcp_servers.*] sections
        # Note: credential env injection happens separately via
        # inject_credential_env_into_toml() after sandbox path rewriting.
        config_path = config_out_dir / f"{agent_name}-{sub_hash}-mcp.toml"
        _write_whole(config_path, _servers_to_toml(servers))
        # Save servers dict as JSON for re-generation after credential injection
        _write_whole(config_path.with_suffix(".servers.json"), json.dumps(servers, indent=2))
    else:
        # Claude CLI: generate mcp-config.json with mcpServers dict. The
        # manifest's limit becomes Claude Code's own key; a Direct LLM session
        # reads neither (an app action's run reads a "json" build and ignores
        # the key). The build's entries keep the field.
        mcp_config = {"mcpServers": {
            key: claude_entry(key, entry) if mcp_config_format == "json"
            else {k: v for k, v in entry.items() if k != _LIMIT_FIELD}
            for key, entry in servers.items()}}
        config_path = config_out_dir / f"{agent_name}-{sub_hash}.json"
        _write_whole(config_path, json.dumps(mcp_config, indent=2))
    if session_id:
        # A rewrite in place leaves the directory's time alone: touched, so
        # the day-old sweep never takes a resumed session's fresh build.
        with contextlib.suppress(OSError):
            os.utime(config_out_dir)
    return config_path


def _write_whole(path: Path, text: str) -> None:
    """Write ``text`` so a reader meets the old file or the new one, never
    a partial one: the Direct manager reads a config back right after the
    session's own builder wrote the same path. A temp file of its own per
    writer, beside the target (so the rename stays on one filesystem and two
    writers of one path never share a temp name), then one rename; a write
    that fails leaves no temp file behind."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# The per-session build directories under ``user-mcp-configs/``: one per
# session that built, swept once they outlive a session token's day.
_SESSION_BUILDS = "sessions"
_SESSION_BUILD_TTL_S = 24 * 3600
_BUILD_SWEEP_EVERY_S = 3600.0
_build_sweep_at: float | None = None


def _names_cli_token(value: Any) -> bool:
    """Whether a string inside an entry's values names the shell's
    command-line token (Claude would expand ``${OTO_CLI_TOKEN}`` there).
    Keys are not values: the platform's own blank under that name is fine."""
    if isinstance(value, str):
        return CLI_TOKEN_ENV in value
    if isinstance(value, dict):
        return any(_names_cli_token(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_names_cli_token(v) for v in value)
    return False


def _strip_keys(bash_env_keys: set[str]) -> str:
    """A wrapped stdio MCP's ``OTO_STRIP_KEYS``: the shell's git helper
    entries and its command-line token, which the CLI's env carries and no
    MCP may hold."""
    return ",".join(sorted({*bash_env_keys, CLI_TOKEN_ENV}))


def _sweep_session_builds(root: Path) -> None:
    """Remove the session build directories older than a day, at most once
    an hour (the boot empties the whole tree anyway)."""
    import shutil
    import time
    global _build_sweep_at
    now = time.monotonic()
    if _build_sweep_at is not None and now - _build_sweep_at < _BUILD_SWEEP_EVERY_S:
        return
    _build_sweep_at = now
    cutoff = time.time() - _SESSION_BUILD_TTL_S
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        with contextlib.suppress(OSError):
            if entry.is_dir() and not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)


def inject_toml_credentials(
    mcp_config: Path | None, credential_env: dict[str, str] | None,
    *, mcp_format: str | None, exclude_keys: set[str] | None = None,
) -> Path | None:
    """The one credential-injection step every session builder runs after
    its credential environment is complete: a Codex (TOML) config gets the
    environment written into each stdio MCP's env table (Codex spawns MCPs
    from the file alone), the bash-only keys left out; a JSON config, no
    config or no environment comes back unchanged. Synchronous: the async
    builders call it off the loop."""
    if mcp_format != "toml" or not mcp_config or not credential_env:
        return mcp_config
    return inject_credential_env_into_toml(
        mcp_config, credential_env, exclude_keys=exclude_keys,
    )


def inject_credential_env_into_toml(
    toml_path: Path, credential_env: dict[str, str],
    *, exclude_keys: set[str] | None = None,
) -> Path:
    """Re-generate TOML MCP config with credential env vars injected.

    Called AFTER sandbox path rewriting so env vars have correct
    sandbox-internal paths.  Reads the saved servers dict (JSON sidecar),
    injects credential vars into each stdio MCP's env section, and
    regenerates the TOML file.

    ``exclude_keys`` are dropped from what lands in the TOML — used for the
    bash-only git helper entries (GIT_CONFIG_*): Codex MCPs don't need
    them, and a session's shell configuration has no place in an MCP's
    file. They stay in the Codex DAEMON env (the parent process) so bash
    ``git`` finds the session's credential helper.

    Returns the same path (modified in-place).
    """
    json_sidecar = toml_path.with_suffix(".servers.json")
    if not json_sidecar.exists() or not credential_env:
        return toml_path

    try:
        servers = json.loads(json_sidecar.read_text())
    except (OSError, ValueError):
        return toml_path

    _exclude = exclude_keys or set()
    toml_env = {k: v for k, v in credential_env.items() if k not in _exclude}

    # Inject credential env into stdio MCPs only
    for srv_entry in servers.values():
        if srv_entry.get("type") == "stdio":
            srv_entry.setdefault("env", {}).update(toml_env)

    toml_path.write_text(_servers_to_toml(servers))
    json_sidecar.unlink(missing_ok=True)
    return toml_path


# The entry's field for the manifest's per-call limit (``server.tool_timeout_sec``).
_LIMIT_FIELD = "tool_timeout_sec"
# Codex's per-call limit when a manifest names none, and the bound a stand-in
# gives its program's start (``stdio_path_interceptor._START_TIMEOUT_S``).
_CODEX_TOOL_TIMEOUT_S = 60
_STANDIN_START_S = 60
# Claude Code's own limit on a call that sends nothing (no answer, no
# progress notification): 30 minutes on a server it starts (``stdio``, or no
# type), 5 minutes on any other. Its per-server ``timeout`` (ms) replaces that
# limit and the whole call's (about 28 hours unset) alike. Read from the
# pinned CLI, read again at every bump of its pin.
_CLAUDE_STDIO_SILENCE_S = 1800
_CLAUDE_SILENCE_S = 300
# A Docker tool's entry: its first read may wait for its container's start
# (the door's 25 s), and each call gets the door's wake budget on top
# (``door.WAKE_BUDGET_S``).
_CONTAINER_START_S = 30


def _servers_to_toml(servers: dict[str, dict]) -> str:
    """Convert mcpServers dict to Codex-compatible TOML string.

    Generates [mcp_servers.<name>] sections.  Handles command, args, env,
    and url fields.  Uses manual string building to avoid a tomli_w dependency.
    """
    lines: list[str] = []
    for name, entry in servers.items():
        lines.append(f"[mcp_servers.{name}]")
        # Lean-start: bound a single MCP's startup so one slow/hung
        # server can't drag the OtoDock warm-gate to its 15s cap. Confirmed a
        # recognized `McpServerConfig` field on the installed codex-cli 0.136
        # (the field literal is present in the native binary alongside
        # `startup_timeout_ms`/`tool_timeout_sec`), so it can't be rejected as an
        # unknown key. 10s sits below the warm-gate cap and well above the
        # slowest measured warm MCP (~3s); MCPs are pre-warmed before the session
        # A valid server isn't dropped. This 10s is the LOCAL value;
        # `_rewrite_mcp_toml_for_remote` raises it to a remote floor (with
        # per-MCP overrides) because remote cold starts pay the tunnel broker
        # fetch + on-satellite import.
        # A Docker tool's entry (the gateway's, ``gateway_entry``): its first
        # read may wait for its container's start, and so may each call.
        container = _is_container_entry(name, entry)
        lines.append(f"startup_timeout_sec = {_CONTAINER_START_S if container else 10}")
        # The call's bound (``call_limit_s``; Codex's own default is 60 s). In
        # the server's own table, ahead of any ``http_headers`` sub-table a
        # key after would belong to.
        tool_timeout = call_limit_s(name, entry, _CODEX_TOOL_TIMEOUT_S)
        if tool_timeout:
            lines.append(f"tool_timeout_sec = {tool_timeout}")
        srv_type = entry.get("type", "stdio")

        if srv_type == "stdio":
            lines.append(f'command = "{_toml_escape(entry.get("command", ""))}"')
            args = entry.get("args", [])
            if args:
                args_str = ", ".join(f'"{_toml_escape(a)}"' for a in args)
                lines.append(f"args = [{args_str}]")
            env = entry.get("env", {})
            if env:
                env_pairs = ", ".join(
                    f'"{_toml_escape(k)}" = "{_toml_escape(v)}"'
                    for k, v in env.items()
                )
                lines.append(f"env = {{ {env_pairs} }}")
        elif srv_type in ("sse", "streamable-http"):
            url = entry.get("url", "")
            lines.append(f'url = "{_toml_escape(url)}"')
            # CRITICAL: emit headers for bearer-injecting remote HTTP MCPs
            # (github-mcp, Slack, Linear, Notion, Zoom). Codex's MCP server
            # config field for custom headers is ``http_headers`` (an inline
            # table) — NOT ``headers``. An unknown ``[mcp_servers.X.headers]``
            # sub-table is silently ignored by Codex (verified on 0.120.0:
            # ``codex mcp get`` reports Auth "Unsupported"), so the request
            # goes out with no Authorization header and the vendor/sidecar
            # returns 401. Must be ``http_headers`` for the header to be sent.
            headers = entry.get("headers", {}) or {}
            if headers:
                lines.append(f"[mcp_servers.{name}.http_headers]")
                for hk, hv in headers.items():
                    lines.append(f'"{_toml_escape(hk)}" = "{_toml_escape(hv)}"')

        lines.append("")  # blank line between sections

    return "\n".join(lines)


def _call_start_s(name: str, entry: dict) -> int:
    """The seconds a call of this entry may spend starting its tool before
    the tool's own work: a stand-in's program, a sleeping Docker tool's wake
    (``door.WAKE_BUDGET_S``, read at the call)."""
    start = _STANDIN_START_S if STANDIN_ENV in (entry.get("env") or {}) else 0
    if _is_container_entry(name, entry):
        from services.containers.door import WAKE_BUDGET_S
        start += int(WAKE_BUDGET_S)
    return start


def call_limit_s(name: str, entry: dict, engine_default_s: int) -> int:
    """The seconds one call of this entry may take: the manifest's limit
    (``server.tool_timeout_sec``, the engine's default when it names none)
    plus the start the call carries. 0 when it names none and nothing
    starts: the engine keeps its own. The one bound every writer turns into
    its engine's key."""
    declared = int(entry.get(_LIMIT_FIELD) or 0)
    start = _call_start_s(name, entry)
    if not declared and not start:
        return 0
    return (declared or engine_default_s) + start


def claude_entry(name: str, entry: dict) -> dict:
    """The entry as Claude Code's config carries it: without the manifest's
    field, with a per-server ``timeout`` (ms) when the tool names a limit
    longer than the engine's own on a silent call over its transport. That
    key bounds the whole call too, as Codex's does, so a value at or under
    the engine's own is never written: it would cut a stdio tool's long
    call and a call that reports progress sooner than the engine does."""
    out = {k: v for k, v in entry.items() if k != _LIMIT_FIELD}
    if not entry.get(_LIMIT_FIELD):
        return out
    own = _CLAUDE_STDIO_SILENCE_S if entry.get("type", "stdio") == "stdio" else _CLAUDE_SILENCE_S
    bound = call_limit_s(name, entry, own)
    if bound > own:
        out["timeout"] = bound * 1000
    return out


def _is_container_entry(name: str, entry: dict) -> bool:
    """A Docker tool's gateway entry: its URL names the gateway route for
    ``name`` and ``name`` is a container's."""
    from core.credentials.mcp_gateway import gateway_key_of
    from services.mcp import mcp_manifest_types as _mt
    from services.mcp.mcp_manifest_store import get_manifest_by_config_key
    if gateway_key_of(str(entry.get("url") or "")) != name:
        return False
    manifest = get_manifest_by_config_key(name)
    return manifest is not None and _mt.is_container(manifest.server)


def _toml_escape(s: str) -> str:
    """Escape a string for TOML double-quoted values."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
