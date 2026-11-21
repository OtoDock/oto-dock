"""The upgrade run: the previous release by the README with data in it, then
``upgrade.sh`` to the candidate, then turns on the old chats. A release condition."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

from lib.chat import assistant_text, script
from lib.key import Key
from lib.person import Http
from lib.scenario import CheckFailed, Inconclusive, check, scenario
from run import ENGINES
from scenarios import (
    s10_install,
    s20_turns,
    s30_automation,
    s50_machine,
    s55_continuity,
    s70_compute,
)

# The disk cap makes a first boot slower than the script's default wait.
HEALTH_WAIT_S = 900
# An agent's choice of one of the owner's accounts of a tool, as the previous release
# keeps it. A fresh install has no tool that takes a person's own account, so the row
# is written in the database, in the previous release's columns.
ACCOUNT_TOOL, ACCOUNT_LABEL = "rig-mail", "rig-work"
# What upgrade.sh says from a release that cannot wait, what it says when it
# waits, and the line of the swap.
CANNOT_WAIT, WAITS, SWAP = "cannot wait for open work", "waiting for open work", "docker compose up -d --remove-orphans)"


def _sql(ctx, query: str) -> str:
    ctx.require_rig()
    return ctx.sql(query)


def _rows_before(ctx) -> dict:
    return {name: len(ctx.owner.rows(chat)) for name, chat in ctx.state["chats"].items()}


@scenario("old-install", layer="upgrade", needs_rig=True)
def old_install(ctx):
    """The previous release, installed by the README, answers."""
    status, body = Http(ctx.base).call("GET", "/health", timeout=10)
    check(status == 200 and body.get("status") == "ok", f"/health answered {status}")
    ctx.state["old_build"] = body.get("build")
    return f"build {body.get('build')}"


@scenario("old-wizard", layer="upgrade", needs=("old-install",), needs_rig=True)
def old_wizard(ctx):
    """The wizard on the previous release."""
    s10_install.wizard(ctx)


@scenario("old-people", layer="upgrade", needs=("old-wizard",), needs_rig=True)
def old_people(ctx):
    """A creator and a member on the previous release."""
    s10_install.sign_in(ctx)


@scenario("old-account", layer="upgrade", needs=("old-people",), needs_rig=True)
def old_account(ctx):
    """An agent bound to one of the owner's accounts of a tool on the previous release."""
    _sql(ctx, "INSERT INTO agent_account_bindings (user_sub, mcp_name, agent_name, account_label, set_at) "
              f"SELECT sub, '{ACCOUNT_TOOL}', '{ctx.agent}', '{ACCOUNT_LABEL}', now()::text FROM users "
              f"WHERE email = '{ctx.email('owner')}'")
    rows = _sql(ctx, f"SELECT count(*) FROM agent_account_bindings WHERE mcp_name = '{ACCOUNT_TOOL}'")
    check(rows == "1", f"the binding's rows: {rows}")


@scenario("old-engines", layer="upgrade", needs=("old-wizard",), needs_rig=True)
def old_engines(ctx):
    """The scripted engines on the previous release."""
    s10_install.engines(ctx)


@scenario("old-chats", layer="upgrade", needs=("old-engines",), needs_rig=True)
def old_chats(ctx):
    """Chats on each engine, headless, and a Codex terminal."""
    for word in ENGINES:
        s20_turns._headless_turn(ctx, word)
    s20_turns._terminal_turn(ctx, "codex")


@scenario("old-tasks", layer="upgrade", needs=("old-engines",), needs_rig=True)
def old_tasks(ctx):
    """A run-when-asked task that has run, and a schedule waiting."""
    s30_automation.task(ctx)
    engine, model = ENGINES["claude"]
    made = ctx.owner.ok("POST", "/v1/tasks/scheduled", {
        "name": "Rig schedule", "agent": ctx.agent, "prompt": "Say hello.", "schedule": "0 3 1 1 *",
        "notification_mode": "none", "layer": engine, "model": model})
    ctx.state["schedule_id"] = made.get("task_id") or made.get("id")


@scenario("old-app", layer="upgrade", needs=("old-engines",), needs_rig=True)
def old_app(ctx):
    """An app deployed on the previous release."""
    s30_automation.app(ctx)


@scenario("old-machine", layer="upgrade", needs=("old-engines",), needs_rig=True)
def old_machine(ctx):
    """A paired machine on the previous release, with the pairing rewrite it needs, and a turn."""
    detail = s50_machine.pair_linux(ctx, rewrite_for_plain_http=True)
    s20_turns._headless_turn(ctx, "claude", target=ctx.state["machine_id"])
    return detail


