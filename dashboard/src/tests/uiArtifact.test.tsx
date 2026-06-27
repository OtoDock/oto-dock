import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render } from '@testing-library/react'

import UiArtifact from '@/components/chat/media/UiArtifact'
import { emitFileUpdate } from '@/lib/fileUpdates'
import { eventToBlock, liveBlockToMessageBlock } from '@/lib/messageBlocks'
import { titleFor } from '@/hooks/useArtifactWindows'

const PROPS = {
  token: 'tok-1',
  uiUrl: '/v1/ui/tok-1',
  title: 'Tip calculator',
}

function getFrame(container: HTMLElement): HTMLIFrameElement {
  const f = container.querySelector('iframe')
  expect(f).toBeTruthy()
  return f as HTMLIFrameElement
}

async function nextFrame() {
  await new Promise((r) => requestAnimationFrame(() => r(null)))
}

describe('UiArtifact', () => {
  it('renders a sandboxed iframe — allow-scripts ONLY, never allow-same-origin', () => {
    const { container } = render(<UiArtifact {...PROPS} />)
    const f = getFrame(container)
    // Load-bearing security assertion: any extra token (especially
    // allow-same-origin) would let agent HTML out of the opaque origin.
    expect(f.getAttribute('sandbox')).toBe('allow-scripts')
    expect(f.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(f.getAttribute('allow')).toBe('')
    expect(f.getAttribute('src')).toBe('/v1/ui/tok-1?theme=light')
  })

  it('shows the persistent generated-content marker', () => {
    const { getByText } = render(<UiArtifact {...PROPS} />)
    expect(getByText('generated')).toBeTruthy()
  })

  it('live-reloads on a matching file_updated only (update-in-place)', async () => {
    const { container } = render(
      <UiArtifact {...PROPS} agent="a1" path="workspace/trip.html" />,
    )
    const f = getFrame(container)
    const before = f.getAttribute('src')
    await act(async () => {
      emitFileUpdate({ agent_slug: 'other-agent', rel_path: 'workspace/trip.html' })
      emitFileUpdate({ agent_slug: 'a1', rel_path: 'workspace/other.html' })
    })
    expect(f.getAttribute('src')).toBe(before)
    await act(async () => {
      emitFileUpdate({ agent_slug: 'a1', rel_path: 'workspace/trip.html' })
    })
    expect(container.querySelector('iframe')!.getAttribute('src')).toBe(
      '/v1/ui/tok-1?theme=light&v=1',
    )
  })

  it('never reloads on surfaces that do not bind an agent', async () => {
    const { container } = render(
      <UiArtifact {...PROPS} path="workspace/trip.html" />,
    )
    const before = getFrame(container).getAttribute('src')
    await act(async () => {
      emitFileUpdate({ agent_slug: 'a1', rel_path: 'workspace/trip.html' })
    })
    expect(getFrame(container).getAttribute('src')).toBe(before)
  })

  it('applies valid height messages from the artifact frame, clamped', async () => {
    const { container } = render(<UiArtifact {...PROPS} />)
    const f = getFrame(container)
    const send = (data: any, source: any) =>
      window.dispatchEvent(new MessageEvent('message', { data, source }))

    await act(async () => {
      send({ source: 'otodock-artifact', v: 1, type: 'height', height: 400 }, f.contentWindow)
      await nextFrame()
    })
    expect(f.style.height).toBe('400px')

    // Below the floor → clamped up.
    await act(async () => {
      send({ source: 'otodock-artifact', v: 1, type: 'height', height: 5 }, f.contentWindow)
      await nextFrame()
    })
    expect(f.style.height).toBe('60px')
  })

  it('ignores foreign-source and malformed messages', async () => {
    const { container } = render(<UiArtifact {...PROPS} />)
    const f = getFrame(container)
    const before = f.style.height
    // Right shape, WRONG source window (any page window can postMessage).
    window.dispatchEvent(new MessageEvent('message', {
      data: { source: 'otodock-artifact', type: 'height', height: 800 },
      source: window,
    }))
    // Right source, wrong shape.
    window.dispatchEvent(new MessageEvent('message', {
      data: { source: 'evil', type: 'height', height: 700 },
      source: f.contentWindow,
    }))
    window.dispatchEvent(new MessageEvent('message', {
      data: { source: 'otodock-artifact', type: 'height', height: 'tall' },
      source: f.contentWindow,
    }))
    await nextFrame()
    expect(f.style.height).toBe(before)
  })

  it('uses a fixed height when the block carries one (no auto-resize)', async () => {
    const { container } = render(<UiArtifact {...PROPS} height={500} />)
    const f = getFrame(container)
    expect(f.style.height).toBe('500px')
    window.dispatchEvent(new MessageEvent('message', {
      data: { source: 'otodock-artifact', type: 'height', height: 200 },
      source: f.contentWindow,
    }))
    await nextFrame()
    expect(f.style.height).toBe('500px')
  })
})

describe('ui block mapping', () => {
  const wire = {
    type: 'ui', token: 'tok-9', ui_url: '/v1/ui/tok-9',
    title: 'Chart', height: 380, path: 'workspace/generated-ui/c.html',
  }
  const expected = {
    type: 'ui', token: 'tok-9', uiUrl: '/v1/ui/tok-9',
    title: 'Chart', height: 380, path: 'workspace/generated-ui/c.html',
  }

  it('eventToBlock and liveBlockToMessageBlock carry every field', () => {
    expect(eventToBlock(wire)).toEqual(expected)
    expect(liveBlockToMessageBlock(wire)).toEqual(expected)
  })

  it('auto-height artifacts map height to undefined', () => {
    const b = eventToBlock({ type: 'ui', token: 't', ui_url: '/v1/ui/t', height: null })
    expect(b && 'height' in b ? b.height : -1).toBeUndefined()
  })

  it('titleFor names the floating window from the block title', () => {
    expect(titleFor({ type: 'ui', token: 't', uiUrl: '/u', title: 'Calc' })).toBe('Calc')
    expect(titleFor({ type: 'ui', token: 't', uiUrl: '/u' })).toBe('UI artifact')
  })

  it('artifact_interaction maps through both mappers', () => {
    const wire2 = {
      type: 'artifact_interaction', token: 'tok-9', title: 'Chart',
      payload: { action: 'analyze', month: '2026-03' },
    }
    const expected2 = {
      type: 'artifact_interaction', token: 'tok-9', title: 'Chart',
      payload: { action: 'analyze', month: '2026-03' },
    }
    expect(eventToBlock(wire2)).toEqual(expected2)
    expect(liveBlockToMessageBlock(wire2)).toEqual(expected2)
  })

  it('app_action maps through both mappers', () => {
    const wire3 = {
      type: 'app_action', app_id: 'a-1', slug: 'brief', title: 'Brief',
      action_id: 'ask', label: 'Ask', prompt: 'Analyze May',
    }
    const expected3 = {
      type: 'app_action', appId: 'a-1', slug: 'brief', title: 'Brief',
      actionId: 'ask', label: 'Ask', prompt: 'Analyze May',
    }
    expect(eventToBlock(wire3)).toEqual(expected3)
    expect(liveBlockToMessageBlock(wire3)).toEqual(expected3)
  })
})

// ─────────────────── backchannel (otodock.send → chat) ─────────────────────

describe('UiArtifact backchannel', () => {
  beforeEach(() => localStorage.clear())

  const action = (f: HTMLIFrameElement, payload: unknown) =>
    window.dispatchEvent(new MessageEvent('message', {
      data: { source: 'otodock-artifact', v: 1, type: 'action', payload },
      source: f.contentWindow,
    }))

  function ackSpy(f: HTMLIFrameElement) {
    const spy = vi.fn()
    ;(f.contentWindow as any).postMessage = spy
    return spy
  }

  it('acks unavailable when no interaction handler is wired (read-only views)', async () => {
    const { container } = render(<UiArtifact {...PROPS} />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { action(f, { x: 1 }) })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ source: 'otodock-host', type: 'action_ack', status: 'unavailable' }),
      '*',
    )
  })

  it('first use shows the consent chip; Allow delivers + forwards the ack', async () => {
    const onInteraction = vi.fn(async () => ({ status: 'sent' }))
    const { container, getByText } = render(
      <UiArtifact {...PROPS} path="ws/x.html" onInteraction={onInteraction} />,
    )
    const f = getFrame(container)
    const spy = ackSpy(f)

    await act(async () => { action(f, { click: 1 }) })
    // Held behind consent: nothing sent yet.
    expect(onInteraction).not.toHaveBeenCalled()
    await act(async () => { fireEvent.click(getByText('Allow')) })
    expect(onInteraction).toHaveBeenCalledWith('tok-1', 'Tip calculator', { click: 1 })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'action_ack', status: 'sent' }), '*',
    )
    expect(localStorage.getItem('otodock-artifact-consent:ws/x.html')).toBe('allowed')
  })

  it('Block persists and denies without calling the handler', async () => {
    const onInteraction = vi.fn(async () => ({ status: 'sent' }))
    const { container, getByText } = render(
      <UiArtifact {...PROPS} onInteraction={onInteraction} />,
    )
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { action(f, { click: 1 }) })
    await act(async () => { fireEvent.click(getByText('Block')) })
    expect(onInteraction).not.toHaveBeenCalled()
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'action_ack', status: 'blocked' }), '*',
    )
    // Consent falls back to the token when the block has no path.
    expect(localStorage.getItem('otodock-artifact-consent:tok-1')).toBe('blocked')
    // Subsequent actions stay blocked, no chip.
    await act(async () => { action(f, { click: 2 }) })
    expect(onInteraction).not.toHaveBeenCalled()
  })

  it('rate-limits rapid sends client-side after consent', async () => {
    localStorage.setItem('otodock-artifact-consent:tok-1', 'allowed')
    const onInteraction = vi.fn(async () => ({ status: 'sent' }))
    const { container } = render(<UiArtifact {...PROPS} onInteraction={onInteraction} />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { action(f, { n: 1 }) })
    await act(async () => { action(f, { n: 2 }) })
    expect(onInteraction).toHaveBeenCalledTimes(1)
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'action_ack', status: 'denied', reason: 'rate limited' }), '*',
    )
  })

  it('honors a consent granted after mount (e.g. from another tab/instance)', async () => {
    const onInteraction = vi.fn(async () => ({ status: 'sent' }))
    const { container, queryByText } = render(
      <UiArtifact {...PROPS} onInteraction={onInteraction} />,
    )
    const f = getFrame(container)
    // Grant lands out-of-band after mount — a once-at-mount consent read
    // would miss it and pointlessly re-prompt.
    localStorage.setItem('otodock-artifact-consent:tok-1', 'allowed')
    await act(async () => { action(f, { click: 1 }) })
    expect(onInteraction).toHaveBeenCalledWith('tok-1', 'Tip calculator', { click: 1 })
    expect(queryByText('Allow')).toBeNull()
  })

  it('anchors the consent chip sticky-first and scrolls it into view', async () => {
    const scrollSpy = vi.fn()
    ;(Element.prototype as any).scrollIntoView = scrollSpy // jsdom has none
    try {
      const onInteraction = vi.fn(async () => ({ status: 'sent' }))
      const { container, getByTestId } = render(
        <UiArtifact {...PROPS} onInteraction={onInteraction} />,
      )
      const f = getFrame(container)
      await act(async () => { action(f, { click: 1 }) })
      const sentinel = getByTestId('ui-consent-chip')
      // Sticky pins the chip to the artifact's VISIBLE portion — and only
      // works from the wrapper's first flow position, so first child is
      // load-bearing (an absolute top anchor rendered offscreen on tall or
      // scrolled artifacts, silently holding the pending action).
      expect(sentinel.className).toContain('sticky')
      expect(sentinel.parentElement?.firstElementChild).toBe(sentinel)
      expect(scrollSpy).toHaveBeenCalledWith({ block: 'nearest' })
    } finally {
      delete (Element.prototype as any).scrollIntoView
    }
  })

  it('embedded (PiP) mode gates behind the same consent chip', async () => {
    const onInteraction = vi.fn(async () => ({ status: 'sent' }))
    const { container, getByText } = render(
      <UiArtifact {...PROPS} embedded onInteraction={onInteraction} />,
    )
    const f = getFrame(container)
    await act(async () => { action(f, { click: 1 }) })
    expect(onInteraction).not.toHaveBeenCalled()
    await act(async () => { fireEvent.click(getByText('Allow')) })
    expect(onInteraction).toHaveBeenCalledWith('tok-1', 'Tip calculator', { click: 1 })
  })

  it('ignores action messages from foreign windows', async () => {
    const onInteraction = vi.fn(async () => ({ status: 'sent' }))
    localStorage.setItem('otodock-artifact-consent:tok-1', 'allowed')
    render(<UiArtifact {...PROPS} onInteraction={onInteraction} />)
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { source: 'otodock-artifact', v: 1, type: 'action', payload: {} },
        source: window,  // not this artifact's iframe
      }))
    })
    expect(onInteraction).not.toHaveBeenCalled()
  })
})
