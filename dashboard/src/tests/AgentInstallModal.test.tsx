import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

// Shared mock state — hoisted so the vi.mock factories below can close over it.
const h = vi.hoisted(() => ({
  installMock: vi.fn(),
  // useInstallPreview result; tests swap `data` per case.
  preview: { data: undefined as any, isLoading: false },
}))

vi.mock('@/api/agents', () => ({
  useCreateAgent: () => ({ mutate: vi.fn(), isPending: false }),
}))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { role: 'admin', sub: 'u1', agent_roles: {} } }),
}))
vi.mock('@/api/communityAgents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/communityAgents')>()),
  useInstallCommunityAgent: () => ({ mutateAsync: h.installMock, isPending: false }),
  useInstallPreview: () => h.preview,
}))

import AgentInstallModal from '@/components/AgentInstallModal'

const template = {
  slug: 'personal-assistant-pro',
  display_name: 'Personal Assistant Pro',
  version: '1.2.0',
  author: 'OtoDock',
  description: 'Full personal assistant',
  color: '#3B82F6',
} as any

interface Tool {
  name: string
  needs_request: boolean
  blocked: boolean
  label?: string
  core?: boolean
  installed?: boolean
  request_type?: 'install' | 'access' | null
}

function previewData(mcps: Tool[], tasks = 0) {
  return {
    slug_available: true,
    suggested_slug: '',
    required_mcps: mcps.map(m => ({
      installed: true, request_type: null, core: false, label: m.name, ...m,
      reason: m.blocked ? 'NOT in any catalog' : 'ok',
    })),
    will_create_tasks_agent_scope: tasks,
  }
}

function renderModal() {
  return render(
    <MemoryRouter>
      <AgentInstallModal open mode="install" template={template} onClose={() => {}} />
    </MemoryRouter>,
  )
}

const installButton = () => screen.getByRole('button', { name: /Install agent/ })

