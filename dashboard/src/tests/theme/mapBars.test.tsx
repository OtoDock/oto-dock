// A page that paints its own bars (the agents map) hands the apps the colour
// its theme class gives it while it shows, and the theme's colour after.
import { render } from '@testing-library/react'
import { useRef } from 'react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

const { callNative } = vi.hoisted(() => ({ callNative: vi.fn(() => true) }))
vi.mock('../../lib/nativeBridge', () => ({ callNative }))
vi.mock('../../lib/device/boot', () => ({ onBootDone: (fn: () => void) => fn() }))

import { useNativeBarsFrom } from '../../contexts/useNativeBars'

function Page({ active }: { active: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  useNativeBarsFrom(ref, active)
  return <div ref={ref} style={{ ['--ot-surface-page' as string]: '#0b1220' }} />
}

describe('the bars a page paints', () => {
  beforeEach(() => {
    callNative.mockClear()
    document.documentElement.style.setProperty('--ot-surface-page', '#ffffff')
  })

  it('sends the page colour while active and the theme colour after', () => {
    const view = render(<Page active />)
    expect(callNative).toHaveBeenCalledWith('setContainerColor', '#0b1220')
    callNative.mockClear()
    view.rerender(<Page active={false} />)
    expect(callNative).toHaveBeenCalledWith('setContainerColor', '#ffffff')
  })

  it('sends nothing while inactive', () => {
    render(<Page active={false} />)
    expect(callNative).not.toHaveBeenCalled()
  })
})
