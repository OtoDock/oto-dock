// What the chat stream's concern hooks (hooks/chatStream/use*.ts) share
// with the entry, `hooks/useChatStream.ts`: the refs that gate every frame
// handler, the setters of the status-bar state the entry owns, and the
// message-list helpers of `useChatMessages`. The entry builds one plain
// object per render and hands it to each hook in order; a hook returns its
// slice of the socket callbacks (a plain object too: useDashboardWs reads
// the newest callbacks per frame) and whatever the later hooks call.
import type { Dispatch, MutableRefObject, SetStateAction } from 'react'
import type { DisplayMessage } from '../../components/chat/types'
import type { WorkflowLive } from '../../components/chat/plan/WorkflowPanel'
import type { SessionPlan } from '../../components/chat/plan/PlanPanel'
import type { ThreadGoal } from '../useDashboardWs.types'
import type { LimitPayload } from '../../api/usage'
import type { useChatMessages } from '../useChatMessages'
import type { UseChatStreamOptions } from './types'

export type Setter<T> = Dispatch<SetStateAction<T>>
export type MeetingSpeaker = { slug: string; displayName: string; color: string }
export type MeetingParticipant = { slug: string; display_name: string; color: string }
export type TodoItem = { content: string; status: string; activeForm?: string }
export type CacheStats = { cacheRead: number; cacheWrite: number; inputTokens: number; outputTokens: number }
export type PlanStatus = 'pending' | 'implemented' | 'rejected'

type MessagesApi = ReturnType<typeof useChatMessages>

export interface StreamCore extends Omit<MessagesApi, 'seedDbHistory'> {
  options: UseChatStreamOptions
  agents: UseChatStreamOptions['agents']
  /** useChatMessages' own seed (the rows and the pagination); the history
   * hook wraps it with the cost gauge's flag. */
  seedDbRows: MessagesApi['seedDbHistory']
  /** The chat this view shows (null on the new-chat page). */
  chatId: string | null
  chatIdRef: MutableRefObject<string | null>
  discardingRef: MutableRefObject<boolean>
  abortedRef: MutableRefObject<boolean>
  erroredRef: MutableRefObject<boolean>
  liveStateSeededRef: MutableRefObject<boolean>
  thinkingBufRef: MutableRefObject<string>
  meetingSpeakerRef: MutableRefObject<MeetingSpeaker | null>
  messagesRef: MutableRefObject<DisplayMessage[]>
  prePlanModeRef: MutableRefObject<string>
  mode: string
  setMode: Setter<string>
  setSessionId: Setter<string | null>
  setTurnStartTime: Setter<number | null>
  setThinkingActive: Setter<boolean>
  setCompressingActive: Setter<boolean>
  setPermissionPending: Setter<boolean>
  setAborting: Setter<boolean>
  setWorkflows: Setter<WorkflowLive[]>
  setTotalCost: Setter<number>
  setBilledCost: Setter<number>
  setToolCost: Setter<number>
  setCostBilled: Setter<boolean>
  setContextUsed: Setter<number>
  setContextMax: Setter<number>
  setCacheStats: Setter<CacheStats>
  setSessionPlans: Setter<SessionPlan[]>
  setCurrentTodos: Setter<TodoItem[]>
  setCurrentGoal: Setter<ThreadGoal | null>
  setLimitWarning: Setter<LimitPayload | null>
  setLimitReached: Setter<boolean>
  setLimitReachedInfo: Setter<LimitPayload | null>
}
