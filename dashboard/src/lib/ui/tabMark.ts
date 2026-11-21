// A dot on the browser tab's icon when an answer arrived while the tab was
// hidden. The one owner of the icon: it draws the page's own icon with a dot
// in the brand colour on a canvas, puts the drawing in the icon's link, and
// puts the original back when the tab is seen again. No image file: the
// brand's assets stay as they are.

const DOT_RADIUS = 0.2
// The ring around the dot is cut out of the icon, so the tab strip shows
// through it on a light strip and on a dark one alike.
const RING = 0.08

let original: string | null = null
let marked = false
// A draw that finishes after the tab was seen must not mark it.
let generation = 0
let listening = false

function iconLink(): HTMLLinkElement | null {
  return document.querySelector<HTMLLinkElement>('link[rel~="icon"]')
}

/** The brand colour the tokens give, read from the stylesheet. */
export function brandColour(): string {
  return getComputedStyle(document.documentElement).getPropertyValue('--ot-brand').trim()
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error('the icon did not load'))
    img.src = src
  })
}

/** The icon at `src` with the dot, as a PNG data address, or null when it
 *  cannot be drawn here. */
export async function drawMarkedIcon(src: string, colour: string): Promise<string | null> {
  if (!colour) return null
  const img = await loadImage(src)
  const size = Math.max(32, img.naturalWidth || 0, img.naturalHeight || 0)
  const canvas = document.createElement('canvas')
  canvas.width = size
  canvas.height = size
  const ctx = canvas.getContext('2d')
  if (!ctx) return null
  ctx.drawImage(img, 0, 0, size, size)
  const r = size * DOT_RADIUS
  const cx = size - r
  const cy = r
  ctx.globalCompositeOperation = 'destination-out'
  ctx.beginPath()
  ctx.arc(cx, cy, r + size * RING, 0, Math.PI * 2)
  ctx.fill()
  ctx.globalCompositeOperation = 'source-over'
  ctx.fillStyle = colour
  ctx.beginPath()
  ctx.arc(cx, cy, r, 0, Math.PI * 2)
  ctx.fill()
  try {
    return canvas.toDataURL('image/png')
  } catch {
    return null
  }
}

function restore(): void {
  generation += 1
  if (!marked) return
  marked = false
  const link = iconLink()
  if (link && original !== null) link.href = original
}

function listen(): void {
  if (listening) return
  listening = true
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') restore()
  })
}

/** Mark the tab when it is hidden: called where the end of a turn plays its
 *  ping. A visible tab is left as it is. */
export async function markTabIfHidden(): Promise<void> {
  if (document.visibilityState !== 'hidden' || marked) return
  const link = iconLink()
  if (!link) return
  listen()
  if (original === null) original = link.getAttribute('href') || link.href
  const mine = ++generation
  const drawn = await drawMarkedIcon(original, brandColour()).catch(() => null)
  if (!drawn || mine !== generation || document.visibilityState !== 'hidden') return
  marked = true
  link.href = drawn
}

/** The end of a turn's notice reached this tab: mark it when the chat it
 *  shows answered while it was hidden. */
export function markTabForAnswer(chatId: string | null | undefined, shownChatId: string | null | undefined): void {
  if (chatId && chatId === shownChatId) void markTabIfHidden()
}

/** Whether the tab shows the mark now (for the tests). */
export function tabMarked(): boolean {
  return marked
}
