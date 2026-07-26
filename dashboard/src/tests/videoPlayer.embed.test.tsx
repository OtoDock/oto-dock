import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'

import VideoPlayer from '@/components/chat/media/VideoPlayer'

function renderEmbed(src: string) {
  const { container, unmount } = render(<VideoPlayer src={src} />)
  const iframe = container.querySelector('iframe')
  return { iframe, unmount }
}

// YouTube rejects referrer-less embeds with player error 153, and the app-wide
// Referrer-Policy is same-origin (sends no Referer cross-origin) — the iframe
// must carry its own override or every YouTube embed breaks.
describe('VideoPlayer embed iframe', () => {
  it.each([
    ['https://www.youtube.com/watch?v=a3cheXcgX5I', 'https://www.youtube.com/embed/a3cheXcgX5I'],
    ['https://youtu.be/a3cheXcgX5I', 'https://www.youtube.com/embed/a3cheXcgX5I'],
    ['https://vimeo.com/76979871', 'https://player.vimeo.com/video/76979871'],
  ])('embeds %s with a cross-origin referrer policy', (src, embedUrl) => {
    const { iframe, unmount } = renderEmbed(src)
    expect(iframe).not.toBeNull()
    expect(iframe!.getAttribute('src')).toBe(embedUrl)
    expect(iframe!.getAttribute('referrerpolicy')).toBe('strict-origin-when-cross-origin')
    unmount()
  })

  it('direct file URLs render <video>, not an iframe', () => {
    const { container, unmount } = render(
      <VideoPlayer src="https://cdn.example.com/clip.mp4" />,
    )
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.querySelector('video')).not.toBeNull()
    unmount()
  })
})
