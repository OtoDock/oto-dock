"""App steps (APPS.md "Steps"): a handler whose name is in the manifest's
``steps`` block runs a script where the agent's sessions run — the local
sandbox with the app identity's mount row, or the machine
``resolve_execution_target`` names — instead of the server route. No model,
no MCP, never ``/config``; the approval is the authority (the script's
sha256 is in the signed manifest and is checked against the live release at
every fire); the verdict is the exit code, kept on the delivery with the
first 32 KB of output; a finished step wakes the server handlers subscribed
to ``step:<name>`` through the ordinary event path.

Where things run mirrors sessions: a shared app's step is an agent-scoped
run (the approver's provenance decides knowledge RW and, on manager
provenance only, the provider tokens the app ``requires``); a personal
app's step runs as its owner. The drain in ``app_handlers`` claims the row
and hands it here (``fire``); every verdict goes back through the same
store, so ``deploy_status`` lists steps like any wake. The run itself is
``services/scripts/runner`` (CHECKS.md's script kind shares it).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from api.apps import manifest as _mf
from services.apps import releases
from services.scripts import runner as _runner
from services.scripts.runner import ScriptRefused, ScriptSpec, ScriptUnavailable
from storage import db_app_deliveries as deliveries
from storage.automation import run_status
from core import placement
from storage.pg import run_db
from auth import roles
from core import layout

logger = logging.getLogger("claude-proxy.apps")

# The lease and the claim outlive the script by a margin; the sweep never
# reclaims a running step and the script's last call still verifies.
LEASE_MARGIN_S = 120
CLAIM_MARGIN_S = 60
# An offline machine re-arms the row uncounted, like "the app never came
# up" does for a server; the day-long age cap still ends it.
OFFLINE_REARM_S = 300
OUTPUT_KEEP_BYTES = deliveries.STEP_OUTPUT_KEEP_BYTES
STEPS_DIR_NAME = "steps"
# The task type a step's OTO_* env names (no session, no model).
TASK_TYPE = "step"

argv_for = _runner.argv_for
scrub = _runner.scrub
_output_text = _runner.output_text
REDACTED = _runner.REDACTED


class StepRefused(Exception):
    """The step must not run: the delivery dies with the reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StepUnavailable(Exception):
    """The place the step runs is not there right now: re-arm, uncounted."""

    def __init__(self, reason: str, retry_after: float = OFFLINE_REARM_S) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


@dataclass
class StepResult:
    exit_code: int | None
    output: str
    timed_out: bool
    ran_on: str
    seconds: float


@dataclass
class StepPlan:
    """Everything a run needs, resolved off the loop before anything starts."""
    row: dict
    delivery: dict
    name: str
    run: str
    timeout: int
    sha256: str
    script: bytes
    identity: object          # core.config.task_config_builder.TaskIdentity
    vis: object               # core.session.visibility.VisibilityResolution
    target: str               # placement.LOCAL or a machine id
    credential_env: dict
    workspace_relative: str   # "workspace" or "users/<u>/workspace"
    knowledge_relative: str   # "knowledge" or "" (Personal-only: no shared trees)


# ── identity, placement, credentials ────────────────────────────────────────


def approver_grants(row: dict) -> bool:
    """Manager provenance: the approver is a platform admin or a per-agent
    manager of the app's agent (the rule knowledge RW already has)."""
    from core.config.task_config_builder import _creator_grants_knowledge_rw
    return _creator_grants_knowledge_rw(row.get("agent") or "", row.get("approved_by") or None)


def identity_for(row: dict):
    """``(TaskIdentity, VisibilityResolution)``: a shared app runs
    agent-scoped with the approver's provenance (knowledge RW when a manager
    approved), a personal app as its owner with the owner's role and the
    agent's mode; ``config_visible`` is never true here."""
    from core.config.task_config_builder import resolve_task_identity
    from core.session.visibility import resolve_visibility
    agent = row.get("agent") or ""
    if row.get("username"):
        identity = resolve_task_identity(agent, "user", row.get("owner_sub") or None)
    else:
        identity = resolve_task_identity(agent, "agent", row.get("approved_by") or None,
                                         allow_knowledge_rw=True)
    vis = resolve_visibility(
        agent, username=identity.username, user_role=identity.role,
        user_sub=identity.creds_user_sub or "", scope_override=identity.scope,
    )
    return identity, vis


