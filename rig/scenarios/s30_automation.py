"""A task run, a scheduled task, a trigger fired, a delegation, an app."""
from __future__ import annotations

import json
import time

from lib.chat import script
from lib.key import Key, mint
from lib.person import Http
from lib.scenario import check, scenario
from run import ENGINES

CLAUDE, CLAUDE_MODEL = ENGINES["claude"]
SECOND_AGENT = "rig-second"


def wait_run_ended(person, run_id: str, *, timeout: float = 240.0) -> dict:
    def ended():
        run = person.ok("GET", f"/v1/tasks/runs/{run_id}")
        return run if run.get("status") in ("completed", "failed", "timeout", "cancelled", "error") else None
    return person.wait_until(f"run {run_id} to end", ended, timeout=timeout)


@scenario("task", needs=("turn-claude",))
def task(ctx):
    """A run-when-asked task runs now, and its chat holds its rows."""
    owner = ctx.owner
    made = owner.ok("POST", "/v1/tasks/one-time", {
        "name": "Rig task", "agent": ctx.agent, "prompt": "Say hello.", "notification_mode": "none",
        "task_type": "trigger", "layer": CLAUDE, "model": CLAUDE_MODEL})
    started = owner.ok("POST", f"/v1/tasks/{made['task_id']}/run")
    run = wait_run_ended(owner, started["run_id"])
    check(run["status"] == "completed", f"the run ended {run['status']}: {str(run)[:300]}")
    chat = owner.ok("GET", f"/v1/tasks/{made['task_id']}/session") if not run.get("chat_id") else {"chat_id": run["chat_id"]}
    rows = owner.rows(chat["chat_id"])
    check(any(r.get("role") == "assistant" for r in rows), f"the run's chat has an answer: {[r.get('role') for r in rows]}")
    ctx.state["task_id"] = made["task_id"]


@scenario("task-scheduled", needs=("task",))
def task_scheduled(ctx):
    """A scheduled task fires on its own."""
    owner = ctx.owner
    made = owner.ok("POST", "/v1/tasks/scheduled", {
        "name": "Rig schedule", "agent": ctx.agent, "prompt": "Say hello.", "interval_seconds": 60,
        "notification_mode": "none", "layer": CLAUDE, "model": CLAUDE_MODEL})
    task_id = made.get("task_id") or made.get("id")

    def fired():
        runs = owner.ok("GET", f"/v1/tasks/runs?task_id={task_id}")
        rows = runs.get("runs", runs) if isinstance(runs, dict) else runs
        return rows[0] if rows else None
    run = owner.wait_until("the schedule's first run", fired, timeout=150, every=5)
    finished = wait_run_ended(owner, run.get("run_id") or run["id"])
    owner.ok("DELETE", f"/v1/tasks/{task_id}")
    check(finished["status"] == "completed", f"the scheduled run ended {finished['status']}")


@scenario("trigger", needs=("wizard",))
def trigger(ctx):
    """A trigger made by route fires by key and tells the person."""
    owner = ctx.owner
    slug = f"rig-trigger-{int(time.time())}"
    made = owner.ok("POST", "/v1/triggers", {
        "name": "Rig trigger", "agent": ctx.agent, "scope": "user", "slug": slug,
        "notify": {"enabled": True, "severity": "info", "title": f"Rig trigger fired {slug}", "body": "From {{source}}."}})
    path = made.get("webhook_path") or f"/v1/webhooks/user/{owner.username}/{slug}"
    key = mint(owner, name=slug, scopes=["triggers"], run_without_asking=False)
    status, _ = Http(ctx.base).call("POST", path, {"source": "nobody"})
    check(status in (401, 403), f"a fire with no key answered {status}")
    fired = Key(ctx.base, key).ok("POST", path, {"source": "the rig"})
    check(fired, "the fire answered")

    def told():
        rows = owner.ok("GET", "/v1/notifications")["deliveries"]
        return [n for n in rows if n.get("title") == f"Rig trigger fired {slug}"]
    owner.wait_until("the trigger's notification", told, timeout=30)


