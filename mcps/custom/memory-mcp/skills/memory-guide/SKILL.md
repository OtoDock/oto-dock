---
name: memory-guide
description: Worked examples for the memory tool. Create a topic, revise a fact in place, append, delete, read one section of a long topic, share a personal topic with the agent, and the exact command shapes. Load when unsure how to write or edit a memory topic file.
---

# Memory tool: worked examples

The Memory section of your instructions carries the rules (the two scopes,
what a memory is, the sizes the tool enforces). These are the command
shapes behind them. Memory is markdown topic files: `/memories/agent/`
(shared with every user of this agent) and `/memories/user/` (private to
the current user). A generated `MEMORY.md` index per scope is maintained by
the platform. Never edit it: edit topic files and the index follows.

## Save a new fact (create a topic)

```
memory(
  command="create",
  path="/memories/user/preferences.md",
  file_text="# Communication preferences\nHow this person likes to be answered.\n\n- Prefers replies in Greek (2026-06-12)\n- Wants metric units everywhere (2026-06-12)\n"
)
```

Start every topic file with a one-line `# heading` and a one-sentence
summary line under it: both become the topic's index entry. Date each fact
`(YYYY-MM-DD)` so its age stays visible. `create` errors if the file
exists: that is your cue the topic already exists, so update it instead of
duplicating. A new topic over the growth cap is refused.

## Update an existing fact (revise in place)

```
memory(
  command="str_replace",
  path="/memories/user/preferences.md",
  old_str="- Wants metric units everywhere (2026-06-12)",
  new_str="- Switched to imperial units (2026-07-02, was metric until then)"
)
```

`old_str` must match exactly once. Replace an outdated fact and keep one
line saying what it was until when. An edit that adds more than the entry
cap is refused: a memory entry is a fact, a rule or a pointer, not a log.
A growing edit that leaves the topic over the growth cap is refused too,
with the size to bring it under. An edit that makes a topic smaller always works.

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
`view` first if unsure of the layout. An entry without a date gets a
reminder in the result.

## Remove a wrong or dead memory

```
memory(command="delete", path="/memories/user/old-project.md")
```

Delete topics that are wrong or no longer matter. Stale memories are worse
than no memories. Everything is git-versioned platform-side, so deletion is
recoverable by humans.

## Read a topic that was not loaded

When a scope outgrows the inline budget, the newest topics that fit load
whole. The rest are listed with their size, their newest date and their
sections with line numbers. Fetch a listed topic, or one section of it:

```
memory(command="view", path="/memories/agent/infrastructure.md")
memory(command="view", path="/memories/agent/infrastructure.md", view_range=[31, 60])
```

`L31 Shared checkout` in the index means the section starts on line 31.
The listing in your prompt is as of the session's start: after a write,
`view` on `/memories/agent/MEMORY.md` gives the current sizes and line
numbers.
`view` on a directory lists it: `memory(command="view", path="/memories/agent")`.

## Share a personal topic with the agent (promote)

```
memory(
  command="rename",
  old_path="/memories/user/suppliers.md",
  new_path="/memories/agent/suppliers.md"
)
```

A rename from `/memories/user/` to `/memories/agent/` moves the topic into
the shared memory, for editors and managers of the agent. The other
direction is refused: shared knowledge stays shared. The new name may
differ from the old one.
