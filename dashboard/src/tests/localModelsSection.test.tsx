// The shared Local models section: one endpoint listed once with a checkbox
// per engine that accepts a local endpoint (read from the engines'
// descriptors, not a hard-coded pair); the add form enables every such
// engine by default (operator decision) and the admin unticks what the
// endpoint should not serve.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

const setEngine = vi.fn()
const addEndpoint = vi.fn()
const delEndpoint = vi.fn()

vi.mock('@/api/executionLayers', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/api/executionLayers')>()
  const { claudeLike, codexLike, directLike } = await import('./fixtures/engines')
  return {
    ...mod,
    // The admin catalog: Claude takes no local endpoint, Codex and Direct do.
    useAdminExecutionLayers: () => ({
      data: [
        { name: 'direct-llm', display_name: 'Direct LLM API', capabilities: directLike() },
        { name: 'claude-code-cli', display_name: 'Claude Code CLI', capabilities: claudeLike() },
        { name: 'codex-cli', display_name: 'OpenAI Codex', capabilities: codexLike() },
      ],
    }),
    useAdminLocalEndpoints: () => ({
      data: [{
        group: 'openai_compatible:abc',
        provider: 'openai_compatible',
        endpoint_url: 'http://192.168.1.8:8080/v1',
        label: 'Local Qwen',
        has_api_key: true,
        engines: {
          'direct-llm': { id: 'd1', status: 'active', active_sessions: 0, is_mine: true },
        },
      }],
    }),
    useSetLocalEndpointEngine: () => ({ mutate: setEngine, mutateAsync: (v: unknown) => Promise.resolve(setEngine(v)), isPending: false, isError: false }),
    useAddLocalEndpoint: () => ({ mutate: addEndpoint, isPending: false, isError: false }),
    useDeleteLocalEndpoint: () => ({ mutate: delEndpoint, mutateAsync: vi.fn(() => Promise.resolve()), isPending: false, isError: false }),
  }
})

import { LocalModelsSection } from '@/pages/admin/ExecutionLayersTab.local'

describe('LocalModelsSection', () => {
  it('lists the endpoint once with a checkbox per engine and toggles the missing one', () => {
    const onDiscover = vi.fn()
    render(<LocalModelsSection layer="direct-llm" onDiscover={onDiscover} />)
    expect(screen.getByText('Local Qwen')).toBeTruthy()
    expect(screen.getByText('Key set')).toBeTruthy()
    const direct = screen.getByLabelText('Direct LLM API') as HTMLInputElement
    const codex = screen.getByLabelText('Codex') as HTMLInputElement
    expect(direct.checked).toBe(true)
    expect(codex.checked).toBe(false)
    // Claude accepts no local endpoint, so it gets no checkbox.
    expect(screen.queryByLabelText('Claude Code CLI')).toBeNull()
    fireEvent.click(codex)
    expect(setEngine).toHaveBeenCalledWith({ group: 'openai_compatible:abc', layer: 'codex-cli', enabled: true })
    fireEvent.click(screen.getByText('Discover'))
    expect(onDiscover).toHaveBeenCalledWith({ id: 'd1', provider: 'openai_compatible', layers: ['direct-llm'] })
  })

  it('cannot discover from an engine the endpoint is not enabled for', () => {
    render(<LocalModelsSection layer="codex-cli" onDiscover={vi.fn()} />)
    expect((screen.getByText('Discover') as HTMLButtonElement).disabled).toBe(true)
  })

  // Forms open where they are invoked: the add form under its button, above
  // the servers; what a server's row opens (its discover panel), under it.
  it('the add form opens above the server list', () => {
    render(<LocalModelsSection layer="direct-llm" onDiscover={vi.fn()} />)
    fireEvent.click(screen.getByText('+ Local server'))
    const address = screen.getByLabelText('Server address')
    const row = screen.getByText('Local Qwen')
    expect(address.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it("a row's panel renders right under that row", () => {
    render(
      <LocalModelsSection
        layer="direct-llm" onDiscover={vi.fn()}
        belowRow={(id) => (id === 'd1' ? <p>Discovered models</p> : null)}
      />,
    )
    const row = screen.getByText('Local Qwen')
    const panel = screen.getByText('Discovered models')
    expect(row.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('adds an endpoint for every local-capable engine by default', () => {
    render(<LocalModelsSection layer="codex-cli" onDiscover={vi.fn()} />)
    fireEvent.click(screen.getByText('+ Local server'))
    const both = [
      screen.getByLabelText('Use with Direct LLM API') as HTMLInputElement,
      screen.getByLabelText('Use with Codex') as HTMLInputElement,
    ]
    expect(both.map((b) => b.checked)).toEqual([true, true])
    fireEvent.change(screen.getByLabelText('Server address'), { target: { value: 'http://192.168.1.8:8080/v1' } })
    fireEvent.click(both[1])
    fireEvent.click(screen.getByText('Add'))
    expect(addEndpoint).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: 'openai_compatible',
        endpoint_url: 'http://192.168.1.8:8080/v1',
        layers: ['direct-llm'],
      }),
      expect.anything(),
    )
  })
})
