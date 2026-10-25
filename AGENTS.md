# AGENTS.md

Instructions for coding agents working in a clone of OtoDock, and for the
people who direct them. People start with [`CONTRIBUTING.md`](CONTRIBUTING.md),
and the map of the code is [`ARCHITECTURE.md`](ARCHITECTURE.md). This file is the
one instruction file of the repository: [`CLAUDE.md`](CLAUDE.md) only points
here, so edit this file and keep it short.

OtoDock is a self-hosted platform for AI agents: a FastAPI backend (`proxy/`)
that runs Claude Code, Codex and direct model sessions in per-session
sandboxes, a React dashboard (`dashboard/`) with an Android shell, a daemon
for remote machines (`satellite/`), a phone-call service (`phone/`), a shared
speech package (`audio/`) and first-party MCP servers (`mcps/`).

## Ground rules

- Work inside this clone. Do not change files, services or settings outside
  it. The checks below are the exception they need: they may fill their
  tools' caches (`~/.npm`, `~/.gradle`, `~/.cache/pip`), and the proxy suite
  creates and drops its own `*_test` databases on the PostgreSQL the person
  set up for it.
- Never start, stop or recreate a database, a container or a service.
- Commit, push or open a pull request only when the person asks you to.
- Setting up the machine (`scripts/dev-setup.sh`, containers, system
  packages, the environments `CONTRIBUTING.md` creates once) is the person's
  step. When a tool, an environment or the test database is missing, skip
  that check, say so and ask. Do not install around it.
- Never commit secrets, tokens, `config.env` or personal data.
- Stay in scope: do what was asked, and propose adjacent improvements
  instead of bundling them in.

## Where code goes

| Path | What lives there |
|---|---|
| `proxy/api/`, `proxy/ws/` | HTTP routes and websocket handlers, one subpackage per surface |
| `proxy/core/` | sessions, the execution layers (engines), the sandbox, remote machines, the event stream |
| `proxy/services/` | business logic, one subpackage per domain |
| `proxy/storage/` | the schema (`schema.py`) and database access, one subpackage per domain |
| `proxy/auth/`, `proxy/hooks/` | authentication, path policy and the command floor, and the hook scripts that run inside sessions |
| `proxy/tests/` | the proxy's pytest suite, mirroring the package layout |
| `dashboard/src/` | the React app (`dashboard/android/` is the Capacitor shell) |
| `satellite/` | the remote-machine daemon (`satellite/_vendored/` holds copies of proxy modules) |
| `phone/` | the phone-call service (`audio/` is the speech package it shares with the proxy) |
| `mcps/custom/`, `mcps/skills/` | first-party MCP servers, one folder each, and skill packages installed from the catalog (a runtime install dir, gitignored) |
| `scripts/`, `docker-compose*.yml` | install and operations scripts, and the compose stacks |
| `VERSIONS.md` | every pinned version (runtimes, images, CLIs) |

`ARCHITECTURE.md` lists every directory with its purpose.

## Checks before a commit

Run the checks of every component you touched, from the repository root, and
report a failure with its output instead of calling the work done. A change
in `audio/` checks the proxy, the phone and the audio package. A change in
`mcps/` checks the proxy, and an MCP that carries its own tests
(`mcps/custom/<name>/tests/` or `__tests__/`) runs them too. A change to a
proxy module the satellite vendors (below) checks the satellite too. A
dashboard change ships inside the Android app. The environments each line
needs (`proxy/venv` with the test
extras, `satellite/venv`, `node_modules`) are set up once as
`CONTRIBUTING.md`, "Running the tests", shows, where each command below
appears as written.

```bash
cd proxy && venv/bin/python -m pytest -n 8       # proxy (needs PostgreSQL)
cd audio && ../proxy/venv/bin/python -m pytest   # audio package
cd phone && ../proxy/venv/bin/python -m pytest   # phone service
cd satellite && venv/bin/python -m pytest        # satellite
cd dashboard && npm run build && npx vitest run  # dashboard: tsc, build, tests
proxy/venv/bin/ruff check .                      # lint, the whole tree
cd proxy && venv/bin/mypy                        # proxy types
cd dashboard && npm run lint                     # dashboard lint
cd dashboard && npm run build && npx cap sync android && cd android && ./gradlew assembleDebug testDebugUnitTest
```

- The proxy suite makes its own disposable database (`otodock_test`, one per
  worker) on the PostgreSQL at `127.0.0.1:5432`. `TEST_DATABASE_URL` points
  it elsewhere, and its database name must contain `test`.
- The Android line is needed when `dashboard/android/` changed, and only
  where the person installed JDK 21 and the Android SDK (`ANDROID_HOME`).
  `npx cap sync android` regenerates two tracked files under
  `dashboard/android/`. They change only when the native plugins do, and a
  change there belongs in a commit only then.
