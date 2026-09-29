/**
 * The artifact kinds — mirrored from the proxy's authority
 * `proxy/core/events/artifact_events.py` (`tests/core/test_kinds.py` binds
 * this file to it; edit both).
 *
 * Eleven kinds ride the wire as frame types (`WIRE.*`) and, for the ones
 * that persist, as `chat_messages.event_type`. The facts here are what the
 * consumers apply per kind: whether the frame is a renderable block or a
 * removal signal, which placeholder it evicts, the block field a later push
 * of the same artifact replaces by (applied only when that field is
 * non-empty — a path-less ui artifact stacks), and what the history replays.
 */
import { WIRE } from '@/api/wireEvents'

export type ArtifactIdentity = 'fileId' | 'path'

export interface ArtifactKindFacts {
  /** A renderable block (`false` = a removal signal carrying no block). */
  block: boolean
  /** A transient skeleton the real artifact replaces. */
  placeholder: boolean
  /** The placeholder kind this one removes. */
  evicts: string | null
  /** The block field a later push of the same artifact replaces by. */
  identity: ArtifactIdentity | null
  /** Buffered by the pump until the turn's flush. */
  deferred: boolean
  /** The pump persists the block at the turn's save. */
  saved: boolean
  /** The share snapshot copies it. */
  shareable: boolean
}

export const ARTIFACT_KINDS = {
  [WIRE.IMAGES]: { block: true, placeholder: false, evicts: WIRE.IMAGE_GENERATING, identity: null, deferred: false, saved: true, shareable: true },
  [WIRE.IMAGE_GENERATING]: { block: true, placeholder: true, evicts: null, identity: null, deferred: false, saved: true, shareable: false },
  [WIRE.IMAGE_GEN_FAILED]: { block: false, placeholder: false, evicts: WIRE.IMAGE_GENERATING, identity: null, deferred: false, saved: false, shareable: false },
  [WIRE.URL]: { block: true, placeholder: false, evicts: null, identity: null, deferred: false, saved: true, shareable: true },
  [WIRE.FILE]: { block: true, placeholder: false, evicts: null, identity: null, deferred: false, saved: true, shareable: true },
  [WIRE.VIDEO]: { block: true, placeholder: false, evicts: WIRE.MEDIA_PROCESSING, identity: null, deferred: false, saved: true, shareable: true },
  [WIRE.AUDIO]: { block: true, placeholder: false, evicts: WIRE.MEDIA_PROCESSING, identity: null, deferred: false, saved: true, shareable: true },
  [WIRE.MEDIA_PROCESSING]: { block: true, placeholder: true, evicts: null, identity: null, deferred: false, saved: false, shareable: false },
  [WIRE.MEDIA_FAILED]: { block: false, placeholder: false, evicts: WIRE.MEDIA_PROCESSING, identity: null, deferred: false, saved: false, shareable: false },
  [WIRE.DOCUMENT_PREVIEW]: { block: true, placeholder: false, evicts: null, identity: 'fileId', deferred: true, saved: true, shareable: false },
  [WIRE.UI]: { block: true, placeholder: false, evicts: null, identity: 'path', deferred: false, saved: true, shareable: true },
} as const satisfies Record<string, ArtifactKindFacts>
export type ArtifactKind = keyof typeof ARTIFACT_KINDS

export function isArtifactKind(t: unknown): t is ArtifactKind {
  return typeof t === 'string' && Object.prototype.hasOwnProperty.call(ARTIFACT_KINDS, t)
}

const kinds = Object.keys(ARTIFACT_KINDS) as ArtifactKind[]

/** The kinds that render as a block in a message. */
export const ARTIFACT_BLOCK_KINDS: ReadonlySet<string> = new Set(kinds.filter((k) => ARTIFACT_KINDS[k].block))

export function isArtifactBlock(type: string): boolean {
  return ARTIFACT_BLOCK_KINDS.has(type)
}

/** The persisted rows the interactive view replays as windows on open — a
 *  block that is not a placeholder (the proxy's `REPLAYABLE_ARTIFACT_EVENT_TYPES`). */
export const REPLAYABLE_ARTIFACT_EVENT_TYPES: ReadonlySet<string> = new Set(
  kinds.filter((k) => ARTIFACT_KINDS[k].block && !ARTIFACT_KINDS[k].placeholder),
)

/** The placeholder kind a frame of `type` removes, or null. */
export function evictedBy(type: string): string | null {
  return isArtifactKind(type) ? ARTIFACT_KINDS[type].evicts : null
}

/** The block field a later push of the same artifact replaces by, or null. */
export function identityOf(type: string): ArtifactIdentity | null {
  return isArtifactKind(type) ? ARTIFACT_KINDS[type].identity : null
}

/** The value of a block's identity field, or '' when the block has none or
 *  it is empty (then the block never replaces another). */
export function identityKey(block: { type: string; fileId?: string; path?: string }): string {
  const field = identityOf(block.type)
  if (!field) return ''
  return String(block[field] ?? '')
}
