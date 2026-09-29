# Settings map — every page and tab, with the role it needs

Roles: *(any)* = any signed-in user · *(manager)* = per-agent manager (platform admin
implied) · *(editor+)* = per-agent editor or above · *(contributor+)* = per-agent
contributor or above (writes the shared workspace but never acts as the agent: no shared
app pins, agent-scope tasks or triggers, agent-scope delegation or shared memory writes)
· *(creator)* = platform creator · *(admin)* = platform admin. The UI hides what a role
can't use — when a user "doesn't have" a tab, check the role first, then whether the
feature is enabled on the install.

## Top-level navigation

- **Agent pill** (top bar) → the **Agents page** *(any)*: Map / Grid / Departments
  views; **Create Agent** and **Browse Community** *(admin/creator)* — a template's
  card shows the apps and checks it ships and, per installed agent, an **Update** when
  the catalog is newer *(admin, or a creator who manages that agent; also a banner on
  that agent's Config tab)*; Departments tab *(admin/creator)*; **Shared with me**
  (apps and chats others shared with you; it appears once there is one) *(any)*.
- **Avatar menu** → User Settings *(any)* · Agent Settings *(any with access — tabs
  vary)* · Admin *(admin)* · Logout.

## User Settings (avatar menu → User Settings)

Tabs: **General · Integrations · Remote Machines · AI Engines · Audio · Usage** — all
*(any)*, own account only.

- **General**: Profile (display name, email, role) · Security (change password,
  passkeys, authenticator 2FA — local accounts) · Appearance (theme; **Chat activity**
  Compact/Detailed) · **Wake word** (on/off, per user, off by default) · **Apps**
  ("Agents know which app I am looking at", "Agents may open apps on my screen", both on
  by default) · Memory ("Clear my memory across all agents").
- **Integrations**: **Connected Accounts** (per-service OAuth sign-in / personal access
  tokens with optional account labels; per-account Reconnect/Disconnect; **Subscribe to
  events** for vendors with event APIs, **Subscribe as** yourself or an agent this
  account serves as service account — managers of that agent only; Personal-only
  agents are not offered) · **API Keys** (personal webhook-trigger keys,
  shown once).
- **Remote Machines**: **Pair Machine**; per machine — remove, filesystem access
  (home-only vs full), **Device control** grants (computer / browser / app connectors),
  the browser control's **Browser mode** (Dedicated profile / My own browser + the
  Playwright Extension token), "Run these agents on this machine". Hidden if the build lacks the feature; replaced
  by a notice if an admin disabled user pairing.
- **AI Engines**: connect Claude / ChatGPT subscriptions; per account — status,
  **Reconnect**, Remove, **Personal use** toggle; admins also see **Agent pool**. The
  Claude popup signs in with the browser's platform.claude.com session, so to add a
  second Claude account sign out there first or use a private window — a code for an
  account already connected only refreshes it and the form says so; a refused paste
  offers Try again.
