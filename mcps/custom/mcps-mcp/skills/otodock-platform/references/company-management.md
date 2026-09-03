# Company management — departments, delegation, meetings, knowledge, memory

Running a company on OtoDock means giving every team its agents, wiring them together,
and keeping shared knowledge flowing. The building blocks:

## Departments

Installation-wide org structure. A department = a name + ordered **levels** (defaults
suggest Head/Senior/Junior; up to 8, renameable). Each agent gets a department + level
in **Agent Settings → Configuration → Department** *(platform admin/creator with
manager access on the agent)*.

Levels drive **auto-wired delegation**: same level ↔ same level; each level ↔ one level
above and below (*Adjacent*, the default) — or every agent ↔ every agent in the
department (*Subtree* reach). Wired links work in BOTH directions, so a junior can hand
work or files up to its manager. Cycles are refused at spawn time (an agent already in
the delegation chain 403s) and chains cap at 4 hops by default. Per-department
**Auto-delegation** toggle turns the wiring off entirely. Auto-wired edges show locked
("via <Department>") in the agent's Delegation Targets and retract cleanly when an agent
leaves; hand-checked targets are never touched. Every agent's prompt states its
department, level, and reachable teammates.

Managing: **admins** manage all departments, **creators** the ones they created — from
the Agents page's **Departments** tab or the map's ⋯ menu. Everyone sees only
departments they're part of.

**The company map**: the Agents page renders departments as a 3D map — amphitheater
seating by level, activity-heat borders, a pulse on agents responding right now,
delegation lines between agents. Zoom navigates (out = whole company, in = enter a
department); Grid view groups by department with level badges; the choice roams across
devices.

## Delegation

An agent hands work to another agent on its **Delegation Targets** roster (Agent
Settings → Configuration, *manager*; departments auto-wire it). Workers are visible,
first-class sessions — a chat lane the user can watch and steer, or a background task —
and results report back to the delegating chat. Multi-lane jobs turn the delegating chat
into an **orchestrator** with a dock of live lane cards and a board file. Agents can
also pass real files: deliverables copy into the target's `workspace/inbox/<sender>/`
(passive — delegate with a prompt to make the target act). Admin caps live on the
Delegation MCP row.

**Cross-agent reads**: in a user's chat, an agent can read whatever that user could see
on other agents (schedules, task history, sessions, triggers, notifications) — reads
only, writes never cross agents. An agent's *scheduled* (no-user) runs read the shared
activity of its delegation roster — the mechanism behind a CEO agent's morning briefing
that summarizes what the team's agents did overnight.

## Meetings

Several agents in one conversation: a moderator convenes with a topic + participants,
directs questions (parallel answers supported), and closes with a summary. Invitees
follow the **convening user's access** (each agent joins with that user's role);
meetings convened by no-user scheduled runs are limited to the agent's delegation
roster. Transcripts stream live with per-agent attribution; history sits on each
agent's **Meetings** tab and a platform-wide admin Meetings page. Meetings are personal
(convened from your chat) or agent-scoped (convened on the agent's behalf).

## Shared knowledge libraries

Promote an agent's `knowledge/` folder — or any subfolder — to a **named library**
("Brand Guidelines") and attach it to other agents installation-wide: **Agent Settings →
Configuration → Shared Knowledge** *(platform admin/creator; managers see it
read-only)*. Attached agents read it at `knowledge/shared/<source>/…` (remote machines
included). Per attachment: **read-only** (edited on the source; stray edits are captured
to the Recover bin and healed) or **writable** (edits AND deletes flow back to the source
and on to every other copy — a file a writable agent deletes in its session or on a remote
machine is gone everywhere within a turn, with the source copy kept in the source agent's
Recover bin; a file deleted at the source disappears from every copy). Agents
with self-config tools can wire libraries from chat — every change confirmed in-chat.

**Bulletins**: a `bulletin/<library name>.md` file inside a library is auto-injected
into the context of every attached agent (and the source) — a runtime broadcast channel
for daily/weekly progress and "what changed" notes, not a memory. The source's writers
AND writable attachments can author it (since 1.5 a RW mirror edit is adopted into the
source like any other library content); renaming the library renames the bulletin. Keep
it short — only the first 4 KB is injected, and past 3 KB the injected copy carries a
prune reminder every attached agent sees.

**Who writes knowledge**: managers (dashboard + machines); manager sessions in chat;
and agent-scope scheduled/delegated runs **created by a manager or admin** (editor-made
runs stay read-only) — that last rule is what lets a nightly curation job maintain the
library. `config/` stays human-manager-only everywhere.

## Memory

Topic-file memory, maintained by agents as they work. Two scopes: **agent memory**
(shared by everyone using the agent) and **user memory** (private per person). Loaded
automatically each session (full content while compact, index + on-demand reads once it
grows). Distinct from persona (identity/working style) and from knowledge (curated
reference): facts about people/projects/state go to memory. Controls: per-agent memory
toggles + "Clear shared agent memory" (Agent Settings → Configuration, *manager*);
"Clear my memory across all agents" (User Settings → General, *any user*); tuning knobs
in Setup → System Settings *(admin)*.

## A working pattern for a company

1. Departments mirror the org chart; each department's agents get level-appropriate
   delegation automatically.
2. One agent per team owns that team's knowledge; share it as a named library; attach it
   to whoever needs it; put the "what matters now" in its bulletin.
3. Department heads get a scheduled morning briefing that reads the roster's overnight
   activity.
4. Shared dashboards (mini-apps pinned "for the whole team") give each team an
   at-a-glance board, refreshed by a scheduled task.
