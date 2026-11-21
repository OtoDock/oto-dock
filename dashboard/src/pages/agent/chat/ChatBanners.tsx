/**
 * ChatBanners — the banner stack between AgentChat's main slot and the
 * composer: machine update, remote fallback, the platform's update line,
 * the warm-up's refusal on a new chat, target mismatch, install progress,
 * the cross-engine switch banner and, nearest the composer, the chat's lock.
 * Every notice takes the composer's own column (`COMPOSER_COLUMN`).
 * Presentational: every value is computed by the page.
 */
import type { RefObject } from 'react'
import type { useChatStream } from '../../../hooks/useChatStream'
import type { useInteractiveChat } from '../../../hooks/useInteractiveChat'
import { currentDashboardTheme } from '../../../hooks/useInteractiveChat'
import type { LayerCapabilities } from '../../../api/agents'
import { machineOf } from '../../../lib/placement'
import type { TargetMismatch } from '../../../store/chatStore'
import InstallProgressBar from '../../../components/chat/InstallProgressBar'
import MachineUpdateBanner from '../../../components/chat/MachineUpdateBanner'
import RemoteFallbackBanner from '../../../components/chat/RemoteFallbackBanner'
import ChatTargetBanner from '../../../components/chat/ChatTargetBanner'
import EngineSwitchBanner from '../../../components/chat/EngineSwitchBanner'
import EngineNotice from '../../../components/common/EngineNotice'
import { ChatLockNotice } from '../../../components/chat/RunsOnChip'
import { COMPOSER_COLUMN, COMPOSER_INSET } from '../../../components/ui/classes'
import type { EngineNoticeValue } from '../../../lib/engines/notice'

type Stream = ReturnType<typeof useChatStream>

interface Props {
  sessionExecutionTarget: Stream['sessionExecutionTarget']
  sessionFallbackReason: Stream['sessionFallbackReason']
  offlineMachineName: Stream['offlineMachineName']
  engineNotice: EngineNoticeValue | null
  chatId: string | null
  pendingEngineSwitch: { layer: string; model: string } | null
  targetMismatch: TargetMismatch | null
  /** A phone call drives the viewed chat (its history is read-only until
   *  the call ends and the page reloads it). */
  phoneCallActive: boolean
  /** The platform holds new work before it stops for an update
   *  (`server_info.updating`, lib/session/serverInfo). */
  platformUpdating: boolean
  /** Why this person cannot write in the chat now (a call writes in it, it
   *  runs on someone else's computer), "" when they can. */
  lockSentence: string
  viewedStreaming: boolean
  warming: boolean
  ws: Stream['ws']
  agentName: string | undefined
  mode: string
  model: string
  chatActiveLayer: string | null
  selectedLayer: string | null
  interactive: ReturnType<typeof useInteractiveChat>
  preWarmedRef: RefObject<string | null>
  preWarmPath: string
  layers: Record<string, LayerCapabilities> | undefined
  engineSwitchBusy: boolean
  engineSwitchError: string | null
  handleEngineSwitchConfirm: () => void
  handleEngineSwitchCancel: () => void
}

export default function ChatBanners({
  sessionExecutionTarget, sessionFallbackReason, offlineMachineName, engineNotice,
  chatId, pendingEngineSwitch, targetMismatch, phoneCallActive, platformUpdating, lockSentence,
  viewedStreaming, warming, ws, agentName,
  mode, model, chatActiveLayer, selectedLayer, interactive, preWarmedRef, preWarmPath,
  layers, engineSwitchBusy, engineSwitchError,
  handleEngineSwitchConfirm, handleEngineSwitchCancel,
}: Props) {
  return (
    <div className={COMPOSER_INSET}>
      <div className={COMPOSER_COLUMN}>
      <MachineUpdateBanner
        machineId={machineOf(sessionExecutionTarget) || null}
      />

      <RemoteFallbackBanner
        fallbackReason={sessionFallbackReason}
        machineName={offlineMachineName}
      />

      {/* An existing chat only: its message waits in the chat's queue. A
          new chat's first message is refused meanwhile, in the server's
          own sentence. */}
      {platformUpdating && chatId && (
        <div
          role="status"
          data-testid="platform-updating-notice"
          className="w-full px-3 py-2 text-xs rounded-card border border-status-info-border bg-status-info-bg text-status-info-fg"
        >
          OtoDock is updating. Messages you send now go out when it is back.
        </div>
      )}

      {phoneCallActive && (
        <div
          role="status"
          data-testid="phone-call-notice"
          className="w-full px-3 py-2 text-xs rounded-card border border-status-info-border bg-status-info-bg text-status-info-fg"
        >
          This chat is on a phone call. It picks up here when the call ends.
        </div>
      )}

      {/* A new chat's pre-warm refused (AgentChat: newChatEngineNotice). */}
      {engineNotice && <EngineNotice {...engineNotice} />}

      {/* Suppressed while an engine switch is pending — a dead chat pinned
          to an offline machine would otherwise stack two competing
          "restart from DB history" offers (move vs switch). */}
      <ChatTargetBanner
        chatId={chatId}
        mismatch={pendingEngineSwitch ? null : targetMismatch}
        moveDisabled={viewedStreaming || warming}
        onMove={() => ws.moveChat()}
      />

      <InstallProgressBar
        chatId={chatId}
        machineId={machineOf(sessionExecutionTarget) || null}
        agent={agentName}
        onRetry={() => {
          // Re-fire warmup with the same agent + mode + model (used by the
          // install-failed banner). Backend unregisters the previous
          // in-flight entry on terminal event, so the next warmup_started
          // reuses the same chat_id cleanly.
          if (chatId && agentName) {
            // Theme rides unconditionally — the backend may resolve this
            // warmup interactive even when the client doesn't know it yet
            // (ignored for -p spawns).
            ws.warmup(agentName, chatId, mode, model, chatActiveLayer ?? selectedLayer ?? undefined, undefined,
              interactive.chatExecMode || undefined,
              currentDashboardTheme())
          } else if (agentName) {
            // New-chat page (no chatId yet) — re-fire pre-warmup. Reset
            // the guard so the eager useEffect picks it up.
            preWarmedRef.current = null
            ws.preWarmup(agentName, model, mode, preWarmPath)
          }
        }}
      />

      {/* Cross-engine switch: provisional pick banner + confirm (closest
          to the composer — the switch blocks sending until resolved). */}
      <EngineSwitchBanner
        chatId={chatId}
        pending={pendingEngineSwitch ? {
          layer: pendingEngineSwitch.layer,
          model: pendingEngineSwitch.model,
          layerLabel: layers?.[pendingEngineSwitch.layer]?.display_name || pendingEngineSwitch.layer,
        } : null}
        busy={engineSwitchBusy}
        error={engineSwitchError}
        onConfirm={handleEngineSwitchConfirm}
        onCancel={handleEngineSwitchCancel}
      />

      <ChatLockNotice sentence={lockSentence} />
      </div>
    </div>
  )
}
