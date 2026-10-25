/**
 * ChatComposerBar — AgentChat's floating bottom bar: ChatStatusBar, the usage
 * limit banner + warning toast, and ChatInput (its duplex `voice` prop is
 * built by pages/agent/chat/useChatDuplexVoice.ts).
 * Presentational: every value is computed by the page. `ChatBottomPanel`
 * holds it with the banners above it and draws the fade over the chat.
 */
import type { Dispatch, JSX, ReactNode, SetStateAction } from 'react'
import type { useChatStream } from '../../../hooks/useChatStream'
import type { useInteractiveChat } from '../../../hooks/useInteractiveChat'
import { utf8ToB64 } from '../../../hooks/useInteractiveChat'
import type { useWorkspaceState } from '../../../hooks/useWorkspaceState'
import type { LayerCapabilities } from '../../../api/agents'
import type { computeModelGroups } from '../../../lib/modelGroups'
import { useChatStore } from '../../../store/chatStore'
import ChatStatusBar from '../../../components/chat/ChatStatusBar'
import ChatInput, { PendingImage, PendingFile } from '../../../components/chat/ChatInput'
import type { QueuedMessage } from '../../../store/types'
import type { ChatInputVoice } from '../../../components/chat/ChatInput'
import TerminalControlBar from '../../../components/chat/terminal/TerminalControlBar'
import { describeLimitReached, describeLimitWarning } from '../../../components/usage/poolCap'
import { usePrimaryPointerCoarse } from '../../../hooks/usePrimaryPointerCoarse'
import { withSendHint } from '../../../lib/composerKeys'
import type { CollectedItems } from '../../../lib/droppedItems'
import { DONT_ASK_MODE, currentMode, runsWithoutAsking } from '../../../lib/engines/modes'
import { TOAST } from '../../../components/ui/classes'

type Stream = ReturnType<typeof useChatStream>

interface Props {
  // ChatStatusBar
  viewedStreaming: boolean
  warming: boolean
  turnStartTime: Stream['turnStartTime']
  thinkingActive: boolean
  compressingActive: boolean
  activeAgents: Stream['activeAgents']
  mode: string
  pendingEngineSwitch: { layer: string; model: string } | null
  model: string
  modelCompound: string
  billedCost: number
  toolCost: number
  costBilled: Stream['costBilled']
  contextUsed: number
  contextMax: number
  cacheStats: Stream['cacheStats']
  meetingActive: boolean
  permissionModes: string[] | undefined
  agentLayerModels: LayerCapabilities['models']
  modelGroups: ReturnType<typeof computeModelGroups>
  interactiveAvailable: boolean
  interactive: ReturnType<typeof useInteractiveChat>
  interactiveLocked: boolean
  /** Non-empty on a chat that runs as the agent this person may not drive:
   *  locks the mode, model and terminal pickers, with this as the reason. */
  pickersLockReason: string
  handleInteractiveToggle: (next: boolean) => void
  handleToggleRichView: () => void
  isTaskChat: boolean
  chatId: string | null
  agentName?: string
  ws: Stream['ws']
  handleModeChange: (m: string) => void
  handleModelChange: (compound: string) => void
  chatActiveLayer: string | null
  effectiveLayer: string
  // The effective engine compacts on request (its descriptor's
  // behaviour.supports_compact — lib/engines supportsCompact).
  supportsCompact: boolean
  // Usage limit banner + toast
  limitReached: boolean
  limitReachedInfo: Stream['limitReachedInfo']
  limitWarning: Stream['limitWarning']
  setLimitWarning: Stream['setLimitWarning']
  /** Engine id → label (lib/engines engineLabels over the page's catalog), so
   *  the banner names the engine whose pool cap blocked the turn. */
  engineLabels: Record<string, string>
  // ChatInput
  draftInput: string
  setDraftInput: (text: string) => void
  handleSend: (text: string) => void
  handleAbort: () => void
  handleEditQueued: () => void
  handleEngage: () => void
  permissionPending: boolean
  warmingUp: boolean
  aborting: boolean
  queuedMessages: QueuedMessage[]
  editText: Stream['editText']
  setEditText: Stream['setEditText']
  pendingImages: PendingImage[]
  draftKey: string
  pendingFiles: PendingFile[]
  handleAddFiles: (files: PendingFile[]) => void
  handleAddFolder: (name: string, items: CollectedItems | Promise<CollectedItems>) => void
  handleRemoveFile: (id: string) => void
  handleRetryFile: (id: string) => void
  workspace: ReturnType<typeof useWorkspaceState>
  appsActive: boolean
  toggleApps: () => void
  setAppsOpen: Dispatch<SetStateAction<boolean>>
  projectsActive: boolean
  isProjectChat: boolean
  dockAvailable: boolean
  setProjectsOpen: Dispatch<SetStateAction<boolean>>
  // ChatInput's duplex voice controls — built by pages/agent/chat/useChatDuplexVoice.ts
  voice: ChatInputVoice
  /** Animate the composer's shrink and regrow (ChatInput `animateHeight`). */
  composerAnimates: boolean
  /** The chat's "Runs on" chip, rendered by ChatStatusBar beside the mode
   *  and model controls. */
  targetSlot?: JSX.Element
  /** Non-empty while the chat is read-only for this person: the
   *  composer is disabled and says why. */
  composerLockReason?: string
}

