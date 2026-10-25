import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, fireEvent, waitFor } from '@testing-library/react'

const modeMutate = vi.fn()
const tokenMutate = vi.fn()
const unattendedMutate = vi.fn()
// The instant controls save through mutateAsync: it records the call, and
// the mock's return decides the answer.
const asAsync = (fn: (v: unknown) => unknown) => (v: unknown) => Promise.resolve(fn(v))
vi.mock('@/api/remoteMachines', () => ({
  useSetBrowserMode: () => ({ mutate: modeMutate, mutateAsync: asAsync(modeMutate), isPending: false, error: null }),
  useSetBrowserToken: () => ({ mutate: tokenMutate, isPending: false, error: null }),
  useSetBrowserUnattended: () => ({ mutate: unattendedMutate, mutateAsync: asAsync(unattendedMutate), isPending: false, error: null }),
}))
const toast = vi.hoisted(() => vi.fn())
vi.mock('@/lib/appToasts', () => ({ emitAppToast: toast, onAppToast: () => () => {} }))

import { BrowserModeSelect, BrowserTokenField } from '@/components/BrowserModeControls'
import type { RemoteMachine } from '@/api/remoteMachines'
import { browserModeDesc } from '@/lib/remote/browserMode'
import { answerDialog, findAlertDialog, renderWithDialogs as render } from './dialogsHarness'

function machine(over: Partial<RemoteMachine> = {}): RemoteMachine {
  return {
    id: 'm1', name: 'laptop', browser_mode: 'dedicated',
    browser_extension_token_set: false, ...over,
  } as unknown as RemoteMachine
}

beforeEach(() => {
  modeMutate.mockReset()
  tokenMutate.mockReset()
  unattendedMutate.mockReset()
  toast.mockReset()
})

// The mode selector lives in the Browser-control row; the token field shows
// only in own mode, with the short install → paste line and nothing else.
describe('BrowserModeSelect', () => {
  it('defaults to the dedicated profile and hides the token field', () => {
    render(<><BrowserModeSelect machine={machine()} scope="me" /><BrowserTokenField machine={machine()} scope="me" /></>)
    expect((screen.getByLabelText('Browser mode') as HTMLSelectElement).value).toBe('dedicated')
    expect(screen.queryByLabelText('Playwright Extension token')).toBeNull()
    expect(browserModeDesc('dedicated')).toMatch(/separate browser profile for each agent/)
    expect(browserModeDesc('own')).toMatch(/signed into/)
  })

  it('switching to own mode asks for confirmation before the PUT', async () => {
    render(<BrowserModeSelect machine={machine()} scope="admin" />)
    fireEvent.change(screen.getByLabelText('Browser mode'), { target: { value: 'own' } })
    const dialog = await findAlertDialog('Use your own browser on laptop?')
    // The disclosure: unattended use needs its own consent (F47).
    expect(dialog.textContent).toContain(
      'can reach your logins, cookies and open tabs. You can also let unattended runs use the '
      + 'saved token. Then scheduled tasks, triggers, calls and meetings use this browser with '
      + 'nobody watching. Those runs ask no one before a browser action.\n\n')
    await act(async () => { await answerDialog('Cancel') })
    expect(modeMutate).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Browser mode'), { target: { value: 'own' } })
    await act(async () => { await answerDialog('Use my browser') })
    await waitFor(() => expect(modeMutate).toHaveBeenCalledWith({ machineId: 'm1', mode: 'own' }))
  })

  it('switching back to the dedicated profile needs no confirmation', async () => {
    render(<BrowserModeSelect machine={machine({ browser_mode: 'own' })} scope="me" />)
    fireEvent.change(screen.getByLabelText('Browser mode'), { target: { value: 'dedicated' } })
    await waitFor(() => expect(modeMutate).toHaveBeenCalledWith({ machineId: 'm1', mode: 'dedicated' }))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
  })

  it('a refused change toasts its reason and says nothing beside the select', async () => {
    modeMutate.mockImplementation(() => { throw new Error('The machine is offline.') })
    render(<BrowserModeSelect machine={machine({ browser_mode: 'own' })} scope="me" />)
    fireEvent.change(screen.getByLabelText('Browser mode'), { target: { value: 'dedicated' } })
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.objectContaining({
      title: 'The browser mode was not saved', body: 'The machine is offline.',
    })))
    expect(screen.getByRole('status').textContent).toBe('')
  })
})

describe('BrowserTokenField', () => {
  it('own mode without a token: install line + extension link, Save gated on input', () => {
    render(<BrowserTokenField machine={machine({ browser_mode: 'own' })} scope="me" />)
    const link = screen.getByRole('link', { name: /Get extension/ })
    expect(link.getAttribute('href')).toMatch(/chromewebstore\.google\.com/)
    expect(link.getAttribute('target')).toBe('_blank')
    expect(screen.getByText(/paste it here/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'About Playwright Extension token' }), { detail: 0 })
    expect(screen.getByRole('tooltip').textContent).toBe(
      'Saving it lets the sessions you drive connect without a click.',
    )
    // No token, no unattended box.
    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(screen.queryByText(/No token/)).toBeNull()
    expect(screen.queryByText('Token saved')).toBeNull()
    const save = screen.getByRole('button', { name: 'Save token' })
    expect(save).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Playwright Extension token'),
      { target: { value: ' PLAYWRIGHT_MCP_EXTENSION_TOKEN=abc ' } })
    expect(save).not.toBeDisabled()
    fireEvent.click(save)
    expect(tokenMutate.mock.calls[0][0]).toEqual({ machineId: 'm1', token: 'PLAYWRIGHT_MCP_EXTENSION_TOKEN=abc' })
  })

  it('Enter in the field saves too', () => {
    render(<BrowserTokenField machine={machine({ browser_mode: 'own' })} scope="admin" />)
    const input = screen.getByLabelText('Playwright Extension token')
    fireEvent.change(input, { target: { value: 'tok-123456789012345' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(tokenMutate.mock.calls[0][0]).toEqual({ machineId: 'm1', token: 'tok-123456789012345' })
  })

  it('a saved token shows the indicator and a Clear that DELETEs it', () => {
    render(<BrowserTokenField machine={machine({ browser_mode: 'own', browser_extension_token_set: true })} scope="me" />)
    expect(screen.getByText('Token saved')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(tokenMutate).toHaveBeenCalledWith({ machineId: 'm1', token: null })
  })
})


describe('unattended use of the saved token (F47)', () => {
  it('asks before letting unattended runs use the browser, and turns off at once', async () => {
    const { rerender } = render(<BrowserTokenField
      machine={machine({ browser_mode: 'own', browser_extension_token_set: true })} scope="me" />)
    const box = screen.getByRole('checkbox')
    expect(box).not.toBeChecked()
    fireEvent.click(box)
    const dialog = await findAlertDialog('Let unattended runs use your browser on laptop?')
    await act(async () => { await answerDialog('Cancel') })
    expect(unattendedMutate).not.toHaveBeenCalled()
    expect(dialog.isConnected).toBe(false)
    fireEvent.click(screen.getByRole('checkbox'))
    await act(async () => { await answerDialog('Let them use it') })
    await waitFor(() => expect(unattendedMutate).toHaveBeenCalledWith({ machineId: 'm1', enabled: true }))
    rerender(<BrowserTokenField machine={machine({
      browser_mode: 'own', browser_extension_token_set: true, browser_unattended: true,
    })} scope="me" />)
    fireEvent.click(screen.getByRole('checkbox'))
    await waitFor(() => expect(unattendedMutate).toHaveBeenLastCalledWith({ machineId: 'm1', enabled: false }))
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })
})
