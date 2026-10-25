# Characterization recordings

The recorded inputs of the golden-master suites `tests/session/test_pump_characterization.py`
and `tests/session/test_tailer_characterization.py` (the helper `golden_master.py` in
this folder), and under `golden/` what the tree produced from them before the pump, the
loop and the two tailers were decomposed in 1.9.0. The `builder-*.json` goldens belong
to `tests/core/test_builder_characterization.py`: the session builders' configs and
collaborator calls, built by `tests/fixtures/loop_builder_rig.py` from rows the test
writes (no recording), before their store reads left the event loop. A golden is never rewritten by a
refactor: a drift is a behaviour change to explain. `CHARACTERIZATION_WRITE=1` writes
them (used once, from a tree whose behaviour is the one to keep) and fails each test it
rewrote: run again without it.

Every file is scrubbed: the agent tree's paths read `/users/dev-admin/...`, the OS
user is `person`, the person's address is `person@example.com`, the organization
and the account context are gone (the Codex `session_meta` creator ids are
placeholders of the same shape), the workspace listing a tool result carries
is synthetic (generic names of the recorded lengths), and the bulky instruction payloads the tailers
ignore are cut to a prefix. Every uuid, id, timestamp and turn-relevant text is as
recorded.

## Raw Claude Code streams (real)

`claude-tools.jsonl`, `claude-subagent.jsonl`, `claude-todo.jsonl`, `claude-plan.jsonl`:
the stream-json NDJSON the pinned Claude Code (2.1.296, `VERSIONS.md`) wrote on the
dev box on 2026-10-17, headless with the proxy's own argv (`-p --input-format
stream-json --output-format stream-json --verbose --include-partial-messages --model
claude-sonnet-5-5 --effort medium`, `core/layers/cli/session.py` `_build_persistent_cmd`),
one prompt each: a turn with Bash, Write, Read and Edit; a foreground Agent spawn with
its inner frames; TaskCreate and TaskUpdate (the checklist snapshot) with ToolSearch
and thinking; and a plan-mode turn (`--permission-mode plan`: the plan file written,
thinking-token pings). The init frame's MCP server list and the slash-command list
are emptied (the host's own). The suite turns the lines into CommonEvents through
the real translator, so the recording is the input and the translator is part of
the code under the pump. The three captures under `tests/fixtures/cli_wake/`
(a backgrounded Bash, a background subagent, the user-turn race; recorded on
2.1.243) are replayed the same way.

## The Codex turn (composed)

`codex-turn.json`: the app-server notifications of one turn, as `[method, params]`
pairs. Composed, not recorded: the box holds no Codex login a recorder may use
outside the platform and the proxy logs no raw notification (a recorder behind a
debug flag would let it be re-taken). The items are the ones the T1 rollout below
recorded for that very turn (a command, a failed command, the display MCP's
`send_url`, the agent messages, the plan), in the v2 wire's camelCase, plus the
shapes the Codex tests pin from live probes (`test_codex_subagent_turn.py`'s collab
sub-agent, `test_codex_steer.py`'s token usage, the reasoning deltas). It runs
through the real `CodexEventTranslator`.

## Transcript files (real, from T1)

`claude-transcript.jsonl`: the session JSONL of a headless chat run on T1 on
2026-10-16 for this purpose (personal-assistant-lite, the admin, Sonnet 5.5): the
prompt, a Bash and a refused Read, a foreground Agent spawn with its report, a
Write, a Read, the `send_url` MCP call, the assistant's `end_turn`, the CLI's own
`attachment`, `queue-operation`, `last-prompt` and `atis-latch` records (bodies
emptied) and the stop-hook summary. `codex-rollout.jsonl`: the rollout of the same
chat on Codex (GPT-6.1 Sol): `session_meta`, `turn_context`, the developer and
AGENTS.md injections (cut), `update_plan` and `exec_command` as `custom_tool_call`
pairs, `token_count` with the account's window state, `item_completed` events,
`task_complete`, and `world_state` and `token_usage_record` records (emptied).

## The edge files (composed)

`claude-transcript-edges.jsonl` and `codex-rollout-edges.jsonl` carry, in the shapes
the synthetic tailer tests pin (verified there against real transcripts), the lines
the two real files have no occasion for: a reseed prefix to strip, an `isMeta` line,
a slash-command record, a task notification, a sidechain line, a foreground and a
background Task with their results, a denied spawn, an error result, the plan
tools, a question left open at a batch's end and answered in the next, an API error
the CLI synthesized, the interrupt marker, a compaction and its reseed line, a
repeated text on a new line and a re-delivered line, an unparsable line, a blank
one; for Codex the `update_plan`, `exec_command` and `apply_patch` pairs, a tool
search, a web search, an MCP call, the skipped delegation call, two `token_count`
lines (one with the window state), `request_user_input` open then answered, an
`error` event, `turn_aborted`, `compacted`.

## The hook items and the injected events (composed from the routes)

`perm-items.json`: one permission-queue item per kind, copied from the routes that
put them (`api/hooks/lifecycle.py`, `permission.py`, `artifacts.py`, `preview.py`,
`core/session/engine_prompts.py`). `injected-events.json`: the events other modules
inject into a pump, in their producers' shapes (`api/tasks/delegation.py`,
`services/checks/render.py`, `core/layers/cli/layer.py`, the meeting orchestrator,
the two translators).

Rebuilding the scrubbed files from new recordings: the scrub and the composers are
not in the repository (every substitution keeps the length it replaces); a new recording follows the rules above and re-runs the suites in write mode
from a tree whose behaviour is the one to keep.
