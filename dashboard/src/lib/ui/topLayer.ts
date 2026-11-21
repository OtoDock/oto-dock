// The top layer: an element shown above the whole page in place, with the
// Popover API (`popover="manual"`, `showPopover()`). It never moves in the
// DOM, so a frame inside it does not reload and a video keeps playing, and
// no ancestor's stacking context, transform or clipping reaches it.

/** Whether this browser can show an element in the top layer in place
 *  (not iOS 16, nor Firefox before 125, nor a web view before Chromium 114). */
export const canPresent = (): boolean =>
  typeof HTMLElement !== 'undefined' && 'showPopover' in HTMLElement.prototype

/** Whether the element is shown in the top layer now. */
export function popoverOpen(el: HTMLElement): boolean {
  try { return el.matches(':popover-open') } catch { return false }
}
