/**
 * The admin's strip while they have connected no AI engine of their own.
 * Nobody is blocked: a person with no engine meets the warm-up's own
 * refusal where it is needed (ChatBanners, EngineNotice).
 */

import { Link } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { isAdmin } from '../lib/permissions'
import { useExecutionLayers } from '../api/agents'
import { codingEngineNames, joinOr } from '../lib/engines'
import { BUTTON_BASE, BUTTON_SIZE, BUTTON_VARIANT } from './ui/classes'
import { BANNER_STRIP, BANNER_TONE } from './admin/bannerStrip'

export default function SetupBanner() {
  const { user } = useAuth()
  // The coding engines the banner names come from the catalog (the same
  // `identity.role === 'coding'` the server's has_own_engine counts).
  const { data: layers } = useExecutionLayers()

  // undefined ⇒ older payload / still loading ⇒ stay hidden.
  if (!isAdmin(user) || user?.has_own_engine !== false) {
    return null
  }
  const engines = joinOr(codingEngineNames(Object.values(layers ?? {}))) || 'a coding engine'

  return (
    <div className={`${BANNER_STRIP} ${BANNER_TONE.warning}`}>
      <div className="flex items-start gap-2 text-sm text-status-warning-fg min-w-0">
        <svg className="w-4 h-4 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.268 16.5c-.77.833.192 2.5 1.732 2.5z" />
        </svg>
        <span>
          <strong>Connect an AI engine.</strong> Your own chats and agents need {engines}. Add one in User Settings, AI Engines.
        </span>
      </div>
      <Link
        to="/user-settings?tab=ai-engines"
        className={`${BUTTON_BASE} ${BUTTON_VARIANT.primary} ${BUTTON_SIZE.xs} shrink-0`}
      >
        Connect an AI engine
      </Link>
    </div>
  )
}