def _definition(ctx, service: str) -> tuple[str, str]:
    """A service's running container and the compose definition it was made from
    (its config hash label): the second says whether an update recreated it."""
    container = ctx.compose("ps", "-q", service, timeout=60).stdout.strip().splitlines()
    check(container, f"no {service} container runs")
    label = ctx.docker("inspect", "-f", '{{index .Config.Labels "com.docker.compose.config-hash"}}', container[0])
    check(label.returncode == 0, f"{service}'s container was not read: {label.stderr[-200:]}")
    return container[0], label.stdout.strip()


@scenario("old-collabora", layer="upgrade", needs=("old-install",), needs_rig=True)
def old_collabora(ctx):
    """Collabora's container on the previous release, kept for the comparison after the update."""
    ctx.state["collabora_before"] = list(_definition(ctx, "collabora"))
    return f"definition {ctx.state['collabora_before'][1][:12]}"


# Files of the previous release in the two folders the working-files window ages, each
# modified two years back. Their change time is the plant's, which keeps them by
# itself: the grace the sweep reports is the claim's proof, the files its witnesses.
OLD_FILES = ("workspace/uploads/photos/rig-old-photo.jpg", "workspace/uploads/files/rig-old-file.txt")
TWO_YEARS_S = 2 * 365 * 86400


def _old_file_paths(ctx) -> list[str]:
    return [f"/opt/otodock/agents/{ctx.agent}/users/{ctx.owner.username}/{rel}" for rel in OLD_FILES]


@scenario("old-working-files", layer="upgrade", needs=("old-people",), needs_rig=True)
def old_working_files(ctx):
    """A photo and a file upload on the previous release, each modified two years ago."""
    when = int(time.time()) - TWO_YEARS_S
    for path in _old_file_paths(ctx):
        done = ctx.proxy_exec(f"mkdir -p {shlex.quote(str(Path(path).parent))} && printf old > {shlex.quote(path)} "
                              f"&& touch -d @{when} {shlex.quote(path)}", timeout=60)
        check(done.returncode == 0, f"{path} was not made: {done.stderr[-200:]}")


# Two chats stored with the mode words of a release before 1.9, and the word each is
# now: the 2.0 migration rewrites them (the column's default is Ask).
OLD_MODES = {"rig-mode-default": ("default", "ask"), "rig-mode-accept-edits": ("acceptEdits", "autoReview")}


@scenario("old-retired-rows", layer="upgrade", needs=("old-people",), needs_rig=True)
def old_retired_rows(ctx):
    """Two chats stored with the mode words from before 1.9, and the person's preferred
    engine's column, as the previous release keeps them."""
    sub = _sql(ctx, f"SELECT sub FROM users WHERE email = '{ctx.email('owner')}'")
    check(sub, "the owner's row was not read")
    for chat_id, (word, _now) in OLD_MODES.items():
        _sql(ctx, "INSERT INTO chats (id, user_sub, agent, title, permission_mode, created_at, updated_at) "
                  f"VALUES ('{chat_id}', '{sub}', '{ctx.agent}', 'Rig mode {word}', '{word}', now()::text, now()::text) "
                  f"ON CONFLICT (id) DO UPDATE SET permission_mode = '{word}'")
    column = _sql(ctx, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'users' "
                       "AND column_name = 'preferred_execution_path'")
    check(column == "1", f"the previous release has no preferred engine's column: {column}")
    return "two chats in 'default' and 'acceptEdits', the preferred engine's column present"


@scenario("old-trigger-key", layer="upgrade", needs=("old-people",), needs_rig=True)
def old_trigger_key(ctx):
    """A trigger and a triggers key minted as the previous release mints one: no expiry."""
    owner = ctx.owner
    slug = f"rig-old-trigger-{int(time.time())}"
    made = owner.ok("POST", "/v1/triggers", {"name": "Rig old trigger", "agent": ctx.agent, "scope": "user", "slug": slug,
                                             "notify": {"enabled": True, "severity": "info", "title": f"Fired {slug}",
                                                        "body": "From the old key."}})
    key = owner.ok("POST", "/v1/user-api-keys", {"name": slug, "permissions": ["triggers"]})
    check(key.get("key") and key.get("id") is not None, f"the previous release's key: {sorted(key)}")
    ctx.state["old_trigger"] = {"path": made.get("webhook_path") or f"/v1/webhooks/user/{owner.username}/{slug}",
                                "key": key["key"], "id": key["id"]}
    status, _ = Key(ctx.base, key["key"]).call("POST", ctx.state["old_trigger"]["path"], {"source": "the old release"})
    check(status < 300, f"the key's fire on the previous release answered {status}")
    return f"trigger {slug}, key {key.get('prefix', '')}"


