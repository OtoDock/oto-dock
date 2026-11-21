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
  plus `/v1/satellite` with remote machines or a compute machine on another server,
  `/v1/api/` when anyone uses a personal API key or the `otodock` command, and
  `/v1/twilio/` with Twilio phone lines.
  Nothing else: the tab icon and the document editor (`/collabora/`) pass with the
  gateway's own sign-in, and `/collabora/` must not be exempted (since 1.9.0 OtoDock
  admits a request there only with a signed-in person's session).
- **Upgrading** = `scripts/upgrade.sh` on a Docker install (it backs up first and prints
  the release's notes); a source install pulls and rebuilds.
  The update waits for open work first: new work is held, and running chats, tasks, calls
  and meetings get up to ten minutes to finish (`OTODOCK_DRAIN_BOUND_S`). What still runs
  then is cut and continues after the update. A turn waiting for a person's answer is not
  waited for, and it asks again afterwards. A server older than 2.0 cannot wait, so the
  update to 2.0 cuts what runs and nothing continues by itself: run it when nothing is
  running. A source install waits only once its service file carries the three new lines
  of `proxy/proxy.service`. Stopping the stack by hand does not wait: what it cuts
  continues after the start.
  `backup.sh` and `restore.sh`
  back up and restore the whole install, and a restore refuses a backup from a newer
  release. After a restore nothing continues by itself: a cut chat gets the card, a cut run
  fails, and no one-time task fires for the time that passed. Messages and wakes stored in
  the backup still go out and start turns. **Setup → OtoDock** *(admin)* shows the running version and whether a newer
  release is out; after an upgrade a one-time dialog shows what is new. Since 1.9.0 the
  stack needs Docker Engine 26 and Docker Compose 2.26 or later (the document editor's key
  is mounted as one file of a volume): `install.sh` and `upgrade.sh` check both before
  they change anything. Locked out (a lost admin password, a lost authenticator)?
  `otodock-admin.sh`, downloaded for the running release into the install folder and run
  there with the stack up, resets a password (`user reset-password <email>`), turns off a
  lost authenticator (`user disable-2fa <email>`) and prints support information with
  every secret left out (`support-info`).
  It also tells whether this is a good moment to update: `status` prints "Idle. A good
  moment to update." or what is busy, such as "Busy: 2 turns, 1 task run. 1 turn waits for
  a person." `drain` waits for open work and holds new work before a restart, and `resume`
  calls that off. A hold with no restart takes new work again after 15 minutes *(whoever
  runs the server, since 2.0)*.
- RAM guide: each concurrent Claude/Codex session grows to ~1 GB; 8 GB ≈ 5–6 sessions,
  16 GB ≈ 10–12. The platform admits sessions based on live free memory.
- **Compute machines.** A Docker install runs two containers of OtoDock itself: the
  server (`otodock-proxy`) and its compute machine (`otodock-compute`). The platform's
  chats, tasks, calls and meetings run on a compute machine, each in its sandbox, and
  apps on the one that comes with the platform. No person owns a compute machine.
  **Admin → Remote Machines → Compute machines** *(admin)* lists them with their sessions
  and free memory. A new chat goes to the one with the most room and stays there. An agent
  with a tool allowed to reach an address on your network runs on the one that comes with
  the platform, so that address is always reached from there.
  - **Add a compute machine** puts one on another server: give it a name, then run the
    command it shows on that server as an account that can use sudo. The command works
    once, within an hour, and the server downloads OtoDock's image (about 4 GB). The
    machine shows as Online once it signs in. **Pair again** makes a new command while it
    is offline. Add only a server you control: chats there run with the accounts of the
    people who use them. Only an install in Docker can add one.
  - "OtoDock's compute machine is not connected, so this cannot run now. Try again in a
    few minutes." means the compute machine is down or still starting. Admins get the
    notice "Compute machine offline", and the Compute machines card reads "No compute
    machine is online, so chats, tasks and calls cannot run." An admin runs `docker compose ps` in the install folder, then
    `docker compose logs otodock-compute`: its log says why when it cannot start a
    sandbox. `docker compose exec otodock-compute /opt/otodock/scripts/sandbox-doctor.sh`
    names the cause on the host.
  - A tool that reads "is being installed on OtoDock's compute machine" is built there.
    A new chat can use it once it is done. "could not be installed" comes with the
    cause.
  - A compose override that mounts a folder or an engine into `otodock-proxy` must mount
    the same into `otodock-compute`. `upgrade.sh` stops before it touches the running
    install when it does not, and names the mounts and the command to run after the
    fix. `upgrade.sh --dry-run` says the same ahead. Coming from 1.9.1, `docker compose`
    refuses the folder between that fix and the update: run the update right away.
  - An install from source needs its compute unit: stop OtoDock and run
    `scripts/dev-setup.sh --service`, which writes it and builds the bundled tools.
    Without the unit the server answers and nothing runs.
- **A tool in its own container** stops after 15 idle minutes and starts again in seconds
  at its next call. Its row under **Admin → MCP Servers** *(admin)* says when it sleeps,
  and **Keep running** keeps it up, using memory all the time. `OTODOCK_TOOLS_IDLE_MINUTES`
  in `.env` sets the wait, and `0` keeps them all running.
- **The browser tool** opens no page on the local network. An admin allows it with **Let
  the browser open pages on your local network** on its row under **Admin → MCP
  Servers**. Changing it restarts the browser, and callers and outside people lose the
  browser while it is on.

## First run

One setup screen: create the **administrator account** (display name, email,
password) → land on **Setup** to connect AI engines. An admin with no engine of their own
sees an amber strip until one is connected. Anyone else meets a notice with a **Connect
an AI engine** button where a chat needs an engine. A fresh
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
  again. With several accounts of one engine, a chat takes one that can run its model and
  moves to another only near a limit, never to balance spending. The chat then shows
  "Account changed".
- **Setup → AI Engines** *(admin)* — platform-level subscriptions, **API keys**
  (Anthropic, OpenAI, Groq), and **local models** (**Ollama** or an **OpenAI-compatible
  server** by URL), plus per-provider model lists and pricing.

Each engine has its own default model, chosen by tier, which an agent on **Auto** runs:
Claude Opus 5.5 on Claude Code, GPT-6.2 Sol on Codex, Sonnet 5.5 on Direct LLM. Every
model list shows its tier, and a pin on a retired model runs on its successor.

Routing rule, engines resolve per person: a person's own connections work on every
agent that allows the engine (an agent's AI Engines switches are an allowlist, every
engine on by default). A new chat or an unpinned task of theirs takes the allowed engine
they use most on that agent (their own chats there in the last 30 days, else on every
agent; the model menu lists their engines in that order, and the first changes only when
another engine clearly leads). With no chats yet it takes the agent's default engine when
they hold it, else any allowed engine they hold with their own account, else one the
platform lends them. There is nothing to set: picking another engine a few times makes
it first. A chat lane an agent starts from a person's chat is theirs on every agent,
Shared-only ones included: it runs on their own accounts and on the engine their own
chat there takes. If they have no account for that engine, the lane is refused with a
note that they can connect one under Settings, AI Engines. Anything that belongs to an
*agent* (agent-scope tasks and triggers, task lanes that work for the whole agent, every
task on a Shared-only agent, phone calls) runs
on the agent's default engine with the subscriptions and keys admins share with agents
(**Shared with agents** on an admin's account, and Setup → AI Engines). A meeting runs
on each agent's default engine too, on the accounts of the person who started it when it
is theirs. A user with
nothing connected can be granted **Platform API** (Admin → Users, per-user switch, off
by default) to borrow platform **API-key or local** connections — never anyone's
personal OAuth subscription. When their own account on an engine reaches its limit,
its notification opens User Settings → AI Engines with a notice: they pick another engine
in a chat's model menu (a new chat at once, an open chat once its session has ended).
A task pinned to a model keeps its pin until they edit the task.

Login health: the platform pre-warns 72 h and 24 h before a Claude login ends and
names the account in the notice. From 72 h before, the account row shows "Login
ends in N days" with a **Reconnect** button. A dead login keeps its Reconnect button.

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
  Native preferred / Platform only / User's choice; the last two are offered once a
  speech provider is set, and Platform only sends every dictation and voice through
  the provider, the device only where no provider covers that side).
- Phone: **Setup → Phone** *(admin)* — add a phone server (**Twilio**: paste account
  SID + auth token, calls enter via the public dashboard URL, no extra ports; or
  **FreePBX/Asterisk**: one-time dialplan bootstrap, PBX must reach TCP 9092/9093),
  click **Verify**, then create **routes** (agent + language + number, optional
  greeting, **Detect the caller's language**, **Transfer targets** (the colleagues the
  agent may hand a caller to), **Require a PIN**, a per-route call log via the clock
  icon). Each route sets a **Caller identity**: **Per caller** (default; every phone
  number gets its own private files and memory; a **Remember callers** toggle; a caller
  gets only what a manager picks on the agent's **External identities** card: tools,
  their own files, web search, knowledge folders and apps, never the shared workspace or
  a shell), **A platform user** (the call runs as that user's own chat, capped at
  manager; require a PIN on an inbound line, or from the person who answers an outbound
  one) or **The agent itself** (a team line: the call runs as the agent's own session,
  the tools that manage OtoDock off unless the route ticks each). Caller files, phone conversations and the call log age out
  together under **Setup → Phone → Caller Data** (90 days by default; **Forget all
  caller data now** is permanent).
