# Proxy test suite

Fast, parallel-safe PostgreSQL-backed tests for the OtoDock proxy.

## Requirements

- Python 3.13 (`PYTHON_VERSION` in VERSIONS.md — the proxy's interpreter; the 3.10 floor is the satellite's) with the proxy dependencies plus the test extras:

  ```bash
  python -m pip install -r requirements.txt -r requirements-test.txt
  ```

- A reachable **PostgreSQL** server. The dev stack already runs one in Docker
  (`otodock-postgres` on `127.0.0.1:5432`, see `scripts/dev-setup.sh`); any
  Postgres works.

## The test database (never your real data)

`conftest.py` owns database setup and **never touches the real `otodock`
database**:

- It connects to a *separate, disposable* database named `otodock_test`
  (override with `TEST_DATABASE_URL`), creating it automatically if missing.
- Under `pytest-xdist` each worker gets its own database — `otodock_test_gw0`,
  `otodock_test_gw1`, … — so parallel workers never share rows.
- Every test starts from a clean slate: the schema is (idempotently) applied
  and all rows are wiped. The wipe uses `DELETE` (not `TRUNCATE`) to avoid a
  per-table `fsync`, which is what previously made the suite take an hour.
- The test databases are throwaway; drop them any time with
  `DROP DATABASE otodock_test*`.

Default connection: `postgresql://otodock:otodock@localhost:5432/otodock_test`.
Point elsewhere with `TEST_DATABASE_URL` (or `DATABASE_URL`).

## Running

```bash
# whole suite, in parallel (recommended) — ~1.5 min
pytest -n 8

# whole suite, serially
pytest

# one area / file / test
pytest tests/auth
pytest tests/auth/test_oauth_engine.py
pytest tests/auth/test_oauth_engine.py::test_pkce_happy_path

# skip tests marked slow
pytest -m "not slow"
```

`pytest.ini` sets a 120s per-test timeout (via `pytest-timeout`) so a single
wedged test can never hang the whole run; it auto-disables under a debugger.

Keep worker count such that `workers × DB_POOL_MAX_SIZE` stays under the
server's `max_connections` (default 100). Example: `-n 8` with
`DB_POOL_MAX_SIZE=6`.

## Layout

Tests live in per-area subpackages under `tests/`:

```
mcp/ auth/ remote/ session/ execution/ agents/ tasks/ meetings/
phone/ audio/ billing/ media/ storage/ core/ api/
```

`loadtest/` is the load-test harness: skipped unless `OTODOCK_LOADTEST=1`,
never part of the normal run; it runs alone, on a quiet box, before a release.

Shared helpers stay at the root: `conftest.py` (fixtures + DB setup),
`fixtures/`, and `_paths.py` (filesystem anchors — import `PROXY_DIR`,
`REPO_ROOT`, `CUSTOM_MCPS` from here instead of computing `__file__`-relative
paths, so a test can move between folders freely).

## CI

CI runs the same commands against a PostgreSQL service container. Because the
per-test wipe uses `DELETE`, no special server tuning (`fsync=off`, etc.) is
required for the suite to be fast.

The public `ci.yml` runs the proxy and audio suites twice: the `proxy` job on
`ubuntu-latest` (a required check) and the `proxy-arm64` job on
`ubuntu-24.04-arm` (the same steps on a GitHub-hosted arm64 runner; not a
required check until it has a history of green runs — add it to the branch
protection then). Both install the audio package with its `localmodels` extra,
so the bundled Silero VAD and Smart Turn models run for real there instead of
skipping; the private `ci-private-python.yml` does the same on amd64 only
(arm64 runners are billed on private repositories). Locally, the release gate
runs the audio suite in `phone/venv` for the same reason.

The `satellite` and `phone` jobs run those two suites in each service's own
environment (its lockfile plus the test tools from `proxy/requirements-test.txt`),
the way a paired machine and the phone image install them. The private
workflow runs all four Python suites in one environment instead.