@scenario("upgrade-script", layer="upgrade", needs=("old-chats",), needs_rig=True)
def upgrade_script(ctx):
    """scripts/upgrade.sh moves the install to the candidate."""
    ctx.require_rig()
    ctx.state["rows_before"] = _rows_before(ctx)
    candidate = Path(ctx.candidate_dir)
    shutil.copy(candidate / "scripts" / "upgrade.sh", ctx.install_dir / "upgrade.sh")
    _the_proxys_own_mount_stops_the_update(ctx, candidate)
    code, said = _update(ctx, candidate, "upgrade-script.log")
    check(code == 0, f"upgrade.sh exited {code}: {said[-500:]}")
    check(CANNOT_WAIT in said and SWAP in said and said.index(CANNOT_WAIT) < said.index(SWAP),
          f"the previous release's sentence comes before the swap: {said[-500:]}")
    http = Http(ctx.base)

    def healthy():
        try:
            status, body = http.call("GET", "/health", timeout=5)
        except OSError:
            return None
        # The candidate can answer before it takes work: the old chats' turns
        # wait until it does.
        return body if status == 200 and body.get("status") == "ok" and body.get("ready") else None
    body = http_wait(ctx, healthy, timeout=1800.0)
    check(body.get("build") != ctx.state["old_build"], f"the build did not change: {body}")
    # The update's own start, read before the rig's next step starts the containers again;
    # judged by its own scenario, so a log that cannot be read fails that one alone.
    try:
        ctx.state["update_build_lines"] = s10_install.build_lines(ctx)
    except CheckFailed as unread:
        ctx.state["update_build_lines"] = {"the update's logs": [str(unread)]}
    s70_compute._healthy(ctx)
    signed = s70_compute._signed_in(ctx, wait_s=600)
    check(signed == "The compute machine is signed in.", f"the compute machine did not sign in: {signed!r}")
    ctx.people.clear()
    return f"build {body.get('build')}"


def _update(ctx, candidate: Path, log_name: str) -> tuple[int, str]:
    log = ctx.run_dir / log_name
    with log.open("w") as out:
        done = subprocess.run(["bash", "upgrade.sh", "--to", ctx.upgrade_target, "--compose-dir", str(candidate)],
                              cwd=ctx.install_dir, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              timeout=1500, check=False, env={**os.environ, "OTODOCK_UPGRADE_WAIT_S": str(HEALTH_WAIT_S)})
    return done.returncode, log.read_text()


# What upgrade.sh says of an override that mounts something for the proxy alone: the mount,
# that nothing was touched, the exact second run, and what the fix does to the folder under
# a release with no compute service. Matched on the log with its line breaks folded.
PROXY_ALONE = "mounts /opt/rig-bin for otodock-proxy only"
UNTOUCHED = "stopped before changing anything: the running stack was not touched"
AGAIN = "then run the update again right away: OTODOCK_COMPOSE_PROJECT={} bash upgrade.sh --to {} --compose-dir {}"
REFUSES_UNTIL = "Until then docker compose in this folder refuses the override"
# What a run that got past its checks says before each thing it does.
DUMP, PULL = "backing up the database", "pulling the"
# The rig's override: the wrapper's folder for the proxy, and the same for the compute service
# (fresh.sh appends it where the install's compose file has that service).
PROXY_PART = Path(__file__).resolve().parent.parent / "install" / "scripted-engine" / "compose-override.yml"
COMPUTE_PART = PROXY_PART.with_name("compose-override.compute.yml")


def _the_proxys_own_mount_stops_the_update(ctx, candidate: Path) -> None:
    """The previous release's override mounts the rig's Claude Code wrapper, which
    CLAUDE_BIN names, into the proxy alone. Chats run in the compute service, so
    the update stops before the dump and the pull, names the mount and the exact
    second run, and leaves the install as it was. Then what its words ask of a
    person: the same mount for the compute service (fresh.sh's part). Until the
    update ran, docker compose refuses that folder when the previous release has
    no compute service, as the refusal said."""
    env, override = ctx.install_dir / ".env", ctx.install_dir / "docker-compose.override.yml"
    # A previous release that has the compute service already mounts the wrapper for it: the
    # proxy's part alone is written first, so every candidate is asked for the refusal.
    override.write_text(PROXY_PART.read_text())
    project = json.loads(ctx.compose("config", "--format", "json", timeout=60).stdout or "{}").get("name", "")
    check(bool(project), "docker compose names no project for the install's folder")

    def folder():
        return env.read_text(), override.read_text(), sorted(p.name for p in ctx.install_dir.iterdir())
    before = folder()
    code, said = _update(ctx, candidate, "upgrade-refused.log")
    words = " ".join(said.split())
    check(code != 0 and PROXY_ALONE in words, f"the update did not stop on the wrapper's mount ({code}): {said[-500:]}")
    check(UNTOUCHED in words and not any(late in words for late in (DUMP, PULL, SWAP)),
          f"the refusal came after the dump, the pull or the swap: {said[-500:]}")
    check(folder() == before, f"the refused update left the install's folder changed: {said[-500:]}")
    check(AGAIN.format(project, ctx.upgrade_target, candidate) in words,
          f"the refusal did not name the second run: {said[-500:]}")
    status, body = Http(ctx.base).call("GET", "/health", timeout=10)
    check(status == 200 and body.get("build") == ctx.state["old_build"],
          f"the refused update touched the running server: {body}")
    override.write_text(override.read_text() + COMPUTE_PART.read_text())
    listed = ctx.compose("ps", timeout=60)
    check((listed.returncode != 0) == (REFUSES_UNTIL in words),
          f"docker compose in the fixed folder answered {listed.returncode}, against the refusal's own words: "
          f"{listed.stderr[-300:]}")


