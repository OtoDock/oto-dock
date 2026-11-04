/**
 * A department set's install job (`set_installs.status`) — mirrored from
 * the proxy's authority `proxy/storage/agents/db_set_installs.py`
 * (`tests/storage/test_status_vocabularies.py` binds this file to it; edit
 * both). The job ends in one of the last three; a restart marks a running
 * one interrupted.
 */

export const SET_INSTALL_STATUS = {
  RUNNING: 'running',
  DONE: 'done',
  FAILED: 'failed',
  INTERRUPTED: 'interrupted',
} as const
export type SetInstallStatus = (typeof SET_INSTALL_STATUS)[keyof typeof SET_INSTALL_STATUS]
