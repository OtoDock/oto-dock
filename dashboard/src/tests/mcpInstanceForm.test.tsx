import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'

// ─── Admin, MCP Servers, an instance MCP (SSH): the new-instance form opens
//     under the Instances header's Add, above the hosts, so a long list never
//     hides it; on a desktop its first field takes the focus, on a phone it
//     does not; Escape and Cancel close it and give the focus back to Add;
//     an edit still opens under its own row ───

const h = vi.hoisted(() => ({
  instances: [] as unknown[],
  created: [] as unknown[],
  createImpl: { current: async (_v: unknown): Promise<unknown> => ({}) },
}))

vi.mock('@/api/mcps', () => {
  const idle = () => ({ mutate: () => {}, mutateAsync: async () => ({}), isPending: false })
  return {
    useMcpInstances: () => ({
      data: {
        instances: h.instances,
        fields: [
          { key: 'host', label: 'Host', input_type: 'text', default: '', required: true, secret: false },
          { key: 'user', label: 'User', input_type: 'text', default: '', required: false, secret: false },
        ],
        delivery: 'config_file',
        max_instances: 0,
      },
    }),
    useCreateMcpInstance: () => ({
      mutateAsync: (v: unknown) => { h.created.push(v); return h.createImpl.current(v) },
      isPending: false,
    }),
    useUpdateMcpInstance: idle,
    useDeleteMcpInstance: idle,
    useSshKeys: () => ({ data: [] }),
    useUploadSshKey: idle,
    useDeleteSshKey: idle,
  }
})
vi.mock('@/api/phone', () => ({ usePhoneRoutes: () => ({ data: [] }), PHONE_OUTBOUND: 'outbound' }))
vi.mock('@/api/audio', () => ({ useAudioProviders: () => ({ data: [] }) }))

import McpInstanceManager from '@/components/admin/McpInstanceManager'

const host = (id: number, name: string) => ({
  id, mcp_name: 'ssh-hosts', instance_name: name, field_values: { host: `${name}.lan`, user: 'root' },
  agents: ['alpha'], assigned_to_all: false,
})

function stubPointer(coarse: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: q === '(pointer: coarse)' ? coarse : false,
    media: q, addEventListener: () => {}, removeEventListener: () => {},
  }))
}
const scrolled: Element[] = []

beforeEach(() => {
  h.instances = Array.from({ length: 12 }, (_, i) => host(i + 1, `host-${i + 1}`))
  h.created.length = 0
  h.createImpl.current = async () => ({})
  scrolled.length = 0
  Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this) }
  stubPointer(false)
})
afterEach(() => vi.unstubAllGlobals())

function renderManager() {
  render(<McpInstanceManager mcpName="ssh-hosts" agents={['alpha', 'beta']} />)
}
const before = (a: Node, b: Node) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)

// The fields are named by their labels.
const hostField = () => screen.getByLabelText(/^Host/)

function pressAdd() {
  const add = screen.getByRole('button', { name: 'Add' })
  add.focus()
  fireEvent.click(add)
  return add
}

describe('McpInstanceManager: the new-instance form opens where it is invoked', () => {
  it('renders under Add, before the first host row, and scrolls into view', () => {
    renderManager()
    const add = pressAdd()
    const name = screen.getByPlaceholderText('e.g. production-server')
    expect(before(name, screen.getByText('host-1'))).toBe(true)
    expect(before(add, name)).toBe(true)
    expect(add).toHaveAttribute('aria-expanded', 'true')
    const form = document.getElementById(add.getAttribute('aria-controls') || '')
    expect(form).toContainElement(name)
    expect(scrolled).toContain(form)
  })

  it('on a desktop the Name field takes the focus', () => {
    renderManager()
    pressAdd()
    expect(screen.getByPlaceholderText('e.g. production-server')).toHaveFocus()
  })

  it('on a phone no field is focused by code', () => {
    stubPointer(true)
    renderManager()
    const add = pressAdd()
    expect(screen.getByPlaceholderText('e.g. production-server')).not.toHaveFocus()
    expect(add).toHaveFocus()
  })

  it('Escape closes the form and the focus is back on Add', () => {
    renderManager()
    const add = pressAdd()
    fireEvent.keyDown(screen.getByPlaceholderText('e.g. production-server'), { key: 'Escape' })
    expect(screen.queryByPlaceholderText('e.g. production-server')).toBeNull()
    expect(add).toHaveFocus()
    expect(add).toHaveAttribute('aria-expanded', 'false')
  })

  it('Cancel closes the form and the focus is back on Add', () => {
    renderManager()
    const add = pressAdd()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByPlaceholderText('e.g. production-server')).toBeNull()
    expect(add).toHaveFocus()
  })

  it('a saved instance closes the form and gives the focus back to Add', async () => {
    renderManager()
    const add = pressAdd()
    fireEvent.change(screen.getByPlaceholderText('e.g. production-server'), { target: { value: 'new-host' } })
    fireEvent.change(hostField(), { target: { value: 'new.lan' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.queryByPlaceholderText('e.g. production-server')).toBeNull())
    expect(h.created).toHaveLength(1)
    expect(add).toHaveFocus()
  })

  it('a refused save keeps the form open', async () => {
    h.createImpl.current = async () => { throw new Error('Name taken') }
    renderManager()
    pressAdd()
    fireEvent.change(screen.getByPlaceholderText('e.g. production-server'), { target: { value: 'host-1' } })
    fireEvent.change(hostField(), { target: { value: 'a.lan' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(h.created).toHaveLength(1))
    expect(screen.getByPlaceholderText('e.g. production-server')).toBeInTheDocument()
  })

  it('an Escape inside the "available to all" confirm does not close the form', async () => {
    renderManager()
    pressAdd()
    fireEvent.click(screen.getByRole('button', { name: 'Toggle available to all agents' }))
    const confirm = await screen.findByRole('alertdialog')
    fireEvent.keyDown(within(confirm).getByRole('button', { name: 'Cancel' }), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(screen.getByPlaceholderText('e.g. production-server')).toBeInTheDocument()
  })

  it('an edit opens under its own row, after it and before the next one', () => {
    renderManager()
    const row = screen.getByText('host-3').closest('.rounded-card') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: 'Edit' }))
    const name = screen.getByDisplayValue('host-3')
    expect(before(screen.getByText('host-3'), name)).toBe(true)
    expect(before(name, screen.getByText('host-4'))).toBe(true)
  })

  it('with no instance yet, Add opens the form where "No instances configured" was', () => {
    h.instances = []
    renderManager()
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(screen.queryByText('No instances configured.')).toBeNull()
    expect(screen.getByPlaceholderText('e.g. production-server')).toBeInTheDocument()
  })
})
