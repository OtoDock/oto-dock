/**
 * The MCP runtime — mirrored from the proxy's authority
 * `proxy/services/mcp/mcp_manifest_types.py` (`tests/core/test_kinds.py`
 * binds this file to it; edit both).
 *
 * A manifest's `server.runtime`: `python` and `node` are installed on the
 * host that runs them (a venv, `node_modules`), `docker` is a container on
 * the platform host, `none` is a context-only MCP with no server process.
 */

export const MCP_RUNTIME = {
  PYTHON: 'python',
  NODE: 'node',
  DOCKER: 'docker',
  NONE: 'none',
} as const
export type McpRuntime = (typeof MCP_RUNTIME)[keyof typeof MCP_RUNTIME]

/** A container the platform host drives (the docker status pill and its controls). */
export function isContainerRuntime(runtime: string | null | undefined): boolean {
  return runtime === MCP_RUNTIME.DOCKER
}
