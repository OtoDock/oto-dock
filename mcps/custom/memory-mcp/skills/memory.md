# Memory tool — worked examples

Your `# Memory` system-prompt sections explain WHAT to remember and when;
this skill shows HOW with the `memory` tool. Memory is markdown topic files:
`/memories/agent/` (shared with every user of this agent) and
`/memories/user/` (private to the current user). A generated `MEMORY.md`
index per scope is maintained by the platform — never edit it; edit topic
files and the index follows.

**Disambiguation**: this is the otodock platform's memory system — the only
one in play. Any LLM-runtime built-in memory (e.g. Claude Code's own
`.claude/.../memory/`) is disabled and unrelated.

## Save a new fact (create a topic)

```
memory(
  command="create",
  path="/memories/user/preferences.md",
  file_text="# Communication preferences\n- Prefers replies in Greek (2026-06-12)\n- Wants metric units everywhere (2026-06-12)\n"
)
```

Start every topic file with a one-line `# heading` — it becomes the topic's
index entry. Date each fact `(YYYY-MM-DD)` so staleness stays visible.
`create` errors if the file exists — that's your cue the topic already
exists: UPDATE it instead of duplicating.

## Update an existing fact (revise in place)

```
memory(
  command="str_replace",
  path="/memories/user/preferences.md",
  old_str="- Wants metric units everywhere (2026-06-12)",
  new_str="- Switched to imperial units (2026-07-02; was metric until then)"
)
```

`old_str` must match exactly once. Supersede outdated facts with a short
"was X until DATE" trail instead of silently erasing them — it keeps the
history readable for you and the user. Prefer targeted `str_replace` /
`insert` edits; never rebuild a whole topic from scratch when a small edit
will do.

## Append to a topic

```
memory(
  command="insert",
  path="/memories/agent/post-history.md",
  insert_line=2,
  insert_text="- Posted launch teaser to company Instagram (2026-06-12)"
)
```

Text is inserted AFTER the given line (`insert_line=0` = top of file). Use
`view` first if unsure of the layout.

## Remove a wrong or dead memory

```
memory(command="delete", path="/memories/user/old-project.md")
```

Delete topics that are wrong or no longer matter — stale memories are worse
than no memories. Everything is git-versioned platform-side, so deletion is
recoverable by humans.

## Read a topic that wasn't inlined

When a scope outgrows the inline budget, your prompt carries only its index.
Fetch a topic on demand:

```
memory(command="view", path="/memories/agent/infrastructure.md")
```

`view` on a directory lists it: `memory(command="view", path="/memories/agent")`.

## Choosing the scope

- Work output, operational facts, shared project state → `/memories/agent/`
  (every user of this agent benefits).
- Personal preferences, facts about THIS user → `/memories/user/`.
- Your default scope is in the tool description; override when the content
  clearly belongs to the other scope.
- Viewers can write only `/memories/user/`; the agent scope is read-only
  for them (the server enforces this — you'll get a clear message).

## What NOT to save

- Ephemeral task state ("currently running the build") — it'll be stale by
  the next session.
- Anything already in your auto-loaded context files.
- Secrets, credentials, tokens — NEVER, in either scope.
