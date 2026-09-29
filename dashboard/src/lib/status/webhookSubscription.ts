/**
 * The webhook subscription's status (`webhook_subscriptions.status`) —
 * mirrored from the proxy's authority
 * `proxy/storage/automation/webhook_subscription_store.py`
 * (`tests/storage/test_status_vocabularies.py` binds this file to it; edit
 * both).
 */

export const WEBHOOK_SUBSCRIPTION_STATUS = {
  CREATING: 'creating',
  ACTIVE: 'active',
  FAILED: 'failed',
  RENEW_FAILED: 'renew_failed',
  EXPIRED: 'expired',
  DISABLED: 'disabled',
} as const
export type WebhookSubscriptionStatus =
  (typeof WEBHOOK_SUBSCRIPTION_STATUS)[keyof typeof WEBHOOK_SUBSCRIPTION_STATUS]
