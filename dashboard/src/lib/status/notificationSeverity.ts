/**
 * A notification's severity (`notifications.severity`,
 * `notification_deliveries.severity`) — mirrored from the proxy's authority
 * `proxy/storage/automation/notification_severity.py`
 * (`tests/storage/test_status_vocabularies.py` binds this file to it; edit
 * both). `danger` is the alarm: it loops until the toast is dismissed.
 */

export const NOTIFICATION_SEVERITY = {
  INFO: 'info',
  SUCCESS: 'success',
  WARNING: 'warning',
  DANGER: 'danger',
} as const
export type NotificationSeverity = (typeof NOTIFICATION_SEVERITY)[keyof typeof NOTIFICATION_SEVERITY]

/** In the order a picker lists them, the mildest first. */
export const NOTIFICATION_SEVERITIES = [
  NOTIFICATION_SEVERITY.INFO,
  NOTIFICATION_SEVERITY.SUCCESS,
  NOTIFICATION_SEVERITY.WARNING,
  NOTIFICATION_SEVERITY.DANGER,
] as const

/** Whether a severity is the alarm (`danger`). */
export function isAlarm(severity?: string): boolean {
  return severity === NOTIFICATION_SEVERITY.DANGER
}
