// The admin card's model groups: the "Supports xhigh" checkbox appears on the
// add form and in a row's editor only under a provider whose descriptor entry
// declares xhigh per model (`effort_per_model`) — read from the declaration,
// never from a provider name. The providers here are invented.
import { describe, it, expect, vi } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent } from '@testing-library/react'

vi.mock('@/api/executionLayers', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/api/executionLayers')>()
  return {
    ...mod,
    useAddModel: () => ({ mutate: vi.fn(), isPending: false }),
    useUpdateModel: () => ({ mutate: vi.fn(), isPending: false }),
    useDeleteModel: () => ({ mutate: vi.fn(), isPending: false }),
  }
})

import { ModelsByProvider } from '@/pages/admin/ExecutionLayersTab.rows'
import type { LayerModel } from '@/api/executionLayers'
import type { EngineProvider } from '@/api/engineDescriptor'

const PROVIDERS: EngineProvider[] = [
  { id: 'forge', label: 'Forge', requires_key: true,
    effort_scale: ['low', 'medium', 'high', 'xhigh', 'max'], effort_per_model: ['xhigh'] },
  { id: 'lan_box', label: 'A box on the LAN', requires_key: false,
    effort_scale: ['low', 'medium', 'high', 'xhigh'], effort_per_model: [] },
]

function row(id: number, provider: string): LayerModel {
  return {
    id, layer: 'acme-engine', provider, model_id: `${provider}-model`, display_name: `${provider} model`,
    is_builtin: 0, enabled: 1, context_window: 0, pricing_input: 0, pricing_output: 0,
    pricing_cache_write: 0, pricing_cache_read: 0, supports_reasoning: 1, supports_xhigh: 0,
    created_at: '', updated_at: '',
  }
}

function Harness() {
  const [showAddModel, setShowAddModel] = useState<string | false>(false)
  return (
    <ModelsByProvider
      models={[row(1, 'forge'), row(2, 'lan_box')]}
      layer="acme-engine"
      pricingEditable
      providers={PROVIDERS}
      showAddModel={showAddModel}
      onAddCustom={(p) => setShowAddModel(showAddModel === p ? false : p)}
      onAddDone={() => setShowAddModel(false)}
    />
  )
}

const xhighBox = () => screen.queryByLabelText(/Supports xhigh effort/)

describe('ModelsByProvider — the xhigh checkbox reads the provider entry', () => {
  it('the add form shows it under the provider that declares xhigh per model only', () => {
    render(<Harness />)
    const [forgeAdd, lanAdd] = screen.getAllByRole('button', { name: '+ Custom' })
    fireEvent.click(forgeAdd)
    expect(xhighBox()).toBeInTheDocument()
    fireEvent.click(forgeAdd)   // closes the form
    fireEvent.click(lanAdd)
    expect(screen.getByPlaceholderText(/Model ID/)).toBeInTheDocument()
    expect(xhighBox()).not.toBeInTheDocument()
  })

  it("a row's editor follows the same declaration", () => {
    render(<Harness />)
    const [forgeEdit, lanEdit] = screen.getAllByTitle('Edit pricing, context & tier')
    fireEvent.click(forgeEdit)
    expect(xhighBox()).toBeInTheDocument()
    fireEvent.click(forgeEdit)  // collapses
    fireEvent.click(lanEdit)
    expect(screen.getByLabelText('Capability tier')).toBeInTheDocument()
    expect(xhighBox()).not.toBeInTheDocument()
  })
})