def http_wait(ctx, probe, *, timeout: float = 300.0):
    from lib.person import Person
    return Person(ctx.base, "", "").wait_until("the upgraded install to answer", probe, timeout=timeout, every=5)


@scenario("after-tools-first-chat", layer="upgrade", needs=("upgrade-script",), needs_rig=True)
def after_tools_first_chat(ctx, broken: bool = False):
    """The first chat after the update starts its tools' programs with the chat, as the
    previous release did, and saves their answers (a program started only for them stops
    once they are kept); the next chat starts none until a call. The first chat of this
    layer, so nothing was saved before it. Broken: the first chat is expected to save none."""
    from lib.tools import programs_of, stand_ins_of
    from scenarios import s73_tools_on_demand as tools
    engine, model = ENGINES["claude"]
    before = _kept_answers(ctx, tools.TOOL)
    check(before == 0, f"answers of {tools.TOOL} were kept before the first chat after the update: {before}")
    seen, kept = {}, 0
    for which in ("first", "second"):
        tools._quiet(ctx, "")
        chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, text="Say hello. " + script({"text": "Hello."}))
        try:
            chat.wait_done()
            seen[which] = tools._lines(ctx, "")
        finally:
            chat.close()
        if which == "first":
            deadline = time.monotonic() + KEPT_WAIT_S
            while not (kept := _kept_answers(ctx, tools.TOOL)) and time.monotonic() < deadline:
                time.sleep(2)
    second = programs_of(seen["second"], tools.TOOL)
    check(bool(kept) is not broken, f"the first chat after the update kept {kept} answer(s) of {tools.TOOL}")
    check(not second, f"the second chat started {tools.TOOL} before any call: {second[:2]}")
    check(stand_ins_of(seen["second"]), f"the second chat holds no stand-in of {tools.TOOL}")
    return f"the first chat started {tools.TOOL} and kept its answers, the second started none"


# How long a first chat's tool takes to have its answers kept after the turn.
KEPT_WAIT_S = 30.0


def _kept_answers(ctx, tool: str) -> int:
    """The answers of ``tool`` kept for the owner on the agent (``mcp_tool_listings``)."""
    found = ctx.sql(f"SELECT count(*) FROM mcp_tool_listings WHERE agent_name = '{ctx.agent}' "
                    f"AND person = '{ctx.owner.username}' AND server_name = '{tool}'")
    return int(found or 0)


@scenario("after-start-builds-nothing", layer="upgrade", needs=("upgrade-script",), needs_rig=True)
def after_start_builds_nothing(ctx, broken: bool = False):
    """The update's start built no tool and held no work for one: its image carries the
    tools' environments, so the proxy names nothing to prepare, and /health says the
    tools are ready. The compute machine beside the platform runs no reconcile of its
    own: its log has nothing to read here. Broken: the proxy's line is expected."""
    return s10_install.builds_nothing(ctx, broken, built=ctx.state["update_build_lines"])


@scenario("after-status", layer="upgrade", needs=("upgrade-script",), needs_rig=True)
def after_status(ctx):
    """The admin command answers busy or idle on the upgraded install."""
    done = ctx.admin("status")
    check(done.returncode == 0 and done.stdout.startswith(("Idle", "Busy")), f"status: {done.stdout!r} {done.stderr[-300:]!r}")
    return done.stdout.strip()


@scenario("after-rows", layer="upgrade", needs=("upgrade-script",), needs_rig=True)
def after_rows(ctx):
    """Every old chat keeps every row."""
    now = _rows_before(ctx)
    for name, count in ctx.state["rows_before"].items():
        check(now.get(name, 0) >= count, f"{name} had {count} rows and now has {now.get(name)}")


def _after_turn(ctx, name: str, word: str, *, terminal: bool = False, target: str = "") -> None:
    engine, model = ENGINES[word]
    chat_id = ctx.state["chats"][name]
    before = len(ctx.owner.rows(chat_id))
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, chat_id=chat_id, text="Again, after the upgrade.",
                          terminal=terminal, target=target)
    try:
        if terminal:
            chat.attach_terminal()
            rows = chat.wait_rows("the old terminal's new answer", lambda r: len(r) > before + 1, timeout=180)
        else:
            chat.wait_done()
            rows = chat.rows()
    finally:
        chat.close()
    shown = [(r.get("role"), (r.get("content") or "")[:120]) for r in rows[before:]]
    check(len(rows) > before and rows[-1].get("role") != "user",
          f"{name} answered after the upgrade: {len(rows)} rows, {before} before: {shown}")


