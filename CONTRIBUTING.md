# Contributing to OtoDock

Thanks for wanting to make OtoDock better! This page gets you from clone to
green tests, and explains where different kinds of contributions live.

## Where things go

- **Platform code** (proxy, dashboard, first-party MCPs, audio) — this repo.
- **New agents for the catalog** — [OtoDock/community-agents](https://github.com/OtoDock/community-agents).
- **New MCP tool servers for the catalog** — [OtoDock/community-mcps](https://github.com/OtoDock/community-mcps).
- **Bugs & ideas** — GitHub issues here. For anything security-sensitive,
  use [SECURITY.md](SECURITY.md) instead of a public issue.

## Dev setup

Two good options:

**Containers (closest to production):**

```bash
git clone https://github.com/OtoDock/oto-dock.git && cd oto-dock
printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)" > config.env
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
systemd unit instead of running the proxy by hand.

## Running the tests

**Proxy** — [`proxy/tests/README.md`](proxy/tests/README.md) is the canonical
reference; the short version:

```bash
cd proxy
python -m pip install -r requirements.txt -r requirements-test.txt

pytest -n 8        # whole suite, in parallel (recommended) — ~1.5 min
pytest tests/auth  # one area
```

The suite needs a reachable PostgreSQL (the dev stack's container on
`127.0.0.1:5432` works out of the box) and creates a disposable
`otodock_test` database; point `TEST_DATABASE_URL` elsewhere if your Postgres
lives somewhere else.

**Audio package** (shares the proxy's Python env; its tests are colocated
with each subpackage and are *not* collected by the proxy run):

```bash
python -m pip install ./audio   # from the repo root, once
cd audio && pytest
```

**Dashboard:**

```bash
cd dashboard
npm ci
npx tsc --noEmit && npm run build   # type-check + production build
npx vitest run                      # unit tests
```

## Code style

- Match the code around you — naming, comment density, idiom. Both codebases
  lean on plain, readable code over cleverness.
- Comments explain constraints the code can't show, not what the next line
  does.
- Behavior changes come with tests in the same PR. The proxy suite is fast
  and parallel — there's no reason to skip it.
- Commit messages follow `type(area): summary` (`fix(proxy): …`,
  `feat(dashboard): …`, `docs(repo): …`).

## Pull requests

- Keep PRs focused — one concern per PR reviews quickly.
- CI runs automatically on every PR (proxy + audio suites against a
  disposable Postgres, dashboard type-check/build/tests) and must be green —
  merging is blocked until it is.
- If you're planning something large, open an issue first so we can agree on
  the shape before you invest the time.
- **AI-assisted contributions** are welcome — much of OtoDock is built that
  way. The bar is the same as for any PR: you understand and stand behind
  every line you submit, tests ride along, and CI is green. Please don't
  open large generated PRs you haven't reviewed yourself.

## How your PR gets merged

This repository is a curated export of OtoDock's internal development tree,
and its history is release-sized on purpose (see the
[CHANGELOG](CHANGELOG.md)). For pull requests that means one extra step you
don't have to do anything for:

1. CI validates your PR and a maintainer reviews it.
2. Once accepted, your commits are applied to the internal tree first — with
   **you as the commit author** — where they run through the full internal
   gates alongside the components that aren't public yet.
3. Your PR is then merged here, and the change ships (and is credited) in the
   next release.

A short gap between approval and merge is that porting step, not a stall.

## Licensing of contributions

OtoDock is fair source, licensed
[FSL-1.1-Apache-2.0](LICENSE) (each release converts to Apache 2.0 after two
years). By contributing, you agree your contribution is licensed under the
same terms — standard inbound = outbound, no CLA to sign.