describe('AgentInstallModal — blocked-MCP gating', () => {
  beforeEach(() => {
    h.installMock.mockReset()
    h.preview.data = undefined
    h.preview.isLoading = false
  })

  it('disables Install and explains when a required MCP is in no catalog', () => {
    h.preview.data = previewData([
      { name: 'task-mcp', needs_request: false, blocked: false },
      { name: 'phone-mcp', installed: false, needs_request: true, blocked: true },
    ])
    renderModal()
    expect(installButton()).toBeDisabled()
    expect(screen.getByTestId('tool-phone-mcp')).toHaveTextContent('It is not in the community list, so it cannot be added.')
    expect(screen.getByText('This agent cannot be installed here.')).toBeInTheDocument()
  })

  it('keeps Install enabled when MCPs merely need admin approval', () => {
    h.preview.data = previewData([
      { name: 'task-mcp', needs_request: false, blocked: false },
      { name: 'camoufox', needs_request: true, blocked: false },
    ])
    renderModal()
    expect(installButton()).toBeEnabled()
  })

  it('does not block install while the preview is still loading', () => {
    h.preview.isLoading = true
    renderModal()
    expect(installButton()).toBeEnabled()
  })

  it('shows the apps and checks a template ships and sends the consent when ticked', async () => {
    h.preview.data = {
      ...previewData([{ name: 'task-mcp', needs_request: false, blocked: false }]),
      consent_scope: 'everyone',
      apps: [{
        slug: 'home', title: 'Home', visibility: 'user', sig: 'sig-home', owner_approval: false,
        blueprint_tasks: [],
        row: {
          id: '', slug: 'home', title: 'Home', scope: 'personal', position: 0, rel_path: '', updated_at: '',
          actions: [{ id: 's', label: 'State', type: 'platform', method: 'setup.status' }],
          actions_sig: '', actions_approved: false, approval_stale: false, can_approve: false, can_manage: false,
          hidden_for_me: false, egress: ['api.open-meteo.com'], files: { read: [], write: [] },
          external: { links: [], challenge: [], session_days: 30 }, manifest_empty: false,
        },
      }],
      checks: [{ name: 'lint', description: 'Lint it', mandatory: true, applies: ['chats'], script: 'lint.sh',
                 sig: 'sig-lint', words: 'a judge run per turn' }],
    }
    h.installMock.mockResolvedValue({ agent_slug: 'pa' })
    renderModal()
    expect(screen.getByTestId('apps-count').textContent).toBe('1 app: Home')
    expect(screen.getByTestId('checks-count').textContent).toBe('1 check')
    expect(screen.getByTestId('consent-app-home')).toHaveTextContent('Every member of this agent gets their own Home.')
    // What the app may do and the check's words are one tap away.
    expect(screen.queryByTestId('manifest-summary')).toBeNull()
    expect(screen.queryByTestId('consent-check-lint')).toBeNull()
    fireEvent.click(within(screen.getByTestId('template-apps')).getByTestId('details-toggle'))
    expect(screen.getByTestId('manifest-summary')).toHaveTextContent('api.open-meteo.com')
    expect(screen.getByTestId('consent-check-lint')).toHaveTextContent(', mandatory')
    expect(screen.getByText('Approve for every member.')).toBeInTheDocument()
    const button = screen.getByRole('button', { name: /Install and approve/ })
    fireEvent.click(button)
    await waitFor(() => expect(h.installMock).toHaveBeenCalled())
    expect(h.installMock.mock.calls[0][0]).toMatchObject({
      template_slug: 'personal-assistant-pro',
      approve_apps: { home: 'sig-home' },
      approve_checks: { lint: 'sig-lint' },
      approve_servers: [],
    })
    // Unticked: the plain install, no consent in the body.
    h.installMock.mockClear()
    fireEvent.click(screen.getByTestId('consent-checkbox'))
    fireEvent.click(screen.getByRole('button', { name: /Install agent/ }))
    await waitFor(() => expect(h.installMock).toHaveBeenCalled())
    expect(h.installMock.mock.calls[0][0]).not.toHaveProperty('approve_apps')
  })

  it('says an app brings a server in the deploy card’s words, and keeps the media sentence under Details', () => {
    const media = { id: 'm', label: 'Show', type: 'platform', method: 'files.media' }
    const base = {
      id: '', title: 'Home', scope: 'personal', position: 0, rel_path: '', updated_at: '',
      actions: [media], actions_sig: '', actions_approved: false, approval_stale: false, can_approve: false,
      can_manage: false, hidden_for_me: false, files: { read: ['workspace/media'], write: [] }, manifest_empty: false,
    }
    h.preview.data = {
      ...previewData([]), consent_scope: 'everyone',
      apps: [
        { slug: 'home', title: 'Home', visibility: 'user', sig: 'sig-home', owner_approval: false,
          blueprint_tasks: [], adds_server: true, row: { ...base, slug: 'home', has_server: true } },
        { slug: 'page', title: 'Page', visibility: 'agent', sig: 'sig-page', owner_approval: false,
          blueprint_tasks: [], row: { ...base, slug: 'page', has_server: false } },
      ],
      checks: [],
    }
    renderModal()
    const home = screen.getByTestId('consent-app-home')
    // The program is said in view, in the app's own sentence, folded or not: the yes covers it.
    expect(home.querySelector('p')?.textContent).toBe('Every member of this agent gets their own Home, with its own program on your OtoDock.')
    expect(screen.getByTestId('apps-count').textContent).toBe('2 apps: Home and Page')
    expect(screen.getByTestId('consent-app-page')).toHaveTextContent('Page is shared by everyone who uses this agent.')
    expect(home).not.toHaveTextContent('pictures')
    fireEvent.click(within(screen.getByTestId('template-apps')).getByTestId('details-toggle'))
    expect(within(home).getByTestId('media-server-line'))
      .toHaveTextContent('While the app is open, its program can get the videos, audio and pictures it shows you.')
    expect(within(home).queryByText(/Platform data/i)).toBeNull()
    expect(home).not.toHaveTextContent('files.media')
    expect(home).toHaveTextContent('with its own program on your OtoDock')
    expect(screen.getByTestId('consent-app-page')).not.toHaveTextContent('program')
    expect(screen.getByTestId('consent-app-page')).not.toHaveTextContent('pictures')
    expect(screen.getByText('Approve for every member.')).toBeInTheDocument()
  })

  it('names the apps whose server it showed in the consent', async () => {
    const base = {
      id: '', title: 'Home', scope: 'personal', position: 0, rel_path: '', updated_at: '',
      actions: [], actions_sig: '', actions_approved: false, approval_stale: false, can_approve: false,
      can_manage: false, hidden_for_me: false, files: { read: [], write: [] }, manifest_empty: true,
    }
    h.preview.data = {
      ...previewData([]), consent_scope: 'everyone',
      apps: [
        { slug: 'home', title: 'Home', visibility: 'user', sig: 'sig-home', owner_approval: false,
          blueprint_tasks: [], adds_server: true, row: { ...base, slug: 'home', has_server: true } },
        { slug: 'page', title: 'Page', visibility: 'agent', sig: 'sig-page', owner_approval: false,
          blueprint_tasks: [], row: { ...base, slug: 'page' } },
      ],
      checks: [],
    }
    h.installMock.mockResolvedValue({ agent_slug: 'pa' })
    renderModal()
    // An app that asks for nothing says so under Details, never an empty box.
    fireEvent.click(within(screen.getByTestId('template-apps')).getByTestId('details-toggle'))
    expect(screen.getByTestId('consent-app-nothing-page').textContent).toBe('It asks for nothing beyond its own page.')
    expect(screen.getByTestId('consent-app-nothing-home').textContent).toBe('It asks for nothing beyond its own page and its program.')
    expect(within(screen.getByTestId('consent-app-details-page')).queryByTestId('manifest-summary')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Install and approve/ }))
    await waitFor(() => expect(h.installMock).toHaveBeenCalled())
    expect(h.installMock.mock.calls[0][0]).toMatchObject({
      approve_apps: { home: 'sig-home', page: 'sig-page' }, approve_servers: ['home'],
    })
  })

  it('tells a non-admin their consent covers the shared apps and their own copy', () => {
    h.preview.data = {
      ...previewData([]),
      consent_scope: 'own',
      apps: [],
      checks: [{ name: 'lint', description: '', mandatory: false, applies: [], script: '', sig: 's', words: '' }],
    }
    renderModal()
    expect(screen.getByText('Approve for you and the agent. Each member approves their own copy.')).toBeInTheDocument()
  })

  it('surfaces the server 400 detail message inline on install failure', async () => {
    h.preview.data = previewData([{ name: 'task-mcp', needs_request: false, blocked: false }])
    h.installMock.mockRejectedValue({
      status: 400,
      message: 'Bad Request',
      body: { detail: { error: 'missing_mcps', message: 'Template requires 1 MCP that are not in the platform OR the community catalog: phone-mcp' } },
    })
    renderModal()
    fireEvent.click(installButton())
    await waitFor(() =>
      expect(screen.getByText(/Template requires 1 MCP/)).toBeInTheDocument(),
    )
  })
})