def placement_for(row: dict, identity) -> str:
    """Where the step runs: ``resolve_execution_target`` with the identity
    the step runs as — a user-scoped run may go to the owner's machine, an
    agent-scoped one to the admin machine or the sandbox, never to a user's
    machine. An offline target raises ``StepUnavailable``."""
    from storage.remote_store import resolve_execution_target
    target, reason = resolve_execution_target(
        row.get("agent") or "", identity.creds_user_sub or None, identity.role)
    if placement.is_offline_sentinel(target):
        raise StepUnavailable("the machine is offline")
    if reason:
        logger.info("App step on %s: placement %s (%s)", row.get("slug"), target[:8], reason)
    return target


def credential_env_for(row: dict, identity) -> dict[str, str]:
    """The OAuth environment of the providers the app ``requires``, for the
    identity the step runs as (the agent's service account for a shared
    app, the owner's for a personal one) — only the ``env_injection``
    names (``GH_TOKEN``, ``GIT_CONFIG_*``), only when the token may leave
    the platform: a personal app's owner's own token, or a shared app's
    service account on manager provenance (Q2 of the plan)."""
    providers = [p for p in (_mf.parse_requires(row).get("providers") or []) if isinstance(p, str)]
    if not providers:
        return {}
    if not row.get("username") and not approver_grants(row):
        return {}
    from services.mcp import mcp_registry
    from services.oauth import credential_resolver
    agent = row.get("agent") or ""
    creds = credential_resolver.resolve_credentials(
        agent, identity.creds_user_sub or None, task_scope=identity.scope)
    out: dict[str, str] = {}
    for m in (mcp_registry.get_agent_mcps(agent, placement=placement.LOCAL_PLACEMENT) or []):
        manifest = getattr(m, "manifest", None) or m
        decl = getattr(manifest, "credentials", None)
        oauth = (getattr(decl, "oauth", None) or {}) if decl else {}
        if not oauth or (oauth.get("provider_id") or "") not in providers:
            continue
        for k, v in (creds.env_by_mcp.get(m.name) or {}).items():
            if k in creds.bash_env_keys and isinstance(v, str):
                out[k] = v
    return out


def read_script(row: dict, step: dict) -> bytes:
    """The script from the LIVE release, verified twice: the release's own
    per-file hash and the sha256 the approval signed."""
    signed = str(step.get("sha256") or "")
    if not signed:
        raise StepRefused("the step was never hashed — deploy the app again")
    try:
        live = releases.live_release_dir(row)
    except releases.ReleaseDamaged:
        raise StepRefused("the live release is damaged")
    if live is None:
        raise StepRefused("the app has no live release")
    try:
        data = releases.read_release_file(live, step["run"])
    except releases.ReleaseDamaged:
        raise StepRefused("the live release is damaged")
    if data is None:
        raise StepRefused(f"{step['run']} is not in the release")
    if hashlib.sha256(data).hexdigest() != signed:
        raise StepRefused("the script changed since the approval — approve the app again")
    if len(data) > 256 * 1024:
        raise StepRefused("the script is larger than 256 KB")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise StepRefused("the script is not UTF-8 text")
    return data


def plan_for(row: dict, d: dict) -> StepPlan:
    """Synchronous: identity, placement, credentials and the script."""
    step = _mf.parse_steps(row).get(d["handler"]) or {}
    if not step:
        raise StepRefused("the handler is not a step")
    script = read_script(row, step)
    identity, vis = identity_for(row)
    target = placement_for(row, identity)
    creds = credential_env_for(row, identity)
    username = getattr(vis, "mount_username", "") or ""
    return StepPlan(
        row=row, delivery=d, name=d["handler"], run=str(step["run"]),
        timeout=int(step.get("timeout") or 60), sha256=str(step["sha256"]), script=script,
        identity=identity, vis=vis, target=target, credential_env=creds,
        workspace_relative=layout.scope_workspace(username),
        knowledge_relative=layout.KNOWLEDGE if getattr(vis, "mount_shared", True) else "",
    )


# ── the claim and the env ───────────────────────────────────────────────────


def step_claim(row: dict, d: dict, timeout: int) -> str:
    """The caller claim the script presents (``OTODOCK_STEP_TOKEN``):
    principal ``platform``, ``kind: step``, this delivery, alive for the
    step's timeout plus a margin — the routes accept it as the bearer
    while the delivery is in flight and refuse it afterwards."""
    from services.apps import app_tokens
    return app_tokens.mint(row["id"], app_tokens.PURPOSE_CALLER, {
        "principal": "platform", "kind": "step", "sub": "", "username": "", "role": "app",
        "handler": d["handler"], "delivery": d["id"], "event": d["event"],
        "trigger_id": d.get("trigger_id") or "", "external": False,
    }, int(timeout) + CLAIM_MARGIN_S)


