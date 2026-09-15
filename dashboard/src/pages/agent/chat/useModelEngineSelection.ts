/**
 * useModelEngineSelection — AgentChat's model + engine selection, as three
 * hooks so each is called exactly where the page declared its first hook
 * (the React hook order is unchanged) and only reads values that exist at
 * that point:
 *
 * - useModelEngineSelection: per-user engine access (canRunLayer,
 *   visiblePaths, zeroAccessible, preWarmPath), the COMMITTED chatActiveLayer
 *   vs the SELECTED selectedLayer, session-process liveness, the provisional
 *   cross-engine switch state and parseModelValue. Runs before the stream
 *   hook — the stream callbacks read its setters.
 * - useModelEngineReconcile: modelGroups (computeModelGroups), the
 *   engine-switch resets, the model/mode seed and the model ↔ layer
 *   reconciliation for new chats. Runs after the stream (needs model, chatId).
 * - useModelEngineHandlers: handleModelChange and the cross-engine switch
 *   confirm/cancel. Runs after isTaskChat exists.
 */
import { useState, useEffect, useCallback, useMemo } from 'react'
import type { useChatStream } from '../../../hooks/useChatStream'
import type { AgentSummary, LayerCapabilities } from '../../../api/agents'
import { useUserExecutionLayers } from '../../../api/executionLayers'
import { computeModelGroups, visibleAgentPaths } from '../../../lib/modelGroups'
import { useAgentPrefsStore } from '../../../store/agentPrefsStore'

type Stream = ReturnType<typeof useChatStream>
type Selection = ReturnType<typeof useModelEngineSelection>

export function useModelEngineSelection({ agentExecutionPaths, agentExecutionPath }: {
  agentExecutionPaths: string[]
  agentExecutionPath: string
}) {
  // Per-user engine access (server-computed `can_run` on
  // /v1/users/me/execution-layers) — drives the engine/model filtering. A
  // layer absent from the map (older proxy / loading) counts as runnable.
  const { data: userLayers } = useUserExecutionLayers()
  const canRunLayer = useMemo(() => {
    const m: Record<string, boolean> = {}
    for (const l of userLayers ?? []) m[l.name] = l.can_run !== false
    return m
  }, [userLayers])
  // The agent's enabled engines this user can run — unfiltered fallback when
  // that would be empty (an empty selector is a dead-end; the inline notice
  // below explains instead).
  const visiblePaths = useMemo(
    () => visibleAgentPaths(agentExecutionPaths, canRunLayer),
    [agentExecutionPaths, canRunLayer],
  )
  const zeroAccessible =
    (userLayers?.length ?? 0) > 0
    && agentExecutionPaths.every(p => canRunLayer[p] === false)
  // Pre-warms target the primary engine unless this user can't run it — then
  // the first accessible one (a doomed pre-warm burns a spawn and fails
  // silently server-side).
  const preWarmPath = (canRunLayer[agentExecutionPath] ?? true)
    ? agentExecutionPath
    : (visiblePaths[0] ?? agentExecutionPath)

  // The execution layer the chat has COMMITTED to. null until the chat actually
  // starts (warmup_ready) or is restored from DB (chat_history) — only then does
  // the model dropdown lock to a single layer. While null (a fresh, unsent chat)
  // the dropdown shows every enabled layer so the user can pick any.
  const [chatActiveLayer, setChatActiveLayer] = useState<string | null>(null)
  // The layer of the currently-SELECTED model on a not-yet-committed chat (the
  // user's pick, or the default-model reconciliation). Drives the dropdown
  // highlight + the warmup layer WITHOUT collapsing the dropdown — that's what
  // keeps "all layers visible before the first prompt" working.
  const [selectedLayer, setSelectedLayer] = useState<string | null>(null)
  // Best-effort liveness of the chat's session process. Set from the
  // chat_history meta, warmup_ready (alive), engine_switched (dead) and the
  // probe_liveness answer (fired when the model dropdown opens — headless
  // idle-reap emits no frame). While true the dropdown stays locked to the
  // chat's engine; a DEAD chat additionally offers the agent's other
  // accessible engines (the cross-engine switch flow). The backend re-checks
  // authoritatively — this only shapes the dropdown.
  const [processAlive, setProcessAlive] = useState(true)
  // Provisional cross-engine pick (banner + confirm flow). Invariant: never
  // coexists with chatActiveLayer === null; cleared centrally on chat/agent
  // change, on every chat_history arrival, and when the chat revives.
  const [pendingEngineSwitch, setPendingEngineSwitch] =
    useState<{ layer: string; model: string } | null>(null)
  const [engineSwitchBusy, setEngineSwitchBusy] = useState(false)
  const [engineSwitchError, setEngineSwitchError] = useState<string | null>(null)
  // (modelGroups — the layer::model_id dropdown groups — is declared further
  // down, after the viewedStreaming/warming state it depends on; the math
  // itself lives in lib/modelGroups.)

  // Helper: parse layer::model compound value
  const parseModelValue = useCallback((compound: string): { layer: string; model: string } => {
    const sep = compound.indexOf('::')
    if (sep >= 0) return { layer: compound.slice(0, sep), model: compound.slice(sep + 2) }
    return { layer: agentExecutionPath, model: compound }
  }, [agentExecutionPath])

  return {
    canRunLayer, visiblePaths, zeroAccessible, preWarmPath,
    chatActiveLayer, setChatActiveLayer, selectedLayer, setSelectedLayer,
    processAlive, setProcessAlive,
    pendingEngineSwitch, setPendingEngineSwitch,
    engineSwitchBusy, setEngineSwitchBusy, engineSwitchError, setEngineSwitchError,
    parseModelValue,
  }
}

