/**
 * An app server's state — mirrored from the proxy's authority
 * `proxy/services/apps/app_supervisor.py` (`tests/storage/test_status_vocabularies.py`
 * binds this file to it; edit both).
 *
 * What arrives: the supervisor's eight words on `X-OtoDock-Server`, the app
 * shape's `server`, the logs and deploy-status routes and the runtime
 * shim's `server_status` message — plus the shim's own `failed` for a
 * server that stayed 503 past the shim's wait. The frame adds two words of
 * its own for the viewer-token mint (`FRAME_STATE`), never spoken by the
 * proxy.
 */

export const APP_SERVER_STATE = {
  STOPPED: 'stopped',
  STARTING: 'starting',
  UP: 'up',
  BACKOFF: 'backoff',
  QUOTA_FULL: 'quota_full',
  STATIC: 'static',
  UNAPPROVED: 'unapproved',
  SECRETS: 'secrets',
  FAILED: 'failed',
} as const
export type AppServerState = (typeof APP_SERVER_STATE)[keyof typeof APP_SERVER_STATE]

/** The frame's own states while the viewer token is minted. */
export const FRAME_STATE = {
  CONNECTING: 'connecting',
  UNREACHABLE: 'unreachable',
} as const
export type FrameState = (typeof FRAME_STATE)[keyof typeof FRAME_STATE]
