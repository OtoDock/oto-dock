// The viewport's zoom cap stays on an Apple touch device (it stops iOS
// zooming into a focused field, pinch zoom stays) and leaves everywhere else
// (it would only take pinch zoom away).
import { describe, expect, it } from 'vitest'
import { fitViewportToDevice, isAppleTouch } from '@/lib/device/viewport'

const IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1'
const IPAD_DESKTOP = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15'
const ANDROID = 'Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Mobile Safari/537.36'
const DESKTOP = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36'
const CAPPED = 'width=device-width, initial-scale=1.0, viewport-fit=cover, maximum-scale=1'

function doc(content = CAPPED): Document {
  const d = document.implementation.createHTMLDocument('t')
  const meta = d.createElement('meta')
  meta.setAttribute('name', 'viewport')
  meta.setAttribute('content', content)
  d.head.appendChild(meta)
  return d
}
const content = (d: Document) => d.querySelector('meta[name="viewport"]')!.getAttribute('content')

describe('the viewport cap', () => {
  it('knows an iPhone and iPadOS, whose agent says Macintosh', () => {
    expect(isAppleTouch({ userAgent: IPHONE })).toBe(true)
    expect(isAppleTouch({ userAgent: IPAD_DESKTOP, platform: 'MacIntel', maxTouchPoints: 5 })).toBe(true)
    expect(isAppleTouch({ userAgent: IPAD_DESKTOP, platform: 'MacIntel', maxTouchPoints: 0 })).toBe(false)
    expect(isAppleTouch({ userAgent: ANDROID, platform: 'Linux armv8l', maxTouchPoints: 5 })).toBe(false)
  })

  it('stays on an iPhone and an iPad', () => {
    const a = doc()
    fitViewportToDevice(a, { userAgent: IPHONE })
    expect(content(a)).toBe(CAPPED)
    const b = doc()
    fitViewportToDevice(b, { userAgent: IPAD_DESKTOP, platform: 'MacIntel', maxTouchPoints: 5 })
    expect(content(b)).toBe(CAPPED)
  })

  it('leaves Android and a desktop with pinch zoom, the rest of the viewport kept', () => {
    for (const nav of [{ userAgent: ANDROID, maxTouchPoints: 5 }, { userAgent: DESKTOP, platform: 'Linux x86_64' }]) {
      const d = doc()
      fitViewportToDevice(d, nav)
      expect(content(d)).toBe('width=device-width, initial-scale=1.0, viewport-fit=cover')
    }
    // The link page's own viewport, the cap in the middle.
    const link = doc('width=device-width, maximum-scale=1, initial-scale=1')
    fitViewportToDevice(link, { userAgent: ANDROID })
    expect(content(link)).toBe('width=device-width, initial-scale=1')
  })
})
