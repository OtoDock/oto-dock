# Setup — install, first run, engines, voice, remote machines

## Installing

- **Docker Compose** is the standard install: a short installer script writes a `.env`,
  downloads the release-pinned `docker-compose.yml`, and starts the stack. The platform
  serves API + dashboard on port **8400**; `curl http://localhost:8400/health` → `ok`.
- `.env` is the whole deployment config. The keys users actually touch:
  `DASHBOARD_PUBLIC_URL` (the address users browse to — drives cookies, OAuth
  redirects, notification links, and remote-machine pairing; connecting an account works
  only from the dashboard at this address), `TRUSTED_PROXY` (reverse proxy IP), `TZ`,
  `PROXY_PORT`, `OTODOCK_MAX_FILE_MB` (upload/sync cap, default 1 GB), `OIDC_*` (SSO).
  A yes/no setting takes `true`/`false`, `1`/`0`, `yes`/`no` or `on`/`off`; another
  word stops the server at start, naming the setting.
- **Behind a reverse proxy / forced-login gateway**, these paths must skip the login:
  `/ws/`, `/ui-kit/`, `/v1/webhooks/`, `/v1/images/temp/`, `/s/` (share links) and the
  app-server routes (`^/v1/apps/[0-9a-f-]{36}/(api|ws|platform|client|inbound)(/|$)`),
  plus `/v1/satellite` with remote machines and `/v1/twilio/` with Twilio phone lines.
  Nothing else: the tab icon and the document editor (`/collabora/`) pass with the
  gateway's own sign-in, and `/collabora/` must not be exempted (since 1.9.0 OtoDock
  admits a request there only with a signed-in person's session).
- **Upgrading** = `scripts/upgrade.sh` on a Docker install (it backs up first and prints
  the release's notes); a source install pulls and rebuilds. `backup.sh` and `restore.sh`
  back up and restore the whole install, and a restore refuses a backup from a newer
  release. **Setup → OtoDock** *(admin)* shows the running version and whether a newer
  release is out; after an upgrade a one-time dialog shows what is new. Since 1.9.0 the
  stack needs Docker Engine 26 and Docker Compose 2.26 or later (the document editor's key
  is mounted as one file of a volume): `install.sh` and `upgrade.sh` check both before
  they change anything. Locked out (a lost admin password, a lost authenticator)?
  `otodock-admin.sh`, downloaded for the running release into the install folder and run
  there with the stack up, resets a password (`user reset-password <email>`), turns off a
  lost authenticator (`user disable-2fa <email>`) and prints support information with
  every secret left out (`support-info`).
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
  admins additionally get **Shared with agents** (contribute the account to the subscriptions agents use).
  The popup signs in with the browser's platform.claude.com session, so to add a second
  Claude account sign out there first or use a private window — a code for an account
  already connected only refreshes it and the form says so; a refused paste offers Try
  again.
- **Setup → AI Engines** *(admin)* — platform-level subscriptions, **API keys**
  (Anthropic, OpenAI, Groq), and **local models** (**Ollama** or an **OpenAI-compatible
  server** by URL), plus per-provider model lists and pricing.

Each engine has its own default model, chosen by tier, which an agent on **Auto** runs:
Claude Opus 5.5 on Claude Code, GPT-6.1 Sol on Codex, Sonnet 5.5 on Direct LLM. Every
model list shows its tier, and a pin on a retired model runs on its successor.

Routing rule: a user's own chats/tasks run on **their own** connections first; anything
that belongs to an *agent* (agent-scope tasks, shared agents, meetings, triggers, phone
calls) runs on the subscriptions and keys admins share with agents (**Shared with
agents** on an admin's account, and Setup → AI Engines). A user with nothing connected
can be granted **Platform API** (Admin → Users, per-user switch, off by default) to
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
  greeting, **Detect the caller's language**, **Transfer targets** (the colleagues the
  agent may hand a caller to), **Require a PIN**, a per-route call log via the clock
  icon). Each route sets a **Caller identity**: **Per caller** (default; every phone
  number gets its own private files and memory; callers read the agent's shared folders
  as viewers and change nothing; a **Remember callers** toggle; callers never get
  platform tools or a shell) or **A platform user** (the call runs as that user's own
  session, capped at manager; require a PIN on an inbound line, or from the person who
  answers an outbound one). Caller files, phone conversations and the call log age out
  together under **Setup → Phone → Caller Data** (90 days by default; **Forget all
  caller data now** is permanent).