describe('AgentInstallModal — the tools it needs', () => {
  beforeEach(() => {
    h.installMock.mockReset()
    h.preview.isLoading = false
  })

  const schedules: Tool = { name: 'schedules-mcp', label: 'Schedules', core: true, needs_request: false, blocked: false }
  const memory: Tool = { name: 'memory-mcp', label: 'Memory', core: true, needs_request: false, blocked: false }
  const search: Tool = { name: 'web-search-mcp', label: 'Web Search', needs_request: false, blocked: false }
  const github: Tool = { name: 'github-mcp', label: 'GitHub', request_type: 'access', needs_request: true, blocked: false }
  const notion: Tool = { name: 'notion-mcp', label: 'Notion', installed: false, request_type: 'install', needs_request: false, blocked: false }
  const navis: Tool = { name: 'navis-mcp', installed: false, needs_request: true, blocked: true }
  const phone: Tool = { name: 'phone-mcp', label: 'Phone Calls', request_type: 'access', needs_request: true, blocked: true }

  const tools = () => screen.getByTestId('required-tools')
  const count = () => screen.getByTestId('tools-count')
  const toggle = () => within(tools()).getByTestId('details-toggle')

  it('folds tools that are all ready into one line, with Details beside it', () => {
    h.preview.data = previewData([schedules, memory, search])
    renderModal()
    expect(count().textContent).toBe('3 tools, all ready')
    expect(tools()).not.toHaveTextContent('Schedules')
    expect(tools()).not.toHaveTextContent('Web Search')
    expect(within(tools()).queryAllByTestId(/^tool-/)).toHaveLength(0)
    expect(toggle()).toHaveTextContent('Details')
  })

  it('opens a list by display name, its own tools before the ones every agent has', () => {
    h.preview.data = previewData([schedules, memory, search])
    renderModal()
    fireEvent.click(toggle())
    expect(toggle()).toHaveTextContent('Hide details')
    const text = tools().textContent ?? ''
    expect(text.indexOf('Added for this agent')).toBeGreaterThan(-1)
    expect(text.indexOf('Web Search')).toBeGreaterThan(text.indexOf('Added for this agent'))
    expect(text.indexOf('Every agent has these')).toBeGreaterThan(text.indexOf('Web Search'))
    expect(text.indexOf('Schedules')).toBeGreaterThan(text.indexOf('Every agent has these'))
    expect(text).toContain('Memory')
    for (const id of ['schedules-mcp', 'memory-mcp', 'web-search-mcp']) expect(text).not.toContain(id)
  })

  it("never folds a tool that needs an admin's approval", () => {
    h.preview.data = previewData([schedules, github, search])
    renderModal()
    expect(count().textContent).toBe("3 tools: 2 ready, 1 needs an admin's approval")
    expect(screen.getByTestId('tool-github-mcp')).toHaveTextContent("GitHub It waits for an admin's approval.")
    expect(tools()).not.toHaveTextContent('Schedules')
    expect(installButton()).toBeEnabled()
  })

  it('shows each blocked tool with its cause and holds the install', () => {
    h.preview.data = previewData([schedules, navis, phone])
    renderModal()
    expect(count().textContent).toBe('3 tools: 1 ready, 2 cannot be used here')
    expect(screen.getByTestId('tool-navis-mcp')).toHaveTextContent('navis-mcp It is not in the community list, so it cannot be added.')
    expect(screen.getByTestId('tool-phone-mcp')).toHaveTextContent('Phone Calls An admin must set it up first.')
    expect(screen.getByText('This agent cannot be installed here.')).toBeInTheDocument()
    expect(installButton()).toBeDisabled()
  })

  it('says the install adds a tool from the community list, or waits for an admin to add it', () => {
    h.preview.data = previewData([schedules, notion])
    const { unmount } = renderModal()
    expect(count().textContent).toBe('2 tools: 1 ready, 1 is added to your OtoDock')
    expect(screen.getByTestId('tool-notion-mcp')).toHaveTextContent('Notion The install adds it to your OtoDock.')
    unmount()
    h.preview.data = previewData([schedules, { ...notion, needs_request: true }])
    renderModal()
    expect(count().textContent).toBe("2 tools: 1 ready, 1 needs an admin's approval")
    expect(screen.getByTestId('tool-notion-mcp')).toHaveTextContent('Notion It waits for an admin to add it to your OtoDock.')
  })

  it('counts in the singular and the plural', () => {
    const cases: [Tool[], string][] = [
      [[schedules], '1 tool, ready'],
      [[github], '1 tool'],
      [[schedules, memory], '2 tools, all ready'],
      [[schedules, github, { ...github, name: 'slack-mcp', label: 'Slack' }], "3 tools: 1 ready, 2 need an admin's approval"],
      [[schedules, notion, { ...notion, name: 'linear-mcp', label: 'Linear' }], '3 tools: 1 ready, 2 are added to your OtoDock'],
      [[schedules, navis], '2 tools: 1 ready, 1 cannot be used here'],
      [[schedules, github, notion, navis], "4 tools: 1 ready, 1 is added to your OtoDock, 1 needs an admin's approval, 1 cannot be used here"],
    ]
    for (const [rows, words] of cases) {
      h.preview.data = previewData(rows)
      const { unmount } = renderModal()
      expect(count().textContent).toBe(words)
      unmount()
    }
  })

  it('has no Details when no tool is folded, and says how many scheduled tasks come with it', () => {
    h.preview.data = previewData([github], 1)
    const { unmount } = renderModal()
    expect(within(tools()).queryByTestId('details-toggle')).toBeNull()
    expect(screen.getByText('The agent comes with 1 scheduled task.')).toBeInTheDocument()
    unmount()
    h.preview.data = previewData([schedules], 3)
    renderModal()
    expect(screen.getByText('The agent comes with 3 scheduled tasks.')).toBeInTheDocument()
  })
})