def step_env(plan: StepPlan, claim: str) -> dict[str, str]:
    """What every placement gives the script (APPS.md lists these for
    authors); the placement adds the four paths it alone knows
    (``OTODOCK_PROXY_URL``, ``OTODOCK_STEP_PAYLOAD``, ``OTODOCK_WORKSPACE_DIR``,
    ``OTODOCK_KNOWLEDGE_DIR``). The token is env-only: never in argv, never
    in a log."""
    row, d = plan.row, plan.delivery
    env = {
        "LANG": "C.UTF-8",
        "OTODOCK_APP_ID": row["id"],
        "OTODOCK_APP_SLUG": row["slug"],
        "OTODOCK_DELIVERY_ID": d["id"],
        "OTODOCK_STEP_HANDLER": plan.name,
        "OTODOCK_STEP_EVENT": d["event"],
        "OTODOCK_STEP_TOKEN": claim,
        "OTODOCK_STEP_TIMEOUT": str(plan.timeout),
    }
    env.update(plan.credential_env)
    return env


def secrets_of(plan: StepPlan, claim: str) -> list[str]:
    return [claim, *[v for v in plan.credential_env.values() if isinstance(v, str)]]


# ── the run (services/scripts/runner with the app identity) ─────────────────


def _local_step_dir(row: dict, delivery_id: str) -> Path:
    return releases.app_release_dir(row) / STEPS_DIR_NAME / delivery_id


def _spec_for(plan: StepPlan, claim: str) -> ScriptSpec:
    """The runner's spec: the identity's mount row, the live release
    read-only at ``/app``, the step's scratch directory at ``/step``."""
    from storage.agents import agent_store
    row, d, identity, vis = plan.row, plan.delivery, plan.identity, plan.vis
    env = step_env(plan, claim)
    env["OTO_TASK_TYPE"] = TASK_TYPE
    live = None
    if placement.is_local(plan.target):
        live = releases.live_release_dir(row)
        if live is None:
            raise StepRefused("the app has no live release")
    return ScriptSpec(
        run_id=d["id"], agent=row["agent"], script=plan.script, sha256=plan.sha256,
        run_name=plan.run,
        role=getattr(identity, "role", roles.MANAGER),
        username=getattr(vis, "mount_username", "") or "",
        user_sub=getattr(identity, "creds_user_sub", "") or "",
        is_admin_agent=agent_store.is_admin_only(row["agent"]),
        mount_shared=bool(getattr(vis, "mount_shared", True)),
        knowledge_rw=bool(getattr(identity, "knowledge_rw", False)),
        workspace_relative=plan.workspace_relative, knowledge_relative=plan.knowledge_relative,
        script_dir=live, mount_at="/app", env=env,
        payload_json=_runner.payload_text(d.get("payload") or {}),
        timeout=int(plan.timeout), secrets=secrets_of(plan, claim),
        scratch_root=releases.app_release_dir(row) / STEPS_DIR_NAME,
    )


def _build_local(plan: StepPlan, claim: str) -> tuple[list[str], dict[str, str], Path]:
    """The argv, the env and the scratch directory of a local run (the
    tests read it; the runner is what runs it)."""
    return _runner.build_local(_spec_for(plan, claim))


def _convert(res: _runner.ScriptResult) -> StepResult:
    return StepResult(exit_code=res.exit_code, output=res.output, timed_out=res.timed_out,
                      ran_on=res.ran_on, seconds=res.seconds)


async def run_local(plan: StepPlan, claim: str) -> StepResult:
    try:
        spec = await asyncio.to_thread(_spec_for, plan, claim)
        return _convert(await _runner.run_local(spec))
    except ScriptRefused as e:
        raise StepRefused(e.reason)
    except ScriptUnavailable as e:
        raise StepUnavailable(e.reason)


async def run_remote(plan: StepPlan, claim: str, machine_id: str) -> StepResult:
    """One ``step_run`` frame to the machine; the ack carries the verdict
    (satellite 0.5.122). What the script prints streams into the app log."""
    from services.apps import app_supervisor
    row = plan.row

    async def _on_output(text: str) -> None:
        await app_supervisor.append_log(row, text if text.endswith("\n") else text + "\n")

    try:
        spec = await asyncio.to_thread(_spec_for, plan, claim)
        spec.on_output = _on_output
        return _convert(await _runner.run_remote(spec, machine_id))
    except ScriptRefused as e:
        raise StepRefused(e.reason.replace("too old for scripts", "too old for steps"))
    except ScriptUnavailable as e:
        raise StepUnavailable(e.reason)


