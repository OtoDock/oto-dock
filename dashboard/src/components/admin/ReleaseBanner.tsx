/**
 * The admin banner for a newer OtoDock release: while the daily check reports
 * one, every admin page and chat names it with the release notes and the
 * public upgrading guide. Hidden for members and on the OtoDock cloud (the
 * route answers `enabled: false` there and the Setup card shows the version
 * alone). Dismissing holds for that version: a newer one shows again.
 */

import { useState } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { isAdmin } from '../../lib/permissions'
import { RELEASE_BANNER_DISMISSED, UPGRADE_GUIDE_URL, useReleaseNotice } from './releaseNotice'
import { BUTTON_BASE, BUTTON_SIZE, BUTTON_VARIANT } from '../ui/classes'
import { BANNER_STRIP, BANNER_TONE } from './bannerStrip'

function readDismissed(): string {
  try { return localStorage.getItem(RELEASE_BANNER_DISMISSED) ?? '' } catch { return '' }
}

export function ReleaseBanner() {
  const { user, authConfig } = useAuth()
  const enabled = isAdmin(user) && !!authConfig && !authConfig.cloud
  const { data } = useReleaseNotice(enabled)
  const [dismissed, setDismissed] = useState(readDismissed)

  if (!enabled || !data?.newer || dismissed === data.latest) return null

  const dismiss = () => {
    try { localStorage.setItem(RELEASE_BANNER_DISMISSED, data.latest) } catch { /* no storage: this render only */ }
    setDismissed(data.latest)
  }

  return (
    <div role="status" className={`${BANNER_STRIP} ${BANNER_TONE.info}`}>
      <div className="flex items-start gap-2 text-sm text-primary min-w-0">
        <svg className="w-4 h-4 shrink-0 mt-0.5 text-status-info-fg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v12m0 0l-4-4m4 4l4-4M4 20h16" />
        </svg>
        <span>
          <strong>OtoDock {data.latest} is available.</strong>{' '}
          This install runs {data.running || 'an unknown version'}.{' '}
          {data.url && (
            <a href={data.url} target="_blank" rel="noopener noreferrer" className="underline font-medium text-status-info-fg">
              Release notes
            </a>
          )}
        </span>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <a
          href={UPGRADE_GUIDE_URL}
          target="_blank"
          rel="noopener noreferrer"
          className={`${BUTTON_BASE} ${BUTTON_VARIANT.primary} ${BUTTON_SIZE.xs}`}
        >
          How to upgrade
        </a>
        <button
          type="button"
          onClick={dismiss}
          aria-label="Dismiss"
          className="p-1 rounded-control text-secondary hover:bg-surface-hover hover:text-primary"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
    </div>
  )
}