@scenario("after-account", layer="upgrade", needs=("upgrade-script", "old-account"), needs_rig=True)
def after_account(ctx):
    """The previous release's account binding is the default of a set of one."""
    row = _sql(ctx, "SELECT b.is_default, (SELECT count(*) FROM agent_account_bindings o WHERE o.user_sub = b.user_sub "
                    "AND o.mcp_name = b.mcp_name AND o.agent_name = b.agent_name) FROM agent_account_bindings b "
                    f"WHERE b.mcp_name = '{ACCOUNT_TOOL}' AND b.account_label = '{ACCOUNT_LABEL}'")
    check(row == "t|1", f"the binding after the upgrade (default, the set's size): {row or 'gone'}")
    keys = _sql(ctx, "SELECT count(*) FROM pg_constraint WHERE conname = 'agent_account_bindings_set_key'")
    check(keys == "1", "the table is keyed by the set, each account once")
    return "the default of a set of one"


@scenario("after-turn-claude", layer="upgrade", needs=("upgrade-script",), needs_rig=True)
def after_turn_claude(ctx):
    """An old Claude Code chat takes a new turn."""
    _after_turn(ctx, "headless-claude", "claude")


@scenario("after-turn-codex", layer="upgrade", needs=("upgrade-script",), needs_rig=True)
def after_turn_codex(ctx):
    """An old Codex chat takes a new turn."""
    _after_turn(ctx, "headless-codex", "codex")


@scenario("after-turn-direct", layer="upgrade", needs=("upgrade-script",), needs_rig=True)
def after_turn_direct(ctx):
    """An old Direct LLM chat takes a new turn."""
    _after_turn(ctx, "headless-direct", "direct")


@scenario("after-terminal-codex", layer="upgrade", needs=("upgrade-script",), needs_rig=True)
def after_terminal_codex(ctx):
    """An old Codex terminal takes a new turn."""
    _after_turn(ctx, "terminal-codex", "codex", terminal=True)


@scenario("after-task", layer="upgrade", needs=("upgrade-script", "old-tasks"), needs_rig=True)
def after_task(ctx):
    """The old task runs, and the schedule is still there."""
    owner = ctx.owner
    started = owner.ok("POST", f"/v1/tasks/{ctx.state['task_id']}/run")
    run = s30_automation.wait_run_ended(owner, started["run_id"])
    check(run["status"] == "completed", f"the run ended {run['status']}")
    check(owner.ok("GET", f"/v1/tasks/{ctx.state['schedule_id']}"), "the schedule survives")


@scenario("after-app", layer="upgrade", needs=("upgrade-script", "old-app"), needs_rig=True)
def after_app(ctx):
    """The old app still answers."""
    status, _ = ctx.owner.call("GET", f"/v1/apps/{ctx.state['app_id']}")
    check(status == 200, f"the app answered {status}")


@scenario("after-member", layer="upgrade", needs=("upgrade-script", "old-people"), needs_rig=True)
def after_member(ctx):
    """The member signs in after the upgrade."""
    check(ctx.person("member").me.get("role") == "member", "the member's role")


@scenario("after-machine", layer="upgrade", needs=("upgrade-script", "old-machine"), needs_rig=True)
def after_machine(ctx):
    """The machine comes back, updates itself, and an old chat takes a turn on it."""
    owner = ctx.owner
    versions = (Path(ctx.candidate_dir) / "VERSIONS.md").read_text().splitlines()
    version = next((v.split("=", 1)[1] for v in versions if v.startswith("SATELLITE_VERSION=")), "")

    def current():
        row = owner.ok("GET", f"/v1/admin/remote-machines/{ctx.state['machine_id']}")
        online = row.get("connected") or row.get("status") == "online" or row.get("online")
        return row if online and (not version or row.get("satellite_version") == version) else None
    row = owner.wait_until(f"the machine on satellite {version}", current, timeout=480, every=10)
    _after_turn(ctx, "headless-claude-machine", "claude", target=ctx.state["machine_id"])
    return f"satellite {row.get('satellite_version')}"


# The credential exit's packages, which a satellite's update installs beside its own
# (``satellite/requirements-exit.txt``) and which its own lock does not hold.
EXIT_PACKAGES = ("httpx", "h11", "cryptography")


