/**
 * useOverlayPanels — AgentChat's overlay surfaces, as two hooks so each is
 * called exactly where the page declared its first hook (the React hook
 * order is unchanged):
 *
 * - useOverlayPanels: the workspace overlay state (useWorkspaceState, the
 *   viewer's manage/edit rights, the ?recover=1 deep link), the pinned
 *   mini-apps overlay (appsOpen / appsActive, toggleApps, useAppsAutoOpen),
 *   the agent-home live-sessions strip, the render-time ?wake=1 arm of
 *   keepAppsOnChatEntryRef (the phone-mode keep-open, created by
 *   useAgentChatStream), the Dock (projects) overlay state + the chat-pins
 *   invalidation on file_updated, and the Ctrl/Cmd+E workspace shortcut —
 *   the shortcut must stay mounted while the workspace is CLOSED (it opens
 *   it), so it lives here and not in ChatWorkspaceSlot.
 * - useAppSendPrompt: handleAppSendPrompt, the mini-app send_prompt router.
 *   Runs after handleSelectChat, which its front-page path calls.
 */
import { useState, useEffect, useCallback, useMemo } from 'react'
import type { RefObject } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { useChatStream } from '../../../hooks/useChatStream'
import type { useInteractiveChat } from '../../../hooks/useInteractiveChat'
import { ptyPasteB64, withInteractiveTime } from '../../../hooks/useInteractiveChat'
import type { useSearchParams } from 'react-router-dom'
import type { useAuth } from '../../../contexts/AuthContext'
import { useWorkspaceState } from '../../../hooks/useWorkspaceState'
import { canManageAgent, canEditAgent } from '../../../lib/permissions'
import { useActiveChats } from '../../../hooks/useActiveChats'
import { useAppsAutoOpen } from '../../../hooks/useAppsAutoOpen'
import { useApps, useChatPins, type PinnedApp } from '../../../api/apps'
import type { useChats, useTaskChats } from '../../../api/chats'
import { apiFetch } from '../../../api/auth'
import { onFileUpdate } from '../../../lib/fileUpdates'
import { buildAppActionText, substituteArgs } from '../../../lib/artifactInteraction'
import { pushEscHandler } from '../../../lib/escStack'
import { onIdle as speechIdle } from '../../../audio/speechActivity'

type Stream = ReturnType<typeof useChatStream>
type Overlays = ReturnType<typeof useOverlayPanels>

