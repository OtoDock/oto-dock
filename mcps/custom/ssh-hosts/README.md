# ssh-hosts

Gives agents SSH access to admin-configured hosts through their own shell.
Claude/Codex agents are strictly more capable with plain `ssh`/`scp`/`rsync`
than through a fixed tool schema, so actual SSH access has no exec-style tool
wrapper — the MCP contributes the framework pieces plus ONE read-only lookup
tool:

- **Instances** (`Admin → MCP Servers → SSH → Instances`): one instance per
  host — name, host/IP, port, username, SSH key. Explicit assignment: an
  agent sees a host only when an instance authorizes it.
- **Keys**: uploaded via `Admin → SSH keys` into `keys/` (0600). Keys are
  NEVER synced or tarballed off the platform host; each session gets only the
  keys its agent's authorized instances reference, copied 0600 into the
  session's private config dir and exposed as `$OTO_SSH_KEY_DIR`.
- **Prompt block**: a dynamic-context provider renders the authorized host
  list as ready-to-run `ssh -i "$OTO_SSH_KEY_DIR/<key>" -p <port> user@host`
  lines.
- **`list_ssh_hosts` tool** (`server.py`, minimal stdio server): re-fetches
  that same list mid-session via `GET /v1/agents/{agent}/ssh-hosts` — the
  prompt block is static text and fades from attention in long sessions.
  Key NAMES only; key material never rides the tool path.
- **Network carve**: `network_targets` opens sandbox egress to exactly the
  configured host:port pairs (see LOCAL-NETWORK-ACCESS docs).

`remote_policy: "admin_paired_only"` — the server (like the keys and the
prompt block) exists locally and on admin-paired satellites only; user-paired
machines get a visible exclusion instead.

Anyone authorizing an agent for a host should treat that as shell access to
it: the agent runs arbitrary ssh commands there (gated by the bash permission
tier) and can read the provisioned key material for the duration of the
session.
