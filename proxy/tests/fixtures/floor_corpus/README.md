# The floor's differential corpus

The questions and recorded answers of `tests/auth/test_floor_corpus.py`: the
command gate (`auth/path_shell.py`), the path policy (`auth/path_policy.py`)
and the permission authority (`api/hooks/permission.py`).

`inputs.json` holds the questions:

- `commands`: every top-level shell command a test of the suite hands the
  gate (`"source": "suite"`), recorded by wrapping the gate's functions during
  a run of the whole proxy suite on 2026-10-17, and the concrete forms the
  permission, hook and sandbox docs give (`"source": "documented"`: the
  catastrophe floor and its spellings, the host-state forms, the wrappers,
  the tiers, the protected paths, and the operands of every path-argument
  family: `sed`, `tar`, `chmod`/`chown`/`chgrp`, `egrep`/`fgrep`, a remote
  `rsync`). Every command is also a `split` question; a family's forms are
  also `path_args` questions. Each is asked under the test's fixed
  matrix of contexts (roles, placements, Shared-only, libraries, an external
  caller, the judge, satellites with and without the full filesystem, a
  Windows satellite, an `otodock` command-line session).
- `split`, `path_args`, `command_strings`, `classify`, `write_paths`,
  `path_arg_checks`, `tool_calls`: what the suite handed the inner functions,
  with the recorded context (`contexts`, only the fields that differ from the
  defaults) and flags (`is_remote`, `depth`, `dangerous_only`, `remote_host`,
  `host_asks`), replayed exactly.
- `permission_tools`: the tool calls the suite handed the permission
  authority, asked beside the test's own tool list under its scenarios.
- `questioned`: answers the corpus records as the gate gives them although
  they look wrong; they change only in a commit that says why. No test reads
  this list: it says why each of those recorded answers looks wrong.

One recorded input is left out: a 3 MB PowerShell encoded-command test input
(inputs over 16 KB are not kept).

Paths are stored against `{AGENTS}`, `{PROXY}` and `{HOME}` and replayed
against the run's own roots. The `verdicts-*.json` files hold the answers:
the distinct ones once in `verdicts`, and per case (a hash of its input) one
index per column (the matrix's contexts, the permission family's tools, or a
single answer). `FLOOR_CORPUS_WRITE=1` records them again and fails the run;
a refactor of the three modules never does.
