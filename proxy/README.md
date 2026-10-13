# proxy/ — the OtoDock platform core

A FastAPI service that is the heart of the platform: it owns agent sessions
(spawning real Claude Code / Codex processes inside per-session kernel
sandboxes), the security model (auth, roles, path policy, permission
prompts), scheduling and triggers, MCP tool wiring, file/document handling,
and the WebSocket hub the dashboard streams from. PostgreSQL is its only
required backing service.

- Entry point: `app.py` (serves the API, the WebSockets, and the built
  dashboard).
- Tests: `tests/` — see [`tests/README.md`](tests/README.md) for the
  canonical commands (`pytest -n 8` + a reachable Postgres).
- Configuration: generated `config.env` at the repo root;
  [`../config.env.example`](../config.env.example) documents every knob.

How the proxy fits with the rest of the platform, with the paths of its
core pieces: [`ARCHITECTURE.md`](../ARCHITECTURE.md). Every suite and the
dev setup: [`CONTRIBUTING.md`](../CONTRIBUTING.md). Using and running
OtoDock: [docs.otodock.io](https://docs.otodock.io).
