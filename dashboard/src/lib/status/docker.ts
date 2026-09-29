/**
 * A Docker MCP's container status — mirrored from the proxy's authority
 * `proxy/services/mcp/docker_manager.py` (`tests/storage/test_status_vocabularies.py`
 * binds this file to it; edit both): the six words the container probe
 * returns plus the two the admin list route stamps (`not_checked` — the
 * MCP is not enabled; `unknown` — the probe raised). The enable route's
 * own answer (`ENABLE_DOCKER`) is beside them.
 */

export const DOCKER_STATUS = {
  RUNNING: 'running',
  UNHEALTHY: 'unhealthy',
  STARTING: 'starting',
  STOPPED: 'stopped',
  NOT_FOUND: 'not_found',
  ERROR: 'error',
  NOT_CHECKED: 'not_checked',
  UNKNOWN: 'unknown',
} as const
export type DockerStatus = (typeof DOCKER_STATUS)[keyof typeof DOCKER_STATUS]

/** The enable route's `docker_status` answer. */
export const ENABLE_DOCKER = {
  STARTED: 'started',
  FAILED: 'failed',
} as const
export type EnableDockerResult = (typeof ENABLE_DOCKER)[keyof typeof ENABLE_DOCKER]
