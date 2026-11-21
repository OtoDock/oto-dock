// Where an AI engine is needed: the refusal (`NoSubscriptionError`) names why,
// and the page offers a way to connect one only when connecting cures it.
import type { User } from '../../api/auth'
import { isAdmin } from '../permissions'

/** The reasons a connection cures. `local_only` and `pool_cap` keep their
 *  sentence with no button, and `throttled` is never a notice. */
export const ENGINE_CONNECT_REASONS: ReadonlySet<string> = new Set([
  'auth_off', 'admin_oauth_only', 'no_pool', 'none', 'own_sub_expired',
])

/** A new chat's pre-warm the builders refused (`pre_warmup_refused`). */
export interface PreWarmRefusal {
  agent: string
  executionPath: string
  reason: string
  error: string
}

export interface EngineNoticeValue {
  message: string
  reason: string
}

/** The notice over the composer of a new chat: this agent's, on the engine
 *  the page would start on. Hidden on an existing chat (its send meets the
 *  card) and while an admin's strip already says it. */
export function newChatEngineNotice(
  refusal: PreWarmRefusal | null, agentName: string | undefined, preWarmPath: string,
  chatId: string | null, user: User | null,
): EngineNoticeValue | null {
  if (!refusal || chatId || refusal.agent !== agentName || refusal.executionPath !== preWarmPath) return null
  if (refusal.reason === 'throttled') return null
  if (isAdmin(user) && user?.has_own_engine === false) return null
  return { message: refusal.error, reason: refusal.reason }
}
