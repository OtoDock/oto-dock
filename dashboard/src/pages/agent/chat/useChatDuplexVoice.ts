/**
 * useChatDuplexVoice — AgentChat's full-duplex (phone engine) voice mode, as
 * two hooks so each is called exactly where the page declared its first hook
 * (the React hook order is unchanged):
 *
 * - useChatDuplexVoice: audioCapability + the duplex controller, the
 *   fresh-chat start (duplexPendingRef), the exit note (duplexExitNoteRef),
 *   the `?wake=1` consumption and the wake-silence auto-close. It mirrors
 *   `duplexVoice.active` into the page-owned `duplexActiveRef` — the ref is
 *   created before the stream hook, whose ping callbacks read it.
 * - useDuplexWakeArm: the wake-word ARM effect. Runs after handleNewChat,
 *   which it reuses for the stale-state reset.
 *
 * duplexVoiceProp builds ChatInput's ready-made `voice` prop (the mic
 * toggle's never-warmed branch lives there).
 */
import { useState, useEffect, useRef } from 'react'
import type { RefObject } from 'react'
import type { useSearchParams } from 'react-router-dom'
import type { useChatStream } from '../../../hooks/useChatStream'
import type { useInteractiveChat } from '../../../hooks/useInteractiveChat'
import { currentDashboardTheme } from '../../../hooks/useInteractiveChat'
import type { useChatNotifications } from '../../../hooks/useChatNotifications'
import { useDuplexVoice } from '../../../hooks/useDuplexVoice'
import type { DuplexVoiceController } from '../../../hooks/useDuplexVoice'
import { useChatAudioCapability } from '../../../hooks/useChatAudioCapability'
import type { ChatAudioCapability } from '../../../audio/types'
import type { ChatInputVoice } from '../../../components/chat/ChatInput'

type Stream = ReturnType<typeof useChatStream>

