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
```

Developer docs live at [docs.otodock.io](https://docs.otodock.io).
