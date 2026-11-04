# Company management — departments, delegation, meetings, knowledge, memory

Running a company on OtoDock means giving every team its agents, wiring them together,
and keeping shared knowledge flowing. The building blocks:

## Departments

Installation-wide org structure. A department = a name + ordered **levels** (defaults
suggest Head/Senior/Junior; up to 8, renameable). Each agent gets a department + level
in **Agent Settings → Configuration → Department** *(platform admin/creator with
manager access on the agent)*.

Levels drive **auto-wired delegation**, and each department picks a **mode**: *Off* (no
automatic wiring), *Down only* (the default for a new department: each level delegates to
the level below it and nothing goes up or across, so a head reaches its team and the team
reaches neither the head nor each other), *Down and across* (the same, plus agents on the
same level delegating to each other) or *All directions* (each level delegates up and down
and the same level to each other, so a junior can hand work or files up to its manager).
The **reach** is independent of the mode and applies to whatever it wires: *One level up or
down* (only the level directly above or below) or *Whole department* (every level, however
far apart), so with all directions and the whole department a bottom-level agent reaches the
head and back. Departments that existed before 1.7 keep the wiring they had (one with
auto-delegation off became *Off*). Cycles are refused at spawn time (an agent already in
the delegation chain 403s) and chains cap at 4 hops by default. Auto-wired edges show locked ("via
<Department>") in the agent's Delegation Targets and retract cleanly when an agent leaves;
hand-checked targets are never touched. Every agent's prompt states its department, level,
and reachable teammates.

Managing: **admins** manage all departments, **creators** the ones they created — from
the Agents page's **Departments** tab or the map's ⋯ menu. Everyone sees only
departments they're part of.

**Department sets (1.9.1).** The community catalog also offers whole departments:
a set names its agent templates with their levels, the delegation wiring, the head's
shared library and the briefing schedules. **Browse Community Agents → Department sets**
(or the Departments tab's "Browse the department sets" link) installs one in one step
*(admin/creator)*: the department with its levels, every agent at its level (an agent
you manage that was installed from the same template and sits in no department is used,
not copied, and the dialog and the card name it), the head's knowledge folder shared as a
library named after the department to the others (read only unless the set says
otherwise), and the schedules listed, paused. A member installed alone stays a standalone
agent; when a second member of the set is installed later, the install dialog offers
"Create the <set> department and add both agents to it?". The Agent Creator tool
(`search_community_agents`, `propose_install`, `propose_department_install`,
`propose_move`) proposes the same as a **card in the chat**: the person presses the
button, the platform acts with their role. One set ships first: Yacht office (five agents
for a yacht management office). The Settings tool's `show_card` draws the other
department cards the same way: `create_department` (admins and creators) and
`set_department` (its mode and reach, the admin or the creator who made it). The
team's cards: `hand_files` (a person's finished files from their space to the team's
workspace, offered when the conversation says the work is final), `promote_memory` (a
topic of a person's memory shared with everyone on the agent, editors and above),
`share_app` (an app copied to another agent, editors on both sides; its first release
waits for approval there), `attach_library` (admins and creators, a creator also
manager on the target), and, for an app that follows its source ("Setting a team up"
below), `update_followed_app` and `send_release_to_source`. Nothing happens until the
person presses the button.

An admin can also share a team app with a department: it appears in the Apps panel of
every agent in it, follows the department as agents join or leave, and ends when the
department is deleted (apps.md).

**The company map**: the Agents page renders departments as a 3D map — amphitheater
seating by level, activity-heat borders, a pulse on agents responding right now (both
follow the sessions the viewer may see, their own Active now list), delegation lines
between agents. Opened from a chat's agent pill, the map starts on that agent's
department. From elsewhere it starts on the department of the person's favorite agent
(★). Zoom navigates (out = whole company, in = enter a department). Grid view groups by
department with level badges, and the choice roams across devices.

## Delegation

An agent hands work to another agent on its **Delegation Targets** roster (Agent Settings
→ Configuration). A session's delegation targets are that roster: the edges its department
wires (locked, "via <Department>") plus the targets a manager ticks by hand *(manager)*;
the agent's prompt lists them. A worker that runs as the target agent (agent scope) needs
the person behind the chat to be an **editor+** there; a personal worker runs as that
person and needs only access. A chat lane may also run on the delegating person's own
computer when they ask (`machine` on `delegate`). There it works as that person, with
their role and their own AI account, even when it works for the whole agent. It stops
when they lose the editor role on the agent. Workers are visible, first-class sessions —
a chat lane the
user can watch and steer, or a background task — and results report back to the delegating
chat. A worker's report shows in the delegating chat as a collapsed card on the right,
where the person's own messages sit. It has a short preview, the task's name, its files
as chips and an "Open lane" link to the worker's chat. A click on the card opens it
whole. The task a lane receives, a follow-up into it, a task run's own prompt and a
scheduled wake show the same way, and the platform's framing text around them is
hidden. A worker that stopped before writing its report
gets a card that reads "ended without a report". Multi-lane jobs turn the delegating chat
into an **orchestrator** with a dock of live lane cards and a board file. Agents can also
pass real files: deliverables copy into the target's `workspace/inbox/<sender>/` (passive
— delegate with a prompt to make the target act), and a delegated worker attaches its
deliverables to its result (`attach_result_files`): they land in the delegating chat's
`inbox/<worker>/` with no edge back and the result names them, with an Open per file in
the chat. A worker's result arrives once its background commands and sub-agents have
finished. Admin caps live on the Delegation MCP row.

**Visibility is not delegation.** In a user's chat, an agent can read whatever that user
could see on other agents (schedules, task history, sessions, triggers, notifications),
but it hands work or files (`delegate`, `send_files`) only to its own targets — reads
cross agents, writes do not (a worker's result files go back only to the chat
that delegated it). An agent's *scheduled* (no-user) runs read the shared
activity of its delegation roster — the mechanism behind a CEO agent's morning briefing
that summarizes what the team's agents did overnight.

## Meetings

Several agents in one conversation: a moderator convenes with a topic + participants,
directs questions (parallel answers supported), and closes with a summary. Invitees follow
the **convening user's access** (each agent joins with that user's role); meetings
convened by no-user scheduled runs are limited to the agent's delegation roster.
Transcripts stream live with per-agent attribution; history sits on each agent's
**Meetings** section and a platform-wide admin Meetings page. Meetings are personal (convened
from your chat) or agent-scoped (convened on the agent's behalf). An admin caps how many
participants one person's meetings hold at once under **Admin → MCP Servers → Meeting
Rooms** (four by default). 0 turns meetings off.

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

**The Notebook** (since 1.9.1): a dated, append-only record between
agents. Library bulletins no longer load, and their files stay. Each
agent posts short entries about its own work with `notebook_post`: an
editor or a manager in a person's chat, or a run of the agent itself. A
viewer's or a contributor's chat only reads, and an agent that keeps each
person's chats private (Personal only) never posts. Every session loads
the latest entries of its agent and of every agent one delegation step
away, in either direction. So a department's mode decides who reads
whom: `down` the head and each member, `down_across` and `both` the
siblings too, `off` none. A manual edge counts the same. People read the
entries on the agent's Overview and in the department's card, and a
manager of the posting agent deletes one. The share of each prompt is set
in Setup → System Settings *(admin)*.

**Who writes knowledge**: managers (dashboard + machines); manager sessions in chat;
and agent-scope scheduled/delegated runs **created by a manager or admin** (editor-made
runs stay read-only) — that last rule is what lets a nightly curation job maintain the
library. `config/` stays human-manager-only everywhere.

## Memory

Topic-file memory, maintained by agents as they work. Two scopes: **agent memory**
(shared by everyone using the agent) and **user memory** (private per person). The rule
for which: about a person, their preferences, their situation, user memory; about the
work, the systems, the decisions, the traps, agent memory; when a fact is both, the fact
goes shared and the preference personal. Loaded automatically each session: the newest
topics in full until the inline budget is used, the rest listed with their size, newest
date and sections for on-demand reads. Distinct from persona (identity/working style)
and from knowledge (curated reference): facts about people/projects/state go to memory.
A topic over the growth cap (32 KB by default) no longer grows until the agent shrinks
it, one edit adds at most the entry cap (2 KB by default), and an edit that makes a topic
smaller always works. An editor or a manager can share one of their personal topics with
the agent (the memory tool's rename from the personal to the shared scope); the other
direction is refused. Controls: per-agent memory toggles + "Clear shared agent memory"
(Agent Settings → Configuration, *manager*); "Clear my memory across all agents" (User
Settings → General, *any user*); the inline budget, the nudge, the growth cap and the
entry cap in Setup → System Settings *(admin)*.

## Setting a team up

**The mode fits the team.** *Personal only* for a whole team working privately (each
person their own files, chats and memory, nothing shared), or for one person. *Personal +
shared* for project work: each person drafts in their own space and hands finals to the
team's workspace. *Shared + personal* for a department head's desk: the shared workspace
is home and each person keeps private notes beside it. *Shared only* for one desk the
team runs together, with one chat history (editors and managers only).

**The three kinds of people on a project agent.** A **contributor** from the field (a
site engineer) posts photos and reports: the agent saves them to the shared workspace as
they arrive, and the contributor reads the shared memory.
**Editors** in the office (an architect) draft in their own space, hand finals over, and
act as the agent (schedules, triggers, shared apps, shared memory). The **manager**
curates the persona, the context files, the knowledge and the members. A platform admin
acts as admin everywhere. When the agent has the delegation tools, its prompt says how
many people it serves and in which roles, their names for a platform admin or for an
editor and above while the user directory is open to members, and the role of the
person in the chat.

**Where each artifact lives.** Content decides, never the default alone. A draft, an
exploration and one person's own work stay in their personal space. A final, and anything
that defines how the team works (a process, a template, a checklist, an app the team
uses, the agents' own setup), goes to the shared workspace or a knowledge library. A
process the team follows is knowledge, not memory: memory holds facts, decisions and
traps (shared ones in the agent's memory, a person's own in theirs). The Notebook holds
what the agents tell each other across delegation edges. An app the team uses is a
shared app. An app another agent should run too is shared to it and follows its source
(apps.md). The agent says where it saved every document it creates.

**The hand to the team.** The agent never moves or copies a person's files on its own,
except a contributor's photos and reports, which it saves to the team as they arrive. When the
conversation says the work is final (final, done, send it to the team, the client's copy,
the finished drawings), it draws the `hand_files` card: a move, or a copy when the person
still works on the files. A card answered Not now is asked again later with a new card.

**One agent or several.** A multi-agent setup is proposed only when the person asks to
automate a company function, a department or several projects with different people. An
idea, a research task or one job gets one agent. When a team of agents is right, the
setup goes in order: the `create_department` card (the levels, `mode` `down` or
`down_across`, the reach), `list_departments` for its ids (the card's outcome does not
give them), the agents (the `propose_install` card from the catalog, or the `create_agent`
tool, which installs at once), a `move_agent` card for each agent including the head at
its Head level, the head's method shared as a library (`share_knowledge_folder` or Agent
Settings → Configuration → Shared Knowledge, an admin or a creator), the `attach_library`
card (read-only), the `share_app` card (the project app as a followed copy), then
`hand_files` in daily work. A project persona draws the `hand_files` card when work is
final and never moves a document itself. Each card is the person's click with their own
role. The agent only proposes. The agent-creation skill has the steps per pattern.

**An app that follows its source.** A copy shared to another agent keeps where it came
from. When the source releases again, the copy's card offers *Update to release N* (the
files replaced, the data kept, the release held for approval there) or, when the copy
released on its own since, *Ask the agent to merge*: the source's release is unpacked
beside the copy's files and the agent merges the two and deploys one release. Nothing
reaches the agent by the press alone. From the Apps panel, the person gives the agent
the request (the notice has it to copy, and the card keeps it while the merge waits).
From a card the agent drew, the agent reads `check_card`. The copy's side can *Send this release
to the source*, which waits for the source's approval. *Stop following* ends it. The
person needs editor or above on both sides (the owner, for a personal app) to update,
merge or send. Stop following needs that right on the copy alone. The data never
travels.

## A working pattern for a company

1. Departments mirror the org chart; each department's agents get level-appropriate
   delegation automatically.
2. One agent per team owns that team's knowledge; share it as a named library; attach it
   to whoever needs it. The team's lead agent posts the "what matters now" to the Notebook.
3. Department heads get a scheduled morning briefing that reads the roster's overnight
   activity.
4. A shared app on each team's lead agent gives the team an at-a-glance board, kept
   fresh by a scheduled task. A shared app can call the shared apps of the agents its
   agent delegates to (approved on both cards), so a head's board can read its teams'
   boards; under *Down only* the teams' apps cannot call the head's.
