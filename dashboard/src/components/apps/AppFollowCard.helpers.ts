import type { FollowResult, PinnedApp } from '../../api/apps'
import { DEPLOY_STATE } from '../../lib/status/appDeploy'

/** The words and the small rules of `AppFollowCard`, apart from the
 * component (the hosts and the tests read them). */

/** `release`: the release of this app the notice says waits for approval
 * here, so the notice goes once that release is decided. */
export interface FollowNoticeState { text: string; error: boolean; copy?: string; release?: number }

/** Whether the app shows the release the notice says waits here. */
export function noticeWaits(notice: FollowNoticeState, app: PinnedApp): boolean {
  return !!notice.release && app.deploy_state === DEPLOY_STATE.PENDING && app.pending_release === notice.release
}

export const FOLLOW_MODE = { UPDATE: 'update', MERGE: 'merge', ASKED: 'asked' } as const

/** The two menu rows the hosts ask about first. */
export const FOLLOW_CONFIRM = { SEND: 'send', STOP: 'stop' } as const
export type FollowConfirmKind = (typeof FOLLOW_CONFIRM)[keyof typeof FOLLOW_CONFIRM]

const DISMISSED_PREFIX = 'follow-dismissed:'

/** The release Not now was last pressed for, per app (0 = never). */
export function dismissedRelease(appId: string): number {
  try {
    return Number(window.localStorage.getItem(DISMISSED_PREFIX + appId) || 0)
  } catch {
    return 0
  }
}

export function dismiss(appId: string, release: number): void {
  try {
    window.localStorage.setItem(DISMISSED_PREFIX + appId, String(release))
  } catch {
    // A browser that refuses storage shows the card again next time.
  }
}

/** Whether the card has something to show for this row. */
export function showsFollowCard(app: PinnedApp, dismissedAt = 0): boolean {
  const f = app.follows
  if (!f || !f.update || !f.exists || !f.can_take || !app.can_manage) return false
  return f.update.release > dismissedAt
}

/** The prompt the person gives the agent after a merge is staged. */
export function mergePrompt(app: PinnedApp, r: Pick<FollowResult, 'release' | 'path'>): string {
  const source = app.follows?.agent_name || app.follows?.agent || 'the source'
  const where = r.path || 'beside the app'
  return `Merge release ${r.release} of ${source}’s app, unpacked at ${where}, into the app at /${app.rel_path} and deploy one release.`
}
