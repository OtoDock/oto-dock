/**
 * A toast raised outside the component tree (appToasts) shows on whatever
 * page is open, with no inbox row behind it, and goes away by itself.
 */
import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import AppToastHost from '@/components/AppToastHost'
import ToastViewport from '@/components/ui/Toast'
import { emitAppToast } from '@/lib/appToasts'

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

it('shows a local toast in the page\'s toast region and dismisses it after its time', () => {
  render(<><ToastViewport /><AppToastHost /></>)
  const region = screen.getByRole('region', { name: 'Notifications' })
  expect(region.innerHTML).toBe('')
  act(() => emitAppToast({ severity: 'success', title: 'Account connected', body: 'Your Google account is connected.' }))
  expect(screen.getByText('Account connected')).toBeInTheDocument()
  expect(screen.getByText('Your Google account is connected.')).toBeInTheDocument()
  expect(region).toContainElement(screen.getByRole('status'))
  act(() => { vi.advanceTimersByTime(5_100) })
  expect(region.innerHTML).toBe('')
})

it('folds a toast equal to one still shown, and shows a different one', () => {
  render(<><ToastViewport /><AppToastHost /></>)
  const refused = { severity: 'warning' as const, title: 'The setting was not saved', body: 'Only an admin can change the memory settings.' }
  act(() => { for (let i = 0; i < 4; i++) emitAppToast(refused) })
  expect(screen.getAllByText('The setting was not saved')).toHaveLength(1)
  act(() => emitAppToast({ ...refused, body: 'The server did not answer.' }))
  expect(screen.getAllByText('The setting was not saved')).toHaveLength(2)
  act(() => { vi.advanceTimersByTime(10_100) })
  expect(screen.queryByText('The setting was not saved')).not.toBeInTheDocument()
})
