# app-server-review

A judge check for the code of an app's server. It runs at the end of a turn
that wrote a file under an app's `server/` folder. It reads the changed
files and the app's `app.json`, and sends the agent back with what it
found: a write that never checks the person's role, a body used before it
is checked, SQL built from what a visitor sent, an error's own text sent to
the page, a key written in the code, development mode switched on. The
agent fixes the list once, then the turn ends.

`check_app` already refuses or warns on what a fixed rule can see. This
check is for the judgement such a rule cannot make: does this route need a
role, is this body checked enough.

## Add it to an agent

A manager of the agent adds it in one of three ways.

- Copy `check.json` into the agent's folder as
  `config/checks/app-server-review/check.json`. Copied as a file it is
  mandatory, since its document says so.
- Open the agent's Checks page and press New check. Name it
  `app-server-review`, tick Mandatory, write
  `**/apps/*/server/**, **/user-apps/*/server/**` under "Wrote where", set
  the fix rounds to 1, and paste the `rubric` from `check.json` into the
  instructions for the judge.
- Paste `check.json` into a chat with the agent and ask it to add the
  check. The platform asks you to confirm.

A mandatory check runs on every chat, task and delegation of the agent
that changes server code. Leave it unticked, or set `"mandatory": false`
before you copy the file, to offer it instead, so people attach it to the
chats they choose.

## What it costs

Each run is a session of the agent that reads the code, billed as
unattended use under Checks. It runs only on turns that change server
code, twice at most with its fix round. A manager can set a daily cap for
the agent. A run past the cap shows as skipped, never as a pass.
