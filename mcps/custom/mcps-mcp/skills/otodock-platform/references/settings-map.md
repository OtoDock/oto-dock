# Settings map — every page and tab, with the role it needs

Roles: *(any)* = any signed-in user · *(manager)* = per-agent manager (platform admin
implied) · *(editor+)* = per-agent editor or above · *(creator)* = platform creator ·
*(admin)* = platform admin. The UI hides what a role can't use — when a user "doesn't
have" a tab, check the role first, then whether the feature is enabled on the install.

## Top-level navigation

- **Agent pill** (top bar) → the **Agents page** *(any)*: Map / Grid / Departments
  views; **Create Agent** and **Browse Community** *(admin/creator)*; Departments tab
  *(admin/creator)*.
- **Avatar menu** → User Settings *(any)* · Agent Settings *(any with access — tabs
  vary)* · Admin *(admin)* · Logout.

## User Settings (avatar menu → User Settings)

Tabs: **General · Integrations · Remote Machines · AI Engines · Audio · Usage** — all
*(any)*, own account only.

- **General**: Profile (display name, email, role) · Security (change password,
  passkeys, authenticator 2FA — local accounts) · Appearance (theme; **Chat activity**
  Compact/Detailed) · **Wake word** (on/off, per user, off by default) · Memory
  ("Clear my memory across all agents").
- **Integrations**: **Connected Accounts** (per-service OAuth sign-in / personal access
  tokens with optional account labels; per-account Reconnect/Disconnect; **Subscribe to
  events** for vendors with event APIs) · **API Keys** (personal webhook-trigger keys,
  shown once).
- **Remote Machines**: **Pair Machine**; per machine — remove, filesystem access
  (home-only vs full), **Device control** grants (computer / browser / app connectors),
  "Run these agents on this machine". Hidden if the build lacks the feature; replaced
  by a notice if an admin disabled user pairing.
- **AI Engines**: connect Claude / ChatGPT subscriptions; per account — status,
  **Reconnect**, Remove, **Personal use** toggle; admins also see **Agent pool**.
- **Audio**: voice/dictation engine choice (only when the admin policy is *User
  choice*), dictation language, device-native voice.
- **Usage**: own platform-API usage vs own-subscription reference, daily chart,
  per-agent breakdown. Read-only.

## Agent Settings (avatar menu → Agent Settings, or /agents/<name>)

- **Overview** *(any with access)*: description, tool chips, recent activity; managers
  also see visibility mode + assigned users with roles.
- **MCPs** *(manager)*: per-tool enable checkboxes; service-account binding per capable
  tool; Browse Community drawer.
- **Skills** *(manager)*: per-skill toggles for installed packages; bundled-with-MCP
  list; Browse community skills.
- **Configuration** *(manager; some rows gated higher)*: name/description/color · AI
  Engines multi-select · **Execution Target** *(admin, remote machines)* · **Department
  + Level** *(admin/creator)* · Default Model / Session Mode (headless vs interactive) /
  Effort · Visibility & workspace mode · Admin Only + Default-for-new-users *(admin)* ·
  **Memory** card (scope toggles, clear agent memory) · **Delegation Targets** ·
  **Shared Knowledge** (share/attach libraries — *mutations admin/creator*) · Danger
  Zone: Delete Agent *(admin)*.
- **Monitoring** group: **Conversations** *(manager — external phone/webhook sessions)*
  · **Scheduled Tasks** *(any with access; actions role-gated per row)* · **Triggers**
  *(any; agent-scope creation manager+; Agent API Keys section manager)* ·
  **Notifications** *(any; mutations role-gated)* · **Meetings** *(any)*.

## Admin area (avatar menu → Admin) — all *(admin)*

- **Overview**: platform-wide runs/schedules dashboard.
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
  capacity, max sessions, auto-update, filesystem/device grants); read-only list of
  user-paired machines.
- **Monitoring**: Scheduled Tasks · Triggers · Notifications · Task History · Meetings
  — cross-agent, cross-user audit views.
- **Setup** — tabs in order:
  - **General**: company name; platform-wide agent instructions.
  - **AI Engines**: platform subscriptions, API keys, local endpoints, model lists +
    pricing per layer (Claude Code, Codex, Direct LLM).
  - **OtoDock**: license & billing.
  - **Audio**: chat audio policy; STT/TTS provider cards (keys, per-language voices,
    defaults).
  - **Phone**: phone servers (Twilio / FreePBX / Asterisk) · routes (agent, language,
    PIN, call log) · call prompts · languages · turn classifier · infrastructure ·
    advanced tuning.
  - **Security**: require 2FA, passkey mode, password policy, SMTP, Turnstile bot
    protection, OAuth bearer allowlist.
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
| Mint a webhook key | User Settings → Integrations *(any)* or Agent Settings → Triggers *(manager)* |
| Approve an agent's tool/skill request | Admin → MCP Requests *(admin)* |