- On a call the agent hangs up itself when the conversation is done, hands the caller
  to one of the route's transfer targets by name (a blind transfer, on inbound and
  outbound calls) and, on a call another agent placed, asks that agent. An outbound
  FreePBX transfer needs the AMI user's `read=call` and `write=call` *(admin, on the
  PBX)*. Without `read=call` the transfer tool keeps answering "the PBX has not named
  this call's channel yet". Without `write=call` the PBX refuses the move and the caller
  stays with the agent. An agent places calls through the outbound route an admin picked
  on its **Phone Calls** instance (Admin → MCP Servers). That route may be another
  agent's: that agent speaks on the call, and the agent that placed it follows the call
  and answers that agent's questions. A call a person places from their own chat, on an
  outbound route tied to them, runs inside that chat, which resumes when the call ends.
- Wake word ("Hey OtoDock") is per-user opt-in: **User Settings → General → Wake word**,
  off by default, on-device detection.
- Dictation language: hold the chat's microphone button to pick it (**Automatic** starts
  in the browser's language, or the first listed one, and moves the text to the language
  spoken within a few seconds, among the languages listed under **Setup → Phone →
  Languages** *(admin)*), or set it under **User Settings → Audio**. The menu says which
  recognizer listens: on this device, or through OtoDock. Automatic
  runs through the speech service whenever the install has one (a device's own
  recognizer cannot tell languages apart). Without a speech service, or for a person who
  picked Native for Dictation, the device listens in its own language. Safari on a Mac,
  and every browser on an iPhone or iPad, uses the speech service by default where the
  install has one.
  Under the *Native only* policy nothing changes: every dictation stays on the device.
  Under *Platform only* dictation runs through the speech service when one is set for
  dictation, else on the device. In the Android and iOS apps a device dictation keeps
  listening across pauses until the microphone is tapped again, and stops at the next
  pause when the app goes to the background. A language the phone offers to download
  dictates online meanwhile. A phone that hears nothing in a language says so at the
  stop: pick another language, or ask an admin for a speech provider (User's choice or
  Platform only). On Android 16, dictation needs the current app.
  Automatic detection follows the words as they arrive. Where the speech service scores its words, the
  clearer words decide, so a speaker's accent does not switch the language there. While
  it decides, for up to 20 seconds on each dictation or call, one or two extra speech
  streams run, and the speech provider bills them. A dictation tries at most two other
  languages, and one its words ruled out stays out. A fixed language never adds them. It
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
  agents on this machine"**. Personal: only the owner's sessions run there. An agent's
  SSH hosts work there too, for editors and up, Windows included.
