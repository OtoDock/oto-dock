// The viewport's zoom cap, per device. index.html (and the link page the
// proxy serves) carries `maximum-scale=1`: iOS and iPadOS then stop zooming
// the page into a focused field under 16 px, and keep the person's own pinch
// zoom (WebKit ignores the cap for a pinch). Every other browser that reads
// the cap (Android Chrome) would take pinch zoom away for nothing, since
// none zooms into a field, so the boot drops it there.

const CAP = /,\s*maximum-scale=[\d.]+/

interface DeviceHints {
  userAgent: string
  platform?: string
  maxTouchPoints?: number
}

/** An iPhone, an iPod or an iPad, iPadOS's desktop agent included. */
export function isAppleTouch(nav: DeviceHints): boolean {
  if (/iPad|iPhone|iPod/.test(nav.userAgent)) return true
  return nav.platform === 'MacIntel' && (nav.maxTouchPoints ?? 0) > 1
}

/** Keep the cap on an Apple touch device, drop it everywhere else. */
export function fitViewportToDevice(doc: Document = document, nav: DeviceHints = navigator): void {
  if (isAppleTouch(nav)) return
  const meta = doc.querySelector<HTMLMetaElement>('meta[name="viewport"]')
  if (!meta) return
  const content = meta.getAttribute('content') || ''
  if (CAP.test(content)) meta.setAttribute('content', content.replace(CAP, ''))
}
