/**
 * useChatUrlEntry: the chat page's answer to its own URL. AgentChat is one
 * route element for `/chat/:agent` (the new-chat view) and
 * `/chat/:agent/:chatId`, so a navigation from outside the page (a
 * notification, back or forward, a pasted address) reuses it and every piece
 * of the previous chat's state must be reset here.
 */
import { useEffect } from 'react'
import type { RefObject } from 'react'
import { seedMode } from '../../../lib/engines/modes'
import { useAgentPrefsStore } from '../../../store/agentPrefsStore'
import type { useInteractiveChat } from '../../../hooks/useInteractiveChat'
import type { useAgentChatStream } from './useAgentChatStream'
import type { useFindBar } from './useFindBar'
import type { useModelEngineSelection } from './useModelEngineSelection'

type AgentStream = ReturnType<typeof useAgentChatStream>

export type ChatUrlEntryParams = Pick<AgentStream,
  | 'ws' | 'setMessages' | 'currentMsgRef' | 'warmupSends' | 'thinkingBufRef' | 'setChatId'
  | 'setSessionId' | 'preWarmedRef' | 'setTotalCost' | 'setBilledCost' | 'setToolCost'
  | 'setLimitReached' | 'setLimitWarning' | 'setContextUsed' | 'setContextMax' | 'setSessionPlans'
  | 'setCurrentTodos' | 'setCurrentGoal' | 'setMeetingActive' | 'setMeetingParticipants'
  | 'setMeetingSpeaker' | 'setMeetingRound' | 'meetingSpeakerRef' | 'setTurnStartTime'
  | 'setThinkingActive' | 'setCompressingActive' | 'setWorkflows' | 'setPermissionPending'
  | 'setAborting' | 'setModel' | 'setMode' | 'discardingRef' | 'lastResumedChatIdRef'>
  & Pick<ReturnType<typeof useModelEngineSelection>, 'setChatActiveLayer' | 'setSelectedLayer'>
  & Pick<ReturnType<typeof useFindBar>, 'setFindBarOpen' | 'setFindInput' | 'setFindQuery'>
  & Pick<ReturnType<typeof useInteractiveChat>, 'resetSession' | 'seedExecMode'>
  & {
    agentName: string | undefined
    urlChatId: string | undefined
    /** The chat the page shows: state, which an entry from outside leaves behind. */
    chatId: string | null
    agentDefaultModel: string
    setWarmingUp: (warming: boolean) => void
    heldForAgentsRef: RefObject<unknown>
    newChatModeSeededRef: RefObject<string | null>
  }

export function useChatUrlEntry({
  ws, agentName, urlChatId, chatId, agentDefaultModel, resetSession, seedExecMode,
  setMessages, currentMsgRef, warmupSends, heldForAgentsRef, thinkingBufRef, setChatId,
  setSessionId, setChatActiveLayer, setSelectedLayer, setWarmingUp, preWarmedRef,
  setTotalCost, setBilledCost, setToolCost, setLimitReached, setLimitWarning,
  setContextUsed, setContextMax, setSessionPlans, setCurrentTodos, setCurrentGoal,
  setMeetingActive, setMeetingParticipants, setMeetingSpeaker, setMeetingRound,
  meetingSpeakerRef, setTurnStartTime, setThinkingActive, setCompressingActive,
  setWorkflows, setPermissionPending, setAborting, setFindBarOpen, setFindInput,
  setFindQuery, setModel, setMode, discardingRef, lastResumedChatIdRef, newChatModeSeededRef,
}: ChatUrlEntryParams) {
  // Resume / reset chat state when the URL chat changes. Handles three cases:
  //   - Initial mount with /chat/:agent/:chatId
  //   - In-component navigation via handleSelectChat / handleNewChat (which pre-set
  //     lastResumedChatIdRef to skip this branch — they already reset state inline)
  //   - External URL change: notification deeplinks (incl. cross-agent), browser
  //     back/forward, direct URL paste. AgentChat is reused across same-Route
  //     navigations so we must reset all chat state explicitly here, for an
  //     entry into the new-chat view as for one into a chat.
  useEffect(() => {
    if (!ws.connected || !agentName) return
    if (!urlChatId) {
      lastResumedChatIdRef.current = null  // exited chat — clear tracking
      // Entering the NEW-chat view: the previously-viewed chat's execution-
      // mode override must NOT leak into the new chat (it ran new chats in
      // the wrong mode both ways). Reset the interactive state, then re-seed
      // the agent's sticky pick. Once per visit — the ref guard keeps a WS
      // reconnect / re-render from clobbering a toggle made while composing.
      if (newChatModeSeededRef.current === agentName) return
      newChatModeSeededRef.current = agentName
      resetSession()
      seedExecMode(useAgentPrefsStore.getState().lastInteractive[agentName] || '')
      // A chat still on screen at this entry (a notification, back or forward,
      // another agent's address) leaves with all its state: kept, it showed
      // under the new-chat address as not active, its terminal gone.
      // handleNewChat clears the chat before its navigate.
      if (!chatId) return
    } else {
      newChatModeSeededRef.current = null  // viewing a chat — re-arm for the next new-chat visit
      if (lastResumedChatIdRef.current === urlChatId) return  // already loaded
      lastResumedChatIdRef.current = urlChatId
    }

    // Full reset (mirrors handleNewChat — most-thorough variant, safe for both
    // same-agent and cross-agent transitions; agent-specific defaults like
    // chatActiveLayer/model are re-set by onChatHistory when the chat loads).
    discardingRef.current = true
    setMessages([])
    currentMsgRef.current = null
    warmupSends.discard()
    heldForAgentsRef.current = null  // a held first send must not flush into the opened chat
    thinkingBufRef.current = ''
    setChatId(urlChatId ?? null)
    setSessionId(null)
    setChatActiveLayer(null)
    setSelectedLayer(null)
    setWarmingUp(false)
    preWarmedRef.current = null
    setTotalCost(0)
    setBilledCost(0)
    setToolCost(0)
    setLimitReached(false)
    setLimitWarning(null)
    setContextUsed(0)
    setContextMax(0)
    setSessionPlans([])
    setCurrentTodos([])
    setCurrentGoal(null)
    setMeetingActive(false)
    setMeetingParticipants([])
    setMeetingSpeaker(null)
    setMeetingRound(0)
    meetingSpeakerRef.current = null
    setTurnStartTime(null)
    setThinkingActive(false)
    setCompressingActive(false)
    setWorkflows([])
    setPermissionPending(false)
    setAborting(false)
    setFindBarOpen(false)
    setFindInput('')
    setFindQuery('')
    // Same sticky re-seed as handleSelectChat: a task chat's restored model +
    // 'auto' mode must not leak into the chat this navigation opens.
    if (agentName) {
      const prefs = useAgentPrefsStore.getState()
      setModel(prefs.lastModel[agentName] || agentDefaultModel)
      setMode(seedMode(prefs.lastMode[agentName]))
    }

    if (urlChatId) ws.resumeChat(urlChatId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ws.connected, agentName, urlChatId])
}