export function useModelEngineReconcile({
  layers, agentExecutionPaths, chatActiveLayer, processAlive, canRunLayer, viewedStreaming, warming, model,
  setPendingEngineSwitch, setEngineSwitchBusy, setEngineSwitchError, chatId, agentName, sessionId, setProcessAlive,
  urlChatId, agentDefaultModel, setModel, setMode, selectedLayer, setSelectedLayer, currentAgent, parseModelValue,
}: {
  layers: Record<string, LayerCapabilities> | undefined
  agentExecutionPaths: string[]
  chatActiveLayer: Selection['chatActiveLayer']
  processAlive: Selection['processAlive']
  canRunLayer: Selection['canRunLayer']
  viewedStreaming: boolean
  warming: boolean
  model: Stream['model']
  setPendingEngineSwitch: Selection['setPendingEngineSwitch']
  setEngineSwitchBusy: Selection['setEngineSwitchBusy']
  setEngineSwitchError: Selection['setEngineSwitchError']
  chatId: Stream['chatId']
  agentName: string | undefined
  sessionId: Stream['sessionId']
  setProcessAlive: Selection['setProcessAlive']
  urlChatId: string | undefined
  agentDefaultModel: string
  setModel: Stream['setModel']
  setMode: Stream['setMode']
  selectedLayer: Selection['selectedLayer']
  setSelectedLayer: Selection['setSelectedLayer']
  currentAgent: AgentSummary | undefined
  parseModelValue: Selection['parseModelValue']
}) {
  // Model dropdown groups — locked to the chat's engine while its session
  // process is alive/streaming/warming; a DEAD chat's dropdown adds the
  // agent's other engines this user can run (cross-engine switch flow).
  const modelGroups = useMemo(() => computeModelGroups({
    layers,
    agentPaths: agentExecutionPaths,
    chatActiveLayer,
    processAlive,
    canRun: canRunLayer,
    streaming: viewedStreaming,
    warming,
    activeModel: model,
  }), [layers, agentExecutionPaths, chatActiveLayer, processAlive, canRunLayer, viewedStreaming, warming, model])

  // Centralized pendingEngineSwitch reset — the ONLY chat-exit clear. The
  // sidebar select path (handleSelectChat) pre-stamps lastResumedChatIdRef
  // and SKIPS the URL-effect reset, so per-path sprinkles would leak the
  // banner + disabled composer onto the next chat.
  useEffect(() => {
    setPendingEngineSwitch(null)
    setEngineSwitchBusy(false)
    setEngineSwitchError(null)
  }, [chatId, agentName])
  // A second tab / server-initiated turn revived the chat under an open
  // banner — the dropdown collapses back to the active engine; drop the
  // provisional pick instead of deadlocking send against a hidden group.
  useEffect(() => {
    if (viewedStreaming || warming) {
      setPendingEngineSwitch(null)
      setEngineSwitchBusy(false)
      setEngineSwitchError(null)
    }
  }, [viewedStreaming, warming])
  // warmup_ready adopted a session — the process is alive again.
  useEffect(() => { if (sessionId) setProcessAlive(true) }, [sessionId])

  // Seed model + mode on initial render / new chat.
  // For NEW chats (no urlChatId), the user's per-agent sticky preference
  // wins — opening a new chat for an agent reuses the last pick instead
  // of resetting to default. For EXISTING chats (urlChatId set), the DB
  // value from chat_history later overrides this seed.
  useEffect(() => {
    if (!agentName) return
    if (urlChatId) return  // existing chat; chat_history will set model/mode
    const prefs = useAgentPrefsStore.getState()
    const stickyModel = prefs.lastModel[agentName]
    const stickyMode = prefs.lastMode[agentName]
    // Unconditional re-seed: navigating here from an EXISTING chat leaves its
    // restored model/mode in state — for a task-run chat that's the run's
    // model + 'auto', which must never carry into a new chat ('auto' would
    // spawn it with Don't Ask). The sticky prefs hold the user's own last
    // explicit picks (handleModeChange/handleModelChange save every change),
    // so re-seeding never loses a real choice.
    setModel(stickyModel || agentDefaultModel)
    setMode(stickyMode || 'default')
    // (The interactive toggle's sticky seed lives in the dedicated effect
    // below — it must also re-fire when the ui-prefs hydration lands.)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentName, urlChatId, agentDefaultModel])

  // Reconcile model ↔ chatActiveLayer for NEW chats. The seed effect above
  // sets ``model`` (a bare model_id) but doesn't set ``chatActiveLayer``,
  // and ``modelCompound`` falls back to ``agentExecutionPath`` (primary)
  // when chatActiveLayer is null. That breaks two scenarios:
  //
  //   1. Agent has multiple execution_paths enabled (e.g. codex-cli +
  //      claude-code-cli) and default_model belongs to the SECONDARY layer.
  //      Compound becomes ``<primary>::<secondary-model>`` → no match in
  //      ``modelGroups`` → dropdown renders empty → user clicks Send →
  //      ``ws.warmup`` runs with the wrong execution_path → backend
  //      resolves nothing → message vanishes with no response.
  //
  //   2. Agent has no ``default_model`` set at all. Seed leaves ``model``
  //      as empty string. Compound becomes ``<primary>::`` → no match →
  //      same silent failure on send.
  //
  // Once a chat is active (chatActiveLayer set by warmup_ready or
  // chat_history) this effect bails. Same for existing chats — those
  // restore both model + layer from DB and we shouldn't second-guess.
  useEffect(() => {
    if (urlChatId) return
    if (chatActiveLayer) return    // committed → the layer is already locked
    if (selectedLayer) return      // already reconciled / the user already picked
    // Wait until the agents query resolves. Before it does, agentDefaultModel
    // is "" and ``model`` may be "" — picking firstGroup.models[0] here (the
    // primary layer's first model, e.g. opus-4.7) and persisting it as sticky
    // would override the agent's real default for every future new chat (G1).
    if (!currentAgent) return
    if (!modelGroups || modelGroups.length === 0) return

    // Find the group whose layer owns the current model. Set the SELECTED layer
    // (NOT the committed chatActiveLayer) so warmup uses the right layer while
    // the dropdown keeps showing every enabled layer until the chat starts.
    const matching = modelGroups.find(g =>
      g.models.some(m => m.value === `${g.layer}::${model}`),
    )
    if (matching) {
      setSelectedLayer(matching.layer)
      return
    }

    // No group owns the current model — the agent has no default_model, or its
    // default belongs to a disabled layer. Pick the first available so the
    // dropdown has a valid selection, but do NOT persist it as the sticky pref:
    // this is an automatic reconciliation, not a user choice (handleModelChange
    // is the only place a pick becomes sticky). Persisting here is G1 poisoning.
    const firstGroup = modelGroups[0]
    const firstOption = firstGroup?.models[0]
    if (!firstOption) return
    const { layer, model: modelId } = parseModelValue(firstOption.value)
    setModel(modelId)
    setSelectedLayer(layer)
  }, [urlChatId, chatActiveLayer, selectedLayer, modelGroups, model, agentName, agentDefaultModel, currentAgent, parseModelValue])

  return { modelGroups }
}

export function useModelEngineHandlers({
  ws, parseModelValue, agentName, chatActiveLayer, viewedStreaming, warming, isTaskChat,
  setEngineSwitchBusy, setEngineSwitchError, setPendingEngineSwitch, setModel, setSelectedLayer, pendingEngineSwitch,
}: {
  ws: Stream['ws']
  parseModelValue: Selection['parseModelValue']
  agentName: string | undefined
  chatActiveLayer: Selection['chatActiveLayer']
  viewedStreaming: boolean
  warming: boolean
  isTaskChat: boolean
  setEngineSwitchBusy: Selection['setEngineSwitchBusy']
  setEngineSwitchError: Selection['setEngineSwitchError']
  setPendingEngineSwitch: Selection['setPendingEngineSwitch']
  setModel: Stream['setModel']
  setSelectedLayer: Selection['setSelectedLayer']
  pendingEngineSwitch: Selection['pendingEngineSwitch']
}) {
  const handleModelChange = useCallback((compound: string) => {
    const { layer, model: modelId } = parseModelValue(compound)
    // Cross-engine pick on a committed chat → the provisional switch flow
    // (banner + confirm), NEVER an immediate model_change (the backend
    // rightly refuses foreign models and its refusal echo would snap the
    // selector around), no sticky write (chat-scoped decision, not a
    // preference), and no setSelectedLayer (a cancelled switch must not
    // poison later warmup-layer fallbacks). Gate on the PER-CHAT streaming
    // flag — ws.streaming is connection-global.
    if (chatActiveLayer && layer !== chatActiveLayer) {
      if (viewedStreaming || warming) return  // stale expansion — chat revived
      setEngineSwitchBusy(false)
      setEngineSwitchError(null)
      setPendingEngineSwitch({ layer, model: modelId })
      return
    }
    ws.changeModel(modelId)
    setModel(modelId)
    // Sticky for the same agent's next new chat — but never from a task
    // chat: a one-off pick on a run's chat must not rewrite the user's
    // default.
    if (agentName && !isTaskChat) useAgentPrefsStore.getState().setLastModel(agentName, modelId)
    // Track the selected layer (drives warmup + the dropdown highlight) WITHOUT
    // collapsing the dropdown — a not-yet-started chat keeps every layer visible.
    // The committed chatActiveLayer is set only when the chat actually starts.
    if (!ws.streaming) {
      setSelectedLayer(layer)
    }
  }, [ws, parseModelValue, agentName, chatActiveLayer, viewedStreaming, warming, isTaskChat])

  // Cross-engine switch confirm/cancel (EngineSwitchBanner + its dialog).
  const handleEngineSwitchConfirm = useCallback(() => {
    if (!pendingEngineSwitch) return
    setEngineSwitchBusy(true)
    setEngineSwitchError(null)
    ws.switchEngine(pendingEngineSwitch.layer, pendingEngineSwitch.model)
  }, [ws, pendingEngineSwitch])
  const handleEngineSwitchCancel = useCallback(() => {
    setPendingEngineSwitch(null)
    setEngineSwitchBusy(false)
    setEngineSwitchError(null)
  }, [])

  return { handleModelChange, handleEngineSwitchConfirm, handleEngineSwitchCancel }
}