# ── the fire ────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _record(d: dict, status: str, result: StepResult | None, reason: str) -> None:
    """The verdict on the delivery, the run row and the trigger."""
    from services.apps.app_handlers import RUN_STATUS_OF, _run_update
    from storage.automation import trigger_store
    exit_code = result.exit_code if result else None
    output = result.output if result else ""
    await run_db(deliveries.finish_step, d["id"], status, exit_code=exit_code, output=output,
                 error=reason)
    if status == deliveries.DONE:
        await _run_update(d, status=RUN_STATUS_OF[deliveries.DONE], output_text=output[:32 * 1024],
                          completed_at=_now_iso())
    else:
        await _run_update(d, status=RUN_STATUS_OF[deliveries.DEAD], error_message=reason[:2000],
                          completed_at=_now_iso())
        if d.get("trigger_id"):
            await run_db(trigger_store.set_last_error, d["trigger_id"], f"{d['handler']}: {reason}")


async def _wake_subscribers(row: dict, d: dict, status: str, result: StepResult | None,
                            reason: str) -> None:
    """``step:<name>`` to every server handler that asked for it, through
    the ordinary delivery path (APPS.md "Steps")."""
    from services.apps import app_handlers
    event = f"step:{d['handler']}"
    subscribed = [h for h, evs in (_mf.parse_handlers(row).get("on_event") or {}).items()
                  if event in (evs or [])]
    if not subscribed:
        return
    payload = {
        "step": d["handler"], "delivery_id": d["id"], "status": status,
        "exit_code": result.exit_code if result else None,
        "output": (result.output if result else "")[:OUTPUT_KEEP_BYTES],
        "ran_on": result.ran_on if result else "", "error": reason,
        "started_at": d.get("started_at") or "", "finished_at": _now_iso(),
        "event": d["event"],
    }
    for handler in subscribed:
        await app_handlers.enqueue(row, handler, event, payload, event_id=f"{d['id']}:{handler}")


async def fire(d: dict, row: dict) -> None:
    """The drain's fire for a step handler (the pre-checks passed): plan,
    extend the lease, run where the identity runs, record the verdict,
    wake the subscribers."""
    from services.apps import app_supervisor
    from services.apps.app_handlers import _dead, _rearm, _run_update
    try:
        plan = await asyncio.to_thread(plan_for, row, d)
    except StepRefused as e:
        await _dead(d, e.reason)
        return
    except StepUnavailable as e:
        await _rearm(d, e.reason, e.retry_after)
        return
    claim = step_claim(row, d, plan.timeout)
    await run_db(deliveries.extend_lease, d["id"], plan.timeout + LEASE_MARGIN_S)
    await run_db(deliveries.mark_started, d["id"], plan.target)
    d["started_at"] = _now_iso()
    await _run_update(d, status=run_status.RUNNING, started_at=d["started_at"])
    try:
        if placement.is_local(plan.target):
            result = await run_local(plan, claim)
        else:
            result = await run_remote(plan, claim, plan.target)
        result.output = scrub(result.output, secrets_of(plan, claim))
    except StepRefused as e:
        await _record(d, deliveries.DEAD, None, e.reason)
        await _wake_subscribers(row, d, deliveries.DEAD, None, e.reason)
        return
    except StepUnavailable as e:
        await _rearm(d, e.reason, e.retry_after)
        return
    if result.timed_out:
        status, reason = deliveries.DEAD, f"timed out after {plan.timeout} s"
    elif result.exit_code == 0:
        status, reason = deliveries.DONE, ""
    else:
        last = result.output.strip().splitlines()
        tail = " ".join(last[-3:])[-400:] if last else ""
        status = deliveries.DEAD
        reason = f"exit {result.exit_code}" + (f": {tail}" if tail else "")
    await _record(d, status, result, reason)
    where = "the sandbox" if placement.is_local(result.ran_on) else f"machine {result.ran_on[:8]}"
    try:
        await app_supervisor.append_log(
            row, f"[{_now_iso()}] step {d['handler']}: {status} ({reason or 'exit 0'}) "
                 f"after {result.seconds:.1f}s on {where}\n")
    except Exception:
        logger.debug("App step %s: log trailer failed", d["id"][:8], exc_info=True)
    logger.info("App step %s (%s/%s) %s on %s in %.1fs", d["id"][:8], row.get("slug"),
                d["handler"], status, result.ran_on[:8], result.seconds)
    await _wake_subscribers(row, d, status, result, reason)