@scenario("after-machine-exit-packages", layer="upgrade", needs=("upgrade-script", "after-machine"), needs_rig=True)
def after_machine_exit_packages(ctx, broken: bool = False):
    """The updated satellite installs the credential exit's packages into its own
    environment at its first start (the update ran the previous release's code, which
    knows its own lock alone), in the background. Broken: once they import, a package no
    lock names is expected too."""
    from run import rig_home
    ctx.require_rig()
    python = rig_home() / ".oto-dock" / "satellite" / "venv" / "bin" / "python"

    def imports(names) -> bool:
        done = subprocess.run([str(python), "-c", "import " + ", ".join(names)], capture_output=True, text=True,
                              timeout=60, check=False)
        return done.returncode == 0
    ctx.owner.wait_until(f"{', '.join(EXIT_PACKAGES)} in the satellite's environment", lambda: imports(EXIT_PACKAGES),
                         timeout=600, every=10)
    if broken:
        check(imports((*EXIT_PACKAGES, "rig_no_such_package")), "a package no lock names is not in the environment")
    return f"{', '.join(EXIT_PACKAGES)} import in the updated satellite's environment"


@scenario("after-collabora", layer="upgrade", needs=("upgrade-script", "old-collabora"), needs_rig=True)
def after_collabora(ctx, broken: bool = False):
    """The update recreated Collabora from the candidate's definition on this host's
    Docker, and it runs (Docker 28.1 refused that recreate before its mounts said
    nocopy). Inconclusive when the two releases define it alike: nothing was to be
    recreated. Broken: the previous release's definition is expected."""
    container, running = _definition(ctx, "collabora")
    wanted = ctx.compose("config", "--hash", "collabora", timeout=60).stdout.split()
    check(len(wanted) == 2, f"the candidate's definition of collabora was not read: {wanted}")
    before_container, before_definition = ctx.state["collabora_before"]
    if before_definition == wanted[1] and not broken:
        raise Inconclusive("the previous release defines Collabora as the candidate does: no recreate to see")
    expected = before_definition if broken else wanted[1]
    check(running == expected, f"collabora runs the definition {running[:12]}, not {expected[:12]}")
    check(container != before_container, "collabora's container is the previous release's")
    state = ctx.docker("inspect", "-f", "{{.State.Status}} {{.RestartCount}}", container).stdout.split()
    check(state[:1] == ["running"], f"collabora is {state}")
    engine = ctx.docker("version", "-f", "{{.Server.Version}}").stdout.strip()
    return f"Docker {engine}: collabora recreated from the candidate's definition, running, {state[1:]} restarts"


# Two tags of the candidate's own images, on this host only: from 2.0 on an update
# waits, and a lane's build is tagged under 2.0. The second is relabelled, so its image
# differs and the swap is real.
ROLL_FROM, ROLL_TO = "2.0.0-rc91", "2.0.0-rc92"


def _tag_the_build(ctx, new_tag: str, *, relabel: bool) -> None:
    current = ctx.env_value("OTODOCK_VERSION")
    images = ctx.compose("config", "--images", timeout=60).stdout.split()
    for image in [i for i in images if i.endswith(f":{current}")]:
        target = f"{image.rsplit(':', 1)[0]}:{new_tag}"
        if relabel:
            done = subprocess.run(["docker", "build", "-q", "-t", target, "-"], capture_output=True, text=True,
                                  input=f"FROM {image}\nLABEL rig.rolling={new_tag}\n", timeout=300, check=False)
        else:
            done = subprocess.run(["docker", "tag", image, target], capture_output=True, text=True,
                                  timeout=60, check=False)
        check(done.returncode == 0, f"{target} was not made: {done.stderr[-300:]}")


def _on_the_first_tag(ctx) -> None:
    """The install on ``ROLL_FROM``: what an update from a 2.0 build starts from."""
    _tag_the_build(ctx, ROLL_FROM, relabel=False)
    _tag_the_build(ctx, ROLL_TO, relabel=True)
    env = ctx.install_dir / ".env"
    lines = [f"OTODOCK_VERSION={ROLL_FROM}" if ln.startswith("OTODOCK_VERSION=") else ln
             for ln in env.read_text().splitlines()]
    env.write_text("\n".join(lines) + "\n")
    done = ctx.compose("up", "-d", "--remove-orphans", timeout=HEALTH_WAIT_S)
    check(done.returncode == 0, f"the first tag did not come up: {done.stderr[-300:]}")
    http = Http(ctx.base)
    http_wait(ctx, lambda: http.call("GET", "/health", timeout=5)[0] == 200)
    # The start's hold ends after the compute machine's sessions are adopted, not
    # at the first health answer: a chat sent before is refused as an update.
    s70_compute._signed_in(ctx)
    ctx.people.clear()


def _proxy_image(ctx) -> str:
    return ctx.compose("images", "-q", "otodock-proxy", timeout=60).stdout.strip()