describe('AgentInstallModal — what the agent does', () => {
  beforeEach(() => {
    h.installMock.mockReset()
    h.preview.data = previewData([])
    h.preview.isLoading = false
  })

  function renderWith(over: Record<string, unknown>) {
    return render(
      <MemoryRouter>
        <AgentInstallModal open mode="install" template={{ ...template, ...over }} onClose={() => {}} />
      </MemoryRouter>,
    )
  }

  it('shows the summary and every highlight with its icon, above the name', () => {
    const highlights = [
      { icon: 'shorts', text: 'Shorts for every network' }, { icon: 'video', text: 'Full videos' },
      { icon: 'sparkle', text: 'AI generation' }, { icon: 'mic', text: 'AI voice' },
      { icon: 'calendar', text: 'A content calendar' }, { icon: 'hologram', text: 'A later icon' },
    ]
    renderWith({ summary: "Runs your company's whole content workflow.", highlights })
    const panel = screen.getByTestId('template-panel')
    expect(within(panel).getByTestId('panel-what').textContent).toBe('One agent, installed on its own.')
    expect(within(panel).getByTestId('panel-text').textContent).toBe("Runs your company's whole content workflow.")
    // In full, the description follows the highlights.
    expect(within(panel).getByTestId('panel-description')).toHaveTextContent('Full personal assistant')
    const items = within(screen.getByTestId('panel-features')).getAllByRole('listitem')
    expect(items.map(li => li.textContent)).toEqual(highlights.map(h => h.text))
    expect(items.map(li => li.querySelector('svg')?.getAttribute('data-icon')))
      .toEqual(['shorts', 'video', 'sparkle', 'mic', 'calendar', 'unknown'])
    // The panel's own parts first, then the install's fields, then the folded lines.
    const features = screen.getByTestId('panel-features')
    expect(features.compareDocumentPosition(screen.getByText('Display name')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByText('Display name').compareDocumentPosition(screen.getByTestId('panel-folded')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows the description in full when the template has no summary', () => {
    renderWith({ description: 'Full personal assistant. It reads your mail, keeps your calendar and remembers.' })
    const text = screen.getByTestId('panel-text')
    expect(text.textContent).toBe('Full personal assistant. It reads your mail, keeps your calendar and remembers.')
    expect(text.className).not.toMatch(/line-clamp/)
    expect(screen.queryByTestId('panel-features')).toBeNull()
  })

  it('says a member agent belongs to a department and offers the whole department', () => {
    const onInstall = vi.fn()
    render(
      <MemoryRouter>
        <AgentInstallModal open mode="install" template={template} onClose={() => {}}
                           memberOf={{ name: 'Yacht office', onInstall }} />
      </MemoryRouter>,
    )
    const line = screen.getByTestId('panel-member-of')
    expect(line).toHaveTextContent('Part of the Yacht office department.')
    fireEvent.click(within(line).getByRole('button', { name: 'Install the whole department' }))
    expect(onInstall).toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /Install agent/ })).toBeTruthy()
  })
})

