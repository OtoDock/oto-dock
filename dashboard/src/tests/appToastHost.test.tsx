/**
 * A toast raised outside the component tree (appToasts) shows on whatever
 * page is open, with no inbox row behind it, and goes away by itself.
 */
import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import AppToastHost from '@/components/AppToastHost'
import { emitAppToast } from '@/lib/appToasts'

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

it('shows a local toast on the page and dismisses it after its time', () => {
  const { container } = render(<AppToastHost />)
  expect(container.innerHTML).toBe('')
  act(() => emitAppToast({ severity: 'success', title: 'Account connected', body: 'Your Google account is connected.' }))
  expect(screen.getByText('Account connected')).toBeInTheDocument()
  expect(screen.getByText('Your Google account is connected.')).toBeInTheDocument()
  act(() => { vi.advanceTimersByTime(5_100) })
  expect(container.innerHTML).toBe('')
})