- On a call the agent hangs up itself when the conversation is done, hands the caller
  to a transfer target by name (a blind transfer; a route without targets has none) and,
  on a call another agent placed, asks that agent. A call a person places from their own
  chat, on an outbound route tied to them, runs inside that chat, which resumes when the
  call ends.
- Wake word ("Hey OtoDock") is per-user opt-in: **User Settings → General → Wake word**,
  off by default, on-device detection.
- Dictation language: hold the chat's microphone button to pick it (**Automatic** starts
  in the browser's language, or the first listed one, and moves the text to the language
  spoken within a few seconds, among the languages listed under **Setup → Phone →
  Languages** *(admin)*), or set it under **User Settings → Audio**. Automatic detection
  follows the words as they arrive. Where the speech service scores its words, the
  clearer words decide, so a speaker's accent does not switch the language there. While
  it decides, for up to 20 seconds on each dictation or call, one or two extra speech
  streams run, and the speech provider bills them. A fixed language never adds them. It
  is on for dictation set to Automatic and for routes made since 1.8.0. An older route
  turns it on with **Detect the caller's language**. List only the languages the
  install's callers speak: with more to choose from, a caller ends on a neighbouring
  language more often.

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
  switch a user-paired machine to its own browser. Browser control reaches local
  services only within the admin's site list, and its run-code tool asks or runs by the
  chat's permission mode like its other tools.
- **From the machine's own terminal**: `otodock claude <agent>` (or `otodock codex
  <agent>`) opens a terminal chat with the agent in the current folder, as the machine's
  owner; `--mode ask|auto|dontAsk|plan` picks how much it asks (Auto when left out).
  The chat shows in the dashboard too, where it can be taken over.
- Files sync **both ways** automatically; generated build directories are auto-skipped;
  conflicts and deletes land in the workspace **Recover bin** (7 days). A file a program
  on the machine is still writing is never replaced by a save from the platform: that save
  goes to the Recover bin. Under the engines' own folders (`.claude`, `.codex`) only a
  person's skills, commands, agents and output styles (Codex: prompts and skills) sync,
  the rest stays each side's own. Chats on a machine untouched for the retention period
  (180 days by default) lose their session files there too, never their history (**Setup →
  System Settings → Storage and Retention**, *admin*). Transfers are paced so a slow uplink
  keeps its connection, and a large file keeps going instead of failing on a timeout.
  Whole folders upload too, dropped on the chat or the workspace. Machine cards show live
  status, sessions/capacity, and the CLI versions the machine runs (highlighted on drift).
- **Updates**: with automatic updates on (the machine card, admin), the machine's agent
  software updates itself at its next reconnect after a platform upgrade. The update
  ends every session running on that machine, so a turn in flight there stops with a
  card, and the chat continues from the next message. The Claude Code and Codex CLIs
  move to the platform's pins on every reconnect; the machine's other tools stay as
  they are.
- Kill switch: **Setup → System Settings → "Allow users to pair their own remote
  machines"** *(admin)*; offline-fallback behavior lives next to it. An admin can also
  remove any one user's machine from **Admin → Remote Machines**; its owner is told.

## The Android app

The Android app opens the same dashboard and adds what a phone does best: push
notifications, an alarm that rings for a danger notification until it is dismissed or
for up to five minutes, the phone's location for an agent that asks (Android asks the
person first) and dictation with the phone's microphone. It comes to Google Play after
the 1.9.0 release, and it works best with an OtoDock server on 1.9.0 or later. On first
launch **Connect to Server** asks for the address people use for the dashboard
(`DASHBOARD_PUBLIC_URL`). The app can hold several servers: the avatar menu's **App
Settings** lists them under **Installations**, where a tap switches, and a three-finger
swipe across any page switches too.

## When something's missing

If a user can't see a tab or button you named: (1) their role is below the gate, (2) the
feature is disabled on this install (remote machines, interactive terminals, chat
audio), or (3) the add-on isn't deployed (phone service). Say which one it likely is and
who can change it.
