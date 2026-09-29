/**
 * A folder app's persisted deploy state (`pinned_apps.deploy_state`) —
 * mirrored from the proxy's authority `proxy/storage/db_apps.py`
 * (`tests/storage/test_status_vocabularies.py` binds this file to it; edit
 * both): `idle` (nothing parked) or `pending` (a release waits on the card
 * for approval). The deploy ANSWER (`ok | refused | pending approval |
 * rejected`) never reaches the dashboard.
 */

export const DEPLOY_STATE = {
  IDLE: 'idle',
  PENDING: 'pending',
} as const
export type DeployState = (typeof DEPLOY_STATE)[keyof typeof DEPLOY_STATE]