- **Audio**: voice/dictation engine choice (only when the admin policy is *User
  choice*), dictation language (also picked by holding the chat's microphone button),
  device-native voice.
- **Usage**: own platform-API usage vs own-subscription reference, daily chart,
  per-agent breakdown. Read-only.

## Agent Settings (avatar menu → Agent Settings, or /agents/<name>)

- **Overview** *(any with access)*: description, tool chips, recent activity; managers
  also see visibility mode + assigned users with roles.
- **MCPs** *(manager)*: per-tool enable checkboxes; service-account binding per capable
  tool; beneath a binding, **Subscribe to events for this agent** and the agent's own
  event subscriptions (what agent-scope triggers and shared apps need); Browse
  Community drawer.
- **Skills** *(manager)*: per-skill toggles for installed packages; bundled-with-MCP
  list; Browse community skills.
- **Configuration** *(manager; some rows gated higher)*: name/description/color · AI
  Engines multi-select · **Execution Target** *(admin, remote machines)* · **Department
  + Level** *(admin/creator)* · Default Model (Auto = the engine's default by tier) /
  Session Mode (headless vs interactive) / Effort · Visibility & workspace mode · Admin
  Only + Default-for-new-users *(admin)* ·
  **Memory** card (scope toggles, clear agent memory) · **Delegation Targets**
  (hand-ticked targets; the department's show locked) ·
  **Shared Knowledge** (share/attach libraries — *mutations admin/creator*) · a
  **Template** row naming the community template and the installed version, and a
  **template update banner** when the community catalog carries a newer version of the
  agent's template *(runs for any manager of the agent)* · Danger Zone:
  Delete Agent *(admin)*.
- **Checks** *(any with access)*: the offered and mandatory checks and the verdicts you
  may see; create, edit, remove, mark mandatory and the daily cap *(manager)*.
- **Monitoring** group: **Conversations** *(manager — external phone/webhook sessions)*
  · **Scheduled Tasks** *(any with access; agent-scope tasks editor+; actions role-gated
  per row)* · **Triggers** *(any; agent-scope triggers editor+, though the + New trigger
  form offers the agent scope to managers only, except on a Shared only agent; Agent API
  Keys section manager)* ·
  **Notifications** *(any; mutations role-gated)* · **Meetings** *(any)*.

## Apps and chats (the apps button beside the chat composer; an app's ⋯ menu)

- **The apps button** *(any with access)* opens the agent's apps; the first is the
  agent's home. The approval card appears on an app waiting for approval: approve
  *(its owner; editor+ for a shared app, a manager when its scripts receive the agent's
  accounts; admin)*.
- **An app's menu**: Open full screen *(any who sees it)* · **Share** *(owner; editor+
  for a shared app)* · Roll back · View the working copy · Logs · **Settings** (the
  app's secrets) · Unpin / Unpin for everyone · Delete app and its data *(all: owner;
  editor+ for a shared app)* · Hide for me *(a shared app or one shared with you, any)*.
  Logs, View the working copy, Settings and Delete appear only on an app with a server,
  Roll back once there is an earlier release. Admins hold the same authority on every
  app.
- **A chat's row in the chat history** → **Share** *(its owner; editor+ on a Shared only
  agent)*.

## Admin area (avatar menu → Admin) — all *(admin)*

- **Overview**: platform-wide runs/schedules overview (the runs table; a run that
  finished with background work still running shows an "N left running" badge).
- **Users**: add user (invite link / temp password), platform role (Admin / Creator /
  Member), per-agent role assignments, **Platform Auth** toggle (borrow platform API
  credentials), reset password, delete.
- **Usage** ("Usage & Limits"): totals, daily chart, per-provider/model costs,
  per-user usage + limit overrides, agent-scoped usage, **Agent Budgets**, role default
  budgets (weekly/monthly).
- **MCP Servers**: core/custom/community inventory; enable/config/instances/delete per
  tool; **Check Updates**; **Install** (ZIP); **Browse Community**.
- **Skills**: installed skill packages; Check Updates / Update / Delete; **Browse
  Community Skills**; ZIP install.
- **MCP Requests**: approval queue for tool + skill requests (kind badge); approve with
  instance picker; amber **Needs instance** state resolves itself once an instance
  covers the agent.
- **Remote Machines**: platform machine pairing + cards (capabilities, CLI versions,
  capacity, max sessions, auto-update, filesystem/device grants); the list of
  user-paired machines, each removable by an admin (the owner is told).
- **Shares**: every link made on the install (target, creator, password or none,
  Buttons, opens, expiry), each revocable.
- **Monitoring**: Scheduled Tasks · Triggers · Notifications · Task History · Meetings
  — cross-agent, cross-user audit views.
- **Setup** — tabs in order:
  - **General**: company name; platform-wide agent instructions.
  - **AI Engines**: platform subscriptions, API keys, local endpoints, model lists +
    pricing per layer (Claude Code, Codex, Direct LLM) and, per custom/local model,
    its capability tier + one-line "good at" (builtins are fixed).
  - **OtoDock**: license & billing.
  - **Audio**: chat audio policy; STT/TTS provider cards (keys, per-language voices,
    defaults).
  - **Phone**: phone servers (Twilio / FreePBX / Asterisk) · routes (agent, language,
    PIN, call log) · call prompts · languages · turn classifier · infrastructure ·
    advanced tuning.
  - **Security**: require 2FA, passkey mode, password policy, SMTP, Turnstile bot
    protection, OAuth bearer allowlist, **Sharing** (External links · Links without a
    password · User directory when sharing · Longest link expiry).
  - **System Settings**: timezone · session/login timeouts · "Allow users to pair their
    own remote machines" · offline-fallback toggles · interactive-terminal kill switch
    · concurrency/idle timeout · memory knobs · storage & retention · **Storage
    Quotas** (per-agent shared + per-user folder caps) · chat title generation ·
    **Automatic MCP Updates** (weekly, includes skill packages).

## Quick answers to frequent "where/who" questions

| Ask | Answer |
| --- | --- |
| Connect my Claude/ChatGPT | User Settings → AI Engines *(any)* |
| Add an API key / local model for the platform | Setup → AI Engines *(admin)* |
| Add a user / change a role | Admin → Users *(admin)* |
| Let a user borrow platform credentials | Admin → Users → Platform Auth *(admin)* |
| Give an agent a tool | Agent Settings → MCPs *(manager)*; install first via Admin → MCP Servers *(admin)* |
| Enable a skill | Agent Settings → Skills *(manager)*; package install *(admin)* |
| Create a department / move an agent | Agents page → Departments *(admin/creator)*; Agent Settings → Configuration → Department *(admin/creator)* |
| Share knowledge between agents | Agent Settings → Configuration → Shared Knowledge *(admin/creator)* |
| Pair my laptop | User Settings → Remote Machines *(any, unless disabled)* |
| Pair a team server | Admin → Remote Machines *(admin)* |
| Set spending limits | Admin → Usage *(admin)* |
| Set disk quotas | Setup → System Settings → Storage Quotas *(admin)* |
| Configure voice | Setup → Audio *(admin)*; per-user prefs in User Settings → Audio |
| Set up phone numbers | Setup → Phone *(admin)* |
| Turn on the wake word | User Settings → General → Wake word *(any)* |
| Mint a webhook key | User Settings → Integrations *(any)* or Agent Settings → Monitoring → Triggers *(manager)* |
| Let an agent react to GitHub/Slack/… events | Agent Settings → MCPs → the MCP → Subscribe to events for this agent *(manager)*, or Connected Accounts → Subscribe to events → Subscribe as: the agent |
| Approve an agent's tool/skill request | Admin → MCP Requests *(admin)* |
| Approve an app | its approval card, on the app *(its owner; editor+ for a shared app, a manager when its scripts receive the agent's accounts; admin)* |
| Share an app or a chat, make a link | the app's ⋯ menu → Share, or the chat's row → Share *(owner; editor+ for a shared app or a Shared only agent's chat)* |
| See or revoke every link, switch links off | Admin → Shares; Setup → Security → Sharing *(admin)* |
| Set an app's secret | the app's ⋯ menu → Settings *(owner; editor+ for a shared app)* |
| Roll back an app | the app's ⋯ menu → Roll back *(owner; editor+ for a shared app)*, or ask the agent |
| Create a check or make one mandatory | Agent Settings → Checks *(manager)*; to attach one to a chat, ask the agent *(any)* |
| Change how a department delegates | Agents page → Departments → the department's mode and reach *(admin, or the creator who made it)* |
| Remove someone's machine | Admin → Remote Machines *(admin)* |
| Pick the dictation language | hold the chat's microphone button, or User Settings → Audio *(any)* |
