# checks-mcp

The session side of checks: named units that judge an agent's work at the
end of a turn.

Tools, all proxy routes with the session JWT as the authority:

| tool | who | route |
|---|---|---|
| `list_checks` | everyone | `GET /v1/agents/{agent}/checks`, `GET /v1/checks/attached` |
| `attach_check` / `detach_check` | everyone; `detach_check` not in a task's or a delegation's run | `POST /v1/checks/attach` / `detach` |
| `run_check` | everyone | `POST /v1/checks/run` |
| `create_private_check` / `delete_private_check` | anyone with personal sessions | `PUT` / `DELETE /v1/agents/{agent}/user-checks/{name}` |
| `set_check` / `delete_check` | managers and admins, a human present (`critical` tier: a prompt on every call) | `PUT` / `DELETE /v1/agents/{agent}/checks/{name}` |

The env gate (`OTO_CAN_MANAGE_AGENT`, `OTO_TASK_TYPE`, `OTO_USERNAME`) hides
the tools that cannot succeed; the routes decide. Excluded from meetings,
phone calls and external callers. Ships the `checks` skill (on demand). A core MCP
reaches only agents created after its release: an existing agent gets it
from its MCPs page (the agent's Checks page says so). On a machine the four
session routes ride the satellite's loopback tunnel (0.5.123).
`requirements.txt` is the lock the venv builders read; `requirements.in`
its inputs.