export function useOverlayPanels({
  messages, agentName, user, chatId, searchParams, setSearchParams, urlChatId, chats, taskChats, keepAppsOnChatEntryRef,
}: {
  messages: Stream['messages']
  agentName: string | undefined
  user: ReturnType<typeof useAuth>['user']
  chatId: Stream['chatId']
  searchParams: URLSearchParams
  setSearchParams: ReturnType<typeof useSearchParams>[1]
  urlChatId: string | undefined
  chats: ReturnType<typeof useChats>['data']
  taskChats: ReturnType<typeof useTaskChats>['data']
  /** Created by useAgentChatStream (the stream callbacks clear it); armed
   *  here at RENDER time from ?wake=1 and consumed by useAppsAutoOpen. */
  keepAppsOnChatEntryRef: RefObject<boolean>
}) {
  // ---- Workspace overlay ----
  // Lifted to this page so the overlay can swap the message-area while
  // keeping TopBar, status bar, and ChatInput visible. State is persisted
  // per-agent so chat switches and auto-close-on-send don't lose the user's
  // folder. Reset on agent switch.
  const lastAssistantMessageId = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'assistant') return messages[i].id
    }
    return null
  }, [messages])
  const workspace = useWorkspaceState(agentName ?? '', lastAssistantMessageId)
  const canManageThisAgent =
    !!user && !!agentName && canManageAgent(user, agentName)
  const canEditThisAgent =
    !!user && !!agentName && canEditAgent(user, agentName)

  // ---- Pinned mini-apps overlay ----
  // Permanent agent-level surface (standing dashboards), toggled from the
  // composer button right of the workspace toggle. Slot precedence:
  // workspace > apps > projects.
  const [appsOpen, setAppsOpen] = useState(false)
  const { data: pinnedApps } = useApps(agentName ?? '')
  const appsActive = appsOpen && !workspace.state.open && !!agentName
  // Agent HOME live-sessions strip (operator call 2026-07-11): on the front
  // page (no chat open) the cross-agent "Active now" rows show permanently
  // on top — dashboards/landing render below. Hook is enabled only there;
  // it returns [] otherwise, so nothing extra runs inside chats.
  const homeActiveRows = useActiveChats(!chatId && !!agentName)
  const showHomeActive =
    !chatId && !!agentName && homeActiveRows.length > 0 && !workspace.state.open
  // Deep-link wake (?wake=1 on a concrete chat URL) must arm the keep at
  // RENDER time: on a fresh mount the hook's close runs before ANY effect
  // of this component, so an effect-set ref is always too late. The param
  // is stripped by the consume effect in the same commit.
  if (searchParams.get('wake') === '1') keepAppsOnChatEntryRef.current = true
  // Apps-UI open/close rules — arrival on HOME opens (incl. agent switch),
  // entering any chat closes / never auto-opens (except a kept phone-mode
  // entry). Extracted for direct unit coverage; the rules live in the
  // hook's header comment.
  useAppsAutoOpen(agentName, urlChatId, pinnedApps, setAppsOpen, keepAppsOnChatEntryRef)
  useEffect(() => {
    if (!appsActive) return
    return pushEscHandler(() => setAppsOpen(false))
  }, [appsActive])
  const toggleApps = useCallback(() => {
    // Reveal-intent toggle: the flag can be stale-true UNDER an open
    // workspace (opening the workspace hides apps without clearing appsOpen,
    // so closing it restores them). Toggling the raw flag from that state
    // made the first click an invisible no-op — key on VISIBILITY instead.
    if (appsActive) {
      setAppsOpen(false)
      return
    }
    if (workspace.state.open) workspace.closeWorkspace()
    setAppsOpen(true)
  }, [appsActive, workspace])

  // The active chat's row — task chats resolve through the task list (they
  // never appear in the chat-mode list).
  const activeChatRow = chats?.find((c) => c.id === chatId)
    ?? taskChats?.find((c) => c.id === chatId)

  // Dock overlay (staged feature — this block + the entry button strip from
  // the public cut). Offered when the active chat participates in a
  // delegation project OR carries a chat-scoped pinned dashboard; closes on
  // chat switch like the find bar.
  const [projectsOpen, setProjectsOpen] = useState(false)
  // Every delegation participant gets the dock: orchestrators (stamped even
  // without a project slug) and workers (chat- or task-surface — the lane
  // graph falls back to lineage server-side when project_id is empty).
  const isProjectChat =
    !!activeChatRow?.project_id ||
    activeChatRow?.delegate_role === 'orchestrator' ||
    activeChatRow?.delegate_role === 'worker' ||
    activeChatRow?.origin === 'delegated'
  const { data: chatPins } = useChatPins(chatId ?? undefined)
  const hasChatPin = !!chatPins?.chat || (chatPins?.files?.length ?? 0) > 0
  const dockAvailable = isProjectChat || hasChatPin
  useEffect(() => { setProjectsOpen(false) }, [chatId])
  const projectsActive = projectsOpen && dockAvailable && !workspace.state.open && !appsOpen
  // An agent pin/re-pin broadcasts file_updated for its apps/*.html — that's
  // the signal a Dock pin appeared/changed (the toggle may need to show up
  // while the overlay is closed, so this lives here, not in the overlay).
  // File pin/unpin broadcasts carry the `pin` marker instead (any path).
  const queryClient = useQueryClient()
  useEffect(() => onFileUpdate((u) => {
    if (u.pin || /(^|\/)apps\/[^/]+\.html$/.test(u.rel_path)) {
      // Agents pin/write apps exactly at turn end — wait out live speech
      // (immediate when nothing is playing).
      speechIdle(() => queryClient.invalidateQueries({ queryKey: ['chat-pins'] }))
    }
  }), [queryClient])

  // Ctrl/Cmd+E toggles the workspace overlay.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'e' || e.key === 'E')) {
        const tag = (e.target as HTMLElement | null)?.tagName
        // Don't hijack the shortcut while the user is typing into the
        // textarea — but we still let the textarea bubble it through; if
        // they really mean to fire it they can drop focus first.
        if (tag === 'INPUT') return
        e.preventDefault()
        workspace.toggleWorkspace()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [workspace])

  // Deep-link from a file-conflict notification: ?recover=1 opens the workspace
  // overlay + the Recover bin modal, then clears the param.
  const [recoverRequested, setRecoverRequested] = useState(false)
  useEffect(() => {
    if (searchParams.get('recover') === '1') {
      if (!workspace.state.open) workspace.toggleWorkspace()
      setRecoverRequested(true)
      setSearchParams(prev => { prev.delete('recover'); return prev }, { replace: true })
    }
  }, [searchParams, setSearchParams, workspace])

  return {
    workspace, canManageThisAgent, canEditThisAgent, recoverRequested, setRecoverRequested,
    appsActive, setAppsOpen, showHomeActive, toggleApps,
    setProjectsOpen, isProjectChat, chatPins, dockAvailable, projectsActive,
  }
}

