import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

const modeMutate = vi.fn()
const tokenMutate = vi.fn()
vi.mock('@/api/remoteMachines', () => ({
  useSetBrowserMode: () => ({ mutate: modeMutate, isPending: false, error: null }),
  useSetBrowserToken: () => ({ mutate: tokenMutate, isPending: false, error: null }),
}))

import {
  BrowserModeSelect, BrowserTokenField, browserModeDesc,
} from '@/components/BrowserModeControls'
import type { RemoteMachine } from '@/api/remoteMachines'

function machine(over: Partial<RemoteMachine> = {}): RemoteMachine {
  return {
    id: 'm1', name: 'laptop', browser_mode: 'dedicated',
    browser_extension_token_set: false, ...over,
  } as unknown as RemoteMachine
}

beforeEach(() => {
  modeMutate.mockReset()
  tokenMutate.mockReset()
})

// The mode selector lives in the Browser-control row; the token field shows
// only in own mode, with the short install → paste line and nothing else.
describe('BrowserModeSelect', () => {
  it('defaults to the dedicated profile and hides the token field', () => {
    render(<><BrowserModeSelect machine={machine()} scope="me" /><BrowserTokenField machine={machine()} scope="me" /></>)
    expect((screen.getByLabelText('Browser mode') as HTMLSelectElement).value).toBe('dedicated')
    expect(screen.queryByLabelText('Playwright Extension token')).toBeNull()
    expect(browserModeDesc('dedicated')).toMatch(/dedicated per-agent/)
    expect(browserModeDesc('own')).toMatch(/signed into/)
  })

  it('switching to own mode asks for confirmation before the PUT', () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<BrowserModeSelect machine={machine()} scope="admin" />)
    fireEvent.change(screen.getByLabelText('Browser mode'), { target: { value: 'own' } })
    expect(modeMutate).not.toHaveBeenCalled()
    confirm.mockReturnValue(true)
    fireEvent.change(screen.getByLabelText('Browser mode'), { target: { value: 'own' } })
    expect(modeMutate).toHaveBeenCalledWith({ machineId: 'm1', mode: 'own' })
    // The disclosure: unattended use with the saved token.
    expect(confirm.mock.calls[0][0]).toContain(
      'your logins, cookies and open tabs are reachable to them. With the extension token saved, '
      + 'scheduled tasks, triggers, calls and meetings on this machine use this browser with nobody '
      + 'watching, and browser actions are not asked about; only the tools the browser MCP marks '
      + 'high-risk still need a person.\n\n')
    confirm.mockRestore()
  })

  it('switching back to the dedicated profile needs no confirmation', () => {
    const confirm = vi.spyOn(window, 'confirm')
    render(<BrowserModeSelect machine={machine({ browser_mode: 'own' })} scope="me" />)
    fireEvent.change(screen.getByLabelText('Browser mode'), { target: { value: 'dedicated' } })
    expect(confirm).not.toHaveBeenCalled()
    expect(modeMutate).toHaveBeenCalledWith({ machineId: 'm1', mode: 'dedicated' })
    confirm.mockRestore()
  })
})

describe('BrowserTokenField', () => {
  it('own mode without a token: install line + extension link, Save gated on input', () => {
    render(<BrowserTokenField machine={machine({ browser_mode: 'own' })} scope="me" />)
    const link = screen.getByRole('link', { name: /Get extension/ })
    expect(link.getAttribute('href')).toMatch(/chromewebstore\.google\.com/)
    expect(link.getAttribute('target')).toBe('_blank')
    expect(screen.getByText(/paste it here/)).toBeInTheDocument()
    expect(screen.getByText(
      'Saving it lets scheduled tasks, triggers, calls and meetings use this browser unattended.',
    )).toBeInTheDocument()
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