export default function ChatComposerBar({
  viewedStreaming, warming, turnStartTime, thinkingActive, compressingActive, activeAgents,
  mode, pendingEngineSwitch, model, modelCompound, billedCost, toolCost, costBilled, contextUsed,
  contextMax, cacheStats, meetingActive, permissionModes, agentLayerModels, modelGroups,
  interactiveAvailable, interactive, interactiveLocked, pickersLockReason, handleInteractiveToggle,
  handleToggleRichView, isTaskChat, chatId, agentName, ws, handleModeChange, handleModelChange,
  chatActiveLayer, supportsCompact,
  limitReached, limitReachedInfo, limitWarning, setLimitWarning, engineLabels,
  draftInput, setDraftInput, handleSend, handleAbort, handleEditQueued, handleEngage,
  permissionPending, warmingUp, aborting, queuedMessages, editText, setEditText,
  pendingImages, draftKey, pendingFiles, handleAddFiles, handleAddFolder, handleRemoveFile, handleRetryFile,
  workspace, appsActive, toggleApps, setAppsOpen,
  projectsActive, isProjectChat, dockAvailable, setProjectsOpen,
  voice, composerAnimates, targetSlot, composerLockReason = '',
}: Props) {
  const touchPrimary = usePrimaryPointerCoarse()
  return (
    <div data-composer-bar className="shrink-0 relative bg-surface-page">
      <div className="max-w-4xl mx-auto">
        <ChatStatusBar
          streaming={viewedStreaming}
          warming={warming}
          startTime={turnStartTime}
          thinkingActive={thinkingActive}
          compressingActive={compressingActive}
          activeAgents={activeAgents}
          mode={runsWithoutAsking(mode) ? DONT_ASK_MODE : currentMode(mode)}
          // While a cross-engine pick is pending these are DISPLAY-ONLY
          // overrides — the real `model` state stays on the chat's
          // engine (it feeds handleSend/pre-warm; mutating it would warm
          // the wrong engine if the switch is cancelled).
          model={pendingEngineSwitch ? pendingEngineSwitch.model : model}
          modelValue={pendingEngineSwitch
            ? `${pendingEngineSwitch.layer}::${pendingEngineSwitch.model}`
            : modelCompound}
          costUsd={billedCost}
          toolCostUsd={toolCost}
          costBilled={costBilled}
          contextUsed={contextUsed}
          contextMax={contextMax}
          cacheStats={cacheStats}
          meetingActive={meetingActive}
          permissionModes={permissionModes}
          modelOptions={agentLayerModels}
          modelGroups={modelGroups}
          // No terminal toggle on a task chat.
          interactiveAvailable={interactiveAvailable && !isTaskChat}
          interactiveOn={interactive.interactiveMode}
          interactiveDisabled={interactiveLocked || !!pickersLockReason}
          onInteractiveToggle={handleInteractiveToggle}
          richViewAvailable={interactive.sessionInteractive}
          richViewActive={interactive.showRichView}
          onToggleRichView={handleToggleRichView}
          hidePermissions={interactive.interactiveMode || interactive.sessionInteractive}
          interactiveActive={interactive.interactiveMode || interactive.sessionInteractive}
          // A task run's PERMISSIONS are the run's fact ('auto'
          // posture) — display-only. Its MODEL (1.5) follows the same
          // rule as every chat: while the run/session is alive the
          // picker offers the active engine's models only
          // (computeModelGroups), once dead the cross-engine expansion
          // + confirm flow applies — follow-up turns resolve
          // model/engine from the chat row, so the pick governs
          // exactly those. Interactive PTY sessions stay locked (the
          // terminal owns its process).
          modelLocked={interactive.sessionInteractive || !!pickersLockReason}
          modeLocked={isTaskChat || !!pickersLockReason}
          lockReason={pickersLockReason || undefined}
          leftSlot={interactive.sessionInteractive && chatId
            ? <TerminalControlBar className="flex-1 min-w-0" send={(seq) => ws.sendPtyInput(chatId, utf8ToB64(seq))} />
            : undefined}
          targetSlot={targetSlot}
          onModeChange={handleModeChange}
          onModelChange={handleModelChange}
          // Lazy liveness re-probe on dropdown open (committed chats
          // only): headless idle-reap emits no frame, so without this
          // the cross-engine options never appear on a chat that died
          // while being viewed.
          onModelMenuOpen={chatId && chatActiveLayer
            ? () => ws.probeLiveness()
            : undefined}
          onCompactContext={
            // Manual compaction is offered where the engine's descriptor
            // declares it (behaviour.supports_compact — Codex's
            // thread/compact/start today) and headless-only (interactive
            // users type /compact in the TUI); hidden while a compaction is
            // already in flight.
            supportsCompact
            && !interactive.sessionInteractive && !compressingActive
              ? () => ws.compactContext()
              : undefined
          }
        />
      </div>
      {/* Usage limit banner — names the budget that blocked (platform
          budget, own API-key budget, or the subscription pool cap). */}
      {limitReached && (
        <div data-testid="limit-banner" className="mx-4 mb-2 p-3 rounded-card bg-status-danger-bg border border-status-danger-border text-sm text-status-danger-fg">
          <strong>{describeLimitReached(limitReachedInfo, engineLabels).title}</strong>{' '}
          {describeLimitReached(limitReachedInfo, engineLabels).body}
        </div>
      )}
      {/* Usage limit warning toast */}
      {limitWarning && (
        <div className={`fixed top-4 right-4 z-50 max-w-sm p-4 ${TOAST} border-l-4 border-l-status-warning-solid text-sm`}>
          <div className="flex items-start gap-2">
            <span className="text-status-warning-solid text-lg leading-none">&#9888;</span>
            <div>
              <p className="font-medium text-status-warning-fg">Usage limit warning</p>
              <p className="text-secondary mt-0.5">{describeLimitWarning(limitWarning, engineLabels)}</p>
            </div>
            <button onClick={() => setLimitWarning(null)} className="text-tertiary hover:text-primary ml-auto">&times;</button>
          </div>
        </div>
      )}
      <ChatInput
        value={draftInput}
        agentName={agentName}
        draftKey={draftKey}
        onChange={setDraftInput}
        onSend={handleSend}
        onAbort={handleAbort}
        onEditQueued={handleEditQueued}
        onEngage={handleEngage}
        disabled={!ws.connected || limitReached || !!composerLockReason}
        sendDisabled={!!pendingEngineSwitch}
        streaming={(viewedStreaming && !permissionPending) || warmingUp}
        queueable={viewedStreaming && !permissionPending && !warmingUp}
        aborting={aborting}
        placeholder={composerLockReason || (pendingEngineSwitch
          ? 'Confirm or cancel the engine switch first…'
          : limitReached ? 'Usage limit reached'
            : withSendHint(viewedStreaming ? 'Type to queue a message' : 'Type a message', touchPrimary))}
        queuedCount={queuedMessages.length}
        editText={editText}
        onClearEditText={() => setEditText(null)}
        pendingImages={pendingImages}
        onAddImages={(imgs) => draftKey && useChatStore.getState().addPendingImages(draftKey, imgs)}
        onRemoveImage={(id) => draftKey && useChatStore.getState().removePendingImage(draftKey, id)}
        pendingFiles={pendingFiles}
        onAddFiles={handleAddFiles}
        onAddFolder={handleAddFolder}
        onRemoveFile={handleRemoveFile}
        onRetryFile={handleRetryFile}
        workspaceOpen={workspace.state.open}
        onToggleWorkspace={workspace.toggleWorkspace}
        workspaceHasNewMessage={workspace.state.hasNewMessage}
        appsOpen={appsActive}
        onToggleApps={toggleApps}
        projectsOpen={projectsActive}
        dockKind={isProjectChat ? 'project' : 'chat'}
        onToggleProjects={dockAvailable ? () => {
          // Same reveal-intent shape as toggleApps (visibility, not flag).
          if (projectsActive) { setProjectsOpen(false); return }
          if (workspace.state.open) workspace.closeWorkspace()
          setAppsOpen(false)
          setProjectsOpen(true)
        } : undefined}
        voice={voice}
        animateHeight={composerAnimates}
      />
    </div>
  )
}

/** The chat page's bottom panel: the banners, the lock line and the
 *  composer, on the page colour. The fade over the chat's last lines sits
 *  above the whole panel, so it never covers a banner. */
export function ChatBottomPanel({ children }: { children: ReactNode }) {
  return (
    <div data-chat-bottom className="shrink-0 relative bg-surface-page">
      <div
        data-composer-fade
        aria-hidden="true"
        className="absolute left-0 right-0 bottom-full h-4 bg-linear-to-t from-surface-page to-transparent pointer-events-none"
      />
      {children}
    </div>
  )
}
