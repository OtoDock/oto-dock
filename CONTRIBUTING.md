# Contributing to OtoDock

Thanks for wanting to make OtoDock better! This page gets you from clone to
green tests, and explains where different kinds of contributions live.

## Where things go

- **Platform code** (proxy, dashboard, Android app, satellite, phone
  service, first-party MCPs, audio): this repo. [ARCHITECTURE.md](ARCHITECTURE.md)
  maps it, and [AGENTS.md](AGENTS.md) holds the instructions for coding agents
  (`CLAUDE.md` loads it).
- **New agents for the catalog**: [OtoDock/community-agents](https://github.com/OtoDock/community-agents).
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
feeds it the version pins from [`VERSIONS.md`](VERSIONS.md), so source builds
stay in lockstep with releases.

**Bare metal (fastest iteration, Debian/Ubuntu):**

```bash
git clone https://github.com/OtoDock/oto-dock.git && cd oto-dock
scripts/dev-setup.sh
# then, in two shells:
cd proxy && venv/bin/python app.py     # backend on :8400
cd dashboard && npm run dev            # hot-reload frontend
```

`dev-setup.sh` installs the pinned toolchain (Python, Node, uv, pnpm,
bubblewrap), builds `proxy/venv`, starts a Postgres container on
`127.0.0.1:5432`, and builds the dashboard once. Add `--service` to install a
systemd unit instead of running the proxy by hand. Re-running it after a pull
rebuilds `proxy/venv` when the pinned Python moved (a patch release included)
and recreates the Postgres container when the pinned Postgres moved (its data
kept), so stop the proxy first: `dev-setup.sh` refuses to run while the
`otodock-proxy` unit is active, and a proxy run in the foreground is yours to
stop.

## Running the tests

Every command below runs from the repository root. The bare-metal setup
builds `proxy/venv` with the proxy's dependencies and the audio package. The
proxy, audio and phone suites and the lint tools share it once the test
extras are in:

```bash
proxy/venv/bin/python -m pip install -r proxy/requirements-test.txt   # once: pytest and its plugins
```

In another Python environment, install `proxy/requirements.txt`, `./audio`
and the test extras into it and use its interpreter in place of
`venv/bin/python`.

**Proxy**: [`proxy/tests/README.md`](proxy/tests/README.md) has more detail.
The short version:

```bash
cd proxy && venv/bin/python -m pytest -n 8          # the whole suite in parallel (recommended), about 1.5 min
cd proxy && venv/bin/python -m pytest tests/auth    # one area
```

The suite needs a reachable PostgreSQL (the dev stack's container on
`127.0.0.1:5432` works out of the box) and creates a disposable
`otodock_test` database. Point `TEST_DATABASE_URL` elsewhere if your Postgres
lives somewhere else (the database name must contain `test`).

**Audio package** (installed into the proxy's environment, with its tests
colocated with each subpackage and *not* collected by the proxy run):

```bash
cd audio && ../proxy/venv/bin/python -m pytest
```

**Phone service** (borrows the proxy's environment and needs no database and
no phone line):

```bash
cd phone && ../proxy/venv/bin/python -m pytest
```

**Satellite** (its own environment: the daemon runs on Python 3.10 and
newer, on the machines it is paired with):

```bash
python3 -m venv satellite/venv && satellite/venv/bin/pip install -r satellite/requirements.txt -r proxy/requirements-test.txt   # once
cd satellite && venv/bin/python -m pytest
```

**Dashboard:**

```bash
cd dashboard && npm ci                            # once
cd dashboard && npm run build && npx vitest run   # type-check + production build, then the unit tests
```

**Android app** (the Capacitor shell in `dashboard/android/`, around the
built dashboard). It needs JDK 21 and the Android SDK with `ANDROID_HOME`
set. It needs no Firebase file and no signing values (the debug build runs
without push notifications, and only release packaging is signed):

```bash
cd dashboard && npm run build && npx cap sync android && cd android && ./gradlew assembleDebug testDebugUnitTest
```

**Lint and types** (CI runs all three, and the required `proxy` check runs
only ruff's import and dead-code rules):

```bash
proxy/venv/bin/python -m pip install "ruff==0.15.22" "mypy==2.4.0"   # once, into the proxy's environment
proxy/venv/bin/ruff check .   # the whole tree
cd proxy && venv/bin/mypy     # the whole proxy (proxy/mypy.ini)
cd dashboard && npm run lint  # ESLint
```

## Code style

- Match the code around you: naming, comment density, idiom. Both codebases
  lean on plain, readable code over cleverness.
- Comments explain constraints the code can't show, not what the next line
  does.
- Many comments and docstrings name a design page by an upper-case file
  name and a section in quotes. Those are the maintainers' internal pages
  and are not part of this repository: read the code and the
  [public docs](https://docs.otodock.io) instead. Don't add a reference to a
  page a reader cannot open. Point at the code, a file in this repository or
  a docs.otodock.io page (new references to internal pages are refused at
  release).
- Behavior changes come with tests in the same PR. The proxy suite is fast
  and parallel, so there's no reason to skip it.
- Lint and types ratchet. ruff froze the findings the code had when it
  was adopted (`ruff-baseline.toml`). ESLint's frozen findings are all fixed
  (`dashboard/eslint-suppressions.json` is empty), and mypy checks the whole
  proxy with none left (`proxy/mypy.ini`), so a NEW finding fails. Fix it
  rather than suppress it, and never add to those files by hand. A
  `# type: ignore[<code>]  # <reason>` is only for a library's typing gap.
- The dashboard writes to the console only through `src/lib/debug/log.ts`:
  `debugLog` / `debugWarn` for a breadcrumb (printed in a development build
  or with `?debug=1`), `logError` for a failure that must never pass unseen.
  ESLint refuses `console.*` anywhere else.
- Dashboard hooks pass the React compiler's lint rules without a new
  `eslint-disable`: a latest-value ref is written in `useLayoutEffect`,
  never in render or inside a state updater; a callback an effect
  registers once is a `useEffectEvent`; state derived from a prop or reset
  on a key change is adjusted in render (keep the source in state and set
  both when it changes), not in an effect; a value from outside React (an
  element's box, a media query) is a `useSyncExternalStore` store; and a
  hook's unstable return object is destructured, its members listed in
  dependency arrays, never the object.
- A dashboard API call goes through `apiJson` / `apiOk`
  (`src/lib/http/apiJson.ts`): the body, or an `ApiError` whose message is
  the server's sentence; `withMessage` / `withFallback` word a refusal,
  `errorText(e)` reads a caught value. Raw `apiFetch` with `if (!res.ok)`
  stays only where the refusal is read another way (a raw body, a custom
  error class, a status branch) or before a session exists.
- Python logs with lazy arguments (`logger.info("Saved %s", path)`, never an
  f-string) and `logger.exception(...)` inside an `except`; a `raise` inside
  an `except` names its cause (`raise X(...) from exc`); `zip()` states
  `strict=`. ruff's import sort keeps `from x import a as b, c` on one
  statement; an import block whose order matters is held with
  `# isort: split` and a one-line reason.
- A task the proxy, the satellite or the phone daemon starts without
  keeping a reference goes through that tree's
  `background.spawn(coro, name=..., owner=...)` (`proxy/core/background.py`,
  `satellite/background.py`, `phone/background.py`), never a bare
  `asyncio.create_task(...)` (ruff's `RUF006` and a proxy test refuse one).
- An `except` names what its body raises. A broad `except Exception`
  stays only at a boundary (a task's last line, a handler that must
  answer, a cleanup, a best-effort step, an SDK with no error set, a
  thread target) and says why on its line:
  `except Exception:  # noqa: BLE001  # <reason>`. In an `async def`, a
  call that blocks goes through `asyncio.to_thread`, a cheap or bounded
  one keeps `# noqa: ASYNC2xx  # <reason>`, and a sleep loop that
  something can wake waits on an `asyncio.Event`.
- The proxy's layers point down: `storage` imports nothing above it, and
  `core` and `services` never import `api` or `ws`
  (`proxy/tests/core/test_import_layers.py`).
- The satellite runs byte-for-byte copies of a few proxy modules
  (`satellite/_vendored/`, pinned by hash in `satellite/config.py`). After
  changing one of their proxy sources, run `scripts/sync-satellite-code.sh`
  and the satellite suite. Never edit a copy.
- A file copied from another project keeps its own licence and copyright:
  give it an annotation in [`REUSE.toml`](REUSE.toml) (`reuse lint` runs in
  CI).
- Commit messages follow `type(area): summary` (`fix(proxy): …`,
  `feat(dashboard): …`, `docs(repo): …`).

## Pull requests

- Keep PRs focused: one concern per PR reviews quickly.
- CI runs automatically on every PR. The `proxy` (with the audio suite),
  `dashboard`, `satellite` and `phone` checks must be green: merging is
  blocked until they are. Lint and types, the Android build and the
  security scans report on the PR without blocking it, and a maintainer
  reads them too.
- If you're planning something large, open an issue first so we can agree on
  the shape before you invest the time.
- **Dependency bumps**: please don't send them. Pins are managed internally
  and swept as a batch before each release, so a bump PR here can't be merged.
  If you spot an advisory in one of our pins, open an issue instead. That's
  genuinely useful.
- **AI-assisted contributions** are welcome, and much of OtoDock is built
  that way. The bar is the same as for any PR: you understand and stand behind
  every line you submit, tests ride along, and CI is green. Please don't
  open large generated PRs you haven't reviewed yourself.

## How your PR gets merged

This repository is a curated export of OtoDock's internal development tree,
and its history is release-sized on purpose (see the
[CHANGELOG](CHANGELOG.md)). For pull requests that means one extra step you
don't have to do anything for:

1. CI validates your PR and a maintainer reviews it.
2. Once accepted, your commits are applied to the internal tree first, with
   **you as the commit author**, where they run through the full internal
   gates alongside the components that aren't public yet.
3. Your PR is then merged here, and the change ships (and is credited) in the
   next release.

A short gap between approval and merge is that porting step, not a stall.

## Licensing of contributions

OtoDock is fair source, licensed
[FSL-1.1-ALv2](LICENSE) (each release converts to Apache 2.0 after two
years). By contributing, you agree your contribution is licensed under the
same terms: standard inbound = outbound, no CLA to sign. You also agree we
may make that Apache 2.0 conversion earlier, for the whole project, at any
time.