@scenario("after-compute", layer="upgrade", needs=("upgrade-script",), needs_rig=True)
def after_compute(ctx, broken: bool = False):
    """The updated install's compute service runs a Claude Code chat, a Codex chat and
    a new chat's pre-warm, never the proxy. Broken: a session is expected in the proxy."""
    return s70_compute.compute_sessions(ctx, broken=broken)


@scenario("after-direct-tools-on-compute", layer="upgrade", needs=("upgrade-script", "old-chats"), needs_rig=True)
def after_direct_tools_on_compute(ctx, broken: bool = False):
    """On the updated install a Direct LLM chat's stdio tool runs on the compute service
    and in no other container. Broken: the tool is expected in the proxy's."""
    from claims import c40_direct_compute
    return c40_direct_compute.direct_tools_on_compute(ctx, broken=broken)


@scenario("after-working-files-wait", layer="upgrade", needs=("upgrade-script", "old-working-files"), needs_rig=True)
def after_working_files_wait(ctx, broken: bool = False):
    """The working-files window waits 30 days after the update: a sweep run now says so
    and removes nothing, and the previous release's old uploads are there. Broken: the
    sweep is expected to wait for nothing."""
    from datetime import datetime, timedelta, timezone
    answer = ctx.owner.ok("POST", "/v1/admin/retention/run-now", {"dry_run": False}, timeout=600)
    stats = answer.get("stats", answer) if isinstance(answer, dict) else {}
    until = str(stats.get("working_files_grace_until") or "")
    now = datetime.now(timezone.utc)
    waits = bool(until) and now + timedelta(days=29) < datetime.fromisoformat(until) <= now + timedelta(days=30, hours=1)
    check(waits is not broken, f"the sweep's grace ends {until or 'never'}")
    check(not stats.get("working_uploads_binned"), f"the sweep took {stats.get('working_uploads_binned')} upload(s)")
    for path in _old_file_paths(ctx):
        done = ctx.proxy_exec(f"test -f {shlex.quote(path)}", timeout=60)
        check(done.returncode == 0, f"the sweep removed {path}")
    return f"the window waits until {until}; nothing removed"


@scenario("after-retired-rows", layer="upgrade", needs=("upgrade-script", "old-retired-rows"), needs_rig=True)
def after_retired_rows(ctx, broken: bool = False):
    """The update rewrote the stored mode words from before 1.9 (``default`` is Ask,
    ``acceptEdits`` is Auto) and dropped the preferred engine's column. Broken: the old
    words are expected."""
    for chat_id, (word, now) in OLD_MODES.items():
        stored = _sql(ctx, f"SELECT permission_mode FROM chats WHERE id = '{chat_id}'")
        expected = word if broken else now
        check(stored == expected, f"the chat stored with {word!r} reads {stored!r}, not {expected!r}")
    column = _sql(ctx, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'users' "
                       "AND column_name = 'preferred_execution_path'")
    check(column == "0", f"the preferred engine's column is still there: {column}")
    for chat_id in OLD_MODES:
        _sql(ctx, f"DELETE FROM chats WHERE id = '{chat_id}'")
    return "the words rewritten, the column dropped"


@scenario("after-trigger-key", layer="upgrade", needs=("upgrade-script", "old-trigger-key"), needs_rig=True)
def after_trigger_key(ctx, broken: bool = False):
    """A triggers key minted on the previous release still fires its trigger, and has no
    expiry. Broken: the key is expected to carry one."""
    old = ctx.state["old_trigger"]
    status, body = Key(ctx.base, old["key"]).call("POST", old["path"], {"source": "the old key, after the update"})
    check(status < 300, f"the previous release's key fired its trigger with {status}: {str(body)[:200]}")
    rows = ctx.owner.ok("GET", "/v1/user-api-keys").get("keys") or []
    row = next((r for r in rows if r.get("id") == old["id"]), None)
    check(row is not None, "the previous release's key is not in the owner's list")
    check(bool(row.get("expires_at")) is broken, f"the previous release's key expires at {row.get('expires_at')!r}")
    return "the old key fires its trigger and never expires"


@scenario("after-no-host-waits", layer="upgrade", needs=("upgrade-script",), needs_rig=True)
def after_no_host_waits(ctx, broken: bool = False):
    """No tool's host waits for an admin's Allow after the update: the shipped tools'
    hosts are the allowlist's rows, and Admin, Security asks for nothing. Broken: a
    waiting host is expected."""
    listed = ctx.owner.ok("GET", "/v1/admin/oauth-bearer-allowlist")
    pending, entries = listed.get("pending") or [], listed.get("entries") or []
    check(bool(pending) is broken, f"hosts wait for an Allow after the update: {str(pending)[:300]}")
    check(entries, "the allowlist holds no row after the update")
    return f"{len(entries)} allowlist row(s), no host waits"


