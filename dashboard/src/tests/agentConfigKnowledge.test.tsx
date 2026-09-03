import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

// ─── AgentConfig — Shared knowledge card ────────────────────────────────────
// Libraries are per-SUBTREE since the per-folder release: an agent shares its
// whole knowledge folder (subdir '') or disjoint subfolders, each an
// independent library with its own attachments — so every mutation carries
// (source, subdir). Wiring stays platform-role territory: admins + creators
// mutate; per-agent managers see the state read-only (backend 403s their
// mutations, so the controls never render for them). The card itself is
// hidden until the manager-tier attachments GET returns. Un-sharing a
// library with consumers goes through a type-to-confirm listing them.
// The all-libraries picker feed is admin/creator-only server-side, so the
// hook must be called with enabled=false for everyone else.

type KLib = {
  subdir: string; name: string
  consumers: { consumer_agent: string; writable: boolean }[]
  has_bulletin: boolean
}
type KAtt = {
  source_agent: string; subdir: string; writable: boolean; name: string
  has_bulletin: boolean
}

const h = vi.hoisted(() => ({
  setLibraryMock: vi.fn(),
  attachMock: vi.fn(),
  detachMock: vi.fn(),
  // Records the `enabled` arg of the last useKnowledgeLibraries call — the
  // 403-avoidance contract ("never fires for non-admin/creator") is only
  // visible through it once the fetch itself is mocked away.
  librariesEnabledArg: undefined as undefined | boolean,
  agentInfo: {
    name: 'demo',
    display_name: 'Demo',
    collaborative: true,
    default_scope: 'user' as 'user' | 'agent',
    default_model: '',
    execution_path: 'claude-code-cli',
    execution_paths: ['claude-code-cli'],
  },
  knowledgeData: undefined as undefined | {
    is_library: boolean
    libraries: {
      subdir: string; name: string
      consumers: { consumer_agent: string; writable: boolean }[]
      has_bulletin: boolean
    }[]
    attachments: {
      source_agent: string; subdir: string; writable: boolean; name: string
      has_bulletin: boolean
    }[]
  },
  libraries: [] as {
    source_agent: string; subdir: string; created_by: string
    created_at: string; consumers: number; name?: string
  }[],
  // Knowledge tree feeding the share form's folder picker (useAgentFiles).
  fileTree: [] as unknown[],
  user: { role: 'admin', sub: 'u1', agent_roles: {} as Record<string, string> },
}))

const mkLib = (over: Partial<KLib> = {}): KLib => ({
  subdir: '', name: '', consumers: [], has_bulletin: false, ...over,
})
const mkAtt = (over: Partial<KAtt> = {}): KAtt => ({
  source_agent: 'kb', subdir: '', writable: false, name: '',
  has_bulletin: false, ...over,
})

vi.mock('@/api/agents', () => ({
  useAgentInfo: () => ({ data: h.agentInfo, isLoading: false }),
  useUpdateAgent: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteAgent: () => ({ mutate: vi.fn(), isPending: false }),
  useDelegationTargets: () => ({ data: undefined }),
  useSetDelegationTargets: () => ({ mutate: vi.fn(), isPending: false }),
  useExecutionLayers: () => ({ data: undefined }),
  useSetDefaultForNewUsers: () => ({ mutate: vi.fn() }),
  useKnowledgeAttachments: () => ({ data: h.knowledgeData }),
  useKnowledgeLibraries: (enabled: boolean) => {
    h.librariesEnabledArg = enabled
    return { data: enabled ? h.libraries : undefined }
  },
  useSetKnowledgeLibrary: () => ({ mutate: h.setLibraryMock, isPending: false }),
  useAttachKnowledgeLibrary: () => ({ mutate: h.attachMock, isPending: false }),
  useDetachKnowledgeLibrary: () => ({ mutate: h.detachMock, isPending: false }),
  // Picker feed — only queried while the share form is open (name '' = off).
  useAgentFiles: (name: string) => ({ data: name ? h.fileTree : undefined }),
}))
vi.mock('@/api/remoteMachines', () => ({ useRemoteMachines: () => ({ data: [] }) }))
vi.mock('@/api/departments', () => ({ useDepartments: () => ({ data: [] }) }))
vi.mock('@/api/memory', () => ({
  useAgentMemorySettings: () => ({
    data: {
      user_memory_enabled: true,
      agent_memory_enabled: true,
      master: { user_memory_enabled: true, agent_memory_enabled: true },
    },
  }),
  useSetAgentMemoryToggle: () => ({ mutate: vi.fn() }),
  useClearAgentMemory: () => ({ mutate: vi.fn(), isPending: false }),
}))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: h.user }),
}))