@scenario("delegation", needs=("turn-claude",))
def delegation(ctx):
    """A second agent wired; the model delegates; the result returns to the chat."""
    owner = ctx.owner
    second = SECOND_AGENT
    status, _ = owner.call("GET", f"/v1/agents/{second}/info")
    if status != 200:
        owner.ok("POST", "/v1/agents", {"display_name": "Rig Second", "slug": second, "execution_path": CLAUDE,
                                        "default_model": CLAUDE_MODEL})
    owner.ok("PUT", f"/v1/agents/{ctx.agent}/delegation-targets", {"targets": [second]})
    prompt = "Delegate this. " + script(
        {"tool": "delegate", "input": {"agent": second, "name": "Rig worker", "surface": "task", "prompt": "Say hello."}},
        {"text": "Delegated."})
    chat = owner.chat(ctx.agent, engine=CLAUDE, model=CLAUDE_MODEL, text=prompt)
    try:
        chat.wait_done()
        rows = chat.wait_rows("the worker's result in the chat",
                              lambda r: any(x.get("event_type") == "delegate_result" for x in r), timeout=180)
    finally:
        chat.close()
    kinds = [r.get("event_type") for r in rows]
    check("delegate_spawn" in kinds, f"the turn started the worker: {kinds}")
    result = next(r for r in rows if r.get("event_type") == "delegate_result")
    data = json.loads(result["event_data"]) if isinstance(result.get("event_data"), str) else result["event_data"]
    check(data.get("status") == "completed", f"the worker ended {data.get('status')}")
    worker = owner.rows(data["worker_chat_id"])
    check(any(r.get("role") == "assistant" for r in worker), f"the worker's own chat has its answer: {[r.get('role') for r in worker]}")


APP_MANIFEST = {"title": "Rig App", "description": "An app of the rig."}


def _tool_rows(rows: list[dict], name: str) -> list[dict]:
    """A tool's own rows by the end of its name: what it answered, not what the model said."""
    return [json.loads(r["event_data"]) if isinstance(r.get("event_data"), str) else (r.get("event_data") or {})
            for r in rows if r.get("event_type") == "tool" and name in str(r.get("event_data"))]


@scenario("app", needs=("turn-claude",))
def app(ctx):
    """The model deploys a folder app; it is approved by route, and its page opens."""
    owner = ctx.owner
    folder = f"/users/{owner.username}/workspace/apps/rig-app"
    files = (
        f"mkdir -p {folder}/client && "
        f"printf '%s' '{json.dumps(APP_MANIFEST)}' > {folder}/app.json && "
        f"printf '<!doctype html><title>Rig</title><p>The rig app.</p>' > {folder}/client/index.html"
    )
    prompt = "Deploy the app. " + script(
        {"tool": "shell", "input": {"command": files}},
        {"tool": "deploy_app", "input": {"slug": "rig-app", "visibility": "user"}},
        {"text": "Deployed."})
    chat = owner.chat(ctx.agent, engine=CLAUDE, model=CLAUDE_MODEL, text=prompt)
    try:
        chat.wait_done(timeout=240)
        rows = chat.rows()
    finally:
        chat.close()
    deployed = _tool_rows(rows, "deploy_app")
    from scenarios.s71_compute_apps import deploy_said, deploy_took
    check(deployed and not deployed[-1].get("is_error") and deploy_took(deployed[-1]), deploy_said(deployed))

    def listed():
        apps = owner.ok("GET", f"/v1/apps?agent={ctx.agent}")
        found = [a for a in (apps.get("apps", apps) if isinstance(apps, dict) else apps) if a.get("slug") == "rig-app"]
        return found[0] if found else None
    item = owner.wait_until("the app in the list", listed, timeout=60)
    state = owner.ok("GET", f"/v1/apps/{item['id']}/deploy/status")
    if state.get("pending_release"):
        owner.ok("POST", f"/v1/apps/{item['id']}/deploy/approve", {"release": state["pending_release"]})
        state = owner.ok("GET", f"/v1/apps/{item['id']}/deploy/status")
    check(state.get("release") and not state.get("pending_release"), f"the release is live: {str(state)[:300]}")
    status, page, _ = owner.raw("GET", f"/v1/apps/{item['id']}/html")
    check(status == 200 and b"The rig app." in page, f"the app's page answered {status}: {page[:200]!r}")
    ctx.state["app_id"] = item["id"]
