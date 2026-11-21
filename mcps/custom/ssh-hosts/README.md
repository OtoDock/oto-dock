# ssh-hosts

Gives agents SSH access to admin-configured hosts through their own shell.
Claude/Codex agents are strictly more capable with plain `ssh`/`scp`/`rsync`
than through a fixed tool schema. So actual SSH access has no exec-style tool
wrapper. The MCP contributes the framework pieces plus ONE read-only lookup
tool:

- **Instances** (`Admin → MCP Servers → SSH → Instances`): one instance per
  host, with name, host/IP, port, username, SSH key. Explicit assignment: an
  agent sees a host only when an instance authorizes it.
- **Keys**: uploaded via `Admin → SSH keys` into `keys/` (0600). Keys never
  leave the platform. The platform signs each login through the session's
  SSH agent socket (`SSH_AUTH_SOCK`). It uses only the keys the agent's
  authorized instances reference. The session's config dir holds the public
  halves alone, exposed as `$OTO_SSH_KEY_DIR`. A check's judge and a phone
  caller who is not a platform user get no keys.
- **Prompt block**: a dynamic-context provider renders the authorized host
  list as ready-to-run
  `ssh -i "$OTO_SSH_KEY_DIR/<key>.pub" -o IdentitiesOnly=yes -p <port> user@host`
  lines.
- **`list_ssh_hosts` tool** (`server.py`, minimal stdio server): re-fetches
  that same list mid-session via `GET /v1/agents/{agent}/ssh-hosts`. The
  prompt block is static text and fades from attention in long sessions.
  Key NAMES only: key material never rides the tool path.
- **Network carve**: `network_targets` opens sandbox egress to exactly the
  configured host:port pairs.

The tool reaches every machine kind at the editor tier, a person's own machine
included.

Anyone authorizing an agent for a host should treat that as shell access to
it. The agent runs arbitrary ssh commands there (gated by the bash permission
tier). It can ask the session's agent socket for signatures for the duration
of the session, but never reads the key.
