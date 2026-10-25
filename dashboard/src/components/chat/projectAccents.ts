import { Chat } from '../../api/chats'

// Sidebar linkage accents for delegation participants, role-based and
// always on: one purple family (the agent accent, what delegation reads as
// inside a chat too), ranked by strength. An orchestrator, any session that
// used delegate, gets the full accent on a wider edge, so it is the row to
// spot; a delegated worker gets the same accent at half strength. The edge
// only: a purple row background is the live state (generating, active), so
// the role never paints the row. Age is not encoded here: the row's time
// says it.

const WORKER_EDGE = 'border-l-2 border-l-accent-agent-solid/50'
const ORCHESTRATOR_EDGE = 'border-l-[3px] border-l-accent-agent-solid'
const NO_EDGE = 'border-l-2 border-l-transparent'

function role(chat: Chat): 'orchestrator' | 'worker' | null {
  if (chat.delegate_role === 'orchestrator') return 'orchestrator'
  if (chat.origin === 'delegated' || chat.delegate_role === 'worker') return 'worker'
  return null
}

/** The left-edge classes for one sidebar row. The ACTIVE chat gets the
 * transparent edge: the sidebar draws its own selection bar there, and an
 * accent beside it reads as a double border. */
export function rowAccentClass(chat: Chat, opts: { active?: boolean } = {}): string {
  if (opts.active) return NO_EDGE
  const r = role(chat)
  if (r === 'orchestrator') return ORCHESTRATOR_EDGE
  if (r === 'worker') return WORKER_EDGE
  return NO_EDGE
}