def _exit_claim_after(ctx, broken: bool, claim) -> str:
    """One of the credential exit's claims, as written, on the upgraded install: the
    exit has no switch since 2.0, so it holds there with no step by the admin."""
    return claim(ctx, broken=broken)


@scenario("after-exit-in-the-session", layer="upgrade", needs=("upgrade-script",), needs_rig=True)
def after_exit_in_the_session(ctx, broken: bool = False):
    """On the upgraded install a tool's key reaches a session as a placeholder, and the
    value is in no process, file or log line. Broken: the claim's own."""
    from claims import c30_exit
    return _exit_claim_after(ctx, broken, c30_exit.exit_no_value_in_the_session)


@scenario("after-exit-on-compute", layer="upgrade", needs=("upgrade-script",), needs_rig=True)
def after_exit_on_compute(ctx, broken: bool = False):
    """The same on the upgraded install's compute service. Broken: the claim's own."""
    from claims import c30_exit
    return _exit_claim_after(ctx, broken, c30_exit.exit_no_value_on_compute)


@scenario("after-exit-on-the-machine", layer="upgrade", needs=("upgrade-script", "after-machine"), needs_rig=True,
          machine=True)
def after_exit_on_the_machine(ctx, broken: bool = False):
    """The same on the machine paired under the previous release, once it updated
    itself. Broken: the claim's own."""
    from claims import c30_exit
    return _exit_claim_after(ctx, broken, c30_exit.exit_no_value_on_a_paired_machine)


@scenario("after-rolling-order", layer="upgrade", needs=("upgrade-script",), needs_rig=True)
def after_rolling_order(ctx):
    """An update from one 2.0 build to another waits for a running turn: it finishes whole,
    a card is asked again, a message sent during the wait goes out, and the new server
    holds nothing. The last of this layer: it replaces the install under the others."""
    ctx.require_rig()
    _on_the_first_tag(ctx)
    before = _proxy_image(ctx)
    work = s55_continuity.open_work(ctx)
    candidate = Path(ctx.candidate_dir)
    shutil.copy(candidate / "scripts" / "upgrade.sh", ctx.install_dir / "upgrade.sh")
    log = ctx.run_dir / "upgrade-rolling.log"
    ran: list[subprocess.CompletedProcess] = []

    def update(mode: str = "w"):
        with log.open(mode) as out:
            ran.append(subprocess.run(
                ["bash", "upgrade.sh", "--to", ROLL_TO, "--compose-dir", str(candidate)],
                cwd=ctx.install_dir, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                timeout=1500, check=False, env={**os.environ, "OTODOCK_UPGRADE_WAIT_S": str(HEALTH_WAIT_S)}))

    thread = threading.Thread(target=update)
    thread.start()
    try:
        ctx.owner.wait_until("the server holding new work", lambda: "holding" in ctx.admin("status").stdout,
                             timeout=240, every=2)
        s55_continuity.message_during_the_wait(work)
    finally:
        thread.join(timeout=1500)
        work.running.close()
        work.waiting.close()
    said = log.read_text()
    check(ran and ran[0].returncode == 0, f"upgrade.sh failed: {said[-500:]}")
    check(CANNOT_WAIT not in said, f"a 2.0 build was told it cannot wait: {said[-500:]}")
    check(WAITS in said and SWAP in said and said.index(WAITS) < said.index(SWAP),
          f"the wait comes before the swap: {said[-500:]}")
    check(_proxy_image(ctx) != before, "the swap replaced nothing: this run proved no swap")
    http = Http(ctx.base)
    http_wait(ctx, lambda: http.call("GET", "/health", timeout=5)[0] == 200)
    s70_compute._signed_in(ctx)
    ctx.people.clear()
    s55_continuity.after_the_stop(ctx, work)
    status = ctx.admin("status")
    check(status.returncode == 0 and "holding" not in status.stdout, f"the new server still holds: {status.stdout!r}")
    # The script's own advice after a failure: the version it is on. Compose
    # recreates nothing, so the server that drained goes on running.
    swapped_in = _proxy_image(ctx)
    update("a")
    check(ran[-1].returncode == 0, f"upgrade.sh on the same version failed: {log.read_text()[-500:]}")
    check(_proxy_image(ctx) == swapped_in, "the same version replaced the image")
    status = ctx.admin("status")
    check(status.returncode == 0 and "holding" not in status.stdout, f"the same version ends holding: {status.stdout!r}")
    answer = "It answers."
    chat = ctx.owner.chat(ctx.agent, engine=s55_continuity.CLAUDE, model=s55_continuity.CLAUDE_MODEL,
                          text="A new chat. " + script({"text": answer}))
    try:
        chat.wait_done()
        check(answer in assistant_text(chat.rows()), f"a new chat after it: {assistant_text(chat.rows())[:200]!r}")
    finally:
        chat.close()
    return "the turn finished, the card came again, the message went out, nothing holds, also on the same version again"
