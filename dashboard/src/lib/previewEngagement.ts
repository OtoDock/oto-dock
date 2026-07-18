/**
 * Inline Collabora preview chain — shared utilities.
 *
 * The live → frozen → chip state of every preview block is computed at render
 * time by `previewChainModes` (lib/messageBlocks.ts); each DocumentPreview
 * instance defers its OWN downgrade while the user is engaged with it
 * (substantially visible in the viewport, or its fullscreen portal open) —
 * engagement is per block instance, so a new live block's visibility can
 * never postpone an older block's transition. Focus is deliberately NOT the
 * signal: wheel-reading never focuses an iframe, and a clicked iframe stays
 * activeElement forever.
 */

/** Scroll the live preview for a file into view — chip / "previous version"
 * indicator click. Anchors are only set on live (non-embedded) instances. */
export function scrollToLivePreview(fileId: string): void {
  const el = document.querySelector(
    `[data-preview-anchor="${CSS.escape(fileId)}"]`,
  )
  el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