- **Admin-paired** *(admin)*: **Admin → Remote Machines → Pair New Machine** — platform
  machines that can be an agent's default **Execution Target** (set in Agent Settings →
  Configuration, admin only). The chats of editors, contributors and viewers run on the
  OtoDock platform instead: the machine has no sandbox, and an agent there reaches
  everything its OS user can. **Every role of its agents runs here** on the machine card
  *(admin, off by default)* lets their chats run on it, with the machine's own access, its
  browser and computer control and the accounts their chats use. An outside caller always
  runs on the platform. A phone route tied to a platform user runs as that user, like their
  own chat.
- **Access levels** (changeable live on the machine card): Home-only (default) vs full
  filesystem; plus three separate **Device control** grants, all off by default —
  computer control (mouse/keyboard/screen), browser control, app connectors. Browser
  control has a **Browser mode**: **Dedicated profile** (default; a persistent,
  logged-in browser profile per agent) or **My own browser** (the agent works in a
  tab group inside the Chrome/Edge/Brave the machine's user is signed into, through
  the Playwright Extension). With My own browser, **Browser profile** names the profile
  that has the extension, and a pasted extension token lets a chat connect without a
  click. Tasks, triggers, calls, meetings and delegated chat lanes use that browser only
  when the token is saved and the card's **Also let unattended runs** box is ticked.
  Only the machine's owner can switch a user-paired machine to its own browser. Browser
  control reaches local services only within the admin's site list, and its run-code
  tool asks or runs by the chat's permission mode like its other tools.
  If the dedicated browser stops answering, the agent's next try starts a fresh one.
  The tabs that were open are not reopened. Sign-ins stay.
- **On a Mac**: the install command also sets up Homebrew (when the Mac has none) and the
  Python OtoDock needs. Installing Homebrew asks once for the password of an
  administrator account. Computer control asks macOS for two permissions the first time
  it acts. Open System Settings, Privacy & Security, and turn on **OtoDock Satellite**
  under Accessibility (Device Control and Data Access from macOS 27) and under Screen &
  System Audio Recording. Then start a new chat on the Mac. Until then the agent says
  which switch is still off. For browser control, install Chrome and open it once by
  hand. Running the install command again keeps the permissions. Removing the Mac and
  pairing it again asks for them again.