export function useAppSendPrompt({
  interactive, chatId, ws, sessionId, warmingUp, mode, model, chatActiveLayer, selectedLayer,
  setAppsOpen, setWarmingUp, serverKickPendingRef, sendAppAction, agentName, keepAppsOnChatEntryRef, handleSelectChat,
}: {
  interactive: ReturnType<typeof useInteractiveChat>
  chatId: Stream['chatId']
  ws: Stream['ws']
  sessionId: Stream['sessionId']
  warmingUp: boolean
  mode: Stream['mode']
  model: Stream['model']
  chatActiveLayer: string | null
  selectedLayer: string | null
  setAppsOpen: Overlays['setAppsOpen']
  setWarmingUp: (v: boolean) => void
  serverKickPendingRef: RefObject<false | { chatId: string | null }>
  sendAppAction: Stream['sendAppAction']
  agentName: string | undefined
  keepAppsOnChatEntryRef: RefObject<boolean>
  handleSelectChat: (selectedChatId: string, searchQuery?: string) => void
}) {
  // Mini-app send_prompt router — the host page decides the delivery rail:
  // interactive chat → typed into the terminal (composer rail, same as the
  // artifact PiP backchannel; the manifest approval gates it client-side
  // since PTY input is the user's own channel); open headless chat → the
  // app_action WS frame (server-gated); front page → create a chat, view it,
  // then deliver (retrying past the resume race). fire_task never comes here
  // (AppFrame routes it over REST).
  const handleAppSendPrompt = useCallback(
    async (
      app: PinnedApp,
      action: { id: string; label: string; prompt: string },
      args: unknown,
    ): Promise<{ status: string; reason?: string }> => {
      const substituted = substituteArgs(action.prompt, args)
      if (interactive.sessionInteractive && chatId) {
        const built = buildAppActionText(app.title || app.slug, action.label, substituted)
        if ('error' in built) return { status: 'denied', reason: built.error }
        ws.sendPtyInput(chatId, ptyPasteB64(withInteractiveTime(built.framed)), true)
        return { status: 'sent' }
      }
      // No live terminal but interactive is the effective mode (agent default
      // or per-chat toggle) — front page or a dead interactive chat. Ride the
      // composer's own cold-start rail: the framed text goes up WITH the
      // warmup and the backend delivers it into the fresh terminal (or
      // server-kicks it as a -p turn on a decline — arm the adoption ref so
      // that turn engages the streaming UI, like the composer path).
      // Without this, the fresh terminal opened and the prompt never landed.
      if (interactive.interactiveMode && agentName) {
        const built = buildAppActionText(app.title || app.slug, action.label, substituted)
        if ('error' in built) return { status: 'denied', reason: built.error }
        const routed = interactive.routeSend(built.framed, {
          chatId, sessionId, warmingUp,
          warmupParams: { agentName, chatId: chatId || undefined, mode, model, layer: chatActiveLayer ?? selectedLayer ?? undefined },
          onColdStart: () => {
            setAppsOpen(false)
            setWarmingUp(true)
            serverKickPendingRef.current = { chatId: chatId || null }
          },
        })
        if (routed) {
          interactive.setShowRichView(false)
          return { status: 'sent' }
        }
      }
      if (chatId) {
        return sendAppAction(app, action.id, action.label, substituted, args)
      }
      try {
        const res = await apiFetch('/v1/chats', {
          method: 'POST',
          body: JSON.stringify({ agent: agentName }),
        })
        if (!res.ok) return { status: 'denied', reason: 'could not start a chat' }
        const chat = (await res.json()).chat
        setAppsOpen(false)
        keepAppsOnChatEntryRef.current = false // app-action chats close the panel
        handleSelectChat(chat.id)
        for (let i = 0; i < 6; i++) {
          await new Promise((r) => setTimeout(r, 700))
          const ack = await sendAppAction(app, action.id, action.label, substituted, args)
          if (!(ack.status === 'denied' && ack.reason === 'not the viewed chat')) return ack
        }
        return { status: 'denied', reason: 'chat not ready' }
      } catch {
        return { status: 'denied', reason: 'could not start a chat' }
      }
    },
    [interactive.sessionInteractive, interactive.interactiveMode, interactive.routeSend,
     interactive.setShowRichView, chatId, sessionId, warmingUp, mode, model,
     chatActiveLayer, selectedLayer, ws, sendAppAction, agentName, handleSelectChat],
  )

  return { handleAppSendPrompt }
}
