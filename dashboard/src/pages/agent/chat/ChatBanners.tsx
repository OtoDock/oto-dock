/**
 * ChatBanners — the banner stack between AgentChat's main slot and the
 * composer: machine update, remote fallback, the no-engine-access notice,
 * target mismatch, install progress, and the cross-engine switch banner.
 * Presentational: every value is computed by the page.
 */
import type { RefObject } from 'react'
import { Link } from 'react-router-dom'
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

type Stream = ReturnType<typeof useChatStream>

interface Props {
  sessionExecutionTarget: Stream['sessionExecutionTarget']
  sessionFallbackReason: Stream['sessionFallbackReason']
  offlineMachineName: Stream['offlineMachineName']
  zeroAccessible: boolean
  chatId: string | null
  pendingEngineSwitch: { layer: string; model: string } | null
  targetMismatch: TargetMismatch | null
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
  agentLayerModels: LayerCapabilities['models']
  engineSwitchBusy: boolean
  engineSwitchError: string | null
  handleEngineSwitchConfirm: () => void
  handleEngineSwitchCancel: () => void
}

export default function ChatBanners({
  sessionExecutionTarget, sessionFallbackReason, offlineMachineName, zeroAccessible,
  chatId, pendingEngineSwitch, targetMismatch, viewedStreaming, warming, ws, agentName,
  mode, model, chatActiveLayer, selectedLayer, interactive, preWarmedRef, preWarmPath,
  layers, agentLayerModels, engineSwitchBusy, engineSwitchError,
  handleEngineSwitchConfirm, handleEngineSwitchCancel,
}: Props) {
  return (
    <>
      <MachineUpdateBanner
        machineId={machineOf(sessionExecutionTarget) || null}
      />

      <RemoteFallbackBanner
        fallbackReason={sessionFallbackReason}
        machineName={offlineMachineName}
      />

      {zeroAccessible && (
        <div
          role="status"
          data-testid="no-engine-access-notice"
          className="w-full px-3 py-2 mx-auto max-w-4xl text-xs rounded-sm border border-amber-300/40 bg-amber-50/40 text-amber-900 dark:bg-amber-500/10 dark:text-amber-200"
        >
          You can't run any of this agent's AI engines —{' '}
          <Link to="/user-settings?tab=ai-engines" className="underline hover:no-underline">
            connect one in your AI-engine settings
          </Link>{' '}
          or ask an admin.
        </div>
      )}

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
          modelLabel: agentLayerModels.find(m => m.value === pendingEngineSwitch.model)?.label
            || pendingEngineSwitch.model,
        } : null}
        fromLabel={(chatActiveLayer && layers?.[chatActiveLayer]?.display_name) || chatActiveLayer || ''}
        busy={engineSwitchBusy}
        error={engineSwitchError}
        onConfirm={handleEngineSwitchConfirm}
        onCancel={handleEngineSwitchCancel}
      />
    </>
  )
}