- **A Mac paired before 1.9.1** starts chats again after the update. Pair it again so
  every tool installs: remove it under Remote Machines, pair it, then give back its
  agents and its browser and computer control.
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
  A save on the platform answers before the machines hold the file. A turn on a machine
  waits for the files that machine is owed: one minute with nothing moving, five minutes
  at most, one minute for a message into a running turn, five seconds on a call or a
  spoken turn. Then it starts without them. The agent reads one line at the head of its
  message ("[Some files sent with this message have not reached this machine yet...]"),
  and the chat shows "The agent started before all the files reached <machine>. They are
  still on their way, so it may not see them yet." A file may be missing or an older copy,
  so the agent checks it before relying on it and says so if it could not use it. The next
  turn does not wait for the same files.
- **Updates**: with **Auto-update** on (the machine card, admin), a machine takes the
  platform's new version by itself once nothing runs on it, with one restart. Until then
  its card shows **Update waiting**. A machine too old to wait updates at its first
  sign-in, which ends its chats. With Auto-update off, OtoDock refuses the machine when it
  needs a newer version, and **Update now** updates it after saying how many sessions it
  stops. After an update a chat that was working continues by itself, and an idle chat
  starts again at its next message. The Claude Code and Codex CLIs
  move to the platform's pins on every reconnect; the machine's other tools stay as
  they are.
- **A broken local link**: a machine can be online while its sessions cannot reach
  OtoDock. The machine notices and repairs the link itself. Meanwhile its badge turns
  amber ("Connected, local link down"), its row under Remote
  Machines reads "Its sessions cannot reach OtoDock", and a new chat there is refused
  with a sentence that says so. Chats already running are left alone. Past a minute the
  row says to restart the satellite on that machine.
- Kill switch: **Setup → System Settings → "Allow users to pair their own remote
  machines"** *(admin)*; offline-fallback behavior lives next to it. An admin can also
  remove any one user's machine from **Admin → Remote Machines**; its owner is told.
- **A chat on a machine at a platform restart**: the machine keeps working. A Claude Code
  or Codex turn it still holds lands in the chat when it reports back, and a terminal there
  stays open. A turn it lost continues by itself if it reports back within a day. Until the report the chat waits, and after three minutes
  it shows "This turn is waiting for its machine". A person's message continues the chat
  at once. A task run there waits up
  to 30 minutes, then fails and tells its delegator once.
- **A machine that goes away**: a machine that sends nothing reads "slow connection"
  after a minute and offline within 90 seconds. Only its own turns and files wait for it,
  never another machine's or a chat on the platform. The program on the machine keeps
  working: when the link is back, the turn goes on as if nothing happened, also after a
  longer break. A message sent meanwhile waits with a line that says the machine is
  reconnecting or not answering, and goes out when it is back. Files it is owed arrive
  when it returns. A machine too slow to take what it is sent is cut and reconnects by
  itself. When the machine restarts while the platform stays up, for an update or after a
  crash, a chat that was working continues by itself.

## The Android and iOS apps

The Android app opens the same dashboard and adds what a phone does best: push
notifications, an alarm that rings for a danger notification until it is dismissed or
for up to five minutes, the phone's location for an agent that asks (Android asks the
person first) and dictation with the phone's microphone. It comes to Google Play after
the 1.9.0 release, and it works best with an OtoDock server on 1.9.0 or later. The **iOS
app** (iPhone and iPad, on TestFlight for now) does the same, but when the app is not on
screen a danger notification does not ring until dismissed: it is a time-sensitive alert
with its sound, which passes Focus. On first launch **Connect to Server** asks for the
address people use for the dashboard (`DASHBOARD_PUBLIC_URL`). The app can hold several
servers: the avatar menu's **App Settings** lists them under **Installations**, where a
tap switches, and a three-finger swipe across any page switches too, where the phone
does not keep that gesture for itself. In the 1.9.1 apps a switch works while an agent
replies, and the reply is there on return. During a live meeting or voice conversation
in the chat the swipe refuses, and the **Installations** list asks first, because the
switch ends it.
If alarms come late, App Settings shows **Alarms on this phone** while Android may hold
them back: **Open battery settings** opens OtoDock's page in the phone's settings, where
Battery goes to Unrestricted (on some phones, Battery optimization to Don't optimize).
The row leaves once the setting is right. Turning off OtoDock's notifications in the
phone's settings stops a server's pushes and alarms once the app opens that server
again, on iOS and Android 13 and later. On Android 12 and older that switch does not
stop the alarms.

## When something's missing

If a user can't see a tab or button you named: (1) their role is below the gate, (2) the
feature is disabled on this install (remote machines, interactive terminals, chat
audio), or (3) the add-on isn't deployed (phone service). Say which one it likely is and
who can change it.
