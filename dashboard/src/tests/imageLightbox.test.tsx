// ImageLightbox must survive host re-renders with unstable `onClose`
// identities. The history/Android/esc integrations mount ONCE and read the
// latest onClose through a ref — with `[onClose]` deps they re-armed on every
// host re-render (dictation interims, streaming turns), and the history
// effect's cleanup back() raced its own re-pushed entry: one popstate later
// the viewer closed "by itself" (2026-08-13 operator report, reproduced live
// with a single synthetic input event).

import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'

import ImageLightbox from '@/components/chat/media/ImageLightbox'

describe('ImageLightbox stability under host re-renders', () => {
  it('unstable onClose identity cannot self-close it (one history entry, ever)', async () => {
    const pushSpy = vi.spyOn(window.history, 'pushState')
    const backSpy = vi.spyOn(window.history, 'back').mockImplementation(() => {})
    const closes: number[] = []
    const images = [{ url: 'data:image/png;base64,iVBORw0KGgo=' }]

    const { rerender, unmount } = render(
      <ImageLightbox images={images} onClose={() => closes.push(-1)} />,
    )
    expect(pushSpy).toHaveBeenCalledTimes(1) // the mount entry

    for (let i = 0; i < 25; i++) {
      rerender(<ImageLightbox images={images} onClose={() => closes.push(i)} />)
    }
    await new Promise((r) => setTimeout(r, 20))

    expect(pushSpy).toHaveBeenCalledTimes(1) // NO re-pushes across re-renders
    expect(backSpy).not.toHaveBeenCalled()   // NO cleanup churn while open
    expect(closes).toHaveLength(0)           // never self-closed

    // popstate still closes it — and through the LATEST onClose identity.
    window.dispatchEvent(new PopStateEvent('popstate'))
    expect(closes).toEqual([24])

    unmount()
    pushSpy.mockRestore()
    backSpy.mockRestore()
  })
})