import AgentConfig from '@/pages/agent/AgentConfig'

function renderConfig() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/agents/demo/config']}>
        <Routes>
          <Route path="/agents/:name/config" element={<AgentConfig />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const shareFolderBtn = () =>
  screen.getByRole('button', { name: 'Share a folder…' }) as HTMLButtonElement

describe('AgentConfig — execution target row on phones', () => {
  it('lets the machine select fill the row so the status badge wraps below it', () => {
    // Photo-reported 2026-08-28: select + "online" badge sat on one
    // non-wrapping row wider than the phone. The select is full-width +
    // shrinkable (w-full min-w-0) inside a wrapping row; desktop keeps the
    // auto-width select with the badge beside it (sm:w-auto).
    h.knowledgeData = { is_library: false, libraries: [], attachments: [] }
    h.libraries = []
    renderConfig()
    const local = screen.getByRole('option', { name: 'Local (this server)' })
    const select = local.closest('select') as HTMLSelectElement
    const classes = select.className.split(/\s+/)
    for (const cls of ['w-full', 'min-w-0', 'sm:w-auto']) expect(classes).toContain(cls)
    const row = select.parentElement as HTMLElement
    expect(row.className.split(/\s+/)).toContain('flex-wrap')
  })
})

describe('AgentConfig — shared knowledge card', () => {
  beforeEach(() => {
    h.setLibraryMock.mockClear()
    h.attachMock.mockClear()
    h.detachMock.mockClear()
    h.librariesEnabledArg = undefined
    h.knowledgeData = { is_library: false, libraries: [], attachments: [] }
    h.libraries = []
    const dir = (name: string, path: string, children: unknown[] = []) =>
      ({ name, type: 'dir', path, size: 0, modified: '', children })
    h.fileTree = [
      dir('knowledge', 'knowledge', [
        dir('marketing', 'knowledge/marketing', [
          dir('campaigns', 'knowledge/marketing/campaigns'),
        ]),
        dir('docs', 'knowledge/docs'),
        // Reserved consumer-mirror root — the picker must hide it.
        dir('shared', 'knowledge/shared'),
        { name: 'notes.md', type: 'file', path: 'knowledge/notes.md', size: 1, modified: '' },
      ]),
    ]
    h.user = { role: 'admin', sub: 'u1', agent_roles: {} }
  })

  it('is hidden until the attachments query returns', () => {
    h.knowledgeData = undefined
    renderConfig()
    expect(screen.queryByText('Shared Knowledge')).toBeNull()
  })

  it('admin: Share a folder opens the picker; Share commits name + subdir', () => {
    renderConfig()
    fireEvent.click(shareFolderBtn())
    // A library needs a label, so opening the form must NOT promote.
    expect(h.setLibraryMock).not.toHaveBeenCalled()
    const shareBtn = () => screen.getByRole('button', { name: 'Share' }) as HTMLButtonElement
    expect(shareBtn().disabled).toBe(true)
    // The picker lists the knowledge tree (dirs only, nested indented) but
    // NEVER the reserved shared/ mirror root or plain files.
    const picker = screen.getByRole('listbox', { name: 'Library subfolder' })
    expect(within(picker).getByRole('option', { name: /Whole knowledge folder/ })).toBeTruthy()
    expect(within(picker).getByRole('option', { name: /marketing\/campaigns/ })).toBeTruthy()
    expect(within(picker).queryByRole('option', { name: /shared/ })).toBeNull()
    expect(within(picker).queryByText('notes.md')).toBeNull()
    fireEvent.click(within(picker).getByRole('option', { name: /^marketing$/ }))
    fireEvent.change(screen.getByLabelText('Library name'), {
      target: { value: '  Brand Guidelines  ' },
    })
    expect(shareBtn().disabled).toBe(false)
    fireEvent.click(shareBtn())
    expect(h.setLibraryMock).toHaveBeenCalledTimes(1)
    expect(h.setLibraryMock.mock.calls[0][0]).toEqual({
      agent: 'demo', enabled: true, name: 'Brand Guidelines', subdir: 'marketing',
    })
  })

  it('folders covered by an existing library are disabled; the default pick falls to the first free one', () => {
    h.knowledgeData = {
      is_library: true,
      libraries: [mkLib({ subdir: 'marketing', name: 'Brand' })],
      attachments: [],
    }
    renderConfig()
    fireEvent.click(shareFolderBtn())
    const picker = screen.getByRole('listbox', { name: 'Library subfolder' })
    // Root overlaps every library; marketing + its subtree are taken.
    expect((within(picker).getByRole('option', { name: /Whole knowledge folder/ }) as HTMLButtonElement).disabled).toBe(true)
    expect((within(picker).getByRole('option', { name: /^marketing$/ }) as HTMLButtonElement).disabled).toBe(true)
    expect((within(picker).getByRole('option', { name: /marketing\/campaigns/ }) as HTMLButtonElement).disabled).toBe(true)
    const docs = within(picker).getByRole('option', { name: /^docs$/ }) as HTMLButtonElement
    expect(docs.disabled).toBe(false)
    // Preselection skipped the covered root and landed on docs.
    expect(docs.getAttribute('aria-selected')).toBe('true')
    fireEvent.change(screen.getByLabelText('Library name'), { target: { value: 'Documentation' } })
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    expect(h.setLibraryMock.mock.calls[0][0]).toEqual({
      agent: 'demo', enabled: true, name: 'Documentation', subdir: 'docs',
    })
  })

  it('an empty subfolder shares the whole knowledge folder', () => {
    renderConfig()
    fireEvent.click(shareFolderBtn())
    fireEvent.change(screen.getByLabelText('Library name'), {
      target: { value: 'Everything' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    expect(h.setLibraryMock.mock.calls[0][0]).toEqual({
      agent: 'demo', enabled: true, name: 'Everything', subdir: '',
    })
  })

  it('cancelling the share form promotes nothing', () => {
    renderConfig()
    fireEvent.click(shareFolderBtn())
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(h.setLibraryMock).not.toHaveBeenCalled()
    expect(screen.queryByLabelText('Library name')).toBeNull()
  })

  it('creator managing the agent: same powers as admin', () => {
    h.user = { role: 'creator', sub: 'u1', agent_roles: { demo: 'manager' } }
    renderConfig()
    fireEvent.click(shareFolderBtn())
    fireEvent.change(screen.getByLabelText('Library name'), {
      target: { value: 'Brand Guidelines' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    expect(h.setLibraryMock.mock.calls[0][0]).toEqual({
      agent: 'demo', enabled: true, name: 'Brand Guidelines', subdir: '',
    })
    expect(h.librariesEnabledArg).toBe(true)
  })

  it('un-sharing a library with no consumers saves immediately, no modal', () => {
    h.knowledgeData = {
      is_library: true,
      libraries: [mkLib({ subdir: 'docs', name: 'Docs' })],
      attachments: [],
    }
    renderConfig()
    fireEvent.click(screen.getByLabelText('Unshare library docs'))
    expect(screen.queryByText(/Stop sharing/)).toBeNull()
    expect(h.setLibraryMock).toHaveBeenCalledTimes(1)
    expect(h.setLibraryMock.mock.calls[0][0]).toEqual({
      agent: 'demo', enabled: false, subdir: 'docs',
    })
  })

  it('un-sharing with consumers goes through the type-to-confirm listing them', () => {
    h.knowledgeData = {
      is_library: true,
      libraries: [mkLib({
        name: 'Docs',
        consumers: [
          { consumer_agent: 'ops', writable: false },
          { consumer_agent: 'writer', writable: true },
        ],
      })],
      attachments: [],
    }
    renderConfig()
    // Consumer chips with their access level.
    expect(screen.getByText('ops (RO)')).toBeTruthy()
    expect(screen.getByText('writer (RW)')).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Unshare library root'))
    // No mutation yet — the modal gates it, listing the consumers.
    expect(h.setLibraryMock).not.toHaveBeenCalled()
    expect(screen.getByText(/Stop sharing "Docs"/)).toBeTruthy()
    expect(screen.getByText(/ops, writer/)).toBeTruthy()
    fireEvent.change(screen.getByPlaceholderText('CONFIRM'), { target: { value: 'CONFIRM' } })
    fireEvent.click(screen.getByRole('button', { name: 'Stop sharing' }))
    expect(h.setLibraryMock).toHaveBeenCalledTimes(1)
    expect(h.setLibraryMock.mock.calls[0][0]).toEqual({
      agent: 'demo', enabled: false, subdir: '',
    })
  })

  it('renaming reuses the form with the subfolder locked (no picker)', () => {
    h.knowledgeData = {
      is_library: true,
      libraries: [mkLib({ subdir: 'marketing', name: 'Old Name' })],
      attachments: [],
    }
    renderConfig()
    fireEvent.click(screen.getByLabelText('Rename library marketing'))
    // Identity is locked: a static folder chip replaces the picker.
    expect(screen.queryByRole('listbox', { name: 'Library subfolder' })).toBeNull()
    expect(screen.getByText('knowledge/marketing/')).toBeTruthy()
    const nameField = screen.getByLabelText('Library name') as HTMLInputElement
    expect(nameField.value).toBe('Old Name')
    fireEvent.change(nameField, { target: { value: 'Campaigns' } })
    fireEvent.click(screen.getByRole('button', { name: 'Rename' }))
    expect(h.setLibraryMock.mock.calls[0][0]).toEqual({
      agent: 'demo', enabled: true, name: 'Campaigns', subdir: 'marketing',
    })
  })

  it('manager (platform member): sees state read-only, picker feed never fires', () => {
    h.user = { role: 'member', sub: 'u1', agent_roles: { demo: 'manager' } }
    h.knowledgeData = {
      is_library: true,
      libraries: [mkLib({
        consumers: [{ consumer_agent: 'ops', writable: false }],
      })],
      attachments: [mkAtt({ writable: true })],
    }
    renderConfig()
    // Card + state visible…
    expect(screen.getByText('Shared Knowledge')).toBeTruthy()
    expect(screen.getByText('ops (RO)')).toBeTruthy()
    expect(screen.getByText('kb')).toBeTruthy()
    expect(screen.getByText('RW')).toBeTruthy()
    // …but every mutation control is off: no share form, no rename/unshare,
    // no writable button, no detach, no attach picker — and the
    // admin/creator-only libraries feed was called with enabled=false.
    expect(screen.queryByRole('button', { name: 'Share a folder…' })).toBeNull()
    expect(screen.queryByLabelText('Unshare library root')).toBeNull()
    expect(screen.queryByLabelText('Writable kb')).toBeNull()
    expect(screen.queryByLabelText('Detach kb')).toBeNull()
    expect(screen.queryByLabelText('Attach library')).toBeNull()
    expect(h.librariesEnabledArg).toBe(false)
  })

  it('writable toggle re-PUTs the attachment with its subdir + flipped flag', () => {
    h.knowledgeData = {
      is_library: false,
      libraries: [],
      attachments: [mkAtt({ subdir: 'marketing' })],
    }
    renderConfig()
    fireEvent.click(screen.getByLabelText('Writable kb marketing'))
    expect(h.attachMock).toHaveBeenCalledTimes(1)
    expect(h.attachMock.mock.calls[0][0]).toEqual({
      agent: 'demo', source_agent: 'kb', subdir: 'marketing', writable: true,
    })
  })

  it('detach sends the (source, subdir) pair', () => {
    h.knowledgeData = {
      is_library: false,
      libraries: [],
      attachments: [mkAtt({ subdir: 'marketing' })],
    }
    renderConfig()
    fireEvent.click(screen.getByLabelText('Detach kb marketing'))
    expect(h.detachMock).toHaveBeenCalledTimes(1)
    expect(h.detachMock.mock.calls[0][0]).toEqual({
      agent: 'demo', source: 'kb', subdir: 'marketing',
    })
  })

  it('attach picker excludes self + attached pairs and sends the subdir', () => {
    h.knowledgeData = {
      is_library: false,
      libraries: [],
      attachments: [mkAtt({ subdir: 'docs' })],
    }
    h.libraries = [
      // Same source, different subtree → still attachable.
      { source_agent: 'kb', subdir: 'marketing', created_by: 'u1', created_at: 't', consumers: 1 },
      // The exact attached pair → excluded.
      { source_agent: 'kb', subdir: 'docs', created_by: 'u1', created_at: 't', consumers: 1 },
      // Self → excluded.
      { source_agent: 'demo', subdir: '', created_by: 'u1', created_at: 't', consumers: 0 },
    ]
    renderConfig()
    const picker = screen.getByLabelText('Attach library') as HTMLSelectElement
    const values = Array.from(picker.options).map((o) => o.value)
    expect(values).toContain(JSON.stringify(['kb', 'marketing']))
    expect(values).not.toContain(JSON.stringify(['kb', 'docs']))
    expect(values).not.toContain(JSON.stringify(['demo', '']))
    fireEvent.change(picker, { target: { value: JSON.stringify(['kb', 'marketing']) } })
    fireEvent.click(screen.getByLabelText('Writable'))
    fireEvent.click(screen.getByRole('button', { name: 'Attach' }))
    expect(h.attachMock).toHaveBeenCalledTimes(1)
    expect(h.attachMock.mock.calls[0][0]).toEqual({
      agent: 'demo', source_agent: 'kb', subdir: 'marketing', writable: true,
    })
  })

  it('keeps the attach picker inside the card on phones (full-width, shrinkable)', () => {
    // Photo-reported 2026-08-28: the picker's intrinsic width (its widest
    // option, e.g. "Marketing · shared-test-agent/marketing") pushed it past
    // the screen edge. A flex item never shrinks below its content width
    // without min-w-0, so both classes are load-bearing.
    h.knowledgeData = { is_library: false, libraries: [], attachments: [] }
    h.libraries = [
      { source_agent: 'kb', subdir: 'marketing', created_by: 'u1', created_at: 't', consumers: 1, name: 'A very long library name that overflows phones' },
    ]
    renderConfig()
    const picker = screen.getByLabelText('Attach library') as HTMLSelectElement
    for (const cls of ['w-full', 'min-w-0', 'sm:w-auto']) {
      expect(picker.className.split(/\s+/)).toContain(cls)
    }
  })

  it('hides the attach control entirely when no attachable library exists', () => {
    h.libraries = [
      { source_agent: 'demo', subdir: '', created_by: 'u1', created_at: 't', consumers: 0 },
    ]
    renderConfig()
    expect(screen.queryByLabelText('Attach library')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Attach' })).toBeNull()
  })

  it('labels an attached library by name, with the mirror path beside it', () => {
    h.knowledgeData = {
      is_library: false,
      libraries: [],
      attachments: [mkAtt({ subdir: 'marketing', name: 'Brand Guidelines' })],
    }
    renderConfig()
    expect(screen.getByText('Brand Guidelines')).toBeTruthy()
    // The mirror path stays keyed on the source agent + subdir — the label
    // never moved it.
    expect(screen.getByText(/knowledge\/shared\/kb\/marketing\//)).toBeTruthy()
  })

  it('falls back to the agent slug for libraries shared before names existed', () => {
    h.knowledgeData = {
      is_library: false,
      libraries: [],
      attachments: [mkAtt()],
    }
    renderConfig()
    // Never renders blank.
    expect(screen.getByText('kb')).toBeTruthy()
  })

  it('shows a bulletin badge on libraries and attachments that publish one', () => {
    h.knowledgeData = {
      is_library: true,
      libraries: [mkLib({ subdir: 'docs', name: 'Docs', has_bulletin: true })],
      attachments: [mkAtt({ has_bulletin: true })],
    }
    renderConfig()
    expect(screen.getAllByText('bulletin')).toHaveLength(2)
  })

  it("shows this agent's own label and the unchanged mirror layout", () => {
    h.knowledgeData = {
      is_library: true,
      libraries: [mkLib({ name: 'Brand Guidelines' })],
      attachments: [],
    }
    renderConfig()
    const row = screen.getByText('Brand Guidelines').closest('div')!
    // The folder chip shows the whole-folder share; the label is display-only.
    expect(within(row).getByText(/whole folder/)).toBeTruthy()
  })
})
