# Setup — install, first run, engines, voice, remote machines

## Installing

- **Docker Compose** is the standard install: a short installer script writes a `.env`,
  downloads the release-pinned `docker-compose.yml`, and starts the stack. The platform
  serves API + dashboard on port **8400**; `curl http://localhost:8400/health` → `ok`.
- `.env` is the whole deployment config. The keys users actually touch:
  `DASHBOARD_PUBLIC_URL` (the address users browse to — drives cookies, OAuth
  redirects, notification links, and remote-machine pairing), `TRUSTED_PROXY` (reverse
  proxy IP), `TZ`, `PROXY_PORT`, `OTODOCK_MAX_FILE_MB` (upload/sync cap, default 1 GB),
  `OIDC_*` (SSO).
- **Behind a reverse proxy / forced-login gateway**, these paths must skip the login:
  `^/ui-kit/`, `^/v1/webhooks/`, `^/v1/satellite` (remote machines), `/ws/`,
  `^/v1/images/temp/`, `^/v1/twilio/` (Twilio phone), `/favicon.png`.
- **Upgrading** = bump `OTODOCK_VERSION` in `.env`, `docker compose pull && up -d`.
  Always back up first (the platform ships backup/restore scripts).
- RAM guide: each concurrent Claude/Codex session grows to ~1 GB; 8 GB ≈ 5–6 sessions,
  16 GB ≈ 10–12. The platform admits sessions based on live free memory.

## First run

One setup screen: create the **administrator account** (display name, email,
password) → land on **Setup** to connect AI engines. Every user without a personal
engine sees an amber "Connect an AI engine" banner until one is connected. A fresh
install ships one ready agent (the Personal Assistant); admins/creators add the rest.

## Connecting AI engines

Two places, same platform:

- **User Settings → AI Engines** *(any user)* — connect a personal **Claude Pro/Max**
  (popup + paste authorization code) or **ChatGPT** (device-code sign-in) subscription.
  Multiple accounts per provider are fine; each row has a **Personal use** toggle, and
  admins additionally get **Agent pool** (contribute the account to the platform pool).
  The popup signs in with the browser's platform.claude.com session, so to add a second
  Claude account sign out there first or use a private window — a code for an account
  already connected only refreshes it and the form says so; a refused paste offers Try
  again.
- **Setup → AI Engines** *(admin)* — platform-level subscriptions, **API keys**
  (Anthropic, OpenAI, Groq), and **local models** (Ollama or any OpenAI-compatible
  endpoint by URL), plus per-provider model lists and pricing.

Each engine has its own default model, chosen by tier, which an agent on **Auto** runs:
Claude Opus 5.5 on Claude Code, GPT-6 Sol on Codex, Sonnet 5 on Direct LLM. Every model
list shows its tier. Since 1.7 Claude Opus 5.5 replaces Opus 5, and GPT-6 Sol and GPT-6
Luna replace their GPT-5.6 versions; pins on the old ids move over at the next start.

Routing rule: a user's own chats/tasks run on **their own** connections first; anything
that belongs to an *agent* (agent-scope tasks, shared agents, meetings, triggers, phone
calls) runs on the **platform pool** admins contribute to. A user with nothing connected
can be granted **Platform Auth** (Admin → Users, per-user toggle, off by default) to
borrow platform **API-key or local** connections — never anyone's personal OAuth
subscription.

Login health: the platform pre-warns 72 h and 24 h before a Claude/ChatGPT login
expires and flags dead logins with a **Reconnect** button on the account row.

## Voice & phone (optional add-on)

The `otodock-phone` service ships as a compose overlay
(`COMPOSE_FILE=docker-compose.yml:docker-compose.phone.yml`). What needs what:

| Capability | Needs phone service? | Needs Twilio/PBX? |
| --- | --- | --- |
| Dictation + read-aloud | no | no |
| Live voice conversations + wake word | yes | no |
| Real phone calls (in/out) | yes | yes |

- Speech providers: **Setup → Audio** *(admin)* — STT: Deepgram, ElevenLabs; TTS:
  Cartesia, ElevenLabs; per-language voice IDs; a chat-audio policy (Native only /
  Native preferred / User choice).
- Phone: **Setup → Phone** *(admin)* — add a phone server (**Twilio**: paste account
  SID + auth token, calls enter via the public dashboard URL, no extra ports; or
  **FreePBX/Asterisk**: one-time dialplan bootstrap, PBX must reach TCP 9092/9093),
  click **Verify**, then create **routes** (agent + language + number, optional
  greeting, PIN access code, per-route call log via the clock icon). Each route
  sets a **Caller identity**: **Per caller** (default; every phone number gets its
  own private files and memory; callers read the agent's shared folders as
  viewers and change nothing; a **Remember callers** toggle; callers never get
  platform tools or a shell) or **A platform user** (the call runs as that user's own
  session, capped at manager; require a PIN on an inbound line). Caller files,
  phone conversations and the call log age out together under **Setup → Phone →
  Caller Data** (90 days by default; **Forget all caller data now** is permanent).
- Wake word ("Hey OtoDock") is per-user opt-in: **User Settings → General → Wake word**,
  off by default, on-device detection.
- Dictation language: hold the chat's microphone button to pick it, or set it under
  **User Settings → Audio**.

## Remote machines

Run agents natively on a user's own laptop/workstation/server — no sandbox, driven from
the same dashboard. Pairing is outbound-only (no open ports) and needs
`DASHBOARD_PUBLIC_URL` reachable from the machine.

- **User-paired** *(any role)*: **User Settings → Remote Machines → Pair Machine** —
  name it, optionally allow full filesystem access, run the one-line install command
  (Linux/macOS/Windows; token single-use, 1 h) on the machine, then tick **"Run these
  agents on this machine"**. Personal: only the owner's sessions run there.
- **Admin-paired** *(admin)*: **Admin → Remote Machines → Pair New Machine** — platform
  machines that can be an agent's default **Execution Target** (set in Agent Settings →
  Configuration, admin only).
- **Access levels** (changeable live on the machine card): Home-only (default) vs full
  filesystem; plus three separate **Device control** grants, all off by default —
  computer control (mouse/keyboard/screen), browser control, app connectors. Browser
  control has a **Browser mode**: **Dedicated profile** (default; a persistent,
  logged-in browser profile per agent) or **My own browser** (the agent works in a
  tab group inside the Chrome/Edge/Brave the machine's user is signed into, through
  the Playwright Extension; paste the extension's token on the card so unattended
  sessions such as tasks connect without a click). Only the machine's owner can
  switch a user-paired machine to its own browser.
- Files sync **both ways** automatically; generated build directories are auto-skipped;
  conflicts and deletes land in the workspace **Recover bin** (7 days). Machine cards
  show live status, sessions/capacity, and the CLI versions the machine runs
  (highlighted on drift).
- Kill switch: **Setup → System Settings → "Allow users to pair their own remote
  machines"** *(admin)*; offline-fallback behavior lives next to it. An admin can also
  remove any one user's machine from **Admin → Remote Machines**; its owner is told.

## When something's missing

If a user can't see a tab or button you named: (1) their role is below the gate, (2) the
feature is disabled on this install (remote machines, interactive terminals, chat
audio), or (3) the add-on isn't deployed (phone service). Say which one it likely is and
who can change it.
