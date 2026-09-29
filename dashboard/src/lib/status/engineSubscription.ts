/**
 * An AI engine account's status (`execution_layer_subscriptions.status`) —
 * mirrored from the proxy's authority `proxy/storage/billing/subscription_status.py`
 * (a leaf beside the store; `tests/storage/test_status_vocabularies.py` binds
 * this file to it; edit both): `active` (usable), `disabled` (switched off; a
 * reconnect keeps it off), `expired` (the grant died; a reconnect revives it).
 */

export const ENGINE_SUBSCRIPTION_STATUS = {
  ACTIVE: 'active',
  DISABLED: 'disabled',
  EXPIRED: 'expired',
} as const
export type EngineSubscriptionStatus =
  (typeof ENGINE_SUBSCRIPTION_STATUS)[keyof typeof ENGINE_SUBSCRIPTION_STATUS]