export function useChatDuplexVoice({ chatId, warming, duplexActiveRef, searchParams, setSearchParams, chatNotif }: {
  chatId: Stream['chatId']
  warming: boolean
  duplexActiveRef: RefObject<boolean>
  searchParams: URLSearchParams
  setSearchParams: ReturnType<typeof useSearchParams>[1]
  chatNotif: ReturnType<typeof useChatNotifications>
}) {
  // Full-duplex conversation mode (phone engine) — the only live voice mode
  // (the half-duplex hands-free loop was retired in its favor; the mic is
  // plain dictation now).
  const { data: audioCapability } = useChatAudioCapability()
  const duplexVoice = useDuplexVoice(chatId)
  // Fresh-chat start: a duplex session attaches to an existing chat+session,
  // so on a never-warmed chat the toggle first fires the normal warmup (the
  // same one the first message runs) and starts the session once it lands.
  const duplexPendingRef = useRef(false)
  useEffect(() => {
    if (duplexPendingRef.current && chatId && !warming) {
      duplexPendingRef.current = false
      duplexVoice.start()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatId, warming])
  // Exit note: after a live conversation ends, the next TYPED message IN THAT
  // CHAT carries a short bracketed note so the engine drops the spoken-reply
  // style (the duplex context told it the user hears replies aloud — that
  // stopped being true the moment the session closed). Bound to the chat the
  // session lived in: an armed note must never leak into another chat (it
  // showed up as the first message of a brand-new chat in live testing).
  const duplexLiveChatRef = useRef<string | null>(null)
  const duplexExitNoteRef = useRef<string | null>(null)
  // A session that never ran a turn (failed connect, instant close) never
  // injected the spoken-mode context — a note would be wrong. 'thinking'
  // marks the first dispatched utterance.
  const duplexSawTurnRef = useRef(false)
  useEffect(() => {
    if (duplexVoice.phase === 'thinking') duplexSawTurnRef.current = true
  }, [duplexVoice.phase])
  useEffect(() => {
    duplexActiveRef.current = duplexVoice.active
    if (duplexVoice.active) {
      // Captured at activation: on a chat switch the teardown flips active
      // AFTER chatId already points at the new chat, so reading chatId at
      // deactivation would bind the note to the wrong chat.
      duplexLiveChatRef.current = chatId ?? null
      duplexSawTurnRef.current = false
    } else if (duplexLiveChatRef.current) {
      // No note when the AGENT closed the session ([DUPLEX_COMPLETE]
      // farewell) — it knows the spoken mode ended; the note is only for
      // exits the agent didn't see (user toggle, idle timeout, drops).
      if (duplexSawTurnRef.current && !duplexVoice.endedByAgent) {
        duplexExitNoteRef.current = duplexLiveChatRef.current
      }
      duplexLiveChatRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [duplexVoice.active])

  // Wake word: ?wake=1 (set by useWakeWord's navigate) auto-starts a duplex
  // session on this agent's NEW chat — consume-and-strip like ?recover=1,
  // then run the mic toggle's never-warmed branch once the dashboard socket
  // is up (ws.send silently drops before that).
  const [wakeRequested, setWakeRequested] = useState(false)
  // Marks the CURRENT duplex session as wake-initiated (10 s silence guard).
  const wakeInitiatedRef = useRef(false)
  useEffect(() => {
    if (searchParams.get('wake') === '1') {
      setWakeRequested(true)
      setSearchParams(prev => { prev.delete('wake'); return prev }, { replace: true })
    }
  }, [searchParams, setSearchParams])
  // (The wake ARM effect lives below handleNewChat — it reuses it for the
  // stale-state reset and a dep-array reference above its declaration would
  // be a TDZ error.)

  // Wake-initiated sessions auto-close after 10 s of silence — the safety
  // valve for a false trigger (TV speech wakes an empty room). Any sign of a
  // real conversation (a caption, a dispatched turn) disarms it; a manual
  // toggle never arms it.
  useEffect(() => {
    if (!wakeInitiatedRef.current) return
    const phase = duplexVoice.phase
    if (phase === 'off' || phase === 'error') { wakeInitiatedRef.current = false; return }
    if (duplexVoice.caption || phase === 'thinking' || phase === 'speaking') {
      wakeInitiatedRef.current = false // real conversation — guard off
      return
    }
    if (phase !== 'listening') return
    const timer = window.setTimeout(() => {
      if (wakeInitiatedRef.current && duplexVoice.active) {
        wakeInitiatedRef.current = false
        duplexVoice.stop()
        chatNotif.playPing()
      }
    }, 10_000)
    return () => window.clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [duplexVoice.phase, duplexVoice.caption])

  return {
    audioCapability, duplexVoice, duplexPendingRef, duplexExitNoteRef,
    wakeRequested, setWakeRequested, wakeInitiatedRef,
  }
}

export function useDuplexWakeArm({
  wakeRequested, ws, warming, audioCapability, urlChatId, chatId, setWakeRequested, duplexVoice,
  wakeInitiatedRef, handleNewChat, agentName, keepAppsOnChatEntryRef, duplexPendingRef,
  mode, model, chatActiveLayer, selectedLayer, interactive,
}: {
  wakeRequested: boolean
  ws: Stream['ws']
  warming: boolean
  audioCapability: ChatAudioCapability | undefined
  urlChatId: string | undefined
  chatId: Stream['chatId']
  setWakeRequested: (v: boolean) => void
  duplexVoice: DuplexVoiceController
  wakeInitiatedRef: RefObject<boolean>
  handleNewChat: () => void
  agentName: string | undefined
  keepAppsOnChatEntryRef: RefObject<boolean>
  duplexPendingRef: RefObject<boolean>
  mode: Stream['mode']
  model: Stream['model']
  chatActiveLayer: string | null
  selectedLayer: string | null
  interactive: ReturnType<typeof useInteractiveChat>
}) {
  // Wake word ARM (?wake=1 was consumed above into wakeRequested): start
  // duplex on a FRESH chat of the route agent. Never trust the page's chatId
  // state here — right after a wake navigation from another open chat it is
  // STALE (nothing resets state for an EXTERNAL entry into the new-chat
  // view), and starting on it attached duplex to the previous agent's dead
  // interactive chat (live-hit 2026-08-14: session_dead → heal refused
  // interactive_chat → spoken error, no reply). Stale state runs
  // handleNewChat — the canonical full reset, and wake means "new chat" by
  // decision — then the effect re-fires once chatId clears.
  useEffect(() => {
    if (!wakeRequested || !ws.connected || warming) return
    if (!audioCapability?.duplex) return // capability still loading
    if (urlChatId) {
      // Explicit deep-link (?wake=1 on a concrete chat URL): start on that
      // chat once the page state has synced to it.
      if (chatId !== urlChatId) return
      setWakeRequested(false)
      if (!audioCapability.duplex.available || duplexVoice.active) return
      // (Apps keep-open was armed at render time from the ?wake=1 param —
      // the mount-commit close runs before any effect could set it here.)
      wakeInitiatedRef.current = true
      duplexVoice.start()
      return
    }
    if (chatId) { handleNewChat(); return }
    setWakeRequested(false)
    if (!audioCapability.duplex.available || duplexVoice.active) return
    if (!agentName) return
    wakeInitiatedRef.current = true
    keepAppsOnChatEntryRef.current = true // belt-and-braces beside the render-time arm
    duplexPendingRef.current = true
    ws.warmup(agentName, undefined, mode, model,
      chatActiveLayer ?? selectedLayer ?? undefined, undefined,
      interactive.chatExecMode || undefined,
      currentDashboardTheme())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wakeRequested, ws.connected, warming, audioCapability, chatId, urlChatId, handleNewChat])
}

export function duplexVoiceProp({
  audioCapability, duplexVoice, chatId, agentName, keepAppsOnChatEntryRef, duplexPendingRef,
  ws, mode, model, chatActiveLayer, selectedLayer, interactive,
}: {
  audioCapability: ChatAudioCapability | undefined
  duplexVoice: DuplexVoiceController
  chatId: Stream['chatId']
  agentName: string | undefined
  keepAppsOnChatEntryRef: RefObject<boolean>
  duplexPendingRef: RefObject<boolean>
  ws: Stream['ws']
  mode: Stream['mode']
  model: Stream['model']
  chatActiveLayer: string | null
  selectedLayer: string | null
  interactive: ReturnType<typeof useInteractiveChat>
}): ChatInputVoice {
  return {
    duplex: {
      available: !!audioCapability?.duplex?.available,
      active: duplexVoice.active,
      phase: duplexVoice.phase,
      caption: duplexVoice.caption,
      endReason: duplexVoice.endReason,
      getLevels: duplexVoice.getLevels,
      onToggle: () => {
        if (duplexVoice.active) { duplexVoice.stop(); return }
        if (chatId) { duplexVoice.start(); return }
        if (!agentName) return
        // Never-warmed chat: run the normal warmup first (no
        // message payload — the same shape the install-retry path
        // uses), then start once chatId + session land. A
        // phone-mode start from the home keeps the dashboards
        // panel showing through the chat entry (operator rule —
        // the only phone entry not derivable from the URL).
        keepAppsOnChatEntryRef.current = true
        duplexPendingRef.current = true
        ws.warmup(agentName, undefined, mode, model,
          chatActiveLayer ?? selectedLayer ?? undefined, undefined,
          interactive.chatExecMode || undefined,
          currentDashboardTheme())
      },
      muted: duplexVoice.muted,
      onToggleMute: duplexVoice.toggleMute,
      onHold: duplexVoice.hold,
      onRelease: duplexVoice.release,
      onReleaseWithDraft: duplexVoice.releaseWithDraft,
    },
  }
}