- A test that fails, then passes alone, under `-n 8` on a loaded machine is
  worth one serial re-run before you debug it.

## Conventions

- Match the file you edit: naming, comment density, idiom. New code should
  read like the code around it.
- Comments state what the code cannot say (an invariant, an outside quirk,
  why not the obvious way). No narration, no dates, no "changed X to Y".
- Behaviour changes come with tests in the same commit, in the existing
  test module for the area when there is one. Fix the cause of a failing
  test, not the test.
- The proxy's layers point down: `storage` imports nothing above it, and
  `core` and `services` never import `api` or `ws`
  (`proxy/tests/core/test_import_layers.py`).
- A task the proxy starts without keeping it goes through
  `core.background.spawn(coro, name=..., owner=...)`, never a bare
  `asyncio.create_task`.
- Lint and types ratchet: `ruff-baseline.toml` and
  `dashboard/eslint-suppressions.json` froze the old findings, and mypy
  checks the whole proxy (`proxy/mypy.ini`). Fix a new finding, and never
  add to those files.
- Python is type-hinted, and async in the proxy, on the version
  `VERSIONS.md` pins (`PYTHON_VERSION`). The satellite runs on the machines
  it is paired with, down to `PYTHON_MIN_VERSION`: no newer syntax there.
- TypeScript is strict, and `npm run build` (tsc) has zero tolerance.
- Dependencies come from lockfiles (`requirements.in` compiled with
  `uv pip compile` into `requirements.txt`, `package-lock.json`): a new
  dependency is a change to those files, never a package installed into an
  environment by hand. Pinned versions live in `VERSIONS.md`. Dependency
  bumps are the maintainers' work: do not send them.

## Compatibility with running installs

People upgrade installs in place, so what a released install depends on
must keep working:
- Database changes are migrations in `proxy/storage/schema.py` that run
  forward on an existing database. Never assume a fresh one.
- A `config.env` key is never renamed or dropped silently, and a new key is
  documented in `config.env.example`.
- The HTTP and websocket messages the dashboard, the satellite, the phone
  service and the MCPs exchange with the proxy stay compatible.
- A change to the code the satellite runs (a re-vendored module included)
  bumps `SATELLITE_VERSION` in `satellite/config.py`: paired machines update
  to it. `MIN_SATELLITE_VERSION` moves only for a break in the wire
  protocol.

Internal code has no such promise: replace it outright, without a
deprecated twin or a fallback.

## The satellite's vendored modules

`satellite/_vendored/` holds byte-for-byte copies of proxy modules, because
the satellite runs standalone. Edit the proxy source, then run
`scripts/sync-satellite-code.sh`: it copies each module and writes its hash
into `satellite/config.py`, and the satellite refuses to start on a
mismatch. Never edit
a copy. The host OS table is a twin instead: it is defined in both
`proxy/core/host_os.py` and `satellite/config.py` and a test compares them.
Edit the proxy's first and copy the block. `satellite/config.py` imports
nothing beyond the standard library and `satellite/engines.py`. The wire
between the proxy and the satellite is in `satellite/PROTOCOL.md`.

## MCP servers

- A first-party server lives in `mcps/custom/<name>/` with its
  `manifest.json`. Catalog servers belong in OtoDock/community-mcps.
- A tool that works on a scope (a user's or the agent's files, say) writes
  the resolved default scope into its own description or schema: the agent
  cannot read the server's environment.

## Security

- Sessions run as an unprivileged user in a bubblewrap sandbox. Never widen
  a mount, a capability or the network exposure to make something work.
- Changes to authentication, the path policy, the command floor or the
  credential broker come with tests for the refused cases.

## Instruction files

The engines read a repository's instruction file by name: Claude Code reads
`CLAUDE.md`, Codex reads `AGENTS.md`. On the platform the CLIs do not read a
repository's file below their working folder, so each engine names on its
descriptor the files the platform lifts into a session's prompt, its own
first (`repository_instruction_files` in
`proxy/core/layers/<engine>/layer.py`, with 32 KiB shared by every repository
in the person's workspace). In this repository `AGENTS.md` is the
text and `CLAUDE.md` one line that points here. Keep this file well under
16 KiB.

## Commits and pull requests

- `type(area): summary` (`fix(proxy): ...`, `feat(dashboard): ...`), the
  summary imperative and under 72 characters, one logical change per commit.
- A change a user would notice adds a line under `[Unreleased]` in
  `CHANGELOG.md`, written for users: the effect, not the implementation.
- OtoDock is fair source, licensed `FSL-1.1-ALv2` (see `LICENSE`). Never
  describe it as anything but fair source.