describe('AgentInstallModal — what an install left out', () => {
  beforeEach(() => {
    h.installMock.mockReset()
    h.preview.data = previewData([])
    h.preview.isLoading = false
  })

  function mountRouted(onClose = vi.fn()) {
    render(
      <MemoryRouter initialEntries={['/agents']}>
        <Routes>
          <Route path="/agents" element={<AgentInstallModal open mode="install" template={template} onClose={onClose} />} />
          <Route path="/agents/:slug/config" element={<p>config page</p>} />
        </Routes>
      </MemoryRouter>,
    )
    return onClose
  }

  it('holds the dialog on the tasks whose model choice was left out, then opens the agent', async () => {
    h.installMock.mockResolvedValue({ agent_slug: 'personal-assistant-pro', task_pins_dropped: ["The task 'digest' runs on the agent's usual model and engine. The model gpt-x is not turned on for OpenAI Codex."] })
    const onClose = mountRouted()
    fireEvent.click(installButton())
    expect(await screen.findByText('Agent installed')).toBeInTheDocument()
    const box = screen.getByTestId('install-pins-dropped')
    expect(box.textContent).toContain('Task choices left out')
    expect(box.textContent).toContain("The task 'digest' runs on the agent's usual model and engine.")
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Open the agent' }))
    expect(onClose).toHaveBeenCalled()
    expect(await screen.findByText('config page')).toBeInTheDocument()
  })

  it('closes the "Agent installed" dialog with Close and stays on the page', async () => {
    h.installMock.mockResolvedValue({ agent_slug: 'personal-assistant-pro', task_pins_dropped: ["The task 'digest' runs on the agent's usual model and engine. The model gpt-x is not turned on for OpenAI Codex."] })
    const onClose = mountRouted()
    fireEvent.click(installButton())
    expect(await screen.findByText('Agent installed')).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('config page')).toBeNull()
  })

  it('opens the agent at once when nothing was left out, an older proxy included', async () => {
    h.installMock.mockResolvedValue({ agent_slug: 'personal-assistant-pro' })
    const onClose = mountRouted()
    fireEvent.click(installButton())
    expect(await screen.findByText('config page')).toBeInTheDocument()
    expect(onClose).toHaveBeenCalled()
    expect(screen.queryByText('Agent installed')).toBeNull()
  })

  it("lists a refused template's problems apart, and a plain refusal as its sentence", async () => {
    h.installMock.mockRejectedValueOnce(Object.assign(new Error('a; b'), {
      status: 400,
      body: { detail: { error: 'invalid_template', message: 'a; b', problems: ['agent.json: name is missing', 'tasks/x.json: cron is not valid'] } },
    }))
    mountRouted()
    fireEvent.click(installButton())
    const box = await screen.findByTestId('install-problems')
    expect(box.querySelectorAll('li')).toHaveLength(2)
    expect(box.textContent).toContain('This template cannot be installed')

    h.installMock.mockRejectedValueOnce(Object.assign(new Error('Invalid catalog template: agent.json: name is missing'), {
      status: 400, body: { detail: 'Invalid catalog template: agent.json: name is missing' },
    }))
    fireEvent.click(installButton())
    expect(await screen.findByText('Invalid catalog template: agent.json: name is missing')).toBeInTheDocument()
    expect(screen.queryByTestId('install-problems')).toBeNull()
  })
})
