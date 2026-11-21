# The test rig

The rig installs a candidate of OtoDock fresh on a machine of its own, drives it as
a person and as an outside program, and says pass or fail. Nothing else tests a
running install.

**It removes installs. It belongs on a machine that holds nothing else.** Every
`.sh` script under `install/` refuses to run unless the machine's own home holds a marker
naming that machine, and nothing here writes the marker for you. To run the rig on a
virtual machine of your own, with Docker, `sudo` without a password and Python 3.10:

```bash
mkdir -p ~/.otodock-rig && echo "$(uname -n) $(cat /etc/machine-id)" > ~/.otodock-rig/this-host-is-a-rig
# The candidate is a tree without git history: the build refuses a clone.
mkdir ~/candidate && git archive HEAD | tar -x -C ~/candidate
bash ~/candidate/rig/install/vm-run.sh --run-dir ~/rig-run --candidate ~/candidate --public-url http://<this machine>:8400 --tag <version>
```

`vm-run.sh` also takes `--layer scripted|live|all|upgrade|standup` (`all` runs the scripted
layer, then the live layer, on one install; `upgrade` needs `--from-version <the release
before>`; `standup` installs as a person gets it and runs no scenario), `--only a,b`,
`--no-wipe`, `--no-build`, `--ref <a tag or commit of the public repository>` (install.sh
from that ref and its images under `--tag`, nothing built), `--inject
model-error|broken-check|satellite-down` (a failure on purpose: the report must turn red) and
`--floor-script <file>` (after the wipe, a script that boots the images on the oldest Docker
Compose the install scripts accept; a pass is kept per build, and a run with `--ref` or
`--no-wipe`, or the upgrade, boots none).
`python3 rig/run.py --base-url <url> --run-dir <dir> --layer scripted|live|upgrade --list`
prints a layer's scenarios, with the claims in the scripted and live layers.

## The layers

- **Scripted** (`scenarios/`). The real engines against `models/server.py`, which
  speaks the vendors' public model APIs and answers from a script that rides in the
  prompt. Free, repeatable, no account, and no test code in the product.
- **Live** (`live/`). The real engines on their cheapest models, with keys you put in
  `~/.otodock-rig/accounts` (`ANTHROPIC_API_KEY=...`, `OPENAI_API_KEY=...`, mode 0600).
  It asks an engine only for what it declares. With no keys it runs against the
  scripted server and says so.
- **Claims** (`claims/`). The docs' security claims, each a probe, run with both.
  `claim-compute-on-another-host` (`claims/c50_compute_remote.py`) runs the add's one command
  on the VM, as an admin pastes it on another server, and checks that the removal leaves
  nothing there; written in 2.0 and not yet run.
  `claim-google-no-refresh-token-in-the-session` has a Codex twin (`-codex`).
  `claim-proxy-starts-no-sandbox` (`claims/c40_direct_compute.py`) asks for the session's shape twice.
  It must not start in the proxy's container and must start in the compute service's.
  It also reads the proxy container's security options, devices and capabilities.
  Written in 2.0 and not yet run.
  `claim-installs-cannot-read-secrets` (`claims/c60_installer.py`) uploads a planted package whose
  build records what it can reach, served by an index container on the install's network and
  named to the proxy by an override file for the claim's window; written in 2.0 and not yet run.
  `claim-tools-idle`, `-long-call` and `-docker-restart` (`claims/c70_tools_idle.py`): the catalog's video
  tools sleep after a one-minute spell and a call wakes them. Two calls start them once. A call longer than
  the spell is not cut, nor one longer than the link's reap. Collabora and the phone service keep running.
  A sleeping tool's container scores 500 and stays asleep across a restart of Docker. Written in 2.0 and
  not yet run.
- **Upgrade** (`upgrade/`). The previous release by the README, data put in,
  `scripts/upgrade.sh` to the candidate, then turns on the old chats.

Written for 2.0 and not yet run on an install. Scripted: `fresh-start-builds-nothing`
(`scenarios/s10_install.py`), `member-without-an-engine` and `template-task-pins`
(`s37_people_and_templates.py`), `upload-age-survives-up` and `delete-stays-deleted`
(`s58_data_life.py`), `compute-app-listener` and `compute-app-forgets-copies` (`s71_compute_apps.py`),
`tools-first-call-claude`, `-codex`, `-direct`, `-machine`, `tools-start-refused-full-box` and
`direct-tools-leave-no-orphan` (`s73_tools_on_demand.py`), `memory-order` (`s74_memory_order.py`),
`session-caps` and `session-caps-machine` (`s75_session_caps.py`).
Upgrade: `old-collabora`, `old-working-files`, `after-tools-first-chat`, `after-start-builds-nothing`,
`after-working-files-wait`, `after-machine-exit-packages`, `after-collabora` and
`after-direct-tools-on-compute`. `old-retired-rows` and `after-retired-rows`: the old mode words are
rewritten and the preferred engine's column is gone. `old-trigger-key` and `after-trigger-key`: an old key
still fires its trigger and never expires. Then `after-no-host-waits` and the three exit claims on the
upgraded install (`after-exit-in-the-session`, `-on-compute`, `-on-the-machine`). Live:
`live-message-to-person-codex`, a message from Codex's newest model in Ask, with no card.
`claim-proxy-starts-no-sandbox` also finds the sandbox's privileges on the compute service alone.

## Adding a scenario

A file under `scenarios/`, or a function in one. It takes the run's context, drives
through `ctx.owner` (a signed-in person: routes and the dashboard's socket) or a `Key`
(a person's API key: a headless turn, a task, files and notifications, nothing else),
and checks structure, never a model's words:

```python
from lib.chat import script
from lib.scenario import check, scenario
from run import ENGINES

@scenario("my-behaviour", needs=("turn-claude",))
def my_behaviour(ctx):
    """One line: what a person gets."""
    engine, model = ENGINES["claude"]
    prompt = "Run it. " + script({"tool": "shell", "input": {"command": "echo hi"}}, {"text": "Done."})
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, text=prompt)
    try:
        chat.wait_done()
        rows = chat.rows()
    finally:
        chat.close()
    check(rows[-1]["role"] == "assistant", "the turn ended with an answer")
```

`needs` skips it when what it builds on did not pass. `known="..."` with
`known_match="<the failure's text>"` marks a product bug known here: a failure carrying
that text reports `known` and stops a release, any other failure stays `fail`, and a pass
reports `fixed`. `changes_with="<the planned change>"` names what will change the expectation. `needs_rig=True` for
anything that installs or restarts something on the machine. To iterate, run only yours
against the install that is up: `--no-wipe --only my-behaviour`.

A check shows it can fail through a `broken` argument, run under `--no-wipe --only <name>
--inject broken-check` on an install whose scripted layer passed. Without `--only`, a check
that passed is carried and one whose needs have no pass is skipped, so the report turns red
with no broken form run. A broken run records no pass. A form that makes the setup wrong (a bound of 0, an engine ended) needs nothing else. One that
must make the product wrong edits the server's own code in its container
(`ctx.break_product`), and `ctx.mend_product` recreates the container from its image.

`script(..., after_cut=[...])` gives a model steps for after the platform told it its turn
was cut, counted from those words, so it does what the words ask and repeats no call by
itself.

## What runs where

`install/` runs on the machine under test, and every `.sh` script there sources `guard.sh`
before anything else. A test of these scripts fakes every command that acts on a host
(`docker`, `systemctl`, `loginctl`, `sudo`, `fuser`, `ip`): a fake `HOME` isolates
nothing from a service manager or a container engine.
