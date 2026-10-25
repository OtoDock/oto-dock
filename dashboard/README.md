# dashboard/ — the OtoDock web dashboard

The React + TypeScript + Tailwind single-page app for everything you do with
OtoDock: live agent chat (streaming tool calls, diffs, approvals, meetings),
agent configuration, scheduled tasks and runs, files and document previews,
usage, and platform administration. It talks to the proxy's REST API and
streams over its WebSockets; in production the proxy serves the built `dist/`.

```bash
npm ci
npm run dev                          # hot-reload dev server (proxy on :8400)
npx tsc --noEmit && npm run build    # type-check + production build
npx vitest run                       # unit tests
npm run lint                         # ESLint
```

`android/` is the Capacitor shell of the Android app around the built
dashboard; its build is in [`CONTRIBUTING.md`](../CONTRIBUTING.md), with every
other suite. How the dashboard fits with the proxy:
[`ARCHITECTURE.md`](../ARCHITECTURE.md). Using and running OtoDock:
[docs.otodock.io](https://docs.otodock.io).
