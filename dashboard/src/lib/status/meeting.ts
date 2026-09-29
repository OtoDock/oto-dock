/**
 * The meeting's status (`meetings.status`) — mirrored from the proxy's
 * authority `proxy/storage/chat/meeting_status.py`
 * (`tests/storage/test_status_vocabularies.py` binds this file to it; edit
 * both).
 */

export const MEETING_STATUS = {
  PENDING: 'pending',
  ACTIVE: 'active',
  PAUSED: 'paused',
  CONCLUDING: 'concluding',
  CONCLUDED: 'concluded',
  FAILED: 'failed',
} as const
export type MeetingStatus = (typeof MEETING_STATUS)[keyof typeof MEETING_STATUS]
