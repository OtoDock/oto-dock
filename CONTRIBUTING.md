# Contributing to OtoDock

Thanks for wanting to make OtoDock better! This page gets you from clone to
green tests, and explains where different kinds of contributions live.

## Where things go

- **Platform code** (proxy, dashboard, the Android and iOS apps, satellite,
  phone service, first-party MCPs, audio): this repo. [ARCHITECTURE.md](ARCHITECTURE.md)
  maps it, and [AGENTS.md](AGENTS.md) holds the instructions for coding agents
  (`CLAUDE.md` loads it).
- **New agents and department sets for the catalog**: [OtoDock/community-agents](https://github.com/OtoDock/community-agents).
- **New MCP tool servers for the catalog**: [OtoDock/community-mcps](https://github.com/OtoDock/community-mcps).
- **New Agent Skills for the catalog**: [OtoDock/community-skills](https://github.com/OtoDock/community-skills).
- **Bugs and ideas**: GitHub issues here. The issue forms ask for what a
  maintainer needs first (version, install kind, logs). Questions go to
  [Discussions](https://github.com/OtoDock/oto-dock/discussions/categories/q-a).
  For anything security-sensitive, use [SECURITY.md](SECURITY.md) instead
  of a public issue.

## Dev setup

Two good options:

**Containers (closest to production):**

```bash
git clone https://github.com/OtoDock/oto-dock.git && cd oto-dock
(umask 077; printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)" > config.env)
scripts/compose.sh up -d --build
```

`scripts/compose.sh` stacks the build overlay on the base compose file and
feeds it the version pins from [`VERSIONS.md`](VERSIONS.md). Source builds
then stay in lockstep with releases.

**Bare metal (fastest iteration, Debian/Ubuntu):**

```bash
git clone https://github.com/OtoDock/oto-dock.git && cd oto-dock
scripts/dev-setup.sh
# then, in two shells:
cd proxy && venv/bin/python app.py     # backend on :8400
cd dashboard && npm run dev            # hot-reload frontend
```

`dev-setup.sh` does four things:

- It installs the pinned toolchain: Python, Node, uv, pnpm and bubblewrap.
- It builds `proxy/venv`.
- It starts a Postgres container on `127.0.0.1:5432`.
- It builds the dashboard once.

Add `--service` to install a systemd unit instead of running the proxy by
hand.

When you run it again after a pull, it rebuilds `proxy/venv` if the pinned
Python moved, a patch release included. It also recreates the Postgres
container if the pinned Postgres moved, and keeps its data. So stop the proxy
first. `dev-setup.sh` refuses to run while the `otodock-proxy` unit is
active, and a proxy that runs in the foreground is yours to stop.

## Running the tests

Every command below runs from the repository root. The bare-metal setup
builds `proxy/venv` with the proxy's dependencies and the audio package. The
proxy, audio and phone suites and the lint tools share it once the test
extras are in:

```bash
proxy/venv/bin/python -m pip install -r proxy/requirements-test.txt   # once: pytest and its plugins
```

In another Python environment, install `proxy/requirements.txt`, `./audio`
and the test extras into it. Then use its interpreter in place of
`venv/bin/python`.

**Proxy**: [`proxy/tests/README.md`](proxy/tests/README.md) has more detail.
The short version:

```bash
cd proxy && venv/bin/python -m pytest -n 8          # the whole suite in parallel (recommended), about 6 min on ten cores
cd proxy && venv/bin/python -m pytest tests/auth    # one area
```

The suite needs a PostgreSQL it can reach, and creates a disposable
`otodock_test` database there. The dev stack's container on `127.0.0.1:5432`
works out of the box. If your Postgres lives somewhere else, point
`TEST_DATABASE_URL` at it. The database name must contain `test`.

**Audio package.** It is installed into the proxy's environment. Its tests
sit beside each subpackage, and the proxy run does *not* collect them:

```bash
cd audio && ../proxy/venv/bin/python -m pytest
```

**Phone service.** It borrows the proxy's environment, and needs no database
and no phone line:

```bash
cd phone && ../proxy/venv/bin/python -m pytest
```

**Satellite.** It has its own environment: the daemon runs on Python 3.10
and newer, on the machines it is paired with.

```bash
python3 -m venv satellite/venv && satellite/venv/bin/pip install -r satellite/requirements.txt -r proxy/requirements-test.txt   # once
cd satellite && venv/bin/python -m pytest
```

**Dashboard:**

```bash
cd dashboard && npm ci                            # once
cd dashboard && npm run build && npx vitest run   # type-check + production build, then the unit tests
```

**Android app.** This is the Capacitor shell in `dashboard/android/`, around
the built dashboard. It needs JDK 21 and the Android SDK, with `ANDROID_HOME`
set. It needs no Firebase file and no signing values. The debug build runs
without push notifications, and only release packaging is signed:

```bash
cd dashboard && npm run build && npx cap sync android && cd android && ./gradlew assembleDebug testDebugUnitTest
```

**iOS app.** This is the Capacitor shell in `dashboard/ios/`, around the
built dashboard. It needs a Mac with Xcode 16 or later. It needs no Firebase
file and no signing values. A Debug build runs without push notifications,
and only an archive for TestFlight is signed:

```bash
cd dashboard && npm run build && npx cap sync ios
cd dashboard/ios/OtoDockShell && swift test                             # the shell's rules
cd dashboard/ios/App && xcodebuild -project App.xcodeproj -scheme App -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build
```

**Lint and types.** CI runs all three, and the `proxy` job also runs ruff's
import and dead-code rules:

```bash
proxy/venv/bin/python -m pip install "ruff==0.15.22" "mypy==2.4.0"   # once, into the proxy's environment
proxy/venv/bin/ruff check .   # the whole tree
cd proxy && venv/bin/mypy     # the whole proxy (proxy/mypy.ini)
cd dashboard && npm run lint  # ESLint
python3 scripts/prose/prose_check.py .   # the writing rules (WRITING.md)
```

## Code style

In every part of the repository:

- Match the code around you: naming, comment density, idiom. Both codebases
  lean on plain, readable code over cleverness.
- Comments explain constraints the code can't show, not what the next line
  does.
- A comment that points somewhere points at the code, a file in this
  repository or a [docs.otodock.io](https://docs.otodock.io) page. Never at
  a page a reader cannot open.
- Behavior changes come with tests in the same PR. The proxy suite is fast
  and parallel, so there's no reason to skip it.
- A file copied from another project keeps its own licence and copyright.
  Give it an annotation in [`REUSE.toml`](REUSE.toml): `reuse lint` runs in
  CI.
- Commit messages follow `type(area): summary` (`fix(proxy): …`,
  `feat(dashboard): …`, `docs(repo): …`).

Lint and types only get stricter. A new finding fails, so fix it rather than
suppress it. Never add to these three files by hand:

- `ruff-baseline.toml` holds the findings the code had when ruff was adopted.
- `dashboard/eslint-suppressions.json` is empty: ESLint's frozen findings are
  all fixed.
- `proxy/mypy.ini` has mypy check the whole proxy, with no finding left.

A `# type: ignore[<code>]  # <reason>` is only for a library's typing gap.

In the dashboard:

- The dashboard writes to the console only through `src/lib/debug/log.ts`.
  `debugLog` and `debugWarn` leave a breadcrumb, printed in a development
  build or with `?debug=1`. `logError` is for a failure that must never pass
  unseen. ESLint refuses `console.*` anywhere else.
- Hooks pass the React compiler's lint rules without a new `eslint-disable`:
  - Write a latest-value ref in `useLayoutEffect`, never in render or inside
    a state updater.
  - A callback that an effect registers once is a `useEffectEvent`.
  - Adjust state that derives from a prop, or resets on a key change, in
    render and not in an effect. Keep the source in state and set both when
    it changes.
  - A value from outside React, such as an element's box or a media query,
    is a `useSyncExternalStore` store.
  - Destructure a hook's unstable return object. List its members in
    dependency arrays, never the object.
- An API call goes through `apiJson` or `apiOk`, in
  `src/lib/http/apiJson.ts`. It gives the body, or throws an `ApiError` whose
  message is the server's sentence. `withMessage` and `withFallback` word a
  refusal, and `errorText(e)` reads a caught value.
- Raw `apiFetch` with `if (!res.ok)` stays in two cases only. One is before a
  session exists. The other is where the refusal is read another way: a raw
  body, a custom error class or a status branch.

In Python:

- Log with lazy arguments: `logger.info("Saved %s", path)`, never an
  f-string. Inside an `except`, log with `logger.exception(...)`.
- A `raise` inside an `except` names its cause: `raise X(...) from exc`.
  `zip()` states `strict=`.
- ruff's import sort keeps `from x import a as b, c` on one statement. An
  import block whose order matters is held with `# isort: split` and a
  one-line reason.
- A task that the proxy, the satellite or the phone daemon starts without
  keeping a reference goes through that tree's
  `background.spawn(coro, name=..., owner=...)`. The three are
  `proxy/core/background.py`, `satellite/background.py` and
  `phone/background.py`. Never use a bare `asyncio.create_task(...)`: ruff's
  `RUF006` and a proxy test refuse one.
- An `except` names what its body raises. A broad `except Exception` stays
  only at a boundary, and says why on its line:
  `except Exception:  # noqa: BLE001  # <reason>`. A boundary is a task's
  last line, a handler that must answer, a cleanup, a best-effort step, an
  SDK with no error set, or a thread target.
- In an `async def`, a call that blocks goes through `asyncio.to_thread`. A
  cheap or bounded one keeps `# noqa: ASYNC2xx  # <reason>`. A sleep loop
  that something can wake waits on an `asyncio.Event`.
- The proxy's layers point down. `storage` imports nothing above it, and
  `core` and `services` never import `api` or `ws`.
  `proxy/tests/core/test_import_layers.py` holds that.
- When `proxy/tests/core/test_agent_tree_surface.py` fails, open, write or
  remove the file through the helper of `proxy/services/infra/safe_fs.py`
  that its message names. A call on the server's own data instead gets the
  row the message prints, with its reason.
- The satellite runs byte-for-byte copies of a few proxy modules
  (`satellite/_vendored/`, pinned by hash in `satellite/config.py`). After
  changing one of their proxy sources, run `scripts/sync-satellite-code.sh`
  and the satellite suite. Never edit a copy.

## Pull requests

- Keep PRs focused: one concern per PR reviews quickly.
- CI runs automatically on every PR. Every check must be green before a
  merge: the test suites, lint and types, the Android build and the security
  scans.
- Planning something large? Open an issue first, so we can agree on the
  shape before you invest the time.
- **Dependency bumps**: please don't send them. Pins are managed internally
  and swept as a batch before each release, so a bump PR here can't be merged.
  If you spot an advisory in one of our pins, open an issue instead. That's
  genuinely useful.
- **AI-assisted contributions** are welcome, and much of OtoDock is built
  that way. The bar is the same as for any PR. You understand and stand
  behind every line you submit, tests ride along, and CI is green. Please
  don't open large generated PRs you haven't reviewed yourself.

## How your PR gets merged

This repository is a curated export of OtoDock's internal development tree.
Its history is release-sized on purpose, as the [CHANGELOG](CHANGELOG.md)
shows. For pull requests that means one extra step, and you don't have to do
anything for it:

1. CI validates your PR and a maintainer reviews it.
2. Once accepted, your commits are applied to the internal tree first, with
   **you as the commit author**. There they run through the full internal
   gates, alongside the components that aren't public yet. Your name is
   added to [CONTRIBUTORS.md](CONTRIBUTORS.md) in the same step.
3. Your PR is then merged here. The change ships in the next release, and is
   credited there.

A short gap between approval and merge is that porting step, not a stall.

## The contributor agreement

OtoDock asks every contributor to accept a contributor agreement,
[CLA.md](CLA.md). In short:

1. **You keep your copyright, and OtoDock gets a licence.** We can include
   your contribution in OtoDock, change it, ship it, run it in a hosted
   service, sell licences on it and relicense it, as part of OtoDock. We do
   not need to ask you again.
2. **We promise something back.** Every published version of OtoDock that
   contains your contribution stays available under the licence OtoDock had
   when you sent it, or under one that gives people more rights. Today that
   is FSL-1.1-ALv2, which turns into Apache-2.0 two years after each release.
3. **You get credit.** Your name goes in [CONTRIBUTORS.md](CONTRIBUTORS.md)
   and in the release notes of the release that ships your work. Tell us if
   you prefer another name or no name.
4. **You confirm the work is yours to give**, to the best of your knowledge.
5. **You accept by ticking a line** in the pull request template. A check on
   the pull request stays red until a line is ticked. The agreement covers
   each contribution as you send it, and nothing you sent before.
6. **Two cases need a signature.** If you live in Greece and your change is
   above 1,000 lines, or a new component, engine or integration, you sign a
   copy through gov.gr. We send it before we merge. If you contribute as
   part of your job, write to legal@otodock.io and your employer signs the
   company part once.

[CLA.md](CLA.md) has the rest: the patent licence, your moral rights, payment
for work that is not code, liability, the record we keep and the governing
law. Read it before you tick the line.
