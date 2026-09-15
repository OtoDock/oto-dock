/** The scene contents: the teardown, the per-agent world positions and the
 * three builders (dais, cards, edges) that the map's scene effect runs on
 * every structural/heat/level change (split out of AgentsMap3D.tsx on
 * 2026-09-10; the effect and its dependency array stay in the component). */
import type { Dispatch, RefObject, SetStateAction } from 'react'
import * as THREE from 'three'
import type { User } from '../../api/auth'
import type { DelegationEdge } from '../../api/departments'
import { prunePointers, type PointerMap } from './gestures'
import type { MapCluster, MapLayout, MapNode, StageLayout } from './layout'
import type { PopupState } from './mapConstants'
import { disposeObject, type SceneBag } from './sceneBag'
import { buildDais } from './buildDais'
import { buildAgentCards } from './buildAgentCards'
import { buildEdges } from './buildEdges'

/** What the scene effect hands its three builders — ONE object, so the
 * dais, the cards and the edges read the same positions and staged id. */
export interface SceneBuildParams {
  bag: SceneBag
  dynamic: THREE.Group
  layout: MapLayout
  staged: string | null
  arc: StageLayout | null
  positions: Map<string, THREE.Vector3>
  memberCount: Map<string, number>
  heat: Map<string, number>
  liveSet: Set<string>
  streamingSet: Set<string>
  edges: DelegationEdge[]
  nodeBySlug: Map<string, MapNode>
  user: User | null
  moveFromRef: RefObject<string | null>
  linkFromRef: RefObject<string | null>
  attemptLinkRef: RefObject<(from: string, to: string) => Promise<void>>
  openAgentRef: RefObject<(node: MapNode, x: number, y: number) => void>
  onDeptTapRef: RefObject<(departmentId: string, x: number, y: number) => void>
  setPopup: Dispatch<SetStateAction<PopupState | null>>
}

/** The component's inputs: the builders' params minus what buildScene
 * derives itself (dynamic, staged, arc, positions, memberCount), plus the
 * stage state it derives them from and the pointer bookkeeping the
 * post-teardown ghost sweep needs. */
export interface BuildSceneInput extends Omit<
  SceneBuildParams, 'dynamic' | 'staged' | 'arc' | 'positions' | 'memberCount'
> {
  stage: { deptId: string } | null
  stageData: { cluster: MapCluster; arc: StageLayout } | null
  activePointers: RefObject<PointerMap>
  gestureOwnedIds: () => Set<number>
}

export function buildScene({
  bag, layout, stage, stageData, heat, liveSet, streamingSet, edges,
  nodeBySlug, user, activePointers, gestureOwnedIds, moveFromRef,
  linkFromRef, attemptLinkRef, openAgentRef, onDeptTapRef, setPopup,
}: BuildSceneInput) {
  const { dynamic } = bag
  for (const child of [...dynamic.children]) {
    dynamic.remove(child)
    disposeObject(child)
  }
  bag.lineMats = []
  bag.sparks = []
  bag.hitTargets = []
  bag.edgeGlints = []
  // The teardown above just DETACHED every chip's DOM. Touch pointers
  // hold implicit capture on their pointerdown target, so a finger that
  // was down on a chip will deliver its up/cancel at the detached node —
  // invisible to every listener. Sweep those ghosts now (live gesture
  // owners excluded: their entries are refreshed by moves anyway).
  prunePointers(
    activePointers.current, performance.now(),
    (el) => !el || document.contains(el),
    gestureOwnedIds(),
  )

  const staged: string | null = stage ? stage.deptId : null
  const arc = stageData?.arc ?? null
  const slotBySlug = new Map(
    (arc?.slots ?? []).map((s) => [s.slug, s]),
  )

  // World position per agent: overview scatter, except the staged
  // cluster's members who sit in their amphitheater slots.
  const positions = new Map<string, THREE.Vector3>()
  for (const node of layout.nodes) {
    const slot = staged === node.departmentId
      ? slotBySlug.get(node.slug)
      : undefined
    positions.set(node.slug, slot
      ? new THREE.Vector3(slot.x, slot.y, slot.z)
      : new THREE.Vector3(node.x, node.y, node.z))
  }
  const memberCount = new Map<string, number>()
  for (const n of layout.nodes) {
    memberCount.set(n.departmentId, (memberCount.get(n.departmentId) ?? 0) + 1)
  }

  const params: SceneBuildParams = {
    bag, dynamic, layout, staged, arc, positions, memberCount, heat, liveSet,
    streamingSet, edges, nodeBySlug, user, moveFromRef, linkFromRef,
    attemptLinkRef, openAgentRef, onDeptTapRef, setPopup,
  }
  buildDais(params)
  buildAgentCards(params)
  buildEdges(params)
}
