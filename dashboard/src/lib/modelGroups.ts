/** Pure model-dropdown group math for the chat page — extracted from
 * AgentChat so the layer-visibility rules are unit-testable (there is no
 * AgentChat page-mount harness; the page wiring stays thin glue).
 *
 * Rules:
 *  - No committed layer (fresh, unsent chat) → the agent's enabled engines
 *    filtered to what THIS USER can run (`canRun`, from
 *    /v1/users/me/execution-layers). Zero accessible → unfiltered fallback
 *    (an empty selector is a dead-end; the page shows an inline notice).
 *  - Committed layer + session process alive (or streaming/warming) → that
 *    one layer only (today's engine lock).
 *  - Committed layer + process DEAD → the chat's engine first (plain resume,
 *    session-file based when possible) plus every OTHER enabled engine the
 *    user can run — picking one of those is the cross-engine switch flow
 *    (banner + confirm; never an immediate model_change).
 */

export interface ModelGroup {
  layer: string
  layerLabel: string
  models: { value: string; label: string }[]
}

export interface LayerCatalogEntry {
  display_name: string
  models?: { value: string; label: string }[]
}

export interface ComputeModelGroupsArgs {
  layers: Record<string, LayerCatalogEntry> | undefined
  agentPaths: string[]
  chatActiveLayer: string | null
  processAlive: boolean
  /** layer id → can_run; ABSENT layers count as runnable (older proxy). */
  canRun: Record<string, boolean>
  streaming: boolean
  warming: boolean
}

/** The agent's enabled engines this user can run — unfiltered when that
 * would be empty (zero-accessible fallback). */
export function visibleAgentPaths(
  agentPaths: string[], canRun: Record<string, boolean>,
): string[] {
  const accessible = agentPaths.filter(p => canRun[p] ?? true)
  return accessible.length > 0 ? accessible : agentPaths
}

export function computeModelGroups(
  { layers, agentPaths, chatActiveLayer, processAlive, canRun, streaming, warming }: ComputeModelGroupsArgs,
): ModelGroup[] | undefined {
  if (!layers) return undefined

  let pathsToShow: string[]
  if (chatActiveLayer) {
    const expanded = !processAlive && !streaming && !warming
    pathsToShow = expanded
      ? [chatActiveLayer,
         ...agentPaths.filter(p => p !== chatActiveLayer && (canRun[p] ?? true))]
      : [chatActiveLayer]
  } else {
    pathsToShow = visibleAgentPaths(agentPaths, canRun)
  }

  const groups: ModelGroup[] = []
  for (const path of pathsToShow) {
    const cap = layers[path]
    if (cap) {
      const models = (cap.models || [])
        .filter((m) => m.value !== '')
        .map((m) => ({ value: `${path}::${m.value}`, label: m.label }))
      if (models.length > 0) {
        groups.push({ layer: path, layerLabel: cap.display_name, models })
      }
    }
  }
  return groups.length > 0 ? groups : undefined
}
