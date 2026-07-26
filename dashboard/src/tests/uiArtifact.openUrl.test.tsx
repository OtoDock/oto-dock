/**
 * UiArtifact open_url bridge: links inside the sandboxed frame are dead by
 * construction, so the runtime posts them up — the host validates (http(s)
 * only, never same-origin), shows a first-use consent chip with the
 * destination ORIGIN, and opens via openExternalUrl.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render } from '@testing-library/react'

import UiArtifact from '@/components/chat/media/UiArtifact'

function getFrame(container: HTMLElement): HTMLIFrameElement {
  const f = container.querySelector('iframe')
  expect(f).toBeTruthy()
  return f as HTMLIFrameElement
}

function ackSpy(f: HTMLIFrameElement) {
  const spy = vi.fn()
  ;(f.contentWindow as unknown as { postMessage: unknown }).postMessage = spy
  return spy
}

const openUrl = (f: HTMLIFrameElement, url: unknown) =>
  window.dispatchEvent(new MessageEvent('message', {
    data: { source: 'otodock-artifact', v: 1, type: 'open_url', url },
    source: f.contentWindow,
  }))

function renderArtifact() {
  return render(
    <UiArtifact token="tok-1" uiUrl="/v1/ui/tok-1" title="Card" path="workspace/apps/card.html" />,
  )
}

describe('UiArtifact open_url bridge', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('denies non-http and same-origin URLs', async () => {
    const { container } = renderArtifact()
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { openUrl(f, 'javascript:alert(1)') })
    await act(async () => { openUrl(f, `${window.location.origin}/v1/agents`) })
    const denials = spy.mock.calls.filter(
      (c) => c[0]?.type === 'open_url_ack' && c[0]?.status === 'denied',
    )
    expect(denials.length).toBe(2)
    expect(container.querySelector('[data-testid="ui-consent-chip"]')).toBeNull()
  })

  it('first open shows the origin chip; Allow opens and persists consent', async () => {
    const winOpen = vi.spyOn(window, 'open').mockReturnValue({} as Window)
    const { container } = renderArtifact()
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { openUrl(f, 'https://docs.example.com/deep/page') })
    const chip = container.querySelector('[data-testid="ui-consent-chip"]')
    expect(chip).not.toBeNull()
    expect(chip!.textContent).toContain('https://docs.example.com')
    expect(chip!.textContent).not.toContain('/deep')
    const allow = Array.from(chip!.querySelectorAll('button'))
      .find((b) => b.textContent === 'Allow')!
    await act(async () => { allow.click() })
    expect(winOpen).toHaveBeenCalledWith(
      'https://docs.example.com/deep/page', '_blank', 'noopener,noreferrer')
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'open_url_ack', status: 'opened' }), '*',
    )
    expect(localStorage.getItem('otodock-artifact-openurl:workspace/apps/card.html')).toBe('allowed')
  })

  it('a stored block acks blocked without opening', async () => {
    localStorage.setItem('otodock-artifact-openurl:workspace/apps/card.html', 'blocked')
    const winOpen = vi.spyOn(window, 'open').mockReturnValue({} as Window)
    const { container } = renderArtifact()
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { openUrl(f, 'https://example.com/x') })
    expect(winOpen).not.toHaveBeenCalled()
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'open_url_ack', status: 'blocked' }), '*',
    )
  })
})
